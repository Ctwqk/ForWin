"""Regressions for the final P4b review's runtime wiring and rewrite paths."""

import json
from types import SimpleNamespace

import pytest
from sqlalchemy import event, select

from forwin.audit.events import DecisionEventType
from forwin.models.audit import DecisionEvent
from forwin.models.project import ChapterPlan, Project
from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.conflict_repository import WorldConflictRepository
from tests.test_entity_admission import _tick_entity_session_factory
from tests.test_world_conflict_repository import _decision, _draft


@pytest.fixture(params=["sqlite", "postgresql"])
def factory(request):
    engine, factory = _tick_entity_session_factory(request.param)
    yield factory
    engine.dispose()


def test_route_factory_amends_chapter_milestone_with_runtime_wrapper(factory, monkeypatch):
    from forwin.api_schema.world_conflicts import WorldConflictResolveRequest
    from forwin.http.adapters.api_world_conflict_routes import build_handlers
    from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
    from forwin.runtime.container import RuntimeContainer
    from tests.test_arc_beat_sheet import StubLLM, _sheet
    from tests.test_world_conflict_resolution import _amender, _band_world, _chapter_conflict, _chapter_violation

    world, arc_id, _ = _band_world(factory)
    conflict_id = _chapter_conflict(factory, world.project_id, _chapter_violation(
        protection_node_id="", milestone_ref=world.hero, kind="milestone", ref=world.hero,
        field="state.alive_status", value="dead", impact_level="critical",
    ), chapter=3)
    revised = _sheet(world)
    revised["milestones"][0]["description"] = "revised milestone"
    amender = _amender(StubLLM(), StubLLM(revised))
    policies = []

    def build_amender(self, *, policy=None):
        policies.append(policy)
        return amender

    monkeypatch.setattr(RuntimeContainer, "build_plan_amender", build_amender)
    container = RuntimeContainer(infrastructure=None, policy=RuntimePolicy.for_profile("standard"), role="api")
    handlers = build_handlers(get_session=factory, get_plan_amender=container.build_project_plan_amender)
    result = handlers["resolve_project_world_conflict"](
        world.project_id, conflict_id,
        WorldConflictResolveRequest(option="amend_plan", rationale="revise it", reason="keep the draft"),
    )
    assert result.status == "resolved"
    with factory() as session:
        sheet = ArcBeatSheetRepository(session).active_for_arc(arc_id)
        assert sheet.version == 2
        assert result.result_refs == [f"beat_sheet:{sheet.id}"]
        assert session.scalar(select(ChapterPlan.status).where(
            ChapterPlan.project_id == world.project_id, ChapterPlan.chapter_number == 3,
        )) == "planned"
        assert policies[0] == RuntimePolicy.model_validate_json(session.get(Project, world.project_id).runtime_policy_json)


