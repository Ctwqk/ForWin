from __future__ import annotations

import json

import pytest
from sqlalchemy import select

from forwin.canon.admission import CanonStaleVersion
from forwin.maintenance.post_canon import PostCanonMaintenanceError
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import GraphDeltaRow
from forwin.models.entity import Entity
from forwin.models.genesis import PromptTrace
from forwin.models.maintenance import PostCanonMaintenanceRun
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import NodePatch
from forwin.world_engine.drift import WorldDriftRunner
from tests.test_entity_admission import _tick_entity_session_factory
from tests.test_plan_step import _maintenance
from tests.test_world_drift import variable
from tests.test_world_tick_service import (
    OPENING, _ambushing, _commit, _delta, _disrupt_the_later_intent, _later, _outcomes,
    _seed_tick_world, _service, _time,
)


@pytest.fixture(params=["sqlite", "postgresql"])
def db(request):
    engine, factory = _tick_entity_session_factory(request.param)
    yield engine, factory
    engine.dispose()


def _setup(factory, monkeypatch):
    world = _seed_tick_world(factory, monkeypatch)
    node = variable().model_copy(update={"project_id": world.project_id})
    node.profile.update(scope_refs=[world.loc(1)], holder_refs=[world.hero])
    node.state["last_drift_at"] = OPENING
    chapter = _delta(factory, world, 1, _time(8, "日"))
    chapter = chapter.model_copy(update={"node_patches": [*chapter.node_patches, NodePatch(
        node_id=node.id, node_type="world_variable", op="create",
        new_value=node.model_dump(mode="json", exclude={"id", "node_type"}),
    )]})
    _commit(factory, world, chapter)
    with factory.begin() as session:
        arc = session.scalar(select(ArcPlanVersion).where(ArcPlanVersion.project_id == world.project_id))
        plan = ChapterPlan(project_id=world.project_id, arc_plan_id=arc.id, chapter_number=1,
                           title="chapter", status="accepted")
        session.add(plan)
        session.flush()
        plan_id = plan.id
    llm = _ambushing(world, chapter, "veto_world")
    llm.handlers["drift"] = lambda *_: {"changes": [{
        "variable_id": "condition", "new_level": 2, "trend": "rising", "summary": "drift-summary",
    }]}
    maintenance, commit_id = _maintenance(factory, world, plan_id, 1, llm, None)
    maintenance.world_tick = _service(factory, llm)
    maintenance.world_tick.drift_runner = WorldDriftRunner(llm)
    maintenance._step_runners["world"] = maintenance._run_world_step
    # Expiring sessions catch accidental ORM reuse across the read and commit units.
    factory.configure(expire_on_commit=True)
    return world, chapter, llm, maintenance, commit_id


def _world_row(session, commit_id):
    return session.scalar(select(PostCanonMaintenanceRun).where(
        PostCanonMaintenanceRun.canon_commit_id == commit_id,
        PostCanonMaintenanceRun.step_name == "world",
    ))


@pytest.mark.parametrize("expire_on_commit", [True, False])
def test_world_llm_has_no_transaction_or_project_lock(db, monkeypatch, expire_on_commit):
    engine, factory = db
    world, chapter, llm, maintenance, commit_id = _setup(factory, monkeypatch)
    factory.configure(expire_on_commit=expire_on_commit)
    active = []
    runner = maintenance._run_world_step

    def run(session, commit):
        active.append(session)
        return runner(session, commit)

    maintenance._step_runners["world"] = run
    chat = llm.chat
    violations = []

    def checked_chat(*args, **kwargs):
        try:
            assert not active[0].in_transaction()
            with factory.begin() as other:
                assert other.scalar(select(Project.id).where(Project.id == world.project_id)
                                    .with_for_update(nowait=True)) == world.project_id
                assert not list(other.scalars(select(WorldPlanConflictRow)))
        except Exception as exc:
            violations.append(exc)
            raise
        return chat(*args, **kwargs)

    llm.chat = checked_chat
    maintenance.run(canon_commit_id=commit_id, worker_id="test")
    assert not violations
    assert {"world.adjudicate", "world.actor_turn", "world.drift", "review.world_conflict"} <= set(llm.stages())
    with factory() as session:
        row = _world_row(session, commit_id)
        assert row.status == "succeeded"
        assert json.loads(row.result_json)["graph_delta_ids"]


