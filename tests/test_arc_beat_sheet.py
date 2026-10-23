from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state import BookStateCompiler, BookStateRepository
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter
from forwin.maintenance.post_canon import save_world_prompt_trace
from forwin.models import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.planning_v6 import ArcBeatSheetRow
from forwin.models.project import ArcPlanVersion
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet, FactNode, FactPatch, GraphDelta, MapNode, NodePatch, WorldNode,
)
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.writer.prompt_core.planning import WORLD_DIGEST_TITLES
from tests.postgres import postgres_test_url
from tests.test_chapter_delta_translator import OPENING, REGION, SUBWORLD, _World, _pack


POLICY = RuntimePolicy.for_profile("standard")


class StubLLM:
    model = "stub-model"

    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls: list[tuple[list[dict], dict]] = []
        self.llm_attempt_events: list[dict] = []

    def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        response = self.responses[len(self.calls) - 1]
        attempt = {"attempt_no": 1, "stage_key": kwargs["stage_key"], "model": self.model}
        if isinstance(response, Exception):
            self.llm_attempt_events.append({**attempt, "error_class": type(response).__name__})
            raise response
        text = response if isinstance(response, str) else json.dumps(response)
        self.llm_attempt_events.append({**attempt, "output_chars": len(text)})
        return text

    def drain_llm_attempt_events(self):
        events, self.llm_attempt_events = self.llm_attempt_events, []
        return events

    def user_prompt(self, call: int) -> str:
        return self.calls[call][0][-1]["content"]

    def payload(self, call: int) -> dict:
        return json.loads(self.user_prompt(call).split("\n\n", 1)[0])


@pytest.fixture
def Session():
    engine = get_engine(postgres_test_url("arc-beat-sheet"))
    init_db(engine)
    yield get_session_factory(engine)
    engine.dispose()


@pytest.fixture
def digest_calls(monkeypatch) -> list[dict]:
    """The keyword arguments of every world digest the service builds; the real digest still runs."""
    from forwin.planning.beat_sheet import service as module

    calls: list[dict] = []
    real = module.build_world_digest

    def spy(session, **kwargs):
        calls.append(kwargs)
        return real(session, **kwargs)

    monkeypatch.setattr(module, "build_world_digest", spy)
    return calls


def _book(Session, *, chapter_end: int = 12) -> tuple[_World, str]:
    """Chapter-0 canon from a world seed, its Genesis pack active, and one arc of ``chapter_end`` chapters."""
    pack = _pack()
    pack["world_seed"]["reveal_candidates"] = [{"fact_id": "fact-1", "arc_number": 1, "note": "candidate-note"}]
    pack["book_arc_blueprint"] = {"arcs": [{"arc_number": 1, "chapter_start": 1, "chapter_end": chapter_end}]}
    with Session.begin() as session:
        project = StateUpdater(session).create_project(
            title="arc beat sheet", premise="beat sheet", genre="test",
            runtime_policy=POLICY, creation_status="genesis_ready",
        )
        revision = BookGenesisRevision(project_id=project.id, pack_json=json.dumps(pack, ensure_ascii=False))
        session.add(revision)
        session.flush()
        project.active_genesis_revision_id = revision.id
        world = _World(project.id)
        repo = BookStateRepository(session)
        for number in (1, 2, 3):
            repo.create_map_node(MapNode(
                id=world.loc(number), project_id=project.id, subworld_id=SUBWORLD, region_id=REGION,
                node_type="site", name=f"site-{number}", metadata={"source_node_id": f"node-{number}"},
            ))
        session.flush()
        WorldSeedCommitter().commit(session=session, project=project, revision=revision, pack=pack)
        arc = ArcPlanVersion(
            project_id=project.id, arc_number=1, chapter_start=1, chapter_end=chapter_end,
            title="arc-title", goal="arc-goal", stakes="arc-stakes", payoff_direction="arc-payoff",
            arc_synopsis="arc-synopsis",
        )
        session.add(arc)
        session.flush()
        return world, arc.id


