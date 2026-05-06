from __future__ import annotations

import subprocess
import sys
from types import SimpleNamespace

from forwin.config import Config


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


class _FakeURL:
    def render_as_string(self, *, hide_password: bool = True) -> str:
        return "postgresql+psycopg://fake/forwin"


class _FakeEngine:
    url = _FakeURL()

    def __init__(self) -> None:
        self.disposed = False

    def dispose(self) -> None:
        self.disposed = True


def test_runtime_container_builds_services_once_and_injects_orchestrator(monkeypatch) -> None:
    from forwin.runtime import container as container_module
    from forwin.runtime.container import RuntimeContainer

    fake_engine = _FakeEngine()
    fake_session_factory = object()
    init_calls: list[object] = []

    monkeypatch.setattr(container_module, "get_engine", lambda database_url: fake_engine)
    monkeypatch.setattr(container_module, "init_db", lambda engine: init_calls.append(engine))
    monkeypatch.setattr(container_module, "get_session_factory", lambda engine: fake_session_factory)
    monkeypatch.setattr(container_module, "create_memory_index", lambda **kwargs: SimpleNamespace())

    config = Config(
        database_url="postgresql+psycopg://fake/forwin",
        minimax_api_key="",
        retrieval_backend="hash",
        artifact_root="/tmp/forwin-runtime-container-test-artifacts",
    )

    container = RuntimeContainer.from_config(config)
    services = container.services()

    assert container.services() is services
    assert services.config is config
    assert services.engine is fake_engine
    assert services.session_factory is fake_session_factory
    assert init_calls == [fake_engine]

    orchestrator = container.build_writing_orchestrator(progress_callback=lambda *_args: None)

    assert orchestrator.services is services
    assert orchestrator.config is config
    assert orchestrator.writer is services.writer
    assert orchestrator.review_hub is services.review_hub
    assert orchestrator.arc_envelope_manager is services.arc_envelope_manager
    assert services.production_scheduler is not None


def test_runtime_container_builds_callback_bound_production_scheduler(monkeypatch) -> None:
    from forwin.runtime import container as container_module
    from forwin.runtime.container import RuntimeContainer

    fake_engine = _FakeEngine()
    fake_session_factory = object()
    monkeypatch.setattr(container_module, "get_engine", lambda database_url: fake_engine)
    monkeypatch.setattr(container_module, "init_db", lambda engine: None)
    monkeypatch.setattr(container_module, "get_session_factory", lambda engine: fake_session_factory)
    monkeypatch.setattr(container_module, "create_memory_index", lambda **kwargs: SimpleNamespace())

    config = Config(
        database_url="postgresql+psycopg://fake/forwin",
        minimax_api_key="",
        retrieval_backend="hash",
        artifact_root="/tmp/forwin-runtime-container-test-artifacts",
    )
    container = RuntimeContainer.from_config(config)

    scheduler = container.build_production_scheduler(
        runtime_config_provider=lambda: config,
        display_datetime=lambda value: "",
        persist_project_automation=lambda *args, **kwargs: None,
        create_generation_task=lambda **kwargs: "task-1",
        create_continue_generation_task=lambda **kwargs: "task-2",
        active_generation_task_error_cls=RuntimeError,
        generation_terminal_statuses={"completed"},
        upload_terminal_statuses={"succeeded"},
    )

    assert scheduler.session_factory is fake_session_factory
    assert scheduler.config is config


def test_writing_orchestrator_keeps_legacy_config_constructor(monkeypatch) -> None:
    from forwin.orchestrator.loop import WritingOrchestrator
    from forwin.runtime.container import RuntimeContainer
    from forwin.runtime.services import RuntimeServices

    config = Config(database_url="postgresql+psycopg://fake/forwin", minimax_api_key="")
    services = SimpleNamespace(
        config=config,
        engine=_FakeEngine(),
        session_factory=object(),
        llm_client=SimpleNamespace(close=lambda: None),
        skill_runtime=SimpleNamespace(registry=None, router=None, prompt_layer_builder=None),
        arc_director=SimpleNamespace(),
        book_genesis=SimpleNamespace(),
        subworld_manager=SimpleNamespace(),
        retrieval_broker=SimpleNamespace(),
        artifact_store=SimpleNamespace(),
        stage_analyzer=SimpleNamespace(),
        pacing_strategist=SimpleNamespace(),
        replan_governor=SimpleNamespace(),
        npc_intent_generator=SimpleNamespace(),
        world_simulator=SimpleNamespace(),
        arc_envelope_manager=SimpleNamespace(),
        context_assembler=SimpleNamespace(),
        review_hub=SimpleNamespace(),
        writer=SimpleNamespace(),
        provisional_writer=SimpleNamespace(),
        repair_policy=SimpleNamespace(),
        repair_verifier=SimpleNamespace(),
        final_acceptance_gate=SimpleNamespace(),
    )

    class FakeContainer:
        @classmethod
        def from_config(cls, received_config):
            assert received_config is config
            return cls()

        def services(self):
            return services

    monkeypatch.setattr("forwin.orchestrator.loop.RuntimeContainer", FakeContainer, raising=False)

    orchestrator = WritingOrchestrator(config)

    assert orchestrator.services is services
    assert orchestrator.config is config
    assert isinstance(orchestrator.services, RuntimeServices) or orchestrator.services is services
