from __future__ import annotations

from datetime import datetime, timedelta
from tempfile import TemporaryDirectory

import forwin.api as api_module
from forwin.config import Config
from forwin.models.base import get_engine, get_session_factory, init_db, new_id
from forwin.models.observability import PerformanceSpan
from forwin.models.project import Project


def test_performance_api_reports_task_critical_path_and_slow_spans() -> None:
    with TemporaryDirectory() as tmp:
        old_config = api_module._config
        old_engine = api_module._engine
        old_factory = api_module._SessionFactory
        database_url = postgres_test_url("phase-f-performance-api")
        api_module._config = Config(
            database_url=database_url,
            artifact_root=tmp,
            minimax_api_key="",
        )
        api_module._engine = get_engine(database_url)
        init_db(api_module._engine)
        api_module._SessionFactory = get_session_factory(api_module._engine)
        project_id = new_id()
        task_id = "task-performance-api"
        try:
            with api_module._get_session() as session:
                session.add(Project(id=project_id, title="Perf API", premise="p", genre="玄幻"))
                session.add(
                    PerformanceSpan(
                        project_id=project_id,
                        task_id=task_id,
                        operation_id=task_id,
                        trace_id=task_id,
                        span_id="root-span",
                        parent_span_id="",
                        span_name="task.operation",
                        span_kind="task",
                        component="api",
                        stage="task.operation",
                        duration_ms=120,
                        self_duration_ms=20,
                    )
                )
                session.add(
                    PerformanceSpan(
                        project_id=project_id,
                        task_id=task_id,
                        operation_id=task_id,
                        trace_id=task_id,
                        span_id="writer-span",
                        parent_span_id="root-span",
                        span_name="writer.write_chapter",
                        span_kind="writer",
                        component="writer",
                        stage="writing_chapter",
                        duration_ms=100,
                        self_duration_ms=100,
                    )
                )
                session.commit()

            report = api_module.get_task_performance_report(task_id)
            slow = api_module.get_slow_performance_spans(project_id=project_id, task_id="", limit=5)

            assert report.task_id == task_id
            assert [item.span_name for item in report.critical_path] == [
                "task.operation",
                "writer.write_chapter",
            ]
            assert slow[0].span_name == "task.operation"
        finally:
            if api_module._engine is not None:
                api_module._engine.dispose()
            api_module._config = old_config
            api_module._engine = old_engine
            api_module._SessionFactory = old_factory


def test_slow_spans_orders_by_duration_before_limit() -> None:
    with TemporaryDirectory() as tmp:
        old_config = api_module._config
        old_engine = api_module._engine
        old_factory = api_module._SessionFactory
        database_url = postgres_test_url("phase-f-slow-span-duration")
        api_module._config = Config(
            database_url=database_url,
            artifact_root=tmp,
            minimax_api_key="",
        )
        api_module._engine = get_engine(database_url)
        init_db(api_module._engine)
        api_module._SessionFactory = get_session_factory(api_module._engine)
        project_id = new_id()
        task_id = "task-slow-span-duration"
        base_time = datetime(2026, 1, 1, 0, 0, 0)
        try:
            with api_module._get_session() as session:
                session.add(Project(id=project_id, title="Perf API", premise="p", genre="玄幻"))
                for index, duration in enumerate([10, 20, 500]):
                    session.add(
                        PerformanceSpan(
                            project_id=project_id,
                            task_id=task_id,
                            operation_id=task_id,
                            trace_id=task_id,
                            span_id=f"span-{duration}",
                            span_name=f"span.{duration}",
                            span_kind="test",
                            component="test",
                            stage="test",
                            duration_ms=duration,
                            self_duration_ms=duration,
                            created_at=base_time + timedelta(seconds=index),
                        )
                    )
                session.commit()

            slow = api_module.get_slow_performance_spans(project_id=project_id, task_id="", limit=2)

            assert [item.duration_ms for item in slow] == [500, 20]
            assert [item.span_name for item in slow] == ["span.500", "span.20"]
        finally:
            if api_module._engine is not None:
                api_module._engine.dispose()
            api_module._config = old_config
            api_module._engine = old_engine
            api_module._SessionFactory = old_factory