def _sheet(world: _World, *, bands=((1, 6), (7, 12)), required_refs=None) -> dict:
    return {
        "protagonist_ref": world.hero,
        "phases": [
            {"name": "phase-a", "chapter_range": [1, 5], "purpose": "purpose-a"},
            {"name": "phase-b", "chapter_range": [6, 12], "purpose": "purpose-b"},
        ],
        "milestones": [{
            "id": "milestone-1", "description": "milestone-description", "target_range": [4, 8],
            "required_refs": required_refs if required_refs is not None else [world.hero, world.rival],
            "hard": True,
        }],
        "protections": [
            {"ref": world.hero, "kind": "must_be_alive", "until_chapter": 8},
            {"ref": world.rival, "kind": "must_not_know", "until_chapter": 8,
             "observer_ref": world.rival, "fact_ref": world.fact("fact-1")},
        ],
        "reveal_targets": [
            {"fact_ref": world.fact("fact-1"), "observer": "reader", "target_state": "suspects", "range": [9, 12]},
        ],
        "exit_state": [{"ref": world.rival, "field": "alive_status", "value": "alive", "note": "exit-note"}],
        "band_breakdown": [
            {"band_index": index, "chapter_start": start, "chapter_end": end}
            for index, (start, end) in enumerate(bands)
        ],
        "reader_promise": {"genre_promise": "promise-value"},
        "arc_payoff_map": {"awe_kit": ["awe-value"]},
    }


def _service(client):
    from forwin.planning.beat_sheet.service import ArcBeatSheetService
    return ArcBeatSheetService(client, save_trace=save_world_prompt_trace)


def _ensure(Session, service, world: _World, arc_id: str, **kwargs) -> str:
    with Session.begin() as session:
        row = service.ensure_for_arc(
            session, project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id), policy=POLICY,
            **kwargs,
        )
        return row.id


def _spans(data) -> list[tuple[int, int]]:
    return [(band.chapter_start, band.chapter_end) for band in data.band_breakdown]


def _traces(session, project_id: str) -> list[PromptTrace]:
    return list(session.scalars(select(PromptTrace).where(
        PromptTrace.project_id == project_id, PromptTrace.stage_key == "plan.arc_beats",
    )))


def _trace_event_types(session, trace_id: str) -> list[str]:
    return list(session.scalars(select(DecisionEvent.event_type).where(
        DecisionEvent.related_object_type == "prompt_trace", DecisionEvent.related_object_id == trace_id,
    ).order_by(DecisionEvent.created_at, DecisionEvent.id)))


def test_beat_sheet_from_stub_llm_validates_and_persists(Session, digest_calls) -> None:
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository

    world, arc_id = _book(Session)
    with Session.begin() as session:
        NarrativeObligationRepository(session).create_obligation(NarrativeObligation(
            project_id=world.project_id, origin_chapter_number=0, obligation_type="world_change_response",
            status="active", summary="obligation-summary", deadline_chapter=5, payoff_test="payoff-test",
        ))
    client = StubLLM(_sheet(world))
    row_id = _ensure(Session, _service(client), world, arc_id)

    assert len(client.calls) == 1
    kwargs = client.calls[0][1]
    assert (kwargs["stage_key"], kwargs["task_family"]) == ("plan.arc_beats", "planning")
    prompt = client.user_prompt(0)
    # Arc, author-view digest, seeded actors and facts, the reveal candidate and the band size reach the model.
    for expected in ("arc-goal", "arc-stakes", "arc-payoff", WORLD_DIGEST_TITLES["protagonist"],
                     world.hero, world.fact("fact-1"), "proposition-1", "candidate-note"):
        assert expected in prompt
    payload = client.payload(0)
    assert payload["band_size"] == POLICY.planning.band_size
    assert [(item["summary"], item["deadline_chapter"]) for item in payload["open_obligations"]] == [
        ("obligation-summary", 5),
    ]
    actors = {item["id"]: item for item in payload["actors"]}
    assert actors[world.hero]["seeded"] is True and actors[world.hero]["kind"] == "character"
    # The digest: the policy budget, one story day per arc chapter from the opening minute, the fallback protagonist.
    assert digest_calls == [{
        "project_id": world.project_id, "as_of_chapter": 0, "protagonist_ref": world.hero,
        "window_end_minute": OPENING + 12 * 1440, "budget_chars": POLICY.planning.digest_budget_chars,
        "runtime": digest_calls[0]["runtime"],
    }]

    with Session() as session:
        row = session.get(ArcBeatSheetRow, row_id)
        assert (row.version, row.status, row.source, row.revision_reason) == (1, "active", "llm", "activation")
        assert row.protagonist_ref == world.hero
        data = ArcBeatSheetRepository(session).load(row)
        assert _spans(data) == [(1, 6), (7, 12)]
        assert [band.band_index for band in data.band_breakdown] == [0, 1]
        assert [phase.purpose for phase in data.phases] == ["purpose-a", "purpose-b"]
        assert [(item.ref, item.kind) for item in data.protections] == [
            (world.hero, "must_be_alive"), (world.rival, "must_not_know"),
        ]
        assert data.reveal_targets[0].observer == "reader"
        assert data.reader_promise.genre_promise == "promise-value"
        assert data.arc_payoff_map.awe_kit == ["awe-value"]
        (trace,) = _traces(session, world.project_id)
        assert json.loads(trace.output_summary_json)["source"] == "llm"
        assert _trace_event_types(session, trace.id) == [DecisionEventType.LLM_REQUEST_SUCCEEDED]


