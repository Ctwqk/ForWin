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


def test_runtime_container_routes_environment_profiles_after_default_minimax() -> None:
    from forwin.runtime.container import RuntimeContainer

    infrastructure = InfrastructureConfig(
        minimax_api_key="minimax-secret",
        llm_env_profiles=[
            {
                "id": "env-kimi",
                "name": "Kimi",
                "api_key": "kimi-secret",
                "base_url": "https://api.moonshot.cn/v1",
                "model": "kimi-k2.5",
            },
            {
                "id": "env-deepseek",
                "name": "DeepSeek",
                "api_key": "deepseek-secret",
                "base_url": "https://api.deepseek.com/v1",
                "model": "deepseek-chat",
            },
        ],
    )
    client = RuntimeContainer._build_llm_client(
        infrastructure,
        RuntimePolicy.for_profile("standard"),
    )

    try:
        route = client._route_profiles_with_metadata(
            client._request_profiles(),
            task_family="writer",
            stage_key="chapter_draft",
        )
    finally:
        client.close()

    assert [profile["id"] for profile in route["profiles"]] == [
        "env-kimi",
        "env-deepseek",
    ]


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

    core_services = container.core_services()
    generation_services = container.generation_services()
    generation_application = container.build_generation_application_service()
    pipeline = container.build_chapter_pipeline(
        progress_callback=lambda *_args: None,
        task_id="task-1",
        root_event_id="root-1",
    )

    assert container.core_services() is core_services
    assert container.generation_services() is generation_services
    assert core_services.infrastructure is infrastructure
    assert core_services.policy is policy
    assert generation_services.llm_client.api_key == "selected-secret"
    assert generation_services.llm_client.model == "kimi-k2.5"
    assert core_services.engine is fake_engine
    assert core_services.session_factory is fake_session_factory
    assert core_services.generation_application is generation_application
    assert generation_application.session_factory is fake_session_factory
    assert generation_application.infrastructure is infrastructure
    assert schema_calls == [fake_engine]
    assert not hasattr(pipeline, "services")
    assert not hasattr(pipeline, "infrastructure")
    assert pipeline.policy is policy
    assert pipeline.writer is generation_services.writer
    assert pipeline.canon_admission is generation_services.canon_admission
    assert pipeline._audit_task_id == "task-1"
    assert pipeline._audit_root_event_id == "root-1"
    assert not hasattr(pipeline, "config")

    pipeline.close()
    assert fake_engine.disposed is True
    with pytest.raises(RuntimeError, match="closed"):
        container.core_services()
