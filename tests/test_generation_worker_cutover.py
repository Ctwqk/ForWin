from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import forwin.api as api_module
from forwin.config import Config
from forwin.generation.worker import run_one_generation_task
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, Project
from forwin.models.task import GenerationTask
from tests.postgres import postgres_test_url


def test_api_enqueued_continue_task_is_claimed_by_worker(monkeypatch) -> None:
    database_url = postgres_test_url("generation-worker-cutover-e2e")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    old_session_factory = api_module._SessionFactory
    api_module._SessionFactory = Session
    calls: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)
    try:
        with Session.begin() as session:
            session.add(
                Project(
                    id="project-worker-cutover",
                    title="Worker Cutover",
                    premise="测试",
                    genre="玄幻",
                    creation_status="writing",
                    created_at=now,
                    updated_at=now,
                )
            )
            session.flush()
            session.add(
                ArcPlanVersion(
                    id="arc-worker-cutover",
                    project_id="project-worker-cutover",
                    version=1,
                    arc_number=1,
                    status="active",
                    arc_synopsis="worker cutover active arc",
                    created_at=now,
                )
            )

        task_id = api_module._create_continue_generation_task(
            project_id="project-worker-cutover",
            runtime_config=Config(database_url=database_url, minimax_api_key="sk-test"),
            requested_chapters=1,
            max_chapters=1,
            auto_continue=False,
            title="Worker Cutover",
            subtitle="继续生成",
        )
        queued = api_module._get_generation_task_or_404(task_id)
        assert queued["status"] == "queued"

        def fake_run_continue_project_with_config(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})

        monkeypatch.setattr(
            "forwin.api_runtime.run_continue_project_with_config",
            fake_run_continue_project_with_config,
        )

        result = run_one_generation_task(
            session_factory=Session,
            worker_id="worker-cutover",
            config=Config(database_url=database_url, minimax_api_key="sk-test"),
        )

        assert result.claimed is True
        assert result.task_id == task_id
        assert calls
        with Session.begin() as session:
            row = session.get(GenerationTask, task_id)
            assert row is not None
            assert row.status == "running"
            assert row.lease_owner == "worker-cutover"
    finally:
        api_module._SessionFactory = old_session_factory
        engine.dispose()


def test_generation_api_no_longer_starts_daemon_generation_threads() -> None:
    source = Path("forwin/api_core/generation.py").read_text()

    assert "target=_run_generation_with_config" not in source
    assert "target=_run_continue_project_with_config" not in source
    assert "daemon=True" not in source