@pytest.mark.parametrize("expire_on_commit", [True, False])
def test_world_conflict_options_refresh_project_after_adjudication(db, monkeypatch, expire_on_commit):
    _, factory = db
    world, _, llm, maintenance, commit_id = _setup(factory, monkeypatch)
    factory.configure(expire_on_commit=expire_on_commit)
    runner = maintenance._run_world_step
    active = []

    def run(session, commit):
        active.append(session)
        return runner(session, commit)

    maintenance._step_runners["world"] = run
    adjudicate = llm.handlers["adjudicate"]

    def update_total(*args):
        assert not active[0].in_transaction()
        with factory.begin() as other:
            other.get(Project, world.project_id).target_total_chapters = 1
        return adjudicate(*args)

    llm.handlers["adjudicate"] = update_total
    delegate = maintenance.world_tick.conflict_delegate.decide
    offered = []

    def inspect_options(**kwargs):
        assert not active[0].in_transaction()
        with factory() as other:
            assert other.get(Project, world.project_id).target_total_chapters == 1
        offered.append(json.loads(kwargs["conflict"].options_json))
        return delegate(**kwargs)

    monkeypatch.setattr(maintenance.world_tick.conflict_delegate, "decide", inspect_options)
    maintenance.run(canon_commit_id=commit_id, worker_id="test")
    assert offered
    assert all("plot_responds" not in options for options in offered)


def test_chapter_ticks_follow_sequence_when_timestamps_and_ids_disagree(db):
    from datetime import datetime, timedelta
    from forwin.world_engine.tick import _chapter_ticks

    _, factory = db
    with factory.begin() as session:
        session.add(Project(id="tick-order", title="Order", genre="test", premise="test"))
        session.flush()
        stamp = datetime(2026, 10, 22)
        for delta_id, tick_id, created_at in [
            ("z-first", "tick-first", stamp),
            ("a-second", "tick-second", stamp - timedelta(seconds=1)),
        ]:
            session.add(GraphDeltaRow(
                id=delta_id, project_id="tick-order", chapter_number=1, source_type="world_engine",
                created_at=created_at, metadata_json=json.dumps({"tick_id": tick_id}),
            ))
            session.flush()
        assert _chapter_ticks(session, "tick-order", 1) == [
            ("z-first", "tick-first"), ("a-second", "tick-second"),
        ]


@pytest.mark.parametrize("changed", ["project", "plan"])
def test_world_final_unit_refreshes_side_effect_inputs(db, monkeypatch, changed):
    from forwin.world_engine import tick_conflicts

    _, factory = db
    world, _, llm, maintenance, commit_id = _setup(factory, monkeypatch)
    factory.configure(expire_on_commit=False)
    with factory.begin() as session:
        arc = session.scalar(select(ArcPlanVersion).where(ArcPlanVersion.project_id == world.project_id))
        session.add(ChapterPlan(id="next-plan", project_id=world.project_id, arc_plan_id=arc.id,
                                chapter_number=2, title="Next", status="planned"))
    retained = []
    snapshot = tick_conflicts.build_conflict_context_snapshot

    def retain_inputs(session, **kwargs):
        retained.extend([session.get(Project, world.project_id), session.get(ChapterPlan, "next-plan")])
        return snapshot(session, **kwargs)

    monkeypatch.setattr(tick_conflicts, "build_conflict_context_snapshot", retain_inputs)
    binding = {"kind": "intent", "ref": "concurrent-intent", "how": "concurrent binding"}

    def decide(*_args):
        with factory.begin() as other:
            if changed == "project":
                other.get(Project, world.project_id).target_total_chapters = 1
            else:
                other.get(ChapterPlan, "next-plan").world_bindings_json = json.dumps([binding])
        return {"option": "plot_responds", "rationale": "Respond", "risk_level": "low", "evidence": []}

    llm.handlers["review"] = decide
    maintenance.run(canon_commit_id=commit_id, worker_id="test")
    assert retained
    with factory() as session:
        conflict = session.scalars(select(WorldPlanConflictRow)).one()
        bindings = json.loads(session.get(ChapterPlan, "next-plan").world_bindings_json)
        if changed == "project":
            assert conflict.status == "open" and conflict.required_decider == "human"
            assert not bindings
        else:
            assert conflict.status == "resolved"
            assert binding in bindings
            assert any(item["ref"] == conflict.intent_ref for item in bindings)


