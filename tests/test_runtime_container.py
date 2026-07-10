from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

import pytest

from forwin.config import InfrastructureConfig
from forwin.runtime.policy import RuntimePolicy


FAKE_DATABASE_URL = "postgresql+psycopg://fake/forwin"


def test_runtime_container_imports_in_fresh_process() -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            "from forwin.runtime.container import RuntimeContainer; print(RuntimeContainer.__name__)",
        ],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert result.stdout.strip() == "RuntimeContainer"


def test_runtime_container_requires_explicit_policy_and_records_role() -> None:
    from forwin.runtime.container import RuntimeContainer

    infrastructure = InfrastructureConfig(database_url=FAKE_DATABASE_URL)
    policy = RuntimePolicy.for_profile("pulp")

    container = RuntimeContainer.from_config(
        infrastructure,
        policy=policy,
        role="api",
    )

    assert container.infrastructure is infrastructure
    assert container.policy is policy
    assert container.role == "api"
    assert not hasattr(container, "config")

    with pytest.raises(TypeError):
        RuntimeContainer.from_config(infrastructure)  # type: ignore[call-arg]
    with pytest.raises(ValueError, match="Unsupported runtime role"):
        RuntimeContainer.from_config(
            infrastructure,
            policy=policy,
            role="browser",  # type: ignore[arg-type]
        )


class _FakeURL:
    def render_as_string(self, *, hide_password: bool = True) -> str:
        return FAKE_DATABASE_URL


class _FakeEngine:
    url = _FakeURL()

    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def _patch_runtime_infrastructure(monkeypatch):
    from forwin.runtime import container as container_module

    fake_engine = _FakeEngine()
    fake_session_factory = object()
    schema_calls: list[object] = []
    monkeypatch.setattr(container_module, "get_engine", lambda _url: fake_engine)
    monkeypatch.setattr(
        container_module,
        "require_v5_schema",
        lambda engine: schema_calls.append(engine),
    )
    monkeypatch.setattr(
        container_module,
        "get_session_factory",
        lambda _engine: fake_session_factory,
    )
    monkeypatch.setattr(
        container_module,
        "create_memory_index",
        lambda **_kwargs: SimpleNamespace(),
    )
    return fake_engine, fake_session_factory, schema_calls


def test_runtime_container_injects_policy_and_selected_model(monkeypatch) -> None:
    from forwin.runtime.container import RuntimeContainer

    fake_engine, fake_session_factory, schema_calls = _patch_runtime_infrastructure(
        monkeypatch
    )
    infrastructure = InfrastructureConfig(
        database_url=FAKE_DATABASE_URL,
        retrieval_backend="hash",
        artifact_root="/tmp/forwin-runtime-container-test-artifacts",
        llm_env_profiles=[
            {
                "id": "env-kimi",
                "name": "Kimi",
                "api_key": "selected-secret",
                "base_url": "https://api.moonshot.cn/v1",
                "model": "kimi-k2.5",
            }
        ],
    )
    policy = RuntimePolicy.for_profile("pulp", model_profile_id="env-kimi")
    container = RuntimeContainer.from_config(
        infrastructure,
        policy=policy,
        role="generation_worker",
    )

    services = container.services()
    generation_application = container.build_generation_application_service()
    pipeline = container.build_chapter_pipeline(
        progress_callback=lambda *_args: None,
        task_id="task-1",
        root_event_id="root-1",
    )

    assert container.services() is services
    assert services.infrastructure is infrastructure
    assert services.policy is policy
    assert services.llm_client.api_key == "selected-secret"
    assert services.llm_client.model == "kimi-k2.5"
    assert services.engine is fake_engine
    assert services.session_factory is fake_session_factory
    assert services.generation_application is generation_application
    assert generation_application.session_factory is fake_session_factory
    assert generation_application.infrastructure is infrastructure
    assert schema_calls == [fake_engine]
    assert not hasattr(pipeline, "services")
    assert not hasattr(pipeline, "infrastructure")
    assert pipeline.policy is policy
    assert pipeline.writer is services.writer
    assert pipeline.canon_admission is services.canon_admission
    assert pipeline._governance_task_id == "task-1"
    assert pipeline._governance_root_event_id == "root-1"
    assert not hasattr(pipeline, "config")


def test_runtime_container_builds_callback_bound_production_scheduler(
    monkeypatch,
) -> None:
    from forwin.runtime.container import RuntimeContainer

    _fake_engine, fake_session_factory, _init_calls = _patch_runtime_infrastructure(
        monkeypatch
    )
    infrastructure = InfrastructureConfig(
        database_url=FAKE_DATABASE_URL,
        retrieval_backend="hash",
        artifact_root="/tmp/forwin-runtime-container-test-artifacts",
    )
    container = RuntimeContainer.from_config(
        infrastructure,
        policy=RuntimePolicy.for_profile("standard"),
    )

    scheduler = container.build_production_scheduler(
        display_datetime=lambda _value: "",
        persist_project_automation=lambda *_args, **_kwargs: None,
        generation_terminal_statuses={"completed"},
        upload_terminal_statuses={"succeeded"},
    )

    assert scheduler.session_factory is fake_session_factory
    assert scheduler.config is infrastructure
    assert (
        scheduler.generation_application
        is container.build_generation_application_service()
    )