def test_invalid_ref_retries_once_then_falls_back(Session) -> None:
    from forwin.planning.beat_sheet.fallback import split_arc_into_bands
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository

    world, arc_id = _book(Session, chapter_end=14)
    bad = _sheet(world, bands=((1, 7), (8, 14)), required_refs=["ghost-node"])
    bad["phases"][-1]["chapter_range"] = [6, 14]
    client = StubLLM(bad, bad)
    row_id = _ensure(Session, _service(client), world, arc_id)

    assert len(client.calls) == 2
    assert "Retry validation feedback" not in client.user_prompt(0)
    assert "ghost-node" in client.user_prompt(1).split("Retry validation feedback", 1)[1]
    with Session() as session:
        row = session.get(ArcBeatSheetRow, row_id)
        assert (row.version, row.status, row.source) == (1, "active", "fallback")
        data = ArcBeatSheetRepository(session).load(row)
        assert data.band_breakdown == split_arc_into_bands(1, 14, POLICY.planning.band_size)
        assert _spans(data) == [(1, 7), (8, 14)]
        assert [(phase.chapter_range, phase.purpose) for phase in data.phases] == [((1, 14), "arc-goal")]
        assert (data.milestones, data.protections, data.reveal_targets, data.exit_state) == ([], [], [], [])
        # The first seeded core_cast character.
        assert data.protagonist_ref == world.hero
        assert (data.reader_promise, data.arc_payoff_map) == (ReaderPromise(), ArcPayoffMap())
        (trace,) = _traces(session, world.project_id)
        summary = json.loads(trace.output_summary_json)
        assert (summary["source"], summary["failure_reason"]) == ("fallback", "invalid_output")
        assert any("ghost-node" in issue for issue in summary["issues"])
        assert DecisionEventType.RETRY_ATTEMPT in _trace_event_types(session, trace.id)


def test_llm_error_falls_back_and_still_records_the_trace(Session) -> None:
    world, arc_id = _book(Session)
    client = StubLLM(RuntimeError("provider down"), RuntimeError("provider down"))
    row_id = _ensure(Session, _service(client), world, arc_id)

    assert len(client.calls) == 2
    with Session() as session:
        assert session.get(ArcBeatSheetRow, row_id).source == "fallback"
        (trace,) = _traces(session, world.project_id)
        assert json.loads(trace.output_summary_json)["failure_reason"] == "llm_unavailable"
        assert _trace_event_types(session, trace.id).count(DecisionEventType.LLM_REQUEST_FAILED) == 2


@pytest.mark.parametrize(("first", "fed_back"), [
    (RuntimeError("provider down"), False),
    ("not a json object", True),
], ids=["provider_error", "malformed_output"])
def test_only_output_problems_are_fed_back_on_retry(Session, first, fed_back) -> None:
    world, arc_id = _book(Session)
    client = StubLLM(first, _sheet(world))
    row_id = _ensure(Session, _service(client), world, arc_id)

    assert len(client.calls) == 2
    # A provider failure resends the same messages; a malformed answer goes back to the model.
    assert (client.calls[1][0] == client.calls[0][0]) is not fed_back
    assert ("Retry validation feedback" in client.user_prompt(1)) is fed_back
    assert "provider down" not in client.user_prompt(1)
    with Session() as session:
        assert session.get(ArcBeatSheetRow, row_id).source == "llm"