@pytest.mark.parametrize("failure", ["stale", "llm_phase", "completion"])
def test_world_failure_rolls_back_and_claim_retries(db, monkeypatch, failure):
    _, factory = db
    world, chapter, llm, maintenance, commit_id = _setup(factory, monkeypatch)
    target = maintenance if failure == "completion" else maintenance.world_tick.conflict_delegate
    method = "_enqueue_step_trace" if failure == "completion" else "decide"
    original = getattr(target, method)

    def fail(**kwargs):
        if failure == "stale":
            result = original(**kwargs)
            # This is the last model phase: the commit freshness check must catch the write.
            _disrupt_the_later_intent(factory, world, chapter)
            return result
        raise RuntimeError("injected step failure")

    monkeypatch.setattr(target, method, fail)
    with pytest.raises(PostCanonMaintenanceError) as caught:
        maintenance.run(canon_commit_id=commit_id, worker_id="test")
    assert isinstance(caught.value.__cause__, CanonStaleVersion if failure == "stale" else RuntimeError)
    with factory() as session:
        row = _world_row(session, commit_id)
        assert row.status == "failed" and row.attempts == 1
        assert json.loads(row.result_json or "{}") == {}
        assert not list(session.scalars(select(WorldPlanConflictRow)))
        assert not list(session.scalars(select(GraphDeltaRow).where(
            GraphDeltaRow.source_type == "world_engine", GraphDeltaRow.source_id != "conflict-race",
        )))
        assert not list(session.scalars(select(DecisionEvent).where(DecisionEvent.event_family == "business_event",
            DecisionEvent.related_object_type.in_(["world_tick", "world_conflict"]),
            DecisionEvent.related_object_id != "conflict-race")))
    monkeypatch.setattr(target, method, original)
    maintenance.run(canon_commit_id=commit_id, worker_id="retry")
    with factory() as session:
        row = _world_row(session, commit_id)
        assert row.status == "succeeded" and row.attempts == 2
        assert json.loads(row.result_json)["graph_delta_ids"]
        assert len(list(session.scalars(select(PromptTrace)))) == len(llm.calls)


def test_world_step_admits_new_actor_entity(db, monkeypatch):
    from tests.test_world_engine_contracts import _new_actor

    _, factory = db
    world, chapter, llm, maintenance, commit_id = _setup(factory, monkeypatch)
    llm.handlers["adjudicate"] = _outcomes({
        "intent_id": _later(world), "result": "resolved", "summary": "Arrival",
        "effects": {"new_actors": [_new_actor(name="Rowan", location_id=world.loc(1),
                                             affiliation_refs=[world.faction])]},
    })
    maintenance.run(canon_commit_id=commit_id, worker_id="test")
    with factory() as session:
        assert _world_row(session, commit_id).status == "succeeded"
        assert session.scalar(select(Entity).where(Entity.project_id == world.project_id,
                                                   Entity.name == "Rowan")) is not None
