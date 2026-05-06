from __future__ import annotations

import json
from types import SimpleNamespace

from sqlalchemy import select, text

from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.observability import PerformanceSpan
from forwin.models.project import Project
from forwin.observability import OperationContext
from forwin.observability.ports import NullObservability
from forwin.observability.query_service import ObservabilityQueryService
from forwin.observability.service import ObservabilityService
from forwin.observability.sqlalchemy_probe import install_sqlalchemy_query_probe
from forwin.orchestrator.loop import WritingOrchestrator


def _seed_project(session, project_id: str) -> None:
    session.add(
        Project(
            id=project_id,
            title="Phase F Observability",
            premise="premise",
            genre="玄幻",
        )
    )
    session.commit()


def test_observability_service_records_nested_spans_and_errors() -> None:
    engine = get_engine(postgres_test_url("phase-f-spans"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            _seed_project(session, project_id)

        obs = ObservabilityService(
            session_factory=Session,
            artifact_store=None,
            config=Config(database_url=postgres_test_url("phase-f-spans"), minimax_api_key=""),
        )
        ctx = OperationContext(
            project_id=project_id,
            task_id="task-phase-f",
            chapter_number=2,
            stage="writing_chapter",
            operation_id="op-phase-f",
        )

        with obs.span(ctx, "task.operation", span_kind="task", component="api") as root:
            root.metric("completed_chapters", 1)
            with obs.span(ctx, "writer.write_chapter", span_kind="writer", component="writer") as writer:
                writer.tag("mode", "scene")
                writer.metric("char_count", 1200)
            try:
                with obs.span(ctx, "review.hub", span_kind="reviewer", component="reviewer"):
                    raise RuntimeError("review failed")
            except RuntimeError:
                pass

        with Session() as session:
            rows = session.execute(
                select(PerformanceSpan).where(PerformanceSpan.project_id == project_id)
            ).scalars().all()

        by_name = {row.span_name: row for row in rows}
        assert set(by_name) == {"task.operation", "writer.write_chapter", "review.hub"}
        assert by_name["task.operation"].parent_span_id == ""
        assert by_name["writer.write_chapter"].parent_span_id == by_name["task.operation"].span_id
        assert by_name["review.hub"].status == "failed"
        assert json.loads(by_name["review.hub"].error_json)["error_class"] == "RuntimeError"
        assert json.loads(by_name["writer.write_chapter"].tags_json)["mode"] == "scene"
        assert json.loads(by_name["writer.write_chapter"].metrics_json)["char_count"] == 1200
    finally:
        engine.dispose()


def test_null_observability_is_noop() -> None:
    engine = get_engine(postgres_test_url("phase-f-null"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            _seed_project(session, project_id)

        ctx = OperationContext(project_id=project_id, task_id="task-null")
        obs = NullObservability()
        with obs.span(ctx, "task.operation") as span:
            span.metric("ignored", 1)
        obs.event(ctx, event_family="runtime_observation", event_type="ignored", summary="ignored")
        obs.flush()

        with Session() as session:
            count = session.execute(
                select(PerformanceSpan).where(PerformanceSpan.project_id == project_id)
            ).scalars().all()
        assert count == []
    finally:
        engine.dispose()


def test_query_service_builds_task_critical_path_and_breakdowns() -> None:
    engine = get_engine(postgres_test_url("phase-f-query"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            _seed_project(session, project_id)

        obs = ObservabilityService(
            session_factory=Session,
            artifact_store=None,
            config=Config(database_url=postgres_test_url("phase-f-query"), minimax_api_key=""),
        )
        ctx = OperationContext(project_id=project_id, task_id="task-query", operation_id="task-query")
        with obs.span(ctx, "task.operation", span_kind="task", component="api"):
            with obs.span(ctx, "context.provider.map", span_kind="context", component="context"):
                pass
            with obs.span(ctx, "llm.request", span_kind="llm", component="writer") as span:
                span.tag("stage_key", "chapter_draft")
                span.tag("model", "fake-model")
                span.metric("input_chars", 100)
                span.metric("output_chars", 200)

        report = ObservabilityQueryService(
            session_factory=Session,
            display_datetime=lambda value: value.isoformat() if value else "",
        ).task_performance_report("task-query")

        assert report.task_id == "task-query"
        assert report.project_id == project_id
        assert report.critical_path
        assert report.critical_path[0].span_name == "task.operation"
        assert {item.key for item in report.component_breakdown} >= {"api", "context", "writer"}
        assert report.llm_breakdown[0].key == "chapter_draft:fake-model"
    finally:
        engine.dispose()


def test_prompt_trace_llm_spans_attach_to_active_stage_span() -> None:
    engine = get_engine(postgres_test_url("phase-f-prompt-parent"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            _seed_project(session, project_id)

        obs = ObservabilityService(
            session_factory=Session,
            artifact_store=None,
            config=Config(database_url=postgres_test_url("phase-f-prompt-parent"), minimax_api_key=""),
        )
        orchestrator = WritingOrchestrator.__new__(WritingOrchestrator)
        orchestrator.observability = obs
        orchestrator._governance_task_id = "task-prompt-parent"
        orchestrator._governance_root_event_id = ""

        ctx = OperationContext(
            project_id=project_id,
            task_id="task-prompt-parent",
            chapter_number=3,
            stage="writing_chapter",
            operation_id="task-prompt-parent",
        )
        with obs.span(ctx, "stage.writing_chapter", span_kind="stage", component="orchestrator") as stage_span:
            orchestrator._record_prompt_trace_performance_spans(
                project_id=project_id,
                chapter_number=3,
                prompt_trace_id="prompt-trace-parent",
                trace_payload={
                    "trace_scope": "writer",
                    "stage_key": "scene_generation",
                    "attempts": [
                        {
                            "stage_key": "scene_generation",
                            "duration_ms": 123,
                            "model": "deepseek-chat",
                            "http_status": 200,
                            "attempt_no": 1,
                        }
                    ],
                },
            )
            parent_span_id = stage_span.span_id
            trace_id = stage_span.trace_id

        with Session() as session:
            llm_row = session.execute(
                select(PerformanceSpan).where(PerformanceSpan.span_name == "llm.request")
            ).scalar_one()

        assert llm_row.parent_span_id == parent_span_id
        assert llm_row.trace_id == trace_id
    finally:
        engine.dispose()


def test_stage_transition_span_uses_stage_entry_chapter_when_next_stage_moves_on() -> None:
    engine = get_engine(postgres_test_url("phase-f-stage-chapter"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            _seed_project(session, project_id)

        obs = ObservabilityService(
            session_factory=Session,
            artifact_store=None,
            config=Config(database_url=postgres_test_url("phase-f-stage-chapter"), minimax_api_key=""),
        )
        orchestrator = WritingOrchestrator.__new__(WritingOrchestrator)
        orchestrator.observability = obs
        orchestrator._governance_task_id = "task-stage-chapter"
        orchestrator._governance_root_event_id = ""
        orchestrator._governance_runtime_project_id = project_id
        orchestrator._governance_runtime_updater = object()
        orchestrator._governance_stage_name = ""
        orchestrator._governance_stage_started_at = 0.0
        orchestrator._governance_stage_span = None
        recorded_events = []

        def record_event(**kwargs):
            recorded_events.append(kwargs)
            return SimpleNamespace(id=f"event-{len(recorded_events)}")

        orchestrator._record_decision_event = record_event
        orchestrator._record_stage_transition(
            {
                "project_id": project_id,
                "stage": "running_post_acceptance",
                "current_chapter": 28,
            }
        )
        orchestrator._record_stage_transition(
            {
                "project_id": project_id,
                "stage": "running_scenario_rehearsal",
                "current_chapter": 29,
            }
        )

        with Session() as session:
            row = session.execute(
                select(PerformanceSpan).where(PerformanceSpan.span_name == "stage.running_post_acceptance")
            ).scalar_one()

        assert row.chapter_number == 28
        assert json.loads(row.metrics_json)["chapter_number"] == 28
        exited_event = next(item for item in recorded_events if item["event_type"] == "stage_exited")
        assert exited_event["chapter_number"] == 28
    finally:
        engine.dispose()


def test_sqlalchemy_probe_records_db_metrics_on_active_span() -> None:
    engine = get_engine(postgres_test_url("phase-f-db-probe"))
    init_db(engine)
    install_sqlalchemy_query_probe(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            _seed_project(session, project_id)

        obs = ObservabilityService(
            session_factory=Session,
            artifact_store=None,
            config=Config(database_url=postgres_test_url("phase-f-db-probe"), minimax_api_key=""),
        )
        ctx = OperationContext(project_id=project_id, task_id="task-db", operation_id="task-db")
        with obs.span(ctx, "db.probed-work", span_kind="stage", component="test"):
            with Session() as session:
                session.execute(text("SELECT 1")).scalar_one()

        with Session() as session:
            row = session.execute(
                select(PerformanceSpan).where(PerformanceSpan.task_id == "task-db")
            ).scalar_one()
            metrics = json.loads(row.metrics_json)
            tags = json.loads(row.tags_json)

        assert metrics["db.query_count"] >= 1
        assert metrics["db.duration_ms"] >= 0
        assert tags["db.slowest_query_hash"]
        assert "SELECT 1" in tags["db.slowest_query_preview"]
    finally:
        engine.dispose()
