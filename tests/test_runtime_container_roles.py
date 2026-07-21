from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.config import InfrastructureConfig
from forwin.runtime.policy import RuntimePolicy


def _container(role: str):
    from forwin.runtime.container import RuntimeContainer

    return RuntimeContainer.from_config(
        InfrastructureConfig(minimax_api_key=""),
        policy=RuntimePolicy.for_profile("standard"),
        role=role,  # type: ignore[arg-type]
    )


def test_runtime_roles_are_exactly_the_four_owned_processes() -> None:
    for role in (
        "api",
        "generation_worker",
        "publisher_worker",
        "outbox_worker",
    ):
        assert _container(role).role == role

    for removed_role in ("full", "mcp", "maintenance"):
        with pytest.raises(ValueError, match="Unsupported runtime role"):
            _container(removed_role)


def test_monolithic_runtime_services_facade_is_removed() -> None:
    from forwin.runtime import services as services_module
    from forwin.runtime.container import RuntimeContainer

    assert not hasattr(services_module, "RuntimeServices")
    assert not hasattr(RuntimeContainer, "services")


def test_role_owned_accessors_are_cached_and_enforced(monkeypatch) -> None:
    from forwin.runtime.container import RuntimeContainer

    built = {
        "core": SimpleNamespace(),
        "generation": SimpleNamespace(),
        "publisher": SimpleNamespace(),
        "outbox": {"event": lambda _claim: None},
    }
    calls: list[str] = []

    def builder(name: str):
        def build(_self):
            calls.append(name)
            return built[name]

        return build

    monkeypatch.setattr(
        RuntimeContainer, "_build_core_services", builder("core"), raising=False
    )
    monkeypatch.setattr(
        RuntimeContainer,
        "_build_generation_services",
        builder("generation"),
        raising=False,
    )
    monkeypatch.setattr(
        RuntimeContainer,
        "_build_publisher_services",
        builder("publisher"),
        raising=False,
    )
    monkeypatch.setattr(
        RuntimeContainer,
        "_build_outbox_handlers",
        builder("outbox"),
        raising=False,
    )

    api = _container("api")
    assert api.core_services() is api.core_services() is built["core"]
    assert api.generation_services() is api.generation_services() is built["generation"]
    assert api.publisher_services() is api.publisher_services() is built["publisher"]
    with pytest.raises(RuntimeError, match="outbox"):
        api.build_outbox_handlers()

    generation = _container("generation_worker")
    assert generation.core_services() is built["core"]
    assert generation.generation_services() is built["generation"]
    with pytest.raises(RuntimeError, match="publisher"):
        generation.publisher_services()
    with pytest.raises(RuntimeError, match="outbox"):
        generation.build_outbox_handlers()

    publisher = _container("publisher_worker")
    assert publisher.core_services() is built["core"]
    assert publisher.publisher_services() is built["publisher"]
    with pytest.raises(RuntimeError, match="generation"):
        publisher.generation_services()
    with pytest.raises(RuntimeError, match="outbox"):
        publisher.build_outbox_handlers()

    outbox = _container("outbox_worker")
    assert outbox.core_services() is built["core"]
    assert outbox.build_outbox_handlers() is built["outbox"]
    assert outbox.build_outbox_handlers() is built["outbox"]
    with pytest.raises(RuntimeError, match="generation"):
        outbox.generation_services()
    with pytest.raises(RuntimeError, match="publisher"):
        outbox.publisher_services()

    assert calls.count("core") == 4
    assert calls.count("generation") == 2
    assert calls.count("publisher") == 2
    assert calls.count("outbox") == 1


def test_close_disposes_only_materialized_resources(monkeypatch) -> None:
    from forwin.runtime.container import RuntimeContainer

    calls: list[str] = []

    class Engine:
        def dispose(self) -> None:
            calls.append("dispose")

    monkeypatch.setattr(
        RuntimeContainer,
        "_build_core_services",
        lambda _self: SimpleNamespace(engine=Engine()),
        raising=False,
    )
    for name in (
        "_build_generation_services",
        "_build_publisher_services",
        "_build_outbox_handlers",
    ):
        monkeypatch.setattr(
            RuntimeContainer,
            name,
            lambda _self, name=name: calls.append(name),
            raising=False,
        )

    container = _container("api")
    container.core_services()
    container.close()
    container.close()

    assert calls == ["dispose"]
    with pytest.raises(RuntimeError, match="closed"):
        container.core_services()