def test_without_an_llm_client_the_fallback_is_written_with_a_trace(Session) -> None:
    world, arc_id = _book(Session)
    row_id = _ensure(Session, _service(None), world, arc_id)

    with Session() as session:
        row = session.get(ArcBeatSheetRow, row_id)
        assert (row.source, row.protagonist_ref) == ("fallback", world.hero)
        (trace,) = _traces(session, world.project_id)
        summary = json.loads(trace.output_summary_json)
        assert (summary["source"], summary["failure_reason"]) == ("fallback", "llm_unavailable")
        assert json.loads(trace.attempts_json) == []
        assert _trace_event_types(session, trace.id) == []


def test_sheet_after_chapter_one_is_checked_against_the_latest_canon(Session, digest_calls) -> None:
    world, arc_id = _book(Session)
    with Session.begin() as session:
        result = BookStateCompiler(session).compile(ApprovedGraphDeltaSet(
            project_id=world.project_id, chapter_number=1, graph_deltas=[GraphDelta(
                id=f"chapter-1-{world.project_id}", project_id=world.project_id, chapter_number=1,
                story_time_start=OPENING, story_time_end=OPENING + 90,
                node_patches=[NodePatch(node_id=world.rival, node_type="character", op="deactivate",
                                        field_path="is_active", old_value=True)],
                fact_patches=[FactPatch(fact_id="fact-ch1", op="create", proposition="chapter-one-fact",
                                        new_value={"project_id": world.project_id})],
            )],
        ))
        assert result.committed, result.blocked_reasons
    stale = _sheet(world)
    current = _sheet(world, required_refs=[world.hero])
    current["protections"] = current["protections"][:1]
    current["exit_state"] = [{"ref": world.hero, "field": "alive_status", "value": "alive", "note": ""}]
    current["reveal_targets"][0]["fact_ref"] = "fact:fact-ch1"
    client = StubLLM(stale, current)
    row_id = _ensure(Session, _service(client), world, arc_id)

    actor_ids = [item["id"] for item in client.payload(0)["actors"]]
    assert world.hero in actor_ids and world.rival not in actor_ids
    assert "fact:fact-ch1" in [item["ref"] for item in client.payload(0)["facts"]]
    feedback = client.user_prompt(1).split("Retry validation feedback", 1)[1]
    assert f"milestones[0].required_refs[1]: {world.rival!r} is not one of the offered actors" in feedback
    assert digest_calls[0]["as_of_chapter"] == 1
    assert digest_calls[0]["window_end_minute"] == OPENING + 90 + 12 * 1440
    with Session() as session:
        assert session.get(ArcBeatSheetRow, row_id).source == "llm"


@pytest.mark.parametrize(("bands", "issue"), [
    (((1, 8), (10, 17)), "band_breakdown does not cover chapters 9-9"),
    (((1, 8), (8, 17)), "band_breakdown overlaps at chapter 8"),
    (((1, 13), (14, 17)), "band 0 spans 13 chapters"),
], ids=["missing_chapter", "overlap", "band_too_long"])
def test_band_breakdown_must_cover_arc_without_overlap(Session, bands, issue) -> None:
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository

    world, arc_id = _book(Session, chapter_end=17)
    bad, good = _sheet(world, bands=bands), _sheet(world, bands=((1, 8), (9, 17)))
    for sheet in (bad, good):
        sheet["phases"][-1]["chapter_range"] = [6, 17]
    client = StubLLM(bad, good)
    row_id = _ensure(Session, _service(client), world, arc_id)

    assert len(client.calls) == 2
    assert issue in client.user_prompt(1).split("Retry validation feedback", 1)[1]
    with Session() as session:
        row = session.get(ArcBeatSheetRow, row_id)
        assert row.source == "llm"
        assert _spans(ArcBeatSheetRepository(session).load(row)) == [(1, 8), (9, 17)]


