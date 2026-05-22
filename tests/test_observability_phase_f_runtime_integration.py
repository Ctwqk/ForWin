from __future__ import annotations

from types import SimpleNamespace

from sqlalchemy import select

from forwin.api_runtime import run_orchestrator_task
from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.observability import PerformanceSpan
from forwin.models.project import Project
from forwin.observability.service import ObservabilityService


class _FakeCloser:
    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


class _FakeRuntimeEngine:
    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_run_orchestrator_task_records_operation_and_cleanup_spans() -> None:
    engine = get_engine(postgres_test_url("phase-f-runtime"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            session.add(
                Project(
                    id=project_id,
                    title="Runtime Observability",
                    premise="premise",
                    genre="玄幻",
                )
            )
            session.commit()

        obs = ObservabilityService(
            session_factory=Session,
            artifact_store=None,
            config=Config(database_url=postgres_test_url("phase-f-runtime"), minimax_api_key=""),
        )
        fake_llm = _FakeCloser()
        fake_engine = _FakeRuntimeEngine()
        orchestrator = SimpleNamespace(
            _SessionFactory=Session,
            services=SimpleNamespace(observability=obs),
            llm_client=fake_llm,
            engine=fake_engine,
        )
        result = SimpleNamespace(
            status="completed",
            project_id=project_id,
            failed_chapters=[],
            paused_chapters=[],
            frozen_artifacts=[],
        )

        run_orchestrator_task(
            "task-phase-f-runtime",
            orchestrator,
            lambda: result,
            update_task=lambda *_args, **_kwargs: None,
            logger=SimpleNamespace(
                exception=lambda *_args, **_kwargs: None,
            ),
            error_message="runtime failed",
            default_project_id=None,
        )

        with Session() as session:
            rows = session.execute(
                select(PerformanceSpan)
                .where(PerformanceSpan.task_id == "task-phase-f-runtime")
                .order_by(PerformanceSpan.created_at.asc(), PerformanceSpan.id.asc())
            ).scalars().all()

        assert [row.span_name for row in rows] == ["task.operation", "task.cleanup"]
        assert [row.component for row in rows] == ["api", "api"]
        assert all(row.project_id == project_id for row in rows)
        assert rows[0].status == "ok"
        assert fake_llm.closed is True
        assert fake_engine.disposed is True
    finally:
        engine.dispose()


def test_run_orchestrator_task_records_worker_component_when_requested() -> None:
    engine = get_engine(postgres_test_url("phase-f-runtime-worker-component"))
    init_db(engine)
    Session = get_session_factory(engine)
    project_id = new_id()
    try:
        with Session() as session:
            session.add(
                Project(
                    id=project_id,
                    title="Runtime Worker Component",
                    premise="premise",
                    genre="玄幻",
                )
            )
            session.commit()

        obs = ObservabilityService(
            session_factory=Session,
            artifact_store=None,
            config=Config(database_url=postgres_test_url("phase-f-runtime-worker-component"), minimax_api_key=""),
        )
        fake_llm = _FakeCloser()
        fake_engine = _FakeRuntimeEngine()
        orchestrator = SimpleNamespace(
            _SessionFactory=Session,
            services=SimpleNamespace(observability=obs),
            llm_client=fake_llm,
            engine=fake_engine,
        )
        result = SimpleNamespace(
            status="completed",
            project_id=project_id,
            failed_chapters=[],
            paused_chapters=[],
            frozen_artifacts=[],
        )

        run_orchestrator_task(
            "task-phase-f-runtime-worker-component",
            orchestrator,
            lambda: result,
            update_task=lambda *_args, **_kwargs: None,
            logger=SimpleNamespace(exception=lambda *_args, **_kwargs: None),
            error_message="runtime failed",
            default_project_id=None,
            component="worker",
        )

        with Session() as session:
            rows = session.execute(
                select(PerformanceSpan)
                .where(PerformanceSpan.task_id == "task-phase-f-runtime-worker-component")
                .order_by(PerformanceSpan.created_at.asc(), PerformanceSpan.id.asc())
            ).scalars().all()

        assert [row.span_name for row in rows] == ["task.operation", "task.cleanup"]
        assert [row.component for row in rows] == ["worker", "worker"]
    finally:
        engine.dispose()