@pytest.mark.parametrize("tick_id", ["retired-tick", "replacement-tick"])
@pytest.mark.parametrize("node_status", ["absent", "inactive", "retired", "active"])
def test_retick_reuses_its_retired_actor_name(factory, tick_id, node_status):
    from forwin.models.entity import Entity
    from forwin.protocol.book_state import WorldNode
    from forwin.world_engine.delta_builder import WorldTickDeltaBuilder, _tick_local_id
    from tests.test_world_adjudicator import CALENDAR, CONTEXT, LEXICON, _runtime
    from tests.test_world_engine_contracts import _new_actor
    from tests.test_world_tick_delta import _candidate

    candidate = _candidate("arrival", effects={"new_actors": [_new_actor()]})
    actor = candidate.effects.new_actors[0]
    node_id = _tick_local_id("p", "retired-tick", "arrival", "character", actor.key)
    runtime = _runtime()
    if node_status != "absent":
        runtime.world.add_node(WorldNode(id=node_id, project_id="p", node_type="character",
                                        name=actor.name, status=node_status))
    with factory.begin() as session:
        session.add(Project(id="p", title="Test", premise="Test", genre="test"))
        session.flush()
        session.add(Entity(id=node_id, project_id="p", name=actor.name, kind="character", is_active=False))
    with factory() as session:
        # The replacement tick is allowed to reuse only identities created by its retired predecessors.
        builder = WorldTickDeltaBuilder(session, retired_tick_ids=("retired-tick",))
        built = builder.build(context=CONTEXT, candidates=[candidate], news_in_transit=[],
                              tick_id=tick_id, runtime=runtime)
        if node_status == "active":
            assert built.delta is None
            assert [issue.code for issue in built.issues] == ["new_actor_name_taken"]
            return
        from forwin.world_engine.tick import _Tick
        check = _Tick._running_check(SimpleNamespace(
            session=session, context=CONTEXT, runtime=runtime, lexicon=LEXICON, calendar=CALENDAR,
            tick_id=tick_id, policy=RuntimePolicy.for_profile("standard"), onstage_refs=frozenset(),
            chapter=None, retired_tick_ids=("retired-tick",),
        ))
        assert check.issues(candidate) == []
    assert built.delta is not None, built.issues
    assert [patch.node_id for patch in built.delta.node_patches if patch.op == "create"] == [node_id]


def test_replacement_tick_reactivates_retired_entity_through_canon(factory):
    from forwin.book_state.repository import BookStateRepository
    from forwin.models.canon import CanonCommitRecord
    from forwin.models.entity import Entity
    from forwin.models.maintenance import PostCanonMaintenanceRun
    from forwin.protocol.book_state import MapNode
    from forwin.world_engine.delta_builder import WorldTickDeltaBuilder
    from forwin.world_engine.types import TickContext
    from tests.test_historical_rewrite_world_ticks import _book, _chapter_delta, _commit_tick, _create, _rewrite_chapter
    from tests.test_world_engine_contracts import _new_actor
    from tests.test_world_tick_delta import _candidate

    book = _book(factory)
    context = TickContext(project_id=book.project_id, chapter_number=3, now=120, window_start=60, seed=1)
    candidate = _candidate("arrival", effects={"new_actors": [_new_actor()]})
    with factory.begin() as session:
        BookStateRepository(session).create_map_node(MapNode(
            id="site", project_id=book.project_id, node_type="site", name="Site",
        ))
        initial = WorldTickDeltaBuilder(session).build(
            context=context, candidates=[candidate], news_in_transit=[], tick_id="tick-actor",
        )
    assert initial.delta is not None, initial.issues
    _commit_tick(factory, initial.delta)
    with factory.begin() as session:
        run = session.scalar(select(PostCanonMaintenanceRun).where(
            PostCanonMaintenanceRun.canon_commit_id == book.commit_ids[3], PostCanonMaintenanceRun.step_name == "world",
        ))
        run.result_json = json.dumps({"graph_delta_ids": [book.tick_3, initial.delta.id]})
    node_id = initial.delta.node_patches[0].node_id
    outcome = _rewrite_chapter(book, 3, version=2, delta=_chapter_delta(
        book.project_id, 3, "delta-ch3-v2", _create(book.project_id, "event-later"),
    ))
    assert not outcome.blocked, outcome.failure_reason
    with factory() as session:
        assert not session.get(Entity, node_id).is_active
        retired = json.loads(session.get(CanonCommitRecord, outcome.commit_id).result_json)["historical_rewrite"]["retired_tick_ids"]
        replacement = WorldTickDeltaBuilder(session, retired_tick_ids=retired).build(
            context=context, candidates=[candidate], news_in_transit=[], tick_id="replacement-tick",
        )
    assert replacement.delta is not None, replacement.issues
    assert not _commit_tick(factory, replacement.delta).idempotent
    with factory() as session:
        [entity] = session.scalars(select(Entity).where(Entity.name == "New Actor")).all()
        assert entity.id == node_id and entity.is_active


