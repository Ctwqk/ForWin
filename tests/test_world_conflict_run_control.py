from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from fastapi import HTTPException

from forwin.application.read_models.generation import _derive_blocking_reason
from forwin.api_schema import ChapterReviewApproveRequest
from forwin.config import InfrastructureConfig
from forwin.generation.pipeline_core.project_chapters import ChapterExecutionStage
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_types import ConflictDecision, ConflictDraft
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


def _conflict(session, *, blocks_from_chapter=3, required_decider="human"):
    return WorldConflictRepository(session).create(
        ConflictDraft(
            project_id="world-run-project",
            source="world_tick",
            detected_after_chapter=2,
            story_time=120,
            involved_refs=[],
            violated="impact_review",
            proposed_change={},
            impact_level="major",
            blocks_from_chapter=blocks_from_chapter,
            summary="A world change needs a human decision.",
        ),
        required_decider=required_decider,
        options=["veto_world"],
    )


@pytest.fixture
def runtime():
    database_url = postgres_test_url("world-conflict-run-control")
    engine = get_engine(database_url)
    init_db(engine)
    factory = get_session_factory(engine)
    with factory() as session:
        project = Project(
            id="world-run-project", title="Test", premise="Test", genre="test",
            creation_status="writing", target_total_chapters=4,
        )
        ProjectPolicyStore(session).initialize(project, RuntimePolicy.for_profile("standard"))
        session.flush()
        arc = ArcPlanVersion(
            id="world-run-arc", project_id=project.id, arc_synopsis="Test",
            status="active", chapter_start=1, chapter_end=4,
        )
        session.add(arc)
        session.flush()
        session.add_all([
            ChapterPlan(
                id=f"world-run-plan-{number}", project_id=project.id,
                arc_plan_id=arc.id, chapter_number=number, title=f"Chapter {number}",
                status="accepted" if number <= 2 else "planned",
            )
            for number in range(1, 5)
        ])
        session.commit()
    api = HttpRuntimeHarness(
        session_factory=factory, engine=engine,
        config=InfrastructureConfig(database_url=database_url),
    )
    try:
        yield api, factory
    finally:
        engine.dispose()


def _run_chapters(factory, monkeypatch, *, chapters, block_at):
    from forwin.generation.pipeline_core import project_chapters

    events = []
    written = []
    recovered = []
    with factory() as session:
        conflict = _conflict(session, blocks_from_chapter=block_at)
        conflict_id = conflict.id
        for number in chapters:
            session.get(ChapterPlan, f"world-run-plan-{number}").status = "planned"
        session.commit()
        repo = StateRepository(session)
        updater = StateUpdater(session)

        def write(**kwargs):
            written.append(kwargs["chapter_number"])
            return SimpleNamespace(title="Test", char_count=100)

        def commit(number):
            updater.mark_chapter_status("world-run-project", number, "accepted")
            session.commit()
            return SimpleNamespace(blocked=False, commit_id=f"commit-{number}")

        monkeypatch.setattr(project_chapters, "CandidateDraftRepository", lambda _session: SimpleNamespace(
            latest_for_chapter=lambda **_kwargs: SimpleNamespace(id="candidate"),
        ))
        monkeypatch.setattr(project_chapters, "handle_chapter_review_gate", lambda *_args, **_kwargs: SimpleNamespace(
            pause_required=False, gate_approved=False,
        ))
        monkeypatch.setattr(project_chapters, "save_accepted_trope_usage_for_chapter", lambda *_args, **_kwargs: None)
        policy = SimpleNamespace(canon=SimpleNamespace(hard_floor=False))
        stage = SimpleNamespace(
            policy=policy,
            _project_policy=lambda *_args: policy,
            _abort_requested=lambda: False,
            _pause_requested=lambda: False,
            _recover_post_canon_before_chapter=lambda **kwargs: recovered.append(kwargs["chapter_number"]),
            _ensure_chapter_plan=lambda **kwargs: repo.get_chapter_plan(kwargs["project_id"], kwargs["chapter_number"]),
            _strict_progression_block=lambda **_kwargs: ("", "", ""),
            _manual_boundary_checkpoint=lambda *_args, **_kwargs: None,
            _emit_progress=lambda event, **payload: events.append((event, deepcopy(payload))),
            retrieval_broker=SimpleNamespace(last_observability_summary={}, build_chapter_context=lambda *_args: SimpleNamespace()),
            _audit_current_plan_before_write=lambda **kwargs: kwargs["context"],
            _write_chapter_with_attention_fallback=write,
            repair=SimpleNamespace(review_candidate=lambda **kwargs: (kwargs["writer_output"], SimpleNamespace(verdict="pass", issues=[]), False)),
            repair_execution=SimpleNamespace(),
            _review_issue_payloads=lambda _verdict: [],
            _review_canon_risk=lambda _verdict: "low",
            canon_preparation=SimpleNamespace(prepare=lambda **kwargs: SimpleNamespace(blocked=False, plan=kwargs["chapter_number"])),
            canon_preparation_context=SimpleNamespace(),
            canon_admission=SimpleNamespace(commit_plan=commit),
            _make_state_helpers=lambda _session: (repo, updater, SimpleNamespace()),
            _run_phase3_pass=lambda **_kwargs: {"run_ids": {}},
            _run_post_canon_order_controls=lambda **_kwargs: {},
        )
        plan_lookup = Mock(wraps=repo.get_chapter_plan)
        monkeypatch.setattr(repo, "get_chapter_plan", plan_lookup)
        result = ChapterExecutionStage._run_project_chapters(
            stage, session=session, repo=repo, updater=updater, checker=SimpleNamespace(),
            project_id="world-run-project", chapter_numbers=chapters, requested_chapters=len(chapters),
        )
        statuses = [session.get(ChapterPlan, f"world-run-plan-{number}").status for number in chapters]
    return result, events, written, recovered, statuses, conflict_id, plan_lookup