def test_revise_creates_new_version_and_supersedes_old(Session) -> None:
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository

    world, arc_id = _book(Session)
    revised = _sheet(world, bands=((1, 5), (6, 12)))
    revised["milestones"][0]["description"] = "revised-milestone"
    client = StubLLM(_sheet(world), revised)
    service = _service(client)
    first_id = _ensure(Session, service, world, arc_id)
    with Session.begin() as session:
        second_id = service.revise(
            session, project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id), policy=POLICY,
            reason="conflict:conflict-1", conflict_summary="conflict-summary-value",
        ).id

    prompt = client.user_prompt(1)
    assert "conflict-summary-value" in prompt and "milestone-description" in prompt
    with Session() as session:
        repo = ArcBeatSheetRepository(session)
        first, second = session.get(ArcBeatSheetRow, first_id), session.get(ArcBeatSheetRow, second_id)
        assert (first.version, first.status) == (1, "superseded")
        assert (second.version, second.status, second.revision_reason) == (2, "active", "conflict:conflict-1")
        assert repo.active_for_arc(arc_id).id == second_id
        assert [row.id for row in repo.active_for_project(world.project_id)] == [second_id]
        assert repo.load(second).milestones[0].description == "revised-milestone"
        assert len(_traces(session, world.project_id)) == 2
    with Session.begin() as session:
        session.get(ArcPlanVersion, arc_id).status = "superseded"
    with Session() as session:
        # A sheet of a superseded arc plan is no longer one of the project's active sheets.
        assert ArcBeatSheetRepository(session).active_for_project(world.project_id) == []


def test_ensure_is_idempotent(Session) -> None:
    world, arc_id = _book(Session)
    client = StubLLM(_sheet(world))
    service = _service(client)

    first_id = _ensure(Session, service, world, arc_id)
    assert _ensure(Session, service, world, arc_id) == first_id
    assert len(client.calls) == 1
    with Session() as session:
        assert session.scalars(select(ArcBeatSheetRow.version).where(ArcBeatSheetRow.arc_plan_id == arc_id)).all() == [1]


_SEEDED = {"source": "genesis_seed"}
_RUNTIME = BookStateRuntime(project_id="p", as_of_chapter=0, world=ObjectiveWorldGraph(nodes=[
    WorldNode(id="hero", project_id="p", node_type="character", metadata=_SEEDED),
    WorldNode(id="rival", project_id="p", node_type="character", metadata=_SEEDED),
    WorldNode(id="ghost", project_id="p", node_type="character", metadata=_SEEDED, is_active=False),
    WorldNode(id="newcomer", project_id="p", node_type="character", metadata={"source": "chapter_extraction"}),
    WorldNode(id="banner", project_id="p", node_type="faction", metadata=_SEEDED),
    WorldNode(id="relic", project_id="p", node_type="item"),
], facts=[FactNode(id="secret", project_id="p", proposition="secret-value")]), map_graph=MapGraph())
# The actors a prompt built from _RUNTIME lists: every active actor node.
_OFFERED = {"hero", "rival", "newcomer", "banner"}
_VALID = {
    "protagonist_ref": "hero",
    "phases": [{"name": "phase", "chapter_range": [1, 6], "purpose": "purpose"}],
    "band_breakdown": [{"band_index": 0, "chapter_start": 1, "chapter_end": 6}],
    "milestones": [{"id": "m-1", "description": "d", "target_range": [2, 4], "required_refs": ["hero"]}],
    "protections": [{"ref": "hero", "kind": "must_be_alive", "until_chapter": 4}],
    "reveal_targets": [{"fact_ref": "fact:secret", "observer": "reader", "target_state": "suspects", "range": [5, 6]}],
    "exit_state": [{"ref": "rival", "field": "alive_status", "value": "alive"}],
}


def _issues(*, chapter_end: int = 6, **changes) -> list[str]:
    from forwin.planning.beat_sheet.service import beat_sheet_issues
    from forwin.planning.beat_sheet.types import ArcBeatSheetData

    data = ArcBeatSheetData.model_validate({**_VALID, **changes})
    return beat_sheet_issues(data, chapter_start=1, chapter_end=chapter_end, runtime=_RUNTIME, actor_ids=_OFFERED)


def _protection(ref: str, kind: str, until: int = 4, **fields) -> dict:
    return {"protections": [{"ref": ref, "kind": kind, "until_chapter": until, **fields}]}


def _milestone(**fields) -> dict:
    return {"milestones": [{**_VALID["milestones"][0], **fields}]}


def _reveal(**fields) -> dict:
    return {"reveal_targets": [{**_VALID["reveal_targets"][0], **fields}]}


