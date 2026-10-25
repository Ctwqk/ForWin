"""After chapter one of three genres, the post-Canon world step runs one world tick on one shared pipeline.

Each ``tests/fixtures/world_seed/<genre>.json`` carries a ``world_tick`` section beside ``chapter_1``:
the stubbed answers of the tick's three model roles, in their output schemas (``adjudicate`` an
AdjudicationResult, ``actor_turn`` an ActorTurnBatch, ``conflict_review`` the LLM review answer),
and a short ``chapter_2``. The answers name BookState nodes ``@node:<name>`` and book map sites
``@site:<name>`` as the chapter-one section does, and give story minutes as ``{"before_now": <duration>}``
in the book's own units; the stub resolves both against the prompt it is asked, as a model reads its input.

After the real chapter-one commit, the pipeline's post-acceptance pass runs with the world tick's
roles on that stub. Its tick settles two due offstage intents: one into a public event whose news
travels the generated book map, one into a named character's death that LLM vetoes. One actor's
turn plans a new intent; every other actor waits. Only the LLM is stubbed.

Chapter one's planning step puts its pacing goal ahead of chapter two's own goal, so the stubbed
chapter-two extraction marks both goals done.
"""

from __future__ import annotations

import json
import math
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from forwin.writer.llm.adapter import LLMChatResult
from forwin.audit.events import DecisionEventType
from forwin.book_state import BookStateProjection
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.lexicon import BookLexicon, lexicon_from_payload
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.book_state.story_clock import calendar_from_payload, duration_to_minutes
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.canon import CanonAdmissionService
from forwin.checker.rules import ContinuityChecker
from forwin.maintenance.post_canon import build_world_tick_service
from forwin.models.audit import DecisionEvent
from forwin.models.genesis import PromptTrace
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import GraphDelta, WorldNode
from forwin.protocol.writer import WriterOutput
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.world_engine import tick, tick_conflicts
from forwin.world_engine.actor_turns import ActorTurnRunner
from forwin.world_engine.adjudicator import WorldAdjudicator
from forwin.world_engine.conflict_routing import route_conflict
from forwin.world_engine.delta_builder import tick_event_fact_ref
from forwin.world_engine.intents import select_due_intents
from forwin.world_engine.tick_nodes import adjudication_candidate_id
from forwin.world_engine.travel import estimate_travel
from forwin.world_engine.types import (
    WORLD_ENGINE_SOURCE_TYPE,
    ActorTurnBatch,
    AdjudicationResult,
    NewsItem,
    TickCandidate,
)
from tests.chapter_delta_helpers import body_sha256
from tests.test_chapter_delta_multi_genre import (  # noqa: F401 - ``pipeline`` is the shared pytest fixture
    READER,
    _Book,
    _commit,
    _ExtractionClient,
    _observer,
    _resolved,
    _review,
    _start_book,
    pipeline,
)
from tests.test_world_seed_multi_genre import GENRES, load_fixture, spy


SHARED_TICK_PIPELINE = [
    "select_due_intents",
    "WorldAdjudicator.adjudicate",
    "ActorTurnRunner.run",
    "route_conflict",
    "commit_world_tick",
]
# News arrival minutes absorb float noise before rounding up, as propagation does.
_MINUTE_EPSILON = 1e-6


class _WorldClient:
    """Stub LLM for the world tick's roles, answering from the book's ``world_tick`` section.

    Review metadata identifies the profile that served the chain response.
    """

    model = "stub-world-model"

    def __init__(self, answers: dict[str, Any], refs: dict[tuple[str, str], str]) -> None:
        self.answers = answers
        self.refs = refs
        self.stage_keys: list[str] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN003
        stage_key = str(kwargs.get("stage_key") or "")
        self.stage_keys.append(stage_key)
        prompt = json.JSONDecoder().raw_decode(messages[-1]["content"])[0]
        if stage_key == "world.adjudicate":
            asked = {intent["id"] for intent in prompt["intents"]}
            outcomes = self._answer("adjudicate", prompt)["outcomes"]
            answer = AdjudicationResult.model_validate(
                {"outcomes": [outcome for outcome in outcomes if outcome["intent_id"] in asked]},
            ).model_dump(mode="json")
        elif stage_key == "world.actor_turn":
            asked = {actor["actor_id"] for actor in prompt["actors"]}
            turns = self._answer("actor_turn", prompt)["turns"]
            answer = ActorTurnBatch.model_validate(
                {"turns": [turn for turn in turns if turn["actor_id"] in asked]},
            ).model_dump(mode="json")
        elif stage_key == "review.world_conflict":
            answer = self._answer("conflict_review", prompt)
        else:
            raise AssertionError(f"unexpected LLM stage {stage_key!r}")
        return json.dumps(answer, ensure_ascii=False)

    def chat_with_meta(self, messages, **kwargs):
        return LLMChatResult(self.chat(messages, **kwargs), "test-world", self.model)

    def _answer(self, role: str, prompt: dict[str, Any]) -> Any:
        return _story_minutes(_resolved(self.answers[role], self.refs), prompt)

    def close(self) -> None:
        pass