def test_open_human_conflict_stops_run_before_blocked_chapter(runtime, monkeypatch):
    _api, factory = runtime
    result, events, written, recovered, statuses, conflict_id, lookup = _run_chapters(
        factory, monkeypatch, chapters=[2, 3, 4], block_at=3,
    )
    assert result.completed_chapters == [2]
    assert result.paused_chapters == [3]
    assert result.pause_reason == "world_conflict"
    assert result.status == "needs_review"
    assert written == [2]
    assert recovered == [2, 3]
    assert statuses == ["accepted", "planned", "planned"]
    assert [call.args[1] for call in lookup.call_args_list] == [2]
    paused = [payload for _event, payload in events if payload.get("stage") == "paused_for_review"]
    assert len(paused) == 1
    assert paused[0]["payload"] == {"reason": "world_conflict", "conflict_id": conflict_id}
    assert paused[0]["paused_chapters"] == [3]


def test_conflict_blocking_later_chapter_does_not_stop_earlier_ones(runtime, monkeypatch):
    _api, factory = runtime
    result, _events, written, recovered, statuses, _conflict_id, _lookup = _run_chapters(
        factory, monkeypatch, chapters=[2, 3], block_at=4,
    )
    assert result.status == "completed"
    assert result.completed_chapters == written == recovered == [2, 3]
    assert result.paused_chapters == []
    assert statuses == ["accepted", "accepted"]


def test_project_detail_reports_world_conflict_pending(runtime):
    from forwin.http.tasks import _get_task_center_service

    api, factory = runtime
    with factory() as session:
        conflict = _conflict(session)
        conflict_id = conflict.id
        session.commit()
    detail = api.get_project("world-run-project")
    assert detail.blocking_reason.code == "world_conflict_pending"
    assert detail.blocking_reason.chapter_number == 3
    assert conflict_id in detail.blocking_reason.detail
    assert detail.generation_control.blocking_reason == detail.blocking_reason
    assert detail.next_gate == "world_conflict_pending"
    summary = next(item for item in api.list_projects() if item.id == "world-run-project")
    assert summary.blocking_reason == detail.blocking_reason
    service = _get_task_center_service(api.runtime)
    item = service.get_project_backed_task_item_or_404("project-world-run-project")
    assert item.generation_control.blocking_reason == detail.blocking_reason
    listed = next(item for item in service.list_project_backed_task_items(10) if item.project_id == "world-run-project")
    assert listed.generation_control.blocking_reason == detail.blocking_reason


