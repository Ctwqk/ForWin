from __future__ import annotations

from types import SimpleNamespace

from forwin.config import InfrastructureConfig
from forwin.http import runtime as runtime_module
from forwin.http.runtime import HttpRuntime


def test_startup_builds_core_only_and_shutdown_does_not_resolve_unused_bundles(
    monkeypatch,
) -> None:
    calls: list[str] = []
    engine = SimpleNamespace(dispose=lambda: calls.append("engine.dispose"))
    core = SimpleNamespace(engine=engine, session_factory=object())

    class FakeContainer:
        def core_services(self):
            calls.append("core")
            return core

        def generation_services(self):
            raise AssertionError("startup resolved generation services")

        def publisher_services(self):
            raise AssertionError("startup resolved publisher services")

        def build_chapter_pipeline(self):
            raise AssertionError("startup built a pipeline")

        def close(self) -> None:
            calls.append("container.close")

    container = FakeContainer()
    monkeypatch.delenv("FORWIN_DATABASE_URL", raising=False)
    monkeypatch.setattr(
        runtime_module,
        "RuntimeContainer",
        SimpleNamespace(for_api=lambda _config, *, policy: container),
    )
    monkeypatch.setattr(
        "forwin.http.automation.start_automation_scheduler",
        lambda _runtime: calls.append("scheduler.start"),
    )
    monkeypatch.setattr(
        "forwin.http.automation.stop_automation_scheduler",
        lambda _runtime: calls.append("scheduler.stop"),
    )

    runtime = HttpRuntime(config=InfrastructureConfig(minimax_api_key=""))
    runtime.startup()

    assert runtime.engine is engine
    assert runtime.session_factory is core.session_factory
    assert runtime.pipeline is None
    assert runtime.publisher_manager is None
    assert calls == ["core", "scheduler.start"]

    runtime.shutdown()

    assert calls == ["core", "scheduler.start", "scheduler.stop", "container.close"]


def test_pipeline_is_built_and_backfilled_once_on_first_access() -> None:
    calls: list[str] = []

    class Session:
        def __enter__(self):
            calls.append("session.enter")
            return self

        def __exit__(self, *_args):
            calls.append("session.exit")

        def commit(self) -> None:
            calls.append("commit")

        def rollback(self) -> None:
            calls.append("rollback")

    manager = SimpleNamespace(
        backfill_missing_resolutions=lambda *, session: calls.append("backfill") or 2
    )
    pipeline = SimpleNamespace(arc_envelope_manager=manager)

    class Container:
        def build_chapter_pipeline(self):
            calls.append("build")
            return pipeline

    runtime = HttpRuntime(
        config=InfrastructureConfig(minimax_api_key=""),
        container=Container(),  # type: ignore[arg-type]
        session_factory=lambda: Session(),  # type: ignore[arg-type]
    )

    assert runtime.get_pipeline() is pipeline
    assert runtime.get_pipeline() is pipeline
    assert calls == ["build", "session.enter", "backfill", "commit", "session.exit"]


def test_publisher_manager_recovers_interrupted_attempts_once_on_first_access(
    monkeypatch,
) -> None:
    calls: list[str] = []

    class Manager:
        def __init__(self, session_factory, **_kwargs) -> None:
            calls.append(f"init:{session_factory}")

        def recover_interrupted_upload_attempts(self) -> None:
            calls.append("recover")

    monkeypatch.setattr(runtime_module, "PublisherManager", Manager)
    runtime = HttpRuntime(
        config=InfrastructureConfig(minimax_api_key=""),
        session_factory="session-factory",  # type: ignore[arg-type]
    )

    manager = runtime.get_publisher_manager()

    assert runtime.get_publisher_manager() is manager
    assert calls == ["init:session-factory", "recover"]


def test_automation_shutdown_joins_before_clearing_thread() -> None:
    from forwin.http.automation import stop_automation_scheduler

    calls: list[str] = []
    runtime = SimpleNamespace(
        automation_stop=SimpleNamespace(set=lambda: calls.append("stop")),
        automation_thread=None,
    )

    class Thread:
        alive = True

        def is_alive(self) -> bool:
            return self.alive

        def join(self) -> None:
            assert runtime.automation_thread is self
            calls.append("join")
            self.alive = False

    runtime.automation_thread = Thread()
    stop_automation_scheduler(runtime)

    assert calls == ["stop", "join"]
    assert runtime.automation_thread is None


def test_custom_genesis_close_releases_llm_and_artifact_store() -> None:
    from forwin.http.request_support import _close_genesis_service

    calls: list[str] = []
    service = SimpleNamespace(
        _forwin_runtime_shared=False,
        llm_client=SimpleNamespace(close=lambda: calls.append("llm")),
        artifact_store=SimpleNamespace(close=lambda: calls.append("artifact")),
    )

    _close_genesis_service(HttpRuntime(), service)

    assert calls == ["llm", "artifact"]