def _story_minutes(value: Any, prompt: dict[str, Any]) -> Any:
    """``{"before_now": duration}`` as the story minute that long before the prompt's now, in the book's units."""
    if isinstance(value, dict) and value.keys() == {"before_now"}:
        wait = value["before_now"]
        minutes = duration_to_minutes(
            calendar_from_payload(prompt["calendar"]), lexicon_from_payload(prompt["lexicon"]),
            amount=wait["amount"], unit_name=wait["unit"],
        )
        return prompt["context"]["now"] - minutes
    if isinstance(value, dict):
        return {key: _story_minutes(item, prompt) for key, item in value.items()}
    if isinstance(value, list):
        return [_story_minutes(item, prompt) for item in value]
    return value


@dataclass(frozen=True)
class _Ticked:
    """A book after chapter one and its world step."""

    book: _Book
    world_tick: dict[str, Any]
    client: _WorldClient
    # The world step's result_json.
    step: dict[str, Any]
    # As of chapter one, before its tick.
    before: BookStateRuntime
    # The chapter's last story minute: the tick's now.
    now: int
    onstage_refs: frozenset[str]

    def outcome(self, *, kills: bool) -> dict[str, Any]:
        """The adjudicated outcome that kills a named character, or the one whose public event spreads."""

        def killing(outcome: dict[str, Any]) -> bool:
            return any(
                change["field"] == "alive_status" and change["new_value"] == "dead"
                for change in outcome["effects"].get("entity_changes", [])
            )

        (outcome,) = [
            outcome for outcome in self.world_tick["adjudicate"]["outcomes"]
            if (killing(outcome) if kills else bool(outcome["effects"].get("events")))
        ]
        return _resolved(outcome, self.book.refs)

    def planning_turn(self) -> dict[str, Any]:
        """The one actor turn that plans a new intent."""
        (turn,) = [turn for turn in self.world_tick["actor_turn"]["turns"] if turn["new_intents"]]
        return _resolved(turn, self.book.refs)


def _chapter_one(pipeline, genre: str) -> _Book:
    book = _start_book(pipeline, genre)
    client = _ExtractionClient(book.extraction())
    pipeline.llm_client = client
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        output, review = _review(pipeline, session, book)
        assert review.verdict in {"pass", "warn"}, [(issue.rule_name, issue.description) for issue in review.issues]
        _commit(pipeline, session, book, output, review, client)
    return book


