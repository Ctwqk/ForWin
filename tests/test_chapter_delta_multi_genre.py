"""Chapter one of three genres takes one chapter pipeline: extraction, admission, review, translation, gate.

Each ``tests/fixtures/world_seed/<genre>.json`` carries a ``chapter_1`` section: the chapter plan,
the stubbed writer's draft and the stubbed ``chapter_delta_extraction`` answer. After the real
start-writing handoff the draft goes through chapter delta extraction, entity admission, draft
review with the world_consistency signal, canon preparation (translation and world gate v6) and
the canon commit; only the LLM is stubbed.

The extraction answer names BookState nodes ``@node:<name>`` and book map sites ``@site:<name>``.
Start-writing gives them project-scoped ids and generates the book map from the Genesis atlas,
so the test puts in the ids the committed BookState holds under those names. The test fixes the map
seed; each chapter spans two days, far more than its move needs on the generated map, and
the too-fast test derives its claim from the configured travel tolerance.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter
from contextlib import ExitStack
from dataclasses import dataclass, replace
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import select

from forwin.book_state import BookStateProjection
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.extraction.chapter_delta_extractor import ensure_chapter_delta
from forwin.book_state.extraction.translator import ChapterDeltaTranslator
from forwin.book_state.query import BookStateQuery
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.story_clock import duration_to_minutes
from forwin.candidate_drafts import CandidateDraftRepository
from forwin.canon import preparation
from forwin.checker.rules import ContinuityChecker
from forwin.config import InfrastructureConfig
from forwin.generation.pipeline_core import review_autofix
from forwin.genesis import BookGenesisService
from forwin.genesis.handoff.commands import StartWritingCommand
from forwin.models.book_state import GraphDeltaRow
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.writer.prompt_core.world_repair import WORLD_REPAIR_FIXES
from tests.chapter_delta_helpers import body_sha256
from tests.postgres import postgres_test_url
from tests.test_world_seed_multi_genre import (
    GENRES,
    _NoApiKeyClient,
    create_ready_project,
    load_fixture,
    spy,
)
from tests.helpers.band_plans import stub_plan, write_band_plan


_REF = re.compile(r"@(node|site):(.+)")
READER = ("reader", "reader")
# Draft review extracts the draft and gates it; canon preparation reuses that extraction.
SHARED_CHAPTER_PIPELINE = [
    "ensure_chapter_delta", "translate", "review",
    "ensure_chapter_delta", "translate", "review",
]


@pytest.fixture
def pipeline(tmp_path):
    policy = RuntimePolicy.for_profile("standard")
    # The chapter review form is an LLM signal and the strict canon quality gate needs it;
    # only the extraction LLM is stubbed here.
    signals = tuple(signal for signal in policy.review.signals if signal != "canon_quality")
    policy = policy.model_copy(update={
        "review": policy.review.model_copy(update={"signals": signals}),
        "canon": policy.canon.model_copy(update={"quality_gate": "pulp_fatal"}),
    })
    chapter_pipeline = RuntimeContainer.from_config(
        InfrastructureConfig(
            database_url=postgres_test_url("chapter_delta_multi_genre"),
            artifact_root=str(tmp_path / "artifacts"),
            qdrant_url=":memory:",
            embedding_backend="hash",
            minimax_api_key="",
        ),
        policy=policy,
        role="generation_worker",
    ).build_chapter_pipeline()
    try:
        yield chapter_pipeline
    finally:
        chapter_pipeline.close()


class _ExtractionClient:
    """Stub LLM: answers chapter_delta_extraction with ``payload`` and records every stage it is asked for."""

    model = "stub-model"

    def __init__(self, payload: dict[str, Any]) -> None:
        self.payload = payload
        self.stage_keys: list[str] = []

    def chat(self, _messages, **kwargs):  # noqa: ANN001, ANN003
        stage_key = str(kwargs.get("stage_key") or "")
        self.stage_keys.append(stage_key)
        if stage_key != "chapter_delta_extraction":
            raise AssertionError(f"unexpected LLM stage {stage_key!r}")
        return json.dumps(self.payload, ensure_ascii=False)

    def close(self) -> None:
        pass


@dataclass(frozen=True)
class _Book:
    """A genre fixture after start-writing, with the ids its chapter-one refs name."""

    project_id: str
    chapter: dict[str, Any]
    refs: dict[tuple[str, str], str]

    def draft(self) -> WriterOutput:
        """What the stubbed writer hands to review."""
        body = self.chapter["body"]
        return WriterOutput(
            project_id=self.project_id, chapter_number=1, title=self.chapter["plan"]["title"],
            body=body, char_count=len(body), end_of_chapter_summary=self.chapter["summary"],
        )

    def extraction(self) -> dict[str, Any]:
        """The stubbed extraction answer for the draft, with committed ids in place of its refs."""
        return {
            "body_sha256": body_sha256(self.chapter["body"]),
            **_resolved(self.chapter["chapter_delta_extraction"], self.refs),
        }


def _resolved(value: Any, refs: dict[tuple[str, str], str]) -> Any:
    if isinstance(value, dict):
        return {key: _resolved(item, refs) for key, item in value.items()}
    if isinstance(value, list):
        return [_resolved(item, refs) for item in value]
    if isinstance(value, str) and (match := _REF.fullmatch(value)):
        return refs[(match.group(1), match.group(2))]
    return value


def _committed_refs(runtime: BookStateRuntime) -> dict[tuple[str, str], str]:
    """(kind, name) -> id of every uniquely named world node and book map site."""
    named = {
        # A site_state node shares its site's name; refs to a site use the map node id.
        "node": [(node.name, node.id) for node in runtime.world.nodes_by_id.values() if node.node_type != "site_state"],
        "site": [(node.name, node.id) for node in runtime.map.nodes_by_id.values()],
    }
    refs: dict[tuple[str, str], str] = {}
    for kind, pairs in named.items():
        counts = Counter(name for name, _node_id in pairs)
        refs.update({(kind, name): node_id for name, node_id in pairs if counts[name] == 1})
    return refs


def _start_book(pipeline, genre: str) -> _Book:
    fixture = load_fixture(genre)
    chapter = fixture["chapter_1"]
    project_id = f"proj-chapter-one-{genre}"
    service = BookGenesisService(llm_client=_NoApiKeyClient())
    with pipeline._SessionFactory() as session:  # noqa: SLF001
        project = create_ready_project(session, service, fixture, project_id)
        # A project from project_create carries a runtime policy, which chapter context reads.
        ProjectPolicyStore(session).initialize(project, pipeline.policy)
        session.refresh(project)
        # Fix the layout independently of the project's stable seed formula.
        with patch("forwin.map.genesis_adapter._stable_seed", return_value=15):
            service.handoff.start_writing(
                session=session, updater=StateUpdater(session),
                command=StartWritingCommand(project_id=project_id, actor_type="manual_ui"),
            )
        # Arc 1's first band, planned with this book's own chapter one.
        plans = write_band_plan(session, project_id, {1: chapter["plan"], 2: stub_plan(2), 3: stub_plan(3)})
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        planned = _resolved(chapter["plan"], _committed_refs(runtime))
        plans[0].pov_ref = planned["pov_ref"]
        plans[0].onstage_refs_json = json.dumps(planned["onstage_refs"])
        plans[0].location_refs_json = json.dumps(planned["location_refs"])
        session.commit()
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
    return _Book(project_id, chapter, _committed_refs(runtime))


def _review(pipeline, session, book: _Book) -> tuple[WriterOutput, ReviewVerdict]:
    """Extraction, entity admission and draft review of the draft, persisted as a candidate.

    A failing review stops there instead of entering repair, which would call the writer.
    """
    execution = replace(pipeline.repair_execution, _pause_requested=lambda: True)
    repo = StateRepository(session)
    chapter_plan = repo.get_chapter_plan(book.project_id, 1)
    output, review, _forced = pipeline.repair.review_candidate(
        execution=execution,
        session=session,
        repo=repo,
        updater=StateUpdater(session),
        checker=ContinuityChecker(repo),
        project_id=book.project_id,
        chapter_plan=chapter_plan,
        context=pipeline.retrieval_broker.build_chapter_context(repo, book.project_id, chapter_plan),
        writer_output=book.draft(),
    )
    return output, review


def _commit(pipeline, session, book: _Book, output: WriterOutput, review: ReviewVerdict, client) -> None:
    """Canon preparation (translation and world gate) of the reviewed candidate, then the commit."""
    repo, updater, _checker = pipeline._make_state_helpers(session)  # noqa: SLF001
    candidate = CandidateDraftRepository(session).latest_for_chapter(project_id=book.project_id, chapter_number=1)
    prepared = pipeline.canon_preparation.prepare(
        context=replace(pipeline.canon_preparation_context, llm_client=client),
        session=session, repo=repo, updater=updater, candidate_id=candidate.id,
        project_id=book.project_id, chapter_number=1, writer_output=output, verdict=review,
        acceptance_mode="normal", repair_attempt_count=0, residual_review_issues=[], canon_risk_level="low",
    )
    assert prepared.plan is not None, prepared.blocked_path
    session.commit()
    outcome = pipeline.canon_admission.commit_plan(prepared.plan)
    assert not outcome.blocked, outcome.blocked_path
    session.expire_all()


def _world_errors(review: ReviewVerdict) -> list[tuple[str, str]]:
    return [
        (issue.rule_name, issue.description)
        for issue in review.issues
        if issue.reviewer == "world_consistency" and issue.severity == "error"
    ]


def _observer(runtime: BookStateRuntime, ref: str) -> tuple[str, str] | None:
    """The cognition overlay key of the reader or of a world node, found as the world gate finds it."""
    if ref == READER[1]:
        return READER
    return next((key for key in runtime.cognition_by_observer if key[1] == ref and key != READER), None)


def _fact_ref(runtime: BookStateRuntime, proposition: str) -> str:
    (fact_id,) = [fact_id for fact_id, fact in runtime.world.facts_by_id.items() if fact.proposition == proposition]
    return f"fact:{fact_id}"


def _shortest_minutes(session, project_id: str, movement: dict[str, Any]) -> float:
    """The world gate's shortest time for a move: the mover's known route at speed 1.0 or by one of its modes."""
    runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
    modes = {mode.id: mode for mode in BookLexiconRepository(session).get(project_id).travel_modes}
    mover, from_id, to_id = movement["entity_ref"], movement["from_ref"], movement["to_ref"]
    observer = _observer(runtime, mover)
    route = runtime.map.shortest_path(from_id, to_id, metric="travel_time", observer=observer)
    assert route.reachable, route.blocked_reason
    candidates = [route.total_travel_time * 60]
    for mode_id in runtime.world.get_state(mover).get("travel_mode_ids") or []:
        mode = modes[mode_id]
        by_mode = runtime.map.shortest_path(
            from_id, to_id, metric="travel_time", observer=observer,
            allowed_edge_types=frozenset(mode.allowed_edge_types),
        )
        if by_mode.reachable:
            candidates.append(by_mode.total_travel_time * 60 / mode.speed_multiplier)
    return min(candidates)


@pytest.mark.parametrize("genre", GENRES)
def test_chapter_one_fixture_covers_the_required_sections(genre: str) -> None:
    fixture = load_fixture(genre)
    lexicon = fixture["pack"]["world_seed"]["lexicon"]
    calendar = fixture["pack"]["world_seed"]["calendar"]
    extraction = fixture["chapter_1"]["chapter_delta_extraction"]

    assert {movement["travel_mode_id"] for movement in extraction["movements"]} <= {
        mode["id"] for mode in lexicon["travel_modes"]
    }
    assert extraction["movements"] and extraction["relation_changes"]
    assert any(event["observer_ref"] == "reader" for event in extraction["knowledge_events"])
    assert extraction["item_transfers"] and extraction["events"]
    assert any(item["status"] == "done" for item in extraction["plan_fulfillment"])
    assert extraction["time"]["elapsed"]["unit"] in {unit["name"] for unit in calendar["units"]}


@pytest.mark.parametrize("genre", GENRES)
def test_chapter_one_commits_through_translator_and_gate(pipeline, genre: str) -> None:
    book = _start_book(pipeline, genre)
    extraction = book.extraction()
    client = _ExtractionClient(extraction)
    pipeline.llm_client = client

    with pipeline._SessionFactory() as session:  # noqa: SLF001
        output, review = _review(pipeline, session, book)
        assert review.verdict in {"pass", "warn"}, [(issue.rule_name, issue.description) for issue in review.issues]
        assert _world_errors(review) == []
        _commit(pipeline, session, book, output, review, client)

        # Canon reused the draft's extraction instead of extracting again.
        assert client.stage_keys == ["chapter_delta_extraction"]
        rows = session.scalars(select(GraphDeltaRow).where(
            GraphDeltaRow.project_id == book.project_id, GraphDeltaRow.chapter_number == 1,
        )).all()
        assert [row.source_type for row in rows] == ["chapter_extraction"]
        assert json.loads(rows[0].metadata_json)["plan_fulfillment"] == extraction["plan_fulfillment"]

        runtime = BookStateProjection(session).load_runtime_as_of(book.project_id, as_of_chapter=1)
        world = runtime.world
        for movement in extraction["movements"]:
            assert world.get_state(movement["entity_ref"])["location_id"] == movement["to_ref"]
        for relation in extraction["relation_changes"]:
            assert any(
                edge.is_active
                and (edge.source_id, edge.target_id, edge.edge_type)
                == (relation["source_ref"], relation["target_ref"], relation["edge_type"])
                for edge in world.edges_by_id.values()
            ), relation
        for event in extraction["knowledge_events"]:
            fact_ref = _fact_ref(runtime, event["new_fact"]["proposition"])
            observer = _observer(runtime, event["observer_ref"])
            assert runtime.cognition_by_observer[observer].knowledge_by_ref[fact_ref] == event["state"]
        for transfer in extraction["item_transfers"]:
            (item,) = [
                node for node in world.nodes_by_id.values()
                if node.node_type == "item" and node.name == transfer["new_item"]["name"]
            ]
            assert world.get_state(item.id)["holder_id"] == transfer["to_ref"]
        for event in extraction["events"]:
            (node,) = [
                node for node in world.nodes_by_id.values()
                if node.node_type == "event" and node.summary == event["summary"]
            ]
            assert world.get_state(node.id)["participant_ids"] == event["participant_refs"]

        calendar = StoryCalendarRepository(session).get(book.project_id)
        lexicon = BookLexiconRepository(session).get(book.project_id)
        elapsed = extraction["time"]["elapsed"]
        assert BookStateQuery(session).current_story_minute(book.project_id, as_of_chapter=1) == (
            calendar.opening_minute
            + duration_to_minutes(calendar, lexicon, amount=elapsed["amount"], unit_name=elapsed["unit"])
        )


@pytest.mark.parametrize("genre", GENRES)
def test_too_fast_travel_is_caught_per_genre(pipeline, genre: str) -> None:
    book = _start_book(pipeline, genre)
    extraction = book.extraction()
    movement = extraction["movements"][0]

    with pipeline._SessionFactory() as session:  # noqa: SLF001
        shortest = _shortest_minutes(session, book.project_id, movement)
        unit = min(StoryCalendarRepository(session).get(book.project_id).units, key=lambda item: item.minutes)
        # Stay clearly below the shortest allowed time, in whole minutes and the book's own unit.
        allowed_minimum = shortest * (1 - pipeline.policy.review.travel_tolerance)
        claimed_minutes = max(0, math.floor(allowed_minimum / 2))
        assert 0 < claimed_minutes < allowed_minimum
        movement["depart_offset"] = {"amount": 0, "unit": unit.name}
        movement["arrive_offset"] = {"amount": claimed_minutes / unit.minutes, "unit": unit.name}
        pipeline.llm_client = _ExtractionClient(extraction)
        _output, review = _review(pipeline, session, book)

    assert review.verdict == "fail"
    (issue,) = [issue for issue in review.issues if issue.rule_name == "movement_too_fast"]
    assert (issue.severity, issue.reviewer) == ("error", "world_consistency")
    assert review.repair_instruction is not None
    assert any(
        issue.description in item and WORLD_REPAIR_FIXES["movement_too_fast"] in item
        for item in review.repair_instruction.must_fix
    )


def test_three_genres_share_chapter_pipeline(pipeline) -> None:
    sequences: dict[str, list[str]] = {}

    for genre in GENRES:
        book = _start_book(pipeline, genre)
        client = _ExtractionClient(book.extraction())
        pipeline.llm_client = client
        calls: list[str] = []
        # Spies go on after start-writing, whose world seed also passes the gate.
        with pipeline._SessionFactory() as session, ExitStack() as spies:  # noqa: SLF001
            for module in (review_autofix, preparation):
                spies.enter_context(patch.object(
                    module, "ensure_chapter_delta", spy("ensure_chapter_delta", ensure_chapter_delta, calls),
                ))
            spies.enter_context(patch.object(
                ChapterDeltaTranslator, "translate", spy("translate", ChapterDeltaTranslator.translate, calls),
            ))
            spies.enter_context(patch.object(
                BookStateReviewGate, "review", spy("review", BookStateReviewGate.review, calls),
            ))
            output, review = _review(pipeline, session, book)
            _commit(pipeline, session, book, output, review, client)
        sequences[genre] = calls

    assert sequences["xianxia"] == SHARED_CHAPTER_PIPELINE
    assert sequences == {genre: SHARED_CHAPTER_PIPELINE for genre in GENRES}