def test_close_releases_only_materialized_external_resources(monkeypatch) -> None:
    from forwin.runtime.container import RuntimeContainer

    calls: list[str] = []
    core = SimpleNamespace(
        artifact_store=SimpleNamespace(close=lambda: calls.append("artifact")),
        engine=SimpleNamespace(dispose=lambda: calls.append("engine")),
    )
    generation = SimpleNamespace(
        retrieval_broker=SimpleNamespace(close=lambda: calls.append("retrieval")),
        llm_client=SimpleNamespace(close=lambda: calls.append("llm")),
    )
    outbox_index = SimpleNamespace(close=lambda: calls.append("outbox"))
    monkeypatch.setattr(RuntimeContainer, "_build_core_services", lambda _self: core)
    monkeypatch.setattr(
        RuntimeContainer,
        "_build_generation_services",
        lambda _self: generation,
    )
    monkeypatch.setattr(
        RuntimeContainer,
        "_build_memory_index",
        lambda _self, _config: outbox_index,
        raising=False,
    )

    api = _container("api")
    api.generation_services()
    api.close()
    outbox = _container("outbox_worker")
    outbox.core_services()
    outbox._provide_outbox_memory_index()
    outbox.close()
    with pytest.raises(RuntimeError, match="closed"):
        outbox._provide_outbox_memory_index()

    assert calls == ["retrieval", "llm", "outbox", "artifact", "engine"]


def test_outbox_phase3_provider_never_builds_generation_bundle(monkeypatch) -> None:
    from forwin.runtime.container import RuntimeContainer

    service = object()
    core = SimpleNamespace()
    container = _container("outbox_worker")
    monkeypatch.setattr(
        RuntimeContainer,
        "core_services",
        lambda _self: core,
    )
    monkeypatch.setattr(
        RuntimeContainer,
        "_build_generation_services",
        lambda _self: (_ for _ in ()).throw(
            AssertionError("outbox phase3 must not build generation services")
        ),
    )
    monkeypatch.setattr(
        RuntimeContainer,
        "_build_outbox_post_canon_maintenance",
        lambda _self, _core: service,
        raising=False,
    )

    assert container._provide_outbox_post_canon_maintenance() is service
    assert container._provide_outbox_post_canon_maintenance() is service
    assert container._generation_services is None


def test_task_pipeline_build_failure_closes_its_container(monkeypatch) -> None:
    from forwin.application import generation_execution

    calls: list[str] = []

    class Container:
        def build_chapter_pipeline(self, **_kwargs):
            calls.append("build")
            raise RuntimeError("pipeline construction failed")

        def close(self) -> None:
            calls.append("close")

    container = Container()
    monkeypatch.setattr(
        generation_execution.RuntimeContainer,
        "from_config",
        lambda *_args, **_kwargs: container,
    )
    context = SimpleNamespace(
        infrastructure=object(),
        policy=object(),
        task_id="task-1",
        root_event_id="root-1",
    )

    with pytest.raises(RuntimeError, match="construction failed"):
        generation_execution._build_chapter_pipeline_for_task(context)

    assert calls == ["build", "close"]


def test_role_bootstrap_does_not_construct_external_clients(
    monkeypatch,
    tmp_path,
) -> None:
    from forwin.runtime import container as container_module
    from forwin.runtime.container import RuntimeContainer

    external_calls: list[str] = []

    class Engine:
        def dispose(self) -> None:
            return None

    monkeypatch.setattr(container_module, "get_engine", lambda _url: Engine())
    monkeypatch.setattr(container_module, "require_v5_schema", lambda _engine: None)
    monkeypatch.setattr(
        container_module,
        "get_session_factory",
        lambda _engine: object(),
    )
    monkeypatch.setattr(
        container_module,
        "create_memory_index",
        lambda **_kwargs: external_calls.append("qdrant") or object(),
    )
    monkeypatch.setattr(
        "minio.Minio",
        lambda *_args, **_kwargs: external_calls.append("minio") or object(),
    )

    config = InfrastructureConfig(
        database_url="postgresql+psycopg://fake/forwin",
        artifact_root=str(tmp_path),
        artifact_backend="minio",
        minio_endpoint="minio:9000",
        minio_access_key="access",
        minio_secret_key="secret",
        minio_bucket="artifacts",
        retention_cleanup_on_startup=False,
        minimax_api_key="",
    )
    policy = RuntimePolicy.for_profile("standard")
    containers = [
        RuntimeContainer.for_api(config, policy=policy),
        RuntimeContainer.for_generation_worker(config, policy=policy),
        RuntimeContainer.for_publisher_worker(config, policy=policy),
        RuntimeContainer.for_outbox_worker(config, policy=policy),
    ]

    containers[0].core_services()
    containers[1].generation_services()
    containers[2].publisher_services()
    containers[3].build_outbox_handlers()

    assert external_calls == []
    for container in containers:
        container.close()


def test_llm_eval_uses_explicit_runtime_policy() -> None:
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    source = (root / "forwin" / "llm_eval" / "runner.py").read_text(
        encoding="utf-8"
    )

    assert "ChapterPipeline(" not in source
    assert "RuntimePolicy.for_profile" in source
    assert 'role="generation_worker"' in source