def _write_chapter(pipeline, book: _Book, number: int, chapter: dict[str, Any]) -> None:
    """A later fixture chapter through draft review, canon preparation and the canon commit, as chapter one."""
    body = chapter["body"]
    client = _ExtractionClient({
        "body_sha256": body_sha256(body), **_resolved(chapter["chapter_delta_extraction"], book.refs),
    })
    pipeline.llm_client = client
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        repo = StateRepository(session)
        plan = repo.get_chapter_plan(book.project_id, number)
        output, review, _forced = pipeline.repair.review_candidate(
            # A failing review stops there instead of entering repair, which would call the writer.
            execution=replace(pipeline.repair_execution, _pause_requested=lambda: True),
            session=session,
            repo=repo,
            updater=StateUpdater(session),
            checker=ContinuityChecker(repo),
            project_id=book.project_id,
            chapter_plan=plan,
            context=pipeline.retrieval_broker.build_chapter_context(repo, book.project_id, plan),
            writer_output=WriterOutput(
                project_id=book.project_id, chapter_number=number, title=plan.title, body=body,
                char_count=len(body), end_of_chapter_summary=chapter["summary"],
            ),
        )
        assert review.verdict in {"pass", "warn"}, [(issue.rule_name, issue.description) for issue in review.issues]
        repo, updater, _checker = pipeline._make_state_helpers(session)  # noqa: SLF001
        candidate = CandidateDraftRepository(session).latest_for_chapter(
            project_id=book.project_id, chapter_number=number,
        )
        prepared = pipeline.canon_preparation.prepare(
            context=replace(pipeline.canon_preparation_context, llm_client=client),
            session=session, repo=repo, updater=updater, candidate_id=candidate.id,
            project_id=book.project_id, chapter_number=number, writer_output=output, verdict=review,
            acceptance_mode="normal", repair_attempt_count=0, residual_review_issues=[], canon_risk_level="low",
        )
        assert prepared.plan is not None, prepared.blocked_path
        session.commit()
        outcome = pipeline.canon_admission.commit_plan(prepared.plan)
        assert not outcome.blocked, outcome.blocked_path


def _world_step(pipeline, book: _Book, number: int, client: _WorldClient) -> dict[str, Any]:
    """The pipeline's post-acceptance pass with the tick's roles on ``client``; the world step's result.

    The pass runs the chapter's post-Canon maintenance, then its order controls, which the next
    chapter's canon commit waits for.
    """
    # As the container wires it when a model is configured.
    pipeline.post_canon_maintenance.world_tick = build_world_tick_service(
        llm_client=client,
        session_factory=pipeline._SessionFactory,  # noqa: SLF001
        artifact_store=pipeline.artifact_store,
        llm_available=True,
        admission=pipeline.canon_admission,
    )
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        ran = pipeline._run_phase3_pass(  # noqa: SLF001
            session=session, project_id=book.project_id, chapter_number=number,
        )
        pipeline._run_post_canon_order_controls(  # noqa: SLF001
            session=session, project_id=book.project_id, chapter_number=number, trigger_stage="post_acceptance",
            canon_commit_id=ran["canon_commit_id"],
        )
        run = session.scalars(select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == ran["canon_commit_id"],
            PostCanonMaintenanceRun.step_name == "world",
        )).one()
        assert run.status == "succeeded", run.last_error
        return json.loads(run.result_json)


def _ticked(pipeline, genre: str, *, touches_onstage: bool = False) -> _Ticked:
    book = _chapter_one(pipeline, genre)
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        before = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)
        now = BookStateQuery(session).current_story_minute(book.project_id, as_of_chapter=1)
        plan = StateRepository(session).get_chapter_plan(book.project_id, 1)
        extraction = book.extraction()
        onstage_refs = frozenset({
            plan.pov_ref, *json.loads(plan.onstage_refs_json or "[]"),
            *(move["entity_ref"] for move in extraction["movements"]),
            *(ref for event in extraction["events"] for ref in event["participant_refs"]),
        } - {"", None})
    world_tick = load_fixture(genre)["world_tick"]
    if touches_onstage:
        outcome = next(item for item in world_tick["adjudicate"]["outcomes"] if item["effects"].get("events"))
        outcome["effects"]["entity_changes"] = [{
            "ref": book.chapter["chapter_delta_extraction"]["movements"][0]["entity_ref"],
            "field": "alive_status", "new_value": "dead",
        }]
    client = _WorldClient(world_tick, book.refs)
    step = _world_step(pipeline, book, 1, client)
    return _Ticked(book, world_tick, client, step, before, now, onstage_refs)


def _tick_delta(session, project_id: str, number: int) -> GraphDelta:
    (delta,) = [
        delta for delta in BookStateRepository(session).list_graph_deltas(
            project_id, after_chapter=number - 1, through_chapter=number,
        )
        if delta.source_type == WORLD_ENGINE_SOURCE_TYPE
    ]
    return delta


def _knows(runtime: BookStateRuntime, observer_id: str, fact_ref: str) -> str | None:
    key = _observer(runtime, observer_id)
    return runtime.cognition_by_observer[key].knowledge_by_ref.get(fact_ref) if key is not None else None


