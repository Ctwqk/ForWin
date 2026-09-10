from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

import forwin.api as api_entrypoint
from forwin.application.generation import GenerationApplicationService
from forwin.config import InfrastructureConfig
from forwin.generation.worker import run_one_generation_task
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import ArcPlanVersion, Project
from forwin.models.task import GenerationTask
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from tests.http_runtime_harness import HttpRuntimeHarness
from tests.postgres import postgres_test_url


def test_api_enqueued_continue_task_is_claimed_by_worker(monkeypatch) -> None:
    database_url = postgres_test_url("generation-worker-cutover-e2e")
    engine = get_engine(database_url)
    init_db(engine)
    Session = get_session_factory(engine)
    infrastructure = InfrastructureConfig(database_url=database_url, minimax_api_key="sk-test")
    api = HttpRuntimeHarness(
        session_factory=Session,
        config=infrastructure,
        engine=engine,
    )
    calls: list[dict[str, object]] = []
    now = datetime.now(timezone.utc)
    try:
        with Session.begin() as session:
            project = Project(
                id="project-worker-cutover",
                title="Worker Cutover",
                automation_json='{"primary_publish_platform":"qidian"}',
                premise="测试",
                genre="玄幻",
                creation_status="writing",
                created_at=now,
                updated_at=now,
            )
            session.add(project)
            session.flush()
            ProjectPolicyStore(session).initialize(
                project,
                RuntimePolicy.for_profile("standard"),
            )
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

        task_id = api._create_continue_generation_task(
            project_id="project-worker-cutover",
            requested_chapters=1,
            max_chapters=1,
            auto_continue=False,
            title="Worker Cutover",
            subtitle="继续生成",
        )
        queued = api._get_generation_task_or_404(task_id)
        assert queued["status"] == "queued"

        def fake_execute_continuation(*args, **kwargs):
            calls.append({"args": args, "kwargs": kwargs})

        monkeypatch.setattr(
            "forwin.application.generation_execution.execute_continuation",
            fake_execute_continuation,
        )

        result = run_one_generation_task(
            application_service=GenerationApplicationService(
                session_factory=Session,
                infrastructure=infrastructure,
            ),
            worker_id="worker-cutover",
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
        engine.dispose()


def test_generation_api_no_longer_starts_daemon_generation_threads() -> None:
    source = Path("forwin/http/generation.py").read_text()

    assert "target=_run_generation_with_config" not in source
    assert "target=_run_continue_project_with_config" not in source
    assert "daemon=True" not in source


def test_api_split_removes_private_run_alias_compatibility() -> None:
    assert not hasattr(api_entrypoint, "_build_runtime_config")
    assert not hasattr(api_entrypoint, "_build_saved_runtime_config")
    assert not hasattr(api_entrypoint, "_run_generation_with_config")
    assert not hasattr(api_entrypoint, "_run_continue_project_with_config")