def test_continue_and_review_approve_return_409_while_conflict_open(runtime, monkeypatch):
    api, factory = runtime
    with factory() as session:
        _conflict(session)
        session.commit()
    create_task = Mock(side_effect=AssertionError("blocked projects cannot enqueue"))
    monkeypatch.setattr("forwin.http.app._create_continue_generation_task", create_task)
    with pytest.raises(HTTPException) as exc:
        api.continue_project_generation("world-run-project")
    assert exc.value.status_code == 409
    assert exc.value.detail == api.get_project("world-run-project").blocking_reason.message
    create_task.assert_not_called()
    accept_review = Mock(side_effect=AssertionError("blocked review cannot be accepted"))
    api._pipeline = SimpleNamespace(
        accept_review=accept_review,
        policy=RuntimePolicy.for_profile("standard"),
    )
    with pytest.raises(HTTPException) as review_exc:
        api.approve_chapter_review(
            "world-run-project", 3,
            ChapterReviewApproveRequest(continue_generation=True, reason="Reviewed the pending conflict."),
        )
    assert review_exc.value.status_code == 409
    assert review_exc.value.detail == exc.value.detail
    accept_review.assert_not_called()
    create_task.assert_not_called()


def test_resolved_conflict_lets_continue_proceed(runtime, monkeypatch):
    api, factory = runtime
    with factory() as session:
        conflict = _conflict(session)
        WorldConflictRepository(session).mark_resolved(
            conflict,
            decision=ConflictDecision(option="veto_world", decided_by="human", actor_id="reviewer", rationale="Keep the plan.", decided_at=datetime.now(UTC)),
            result_refs=[],
        )
        _conflict(session, required_decider="codex")
        _conflict(session, required_decider="auto")
        _conflict(session, blocks_from_chapter=5)
        session.commit()

    def create_task(_runtime, **kwargs):
        task = api._create_task_record(requested_chapters=kwargs["requested_chapters"])
        task["project_id"] = kwargs["project_id"]
        api._persist_generation_task("world-continue-task", task)
        return "world-continue-task"

    monkeypatch.setattr("forwin.http.app._create_continue_generation_task", create_task)
    assert api.get_project("world-run-project").blocking_reason.code == ""
    assert api.continue_project_generation("world-run-project").task_id == "world-continue-task"


@pytest.mark.parametrize("status", ["planned", "drafted", "needs_review", "failed"])
def test_world_conflict_reason_precedes_plan_and_checkpoint_reasons(status):
    from forwin.application.read_models.generation import WorldConflictBlockInfo

    plans = [
        SimpleNamespace(chapter_number=3, status=status),
        SimpleNamespace(chapter_number=2, status="accepted"),
    ]
    reason = _derive_blocking_reason(
        plans=plans,
        latest_band_checkpoint=SimpleNamespace(status="fail"),
        decision_events=[SimpleNamespace(
            id="event", chapter_number=3, event_type="hard_gate_hit",
            payload={"blocking_reason": "future_constraint_block"}, summary="Constraint",
        )],
        open_world_conflicts=[
            WorldConflictBlockInfo(conflict_id="later", blocks_from_chapter=4, summary="Later"),
            WorldConflictBlockInfo(conflict_id="blocking", blocks_from_chapter=3, summary="Human decision"),
        ],
    )
    assert reason.code == "world_conflict_pending"
    assert reason.chapter_number == 3
    assert "blocking" in reason.detail
    assert "Human decision" in reason.detail


@pytest.mark.parametrize("plans", [[], [SimpleNamespace(chapter_number=3, status="accepted")], [SimpleNamespace(chapter_number=2, status="planned")]])
def test_world_conflict_reason_only_blocks_next_unwritten_chapter(plans):
    from forwin.application.read_models.generation import WorldConflictBlockInfo

    kwargs = dict(plans=plans, latest_band_checkpoint=None)
    assert _derive_blocking_reason(
        **kwargs, open_world_conflicts=[WorldConflictBlockInfo(conflict_id="later", blocks_from_chapter=3, summary="Later")],
    ) == _derive_blocking_reason(**kwargs)