def _event_of(runtime: BookStateRuntime, fact_ref: str) -> WorldNode:
    (event,) = [
        node for node in runtime.world.nodes_by_id.values()
        if node.node_type == "event" and fact_ref in (runtime.world.get_state(node.id).get("result_refs") or [])
    ]
    return event


def _news_by_map(
    runtime: BookStateRuntime, lexicon: BookLexicon, *, participants: list[str], location_id: str, at: int, now: int,
) -> tuple[set[str], set[str], dict[str, int]]:
    """Who a public event's news reaches: witnesses, actors it reaches by now, and those it reaches later.

    The event's participants and the actors at its place witness it. Every other living actor hears it
    after the objective shortest route's travel hours, in minutes, divided by the book's news speed.
    """
    world = runtime.world
    actors = {
        node.id: resolve_world_node_location_id(world, node.id)
        for node in world.nodes_by_id.values()
        if str(node.node_type) in ACTOR_NODE_TYPES and node.is_active
        and world.get_state(node.id).get("alive_status") != "dead"
    }
    witnesses = {actor for actor, place in actors.items() if actor in participants or place == location_id}
    heard: set[str] = set()
    queued: dict[str, int] = {}
    for actor, place in actors.items():
        if actor in witnesses or not place:
            continue
        route = runtime.map.shortest_path(location_id, place, metric="travel_time")
        if not route.reachable:
            continue
        arrive_at = at + math.ceil(route.total_travel_time * 60 / lexicon.news_speed_multiplier - _MINUTE_EPSILON)
        if arrive_at <= now:
            heard.add(actor)
        else:
            queued[actor] = arrive_at
    return witnesses, heard, queued