def _exit(ref: str, field: str) -> dict:
    return {"exit_state": [{"ref": ref, "field": field, "value": "v"}]}


@pytest.mark.parametrize(("changes", "issue"), [
    ({"protagonist_ref": "ghost"}, "protagonist_ref: 'ghost' is not one of the offered actors"),
    ({"protagonist_ref": "newcomer"}, "protagonist_ref: 'newcomer' is not a character node from the world seed"),
    (_milestone(required_refs=["ghost"]), "milestones[0].required_refs[0]: 'ghost' is not one of the offered actors"),
    (_milestone(required_refs=["relic"]), "milestones[0].required_refs[0]: 'relic' is not one of the offered actors"),
    (_milestone(target_range=[5, 9]), "milestones[0].target_range 5-9 is not an ordered range inside the arc 1-6"),
    (_protection("ghost", "must_exist"), "protections[0].ref: 'ghost' is not one of the offered actors"),
    (_protection("hero", "must_be_alive", until=7), "protections[0].until_chapter 7 is outside the arc 1-6"),
    (_protection("banner", "must_be_alive"), "protections[0].ref: must_be_alive needs a character, not 'faction'"),
    (_protection("hero", "must_hold_state"), "protections[0].field is required for must_hold_state"),
    (_protection("hero", "must_hold_state", field="unknown_field"),
     "protections[0].field 'unknown_field' is not a character field"),
    (_protection("hero", "must_hold_state", field="profile.location_id"),
     "protections[0].field 'profile.location_id' is not a character field"),
    (_protection("hero", "must_not_know", observer_ref="ghost", fact_ref="fact:secret"),
     "protections[0].observer_ref: 'ghost' is not an active BookState node"),
    (_protection("hero", "must_not_know", observer_ref="hero", fact_ref="secret"),
     "protections[0].fact_ref: 'secret' is not a fact ref"),
    (_reveal(observer="nobody"), "reveal_targets[0].observer: 'nobody' is not an active BookState node"),
    (_reveal(observer="ghost"), "reveal_targets[0].observer: 'ghost' is not an active BookState node"),
    (_reveal(target_state="revealed"), "reveal_targets[0].target_state 'revealed' is not an allowed value"),
    (_reveal(fact_ref="secret"), "reveal_targets[0].fact_ref: 'secret' is not a fact ref"),
    (_exit("ghost", "alive_status"), "exit_state[0].ref: 'ghost' is not one of the offered actors"),
    (_exit("rival", ""), "exit_state[0].field is required"),
    (_exit("banner", "unknown_field"), "exit_state[0].field 'unknown_field' is not a faction field"),
], ids=[
    "deactivated_protagonist",
    "unseeded_protagonist",
    "deactivated_required_ref",
    "unoffered_required_ref",
    "milestone_outside_arc",
    "deactivated_protection_ref",
    "until_outside_arc",
    "alive_on_faction",
    "hold_state_without_field",
    "hold_state_unknown_field",
    "hold_state_wrong_section",
    "deactivated_observer",
    "bare_fact_ref",
    "unknown_reveal_observer",
    "deactivated_reveal_observer",
    "unknown_target_state",
    "bare_reveal_fact",
    "deactivated_exit_ref",
    "exit_without_field",
    "exit_unknown_field",
])
def test_validation_rejects_refs_and_protections_that_cannot_hold(changes, issue) -> None:
    assert issue in _issues(**changes)


@pytest.mark.parametrize("changes", [
    {},
    # Ruling P4a-R13: the reader is an observer a must_not_know protection may name.
    _protection("reader", "must_not_know", observer_ref="reader", fact_ref="fact:secret"),
    _protection("hero", "must_hold_state", field="location_id", value="site"),
    _protection("hero", "must_hold_state", field="state.location_id", value="site"),
    _protection("hero", "must_hold_state", field="profile.fear", value="fear"),
    _exit("banner", "state.influence_level"),
], ids=["valid", "reader_must_not_know", "bare_field", "state_field", "profile_field", "faction_exit_field"])
def test_validation_accepts_sheets_that_can_hold(changes) -> None:
    assert _issues(**changes) == []