@pytest.mark.parametrize("world_enabled", [True, False])
def test_earlier_rewrite_expires_later_reviewed_chapter_conflicts(factory, world_enabled):
    from forwin.models.project import ArcPlanVersion
    from forwin.world_engine.tick import WorldTickService
    from forwin.book_state.repository import BookStateRepository

    with factory.begin() as session:
        session.add(Project(id="project-1", title="Test", premise="Test", genre="test"))
        session.flush()
        session.add(ArcPlanVersion(id="arc", project_id="project-1", arc_synopsis="Test",
                                   chapter_start=1, chapter_end=8))
        session.flush()
        repo = WorldConflictRepository(session)
        ids = {}
        for number, status in [(1, "needs_review"), (2, "accepted"), (3, "drafted"), (4, "needs_review"), (5, "planned")]:
            session.add(ChapterPlan(project_id="project-1", arc_plan_id="arc", chapter_number=number,
                                    title=f"Chapter {number}", status=status))
            ids[number] = repo.create(_draft(source="chapter_extraction", blocks_from_chapter=number),
                                      required_decider="human", options=["rewrite_chapter"]).id
    locks = []
    with factory() as session:
        @event.listens_for(session, "do_orm_execute")
        def record_locks(execution):
            if getattr(execution.statement, "_for_update_arg", None) is not None:
                locks.append(str(execution.statement))

        from unittest.mock import patch
        with patch.object(BookStateRepository, "latest_canon_chapter", return_value=2):
            WorldTickService.__new__(WorldTickService).run(session, SimpleNamespace(
                id="rewrite", project_id="project-1", chapter_number=1,
                result_json=json.dumps({"historical_rewrite": {"retired_tick_ids": []}}),
            ), policy=RuntimePolicy.for_profile("standard").model_copy(update={
                "world": RuntimePolicy.for_profile("standard").world.model_copy(update={"enabled": world_enabled}),
            }))
        session.commit()
        rows = WorldConflictRepository(session).list("project-1")
        assert {row.blocks_from_chapter for row in rows if row.status == "expired"} == {3, 4}
        events = session.scalars(select(DecisionEvent).where(
            DecisionEvent.event_type == DecisionEventType.WORLD_CONFLICT_EXPIRED,
        )).all()
        assert {row.related_object_id for row in events} == {ids[3], ids[4]}
        assert all("rewrite of chapter 1" in row.reason for row in events)
        assert locks and "world_plan_conflicts" in locks[0]


@pytest.mark.parametrize("missing", ["candidate", "draft"])
def test_missing_resume_draft_consumes_decisions_with_reason(factory, monkeypatch, caplog, missing):
    from forwin.generation.pipeline_core import chapter_conflicts as module
    from forwin.world_engine.conflict_repository import conflict_result_refs

    with factory.begin() as session:
        session.add(Project(id="project-1", title="Test", premise="Test", genre="test"))
        session.flush()
        repo = WorldConflictRepository(session)
        row = repo.create(_draft(source="chapter_extraction"), required_decider="human", options=["rewrite_chapter"])
        repo.mark_resolved(row, decision=_decision("rewrite_chapter"), result_refs=[])
        row_id = row.id
    candidate = None if missing == "candidate" else SimpleNamespace(candidate_draft_id="deleted-draft")
    monkeypatch.setattr(module, "CandidateDraftRepository", lambda session: SimpleNamespace(
        latest_for_chapter=lambda **kwargs: candidate,
    ))
    with factory() as session:
        conflicts = module.ChapterConflicts(None, session, None, None, None, RuntimePolicy.for_profile("standard"),
                                            SimpleNamespace(project_id="project-1", chapter_number=4), None)
        assert conflicts.resume() is None
        repo = WorldConflictRepository(session)
        assert repo.pending_chapter_resumes("project-1", chapter_number=4) == []
        row = repo.get("project-1", row_id)
        assert row.status == "resolved" and row.decision_option == "rewrite_chapter"
        assert "candidate:missing-draft" in conflict_result_refs(row)
        assert row_id in caplog.text and "missing" in caplog.text