@pytest.mark.parametrize("genre", GENRES)
@pytest.mark.parametrize("touches_onstage", [False, True], ids=["offstage", "onstage"])
def test_tick_after_chapter_one_commits_world_delta_and_keeps_reader_unchanged(
    pipeline, genre: str, touches_onstage: bool,
) -> None:
    ticked = _ticked(pipeline, genre, touches_onstage=touches_onstage)
    project_id, tick_id, now = ticked.book.project_id, ticked.step["tick_id"], ticked.now
    outcome = ticked.outcome(kills=False)
    candidate_id = adjudication_candidate_id(outcome["intent_id"])

    assert ticked.step["graph_delta_ids"] == [f"world-tick:{project_id}:{tick_id}"]
    assert len(ticked.step["conflict_ids"]) == 1
    assert (ticked.step["dropped_candidate_ids"], ticked.step["postponed_candidate_ids"]) == (
        [candidate_id] if touches_onstage else [], [],
    )
    assert ticked.step["skipped_reason"] == ""
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        delta = _tick_delta(session, project_id, 1)
        after = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=1)
        calendar = StoryCalendarRepository(session).get(project_id)
        lexicon = BookLexiconRepository(session).get(project_id)
        dropped = list(session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED,
        )))
    assert delta.id == ticked.step["graph_delta_ids"][0]

    # The reader learns nothing from the tick; only chapter text advances the reader.
    assert not [patch for patch in delta.cognition_patches if str(patch.observer_type) == READER[0]]
    assert after.cognition_by_observer[READER].overlay == ticked.before.cognition_by_observer[READER].overlay
    fact_ref = tick_event_fact_ref(project_id, tick_id, candidate_id, 0)
    if touches_onstage:
        assert ticked.before.world.get_node(outcome["intent_id"]).profile["requires_onstage"] is False
        assert after.world.get_node(outcome["intent_id"]).profile["requires_onstage"] is True
        assert after.world.get_state(outcome["intent_id"])["status"] == "due"
        (event,) = dropped
        payload = json.loads(event.payload_json)
        assert (payload["candidate_id"], payload["intent_id"], payload["reason"]) == (
            candidate_id, outcome["intent_id"], "touches_onstage_entity",
        )
        assert any(p.node_id == outcome["intent_id"] and p.field_path == "profile.requires_onstage"
                   and p.old_value is False and p.new_value is True for p in delta.node_patches)
        target = outcome["effects"]["entity_changes"][0]["ref"]
        assert target in ticked.onstage_refs
        assert after.world.get_state(target) == ticked.before.world.get_state(target)
        assert fact_ref.removeprefix("fact:") not in after.world.facts_by_id
        for move in outcome["effects"].get("movements", []):
            assert resolve_world_node_location_id(after.world, move["entity_ref"]) == (
                resolve_world_node_location_id(ticked.before.world, move["entity_ref"])
            )
        return
    assert dropped == []

    # A due offstage intent was adjudicated into a public event at its place.
    (event_answer,) = outcome["effects"]["events"]
    assert set(event_answer["participant_refs"]).isdisjoint(ticked.onstage_refs)
    assert after.world.get_state(outcome["intent_id"])["status"] == outcome["result"]
    event = _event_of(after, fact_ref)
    event_state = after.world.get_state(event.id)
    assert (event.summary, event.profile["location_id"], event_state["visibility_level"]) == (
        event_answer["summary"], event_answer["location_id"], "public",
    )
    for movement in outcome["effects"].get("movements", []):
        assert movement["entity_ref"] not in ticked.onstage_refs
        assert after.world.get_state(movement["entity_ref"])["location_id"] == movement["to_location_id"]

    # Its news travels the book map: witnesses know at once, the near hear it by now, the far are queued.
    witnesses, heard, queued = _news_by_map(
        ticked.before, lexicon, participants=event_state["participant_ids"],
        location_id=event.profile["location_id"], at=event_state["actual_story_time"], now=now,
    )
    assert heard and queued
    assert {observer: _knows(after, observer, fact_ref) for observer in [*witnesses, *heard, *queued]} == {
        **dict.fromkeys(witnesses, "knows"), **dict.fromkeys(heard, "heard"), **dict.fromkeys(queued, None),
    }
    assert [item for item in delta.metadata["news_in_transit"] if item["fact_ref"] == fact_ref] == [
        {"fact_ref": fact_ref, "observer_ref": observer, "arrive_at": arrive_at}
        for observer, arrive_at in sorted(queued.items(), key=lambda item: (item[1], item[0]))
    ]

    # One actor's turn plans a new intent at a place it can reach before it is due.
    turn = ticked.planning_turn()
    (proposed,) = turn["new_intents"]
    (intent,) = [
        node for node in after.world.nodes_by_id.values()
        if node.node_type == "intent" and node.profile.get("owner_ref") == turn["actor_id"]
        and node.name == proposed["summary"]
    ]
    wait = duration_to_minutes(
        calendar, lexicon, amount=proposed["scheduled_in"]["amount"], unit_name=proposed["scheduled_in"]["unit"],
    )
    assert intent.profile["location_id"] == proposed["location_id"]
    assert after.world.get_state(intent.id) == {"status": "planned", "scheduled_at": now + wait, "slip_count": 0}
    travel = estimate_travel(
        ticked.before, lexicon, mover_id=turn["actor_id"],
        from_location_id=resolve_world_node_location_id(ticked.before.world, turn["actor_id"]),
        to_location_id=proposed["location_id"],
    )
    assert travel.route_known and travel.minutes <= wait


@pytest.mark.parametrize("genre", GENRES)
def test_major_change_becomes_conflict_and_llm_veto_is_applied(pipeline, genre: str) -> None:
    ticked = _ticked(pipeline, genre)
    project_id = ticked.book.project_id
    outcome = ticked.outcome(kills=True)
    (death,) = outcome["effects"]["entity_changes"]

    with pipeline._SessionFactory() as session:  # noqa: SLF001
        (conflict,) = session.scalars(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == project_id,
        )).all()
        (review,) = session.scalars(select(PromptTrace).where(
            PromptTrace.project_id == project_id, PromptTrace.stage_key == "review.world_conflict",
        )).all()
        (resolved,) = session.scalars(select(DecisionEvent).where(
            DecisionEvent.project_id == project_id,
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_RESOLVED,
        )).all()
        after = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=1)

    # The death is major: the tick held it back as a conflict for LLM instead of committing it.
    assert ticked.step["conflict_ids"] == [conflict.id]
    assert (conflict.source, conflict.violated, conflict.impact_level, conflict.required_decider) == (
        "world_tick", "impact_review", "major", "llm",
    )
    held = TickCandidate.model_validate_json(conflict.proposed_change_json)
    assert held.candidate_id == adjudication_candidate_id(outcome["intent_id"])
    assert [change.model_dump(mode="json") for change in held.effects.entity_changes] == [death]

    # The chain response records its served model and chooses veto_world.
    assert ticked.client.stage_keys.count("review.world_conflict") == 1
    profile = json.loads(review.model_profile_json)
    assert (review.backend, profile["profile_id"], profile["model"]) == ("openai_compatible", "test-world", "stub-world-model")
    decision = json.loads(conflict.decision_json)
    assert (conflict.status, decision["option"], decision["decided_by"]) == ("resolved", "veto_world", "llm")
    assert json.loads(resolved.payload_json)["trace_id"] == review.id

    # The veto landed in the tick's own delta: the character lives, the intent is vetoed and its owner acts again.
    assert after.world.get_state(death["ref"])["alive_status"] != "dead"
    assert after.world.get_state(outcome["intent_id"])["status"] == "vetoed"
    assert after.world.get_state(conflict.actor_ref)["next_turn_at"] == ticked.now