def test_world_conflict_run_check_follows_recovery_and_never_loads_plan(monkeypatch):
    calls = []
    conflict = SimpleNamespace(id="conflict")
    session = Mock()

    def open_blocking(_repository, project_id, *, next_chapter):
        assert project_id == "project"
        assert next_chapter == 3
        calls.append("conflict_check")
        return conflict

    monkeypatch.setattr(WorldConflictRepository, "open_blocking", open_blocking)
    repo = Mock()
    stage = SimpleNamespace(
        _project_policy=lambda *_args: SimpleNamespace(),
        _abort_requested=lambda: False,
        _pause_requested=lambda: False,
        _recover_post_canon_before_chapter=lambda **_kwargs: calls.append("recovery"),
        _emit_progress=lambda _event, **payload: calls.append(deepcopy(payload)),
    )
    result = ChapterExecutionStage._run_project_chapters(
        stage, session=session, repo=repo, updater=Mock(), checker=Mock(),
        project_id="project", chapter_numbers=[3, 4], requested_chapters=2,
    )
    assert calls[:2] == ["recovery", "conflict_check"]
    assert calls[2]["stage"] == "paused_for_review"
    assert calls[2]["payload"] == {"reason": "world_conflict", "conflict_id": "conflict"}
    assert calls[2]["paused_chapters"] == [3]
    assert result.status == "needs_review"
    assert result.paused_chapters == [3]
    assert result.pause_reason == "world_conflict"
    assert result.completed_chapters == result.failed_chapters == []
    repo.get_chapter_plan.assert_not_called()


def test_world_conflict_read_model_loads_only_open_human_rows(monkeypatch):
    from forwin.application.read_models import generation

    repository = Mock()
    repository.list.return_value = [
        SimpleNamespace(id=decider, blocks_from_chapter=3, summary="Conflict", required_decider=decider, created_at=datetime(2026, 1, 1))
        for decider in ("human", "codex", "auto")
    ]
    monkeypatch.setattr(generation, "WorldConflictRepository", lambda _session: repository)
    conflicts = generation.load_open_world_conflicts(Mock(), "project")
    repository.list.assert_called_once_with("project", status="open")
    assert [item.conflict_id for item in conflicts] == ["human"]
    assert conflicts[0].created_at == datetime(2026, 1, 1)


def test_world_conflict_reason_blocks_next_arc_before_plans_exist():
    from forwin.application.read_models.generation import WorldConflictBlockInfo, build_generation_control

    control = build_generation_control(
        plans=[SimpleNamespace(chapter_number=n, status="accepted") for n in range(1, 5)],
        writable_chapters=range(1, 9),
        open_world_conflicts=[WorldConflictBlockInfo(conflict_id="next-arc", blocks_from_chapter=5, summary="Conflict")],
    )
    assert control.next_chapter == 5
    assert control.blocking_reason.code == "world_conflict_pending"
    assert control.blocking_reason.chapter_number == 5
    assert control.can_resume is False
    assert control.next_gate == "world_conflict_pending"


def test_world_conflict_reason_selects_oldest_at_lowest_blocking_chapter():
    from forwin.application.read_models.generation import WorldConflictBlockInfo

    reason = _derive_blocking_reason(
        plans=[SimpleNamespace(chapter_number=5, status="planned")],
        latest_band_checkpoint=None,
        open_world_conflicts=[
            WorldConflictBlockInfo(conflict_id="newer", blocks_from_chapter=3, summary="Newer", created_at=datetime(2026, 1, 2)),
            WorldConflictBlockInfo(conflict_id="later", blocks_from_chapter=4, summary="Later", created_at=datetime(2025, 1, 1)),
            WorldConflictBlockInfo(conflict_id="oldest", blocks_from_chapter=3, summary="Oldest", created_at=datetime(2026, 1, 1)),
        ],
    )
    assert reason.detail == "conflict_id=oldest: Oldest"


@pytest.mark.parametrize("prefix", ["", "继续执行后"])
def test_world_conflict_pause_message_explains_resolution(prefix):
    from forwin.application.generation_execution import _paused_chapters_message
    from forwin.generation.pipeline_core.result import RunResult

    message = _paused_chapters_message(
        RunResult(project_id="project", requested_chapters=2, paused_chapters=[3], pause_reason="world_conflict"),
        prefix=prefix,
    )
    assert message.startswith(prefix)
    assert "3" in message
    assert "世界冲突" in message
    assert "处理" in message
    assert "质量门" not in message


