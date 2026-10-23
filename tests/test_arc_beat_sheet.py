from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.book_state import BookStateRepository
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.genesis.handoff.world_seed_committer import WorldSeedCommitter
from forwin.maintenance.post_canon import save_world_prompt_trace
from forwin.models import DecisionEvent
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.genesis import BookGenesisRevision, PromptTrace
from forwin.models.planning_v6 import ArcBeatSheetRow
from forwin.models.project import ArcPlanVersion
from forwin.protocol.book_state import FactNode, MapNode, WorldNode
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.writer.prompt_core.planning import WORLD_DIGEST_TITLES
from tests.postgres import postgres_test_url
from tests.test_chapter_delta_translator import REGION, SUBWORLD, _World, _pack


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
        text = json.dumps(response)
        self.llm_attempt_events.append({**attempt, "output_chars": len(text)})
        return text

    def drain_llm_attempt_events(self):
        events, self.llm_attempt_events = self.llm_attempt_events, []
        return events

    def user_prompt(self, call: int) -> str:
        return self.calls[call][0][-1]["content"]


@pytest.fixture
def Session():
    engine = get_engine(postgres_test_url("arc-beat-sheet"))
    init_db(engine)
    yield get_session_factory(engine)
    engine.dispose()


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


def test_beat_sheet_from_stub_llm_validates_and_persists(Session) -> None:
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository

    world, arc_id = _book(Session)
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
    assert json.loads(prompt.split("\n\n", 1)[0])["band_size"] == POLICY.planning.band_size

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
        assert summary["source"] == "fallback"
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
        assert _trace_event_types(session, trace.id).count(DecisionEventType.LLM_REQUEST_FAILED) == 2


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


def test_ensure_is_idempotent(Session) -> None:
    world, arc_id = _book(Session)
    client = StubLLM(_sheet(world))
    service = _service(client)

    first_id = _ensure(Session, service, world, arc_id)
    assert _ensure(Session, service, world, arc_id) == first_id
    assert len(client.calls) == 1
    with Session() as session:
        assert session.scalars(select(ArcBeatSheetRow.version).where(ArcBeatSheetRow.arc_plan_id == arc_id)).all() == [1]


def test_validation_requires_a_seeded_protagonist_and_complete_protections() -> None:
    from forwin.planning.beat_sheet.service import beat_sheet_issues
    from forwin.planning.beat_sheet.types import ArcBeatSheetData

    seeded = {"source": "genesis_seed"}
    runtime = BookStateRuntime(project_id="p", as_of_chapter=0, world=ObjectiveWorldGraph(nodes=[
        WorldNode(id="hero", project_id="p", node_type="character", metadata=seeded),
        WorldNode(id="newcomer", project_id="p", node_type="character", metadata={"source": "chapter_extraction"}),
    ], facts=[FactNode(id="secret", project_id="p", proposition="secret-value")]), map_graph=MapGraph())
    sheet = {
        "protagonist_ref": "newcomer",
        "phases": [{"name": "phase", "chapter_range": [1, 6], "purpose": "purpose"}],
        "band_breakdown": [{"band_index": 0, "chapter_start": 1, "chapter_end": 6}],
        "protections": [
            {"ref": "hero", "kind": "must_hold_state", "until_chapter": 6},
            {"ref": "hero", "kind": "must_not_know", "until_chapter": 7, "observer_ref": "hero", "fact_ref": "secret"},
        ],
        "reveal_targets": [{"fact_ref": "fact:secret", "observer": "reader", "target_state": "revealed", "range": [2, 3]}],
    }

    def issues(**changes) -> list[str]:
        data = ArcBeatSheetData.model_validate({**sheet, **changes})
        return beat_sheet_issues(data, chapter_start=1, chapter_end=6, runtime=runtime)

    assert issues() == [
        "protagonist_ref: 'newcomer' is not a character node from the world seed",
        "protections[0].field is required for must_hold_state",
        "protections[1].until_chapter 7 is outside the arc 1-6",
        "protections[1].fact_ref: 'secret' is not a fact ref",
        "reveal_targets[0].target_state 'revealed' is not an allowed value",
    ]
    assert issues(protagonist_ref="hero", protections=[], reveal_targets=[]) == []
    # An arc shorter than the band minimum is one short band.
    short = {"phases": [{"name": "phase", "chapter_range": [1, 3], "purpose": "purpose"}],
             "band_breakdown": [{"band_index": 0, "chapter_start": 1, "chapter_end": 3}]}
    data = ArcBeatSheetData.model_validate({**sheet, **short, "protagonist_ref": "hero", "protections": [],
                                            "reveal_targets": []})
    assert beat_sheet_issues(data, chapter_start=1, chapter_end=3, runtime=runtime) == []


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