@pytest.mark.parametrize("genre", GENRES)
def test_news_queue_is_delivered_after_chapter_two(pipeline, genre: str) -> None:
    ticked = _ticked(pipeline, genre)
    project_id = ticked.book.project_id
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        queue = [
            NewsItem.model_validate(item)
            for item in _tick_delta(session, project_id, 1).metadata["news_in_transit"]
        ]

    _write_chapter(pipeline, ticked.book, 2, ticked.world_tick["chapter_2"])
    step = _world_step(pipeline, ticked.book, 2, ticked.client)

    assert (step["dropped_candidate_ids"], step["conflict_ids"]) == ([], [])
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        now = BookStateQuery(session).current_story_minute(project_id, as_of_chapter=2)
        delta = _tick_delta(session, project_id, 2)
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=2)
    delivered = [item for item in queue if item.arrive_at <= now]
    waiting = [item for item in queue if item.arrive_at > now]
    # Chapter one's public event is among the news that arrived during chapter two.
    outcome = ticked.outcome(kills=False)
    assert all(set(event["participant_refs"]).isdisjoint(ticked.onstage_refs)
               for event in outcome["effects"]["events"])
    event_fact = tick_event_fact_ref(
        project_id, ticked.step["tick_id"], adjudication_candidate_id(outcome["intent_id"]), 0,
    )
    assert event_fact in {item.fact_ref for item in delivered}
    arrived = [(item.observer_ref, item.fact_ref) for item in delivered]
    assert {key: _knows(runtime, *key) for key in arrived} == dict.fromkeys(arrived, "heard")
    assert delta.metadata["news_in_transit"] == [item.model_dump(mode="json") for item in waiting]


def test_three_genres_share_tick_pipeline(pipeline) -> None:
    sequences: dict[str, list[str]] = {}

    for genre in GENRES:
        book = _chapter_one(pipeline, genre)
        client = _WorldClient(load_fixture(genre)["world_tick"], book.refs)
        calls: list[str] = []
        # Spies go on after chapter one, so they see the world step alone.
        with ExitStack() as spies:
            spies.enter_context(patch.object(
                tick, "select_due_intents", spy("select_due_intents", select_due_intents, calls),
            ))
            spies.enter_context(patch.object(
                WorldAdjudicator, "adjudicate",
                spy("WorldAdjudicator.adjudicate", WorldAdjudicator.adjudicate, calls),
            ))
            spies.enter_context(patch.object(
                ActorTurnRunner, "run", spy("ActorTurnRunner.run", ActorTurnRunner.run, calls),
            ))
            spies.enter_context(patch.object(
                tick_conflicts, "route_conflict", spy("route_conflict", route_conflict, calls),
            ))
            spies.enter_context(patch.object(
                CanonAdmissionService, "commit_world_tick",
                spy("commit_world_tick", CanonAdmissionService.commit_world_tick, calls),
            ))
            _world_step(pipeline, book, 1, client)
        sequences[genre] = calls

    assert sequences["xianxia"] == SHARED_TICK_PIPELINE
    assert sequences == {genre: SHARED_TICK_PIPELINE for genre in GENRES}