def test_an_arc_shorter_than_four_chapters_is_one_band() -> None:
    short = {"phases": [{"name": "phase", "chapter_range": [1, 3], "purpose": "purpose"}],
             "milestones": [], "protections": [], "reveal_targets": []}
    one = [{"band_index": 0, "chapter_start": 1, "chapter_end": 3}]
    two = [{"band_index": 0, "chapter_start": 1, "chapter_end": 1}, {"band_index": 1, "chapter_start": 2, "chapter_end": 3}]
    assert _issues(chapter_end=3, band_breakdown=one, **short) == []
    assert _issues(chapter_end=3, band_breakdown=two, **short) == [
        "band_breakdown: an arc of 3 chapters is a single band",
    ]


def test_fallback_protagonist_needs_a_seeded_core_cast_character() -> None:
    from forwin.genesis.handoff.world_seed_committer import seed_ref_id
    from forwin.planning.beat_sheet.fallback import fallback_protagonist_ref

    hero = seed_ref_id("p", "character", "cast-2")
    runtime = BookStateRuntime(project_id="p", as_of_chapter=0, world=ObjectiveWorldGraph(nodes=[
        WorldNode(id=hero, project_id="p", node_type="character", metadata=_SEEDED),
    ]), map_graph=MapGraph())
    cast = {"world": {"story_engine": {"core_cast": [{"id": "cast-1"}, {"id": "cast-2"}]}}}
    assert fallback_protagonist_ref("p", cast, runtime) == hero
    for pack in ({}, {"world": {"story_engine": {"core_cast": [{"id": "cast-1"}]}}}):
        with pytest.raises(ValueError, match="no seeded core_cast character"):
            fallback_protagonist_ref("p", pack, runtime)


def test_split_arc_into_bands_always_passes_band_validation() -> None:
    from forwin.planning.beat_sheet.fallback import split_arc_into_bands
    from forwin.planning.beat_sheet.service import band_breakdown_issues

    def sizes(start: int, end: int, band_size: int) -> list[int]:
        return [band.chapter_end - band.chapter_start + 1 for band in split_arc_into_bands(start, end, band_size)]

    assert sizes(1, 12, 6) == [6, 6]
    assert sizes(1, 14, 6) == [7, 7]
    assert sizes(1, 23, 6) == [6, 6, 6, 5]
    assert sizes(1, 13, 12) == [6, 7]
    assert sizes(5, 7, 6) == [3]
    for start in (1, 7):
        for length in range(1, 61):
            for band_size in range(4, 13):
                bands = split_arc_into_bands(start, start + length - 1, band_size)
                assert [band.band_index for band in bands] == list(range(len(bands)))
                assert band_breakdown_issues(bands, start, start + length - 1) == [], (length, band_size)


def test_fallback_protagonist_skips_inactive_seeded_character():
    from forwin.genesis.handoff.world_seed_committer import seed_ref_id
    from forwin.planning.beat_sheet.fallback import fallback_protagonist_ref

    refs = [seed_ref_id("p", "character", ref) for ref in ("first", "second")]
    runtime = BookStateRuntime(project_id="p", as_of_chapter=1, world=ObjectiveWorldGraph(nodes=[
        WorldNode(id=refs[0], project_id="p", node_type="character", is_active=False, metadata=_SEEDED),
        WorldNode(id=refs[1], project_id="p", node_type="character", metadata=_SEEDED),
    ]), map_graph=MapGraph())
    pack = {"world": {"story_engine": {"core_cast": [{"id": "first"}, {"id": "second"}]}}}
    assert fallback_protagonist_ref("p", pack, runtime) == refs[1]


def test_provider_failure_after_chapter_one_uses_next_active_seeded_character(Session):
    world, arc_id = _book(Session)
    with Session.begin() as session:
        revision = session.scalars(select(BookGenesisRevision).where(BookGenesisRevision.project_id == world.project_id)).one()
        pack = json.loads(revision.pack_json)
        pack["world"]["story_engine"]["core_cast"].append({"id": "opp-1"})
        revision.pack_json = json.dumps(pack)
        result = BookStateCompiler(session).compile(ApprovedGraphDeltaSet(
            project_id=world.project_id, chapter_number=1, graph_deltas=[GraphDelta(
                id=f"deactivate-{world.project_id}", project_id=world.project_id, chapter_number=1,
                node_patches=[NodePatch(node_id=world.hero, node_type="character", op="deactivate",
                                        field_path="is_active", old_value=True)],
            )],
        ))
        assert result.committed, result.blocked_reasons
    client = StubLLM(RuntimeError("unavailable"), RuntimeError("unavailable"))
    row_id = _ensure(Session, _service(client), world, arc_id)
    with Session() as session:
        row = session.get(ArcBeatSheetRow, row_id)
        assert (row.source, row.protagonist_ref) == ("fallback", world.rival)
        assert len(_traces(session, world.project_id)) == 1
    assert len(client.calls) == 2