def test_next_arc_conflict_blocks_read_models_and_continue_route(runtime, monkeypatch):
    from forwin.http.tasks import _get_task_center_service

    api, factory = runtime
    with factory() as session:
        for n in range(1, 5):
            session.get(ChapterPlan, f"world-run-plan-{n}").status = "accepted"
        # As extend-generation leaves it: the book's target reaches the new arc's end.
        session.get(Project, "world-run-project").target_total_chapters = 8
        session.add(ArcPlanVersion(
            id="world-run-next-arc", project_id="world-run-project", arc_number=2,
            arc_synopsis="Next", status="planned", chapter_start=5, chapter_end=8,
        ))
        conflict = _conflict(session, blocks_from_chapter=5)
        conflict_id = conflict.id
        session.commit()
    detail = api.get_project("world-run-project")
    summary = next(item for item in api.list_projects() if item.id == "world-run-project")
    service = _get_task_center_service(api.runtime)
    item = service.get_project_backed_task_item_or_404("project-world-run-project")
    listed = next(item for item in service.list_project_backed_task_items(10) if item.project_id == "world-run-project")
    for control in (detail.generation_control, summary.generation_control, item.generation_control, listed.generation_control):
        assert control.next_chapter == 5
        assert control.blocking_reason.code == "world_conflict_pending"
        assert control.blocking_reason.chapter_number == 5
        assert conflict_id in control.blocking_reason.detail
        assert control.can_resume is False
    create_task = Mock(side_effect=AssertionError("blocked next arc cannot enqueue"))
    monkeypatch.setattr("forwin.http.app._create_continue_generation_task", create_task)
    with pytest.raises(HTTPException) as exc:
        api.continue_project_generation("world-run-project")
    assert exc.value.status_code == 409
    assert exc.value.detail == detail.blocking_reason.message
    create_task.assert_not_called()


def test_batched_world_conflicts_filter_projects_and_match_run_gate(runtime):
    from sqlalchemy import event
    from forwin.application.read_models.generation import load_open_world_conflicts_by_project

    _api, factory = runtime
    with factory() as session:
        second = Project(id="second-project", title="Second", premise="Test", genre="test")
        excluded = Project(id="excluded-project", title="Excluded", premise="Test", genre="test")
        session.add_all([second, excluded])
        session.flush()
        oldest = _conflict(session)
        oldest.created_at = datetime(2026, 1, 1)
        newer = _conflict(session)
        newer.created_at = oldest.created_at + timedelta(days=1)
        _conflict(session, required_decider="codex")
        _conflict(session, required_decider="auto")
        resolved = _conflict(session)
        resolved.status = "resolved"
        other = _conflict(session)
        other.project_id = second.id
        outside = _conflict(session)
        outside.project_id = excluded.id
        session.commit()
        expected_id = WorldConflictRepository(session).open_blocking("world-run-project", next_chapter=3).id
        statements = []

        def record_query(_conn, _cursor, statement, _parameters, _context, _executemany):
            statements.append(statement)

        engine = session.get_bind()
        event.listen(engine, "before_cursor_execute", record_query)
        try:
            assert load_open_world_conflicts_by_project(session, []) == {}
            assert statements == []
            grouped = load_open_world_conflicts_by_project(session, ["world-run-project", "second-project"])
        finally:
            event.remove(engine, "before_cursor_execute", record_query)
        assert len(statements) == 1
        assert "proposed_change_json" not in statements[0]
        assert "decision_json" not in statements[0]
        assert set(grouped) == {"world-run-project", "second-project"}
        assert [row.conflict_id for row in grouped["world-run-project"]] == [oldest.id, newer.id]
        assert [row.conflict_id for row in grouped["second-project"]] == [other.id]
        reason = _derive_blocking_reason(
            plans=[SimpleNamespace(chapter_number=3, status="planned")],
            latest_band_checkpoint=None,
            open_world_conflicts=grouped["world-run-project"],
        )
        assert reason.detail.startswith(f"conflict_id={expected_id}:")