@pytest.mark.parametrize("revise", [False, True])
def test_generation_rereads_active_sheet_after_llm_and_saves_losing_trace(Session, monkeypatch, revise):
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from forwin.planning.beat_sheet.types import ArcBeatSheetData

    world, arc_id = _book(Session)
    service = _service(StubLLM(_sheet(world), _sheet(world)))
    if revise:
        _ensure(Session, service, world, arc_id)
    real_call = service._call
    with Session.begin() as session:
        winner = []

        def racing_call(*args, **kwargs):
            result = real_call(*args, **kwargs)
            # A competing writer wins while this call generates; the post-call read must see it.
            winner.append(ArcBeatSheetRepository(session).create(
                world.project_id, arc_id, ArcBeatSheetData.model_validate(_sheet(world)), source="llm", reason="winner",
            ))
            return result

        monkeypatch.setattr(service, "_call", racing_call)
        args = dict(project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id), policy=POLICY)
        row = (service.revise(session, **args, reason="revision", conflict_summary="change") if revise
               else service.ensure_for_arc(session, **args))
        assert row.id == winner[0].id
        rows = list(session.scalars(select(ArcBeatSheetRow).where(ArcBeatSheetRow.project_id == world.project_id)))
        assert len(rows) == (2 if revise else 1)
        assert len(_traces(session, world.project_id)) == (2 if revise else 1)


@pytest.mark.parametrize("revise", [False, True], ids=["ensure_for_arc", "revise"])
def test_two_sessions_race(Session, revise):
    import threading
    from forwin.book_state.projection import BookStateProjection
    from forwin.models.book_state import GraphDeltaRow

    world, arc_id = _book(Session)
    if revise:
        _ensure(Session, _service(StubLLM(_sheet(world))), world, arc_id)
    barrier = threading.Barrier(2)
    results, errors = {}, {}

    class BarrierLLM(StubLLM):
        def chat(self, messages, **kwargs):
            barrier.wait(timeout=20)
            return super().chat(messages, **kwargs)

    def run(name):
        try:
            service = _service(BarrierLLM(_sheet(world)))
            with Session.begin() as session:
                args = dict(project_id=world.project_id, arc_plan=session.get(ArcPlanVersion, arc_id), policy=POLICY)
                row = (service.revise(session, **args, reason=f"race-{name}", conflict_summary="change")
                       if revise else service.ensure_for_arc(session, **args))
                results[name] = row.id
        except Exception as exc:  # noqa: BLE001 - report worker failures in the test thread
            errors[name] = repr(exc)

    threads = [threading.Thread(target=run, args=(name,), daemon=True) for name in ("x", "y")]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=25)
    assert not any(thread.is_alive() for thread in threads)
    assert not errors, errors
    assert results["x"] == results["y"]
    with Session() as session:
        rows = list(session.scalars(select(ArcBeatSheetRow).where(ArcBeatSheetRow.project_id == world.project_id)))
        assert [(row.version, row.status) for row in sorted(rows, key=lambda row: row.version)] == (
            [(1, "superseded"), (2, "active")] if revise else [(1, "active")])
        active = [row for row in rows if row.status == "active"]
        assert active[0].id == results["x"]
        deltas = list(session.scalars(select(GraphDeltaRow).where(
            GraphDeltaRow.project_id == world.project_id, GraphDeltaRow.source_type == "plan_constraint")))
        assert len(deltas) == (2 if revise else 1)
        assert len(_traces(session, world.project_id)) == (3 if revise else 2)
        runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=0)
        live = [node for node in runtime.narrative.nodes_by_id.values()
                if node.node_type == "review_constraint" and node.payload.get("status") != "released"]
        assert {node.metadata.get("beat_sheet_id") for node in live} == {results["x"]}
