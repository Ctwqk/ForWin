from __future__ import annotations

import unittest
from pathlib import Path
from tempfile import TemporaryDirectory

import forwin.api as api_module
from forwin.config import Config
from forwin.book_genesis import BookGenesisService
from forwin.llm.router import LLMCallIntent, LLMCallRouter, RoutedModelAdapter
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.runtime_settings import RuntimeSettingsStore


class OrdinaryAdapter:
    provider = "ordinary"
    model = "ordinary-model"
    base_url = "http://ordinary.invalid/v1"
    profile_id = "ordinary-profile"
    profile_name = "Ordinary"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return '{"source":"ordinary"}'

    def drain_model_fallback_events(self):
        return []

    def close(self) -> None:
        pass


class FakeCodexClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, *, intent: LLMCallIntent, **kwargs) -> str:
        self.calls.append({"messages": messages, "intent": intent, "kwargs": kwargs})
        if self.fail:
            raise RuntimeError("codex bridge unavailable")
        return '{"source":"codex"}'


class CapturingLLM:
    provider = "capture"
    model = "capture-model"
    base_url = "http://capture.invalid"
    profile_id = ""
    profile_name = ""
    api_key = "capture-key"

    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []

    def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return "{}"

    def drain_model_fallback_events(self):
        return []

    def close(self) -> None:
        pass


class LLMRouterTests(unittest.TestCase):
    def test_chapter_plan_materialization_never_uses_codex(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        router = LLMCallRouter(ordinary_adapter=ordinary, codex_client=codex, codex_enabled=True)

        result = router.chat(
            [{"role": "user", "content": "plan chapters"}],
            intent=LLMCallIntent(task_family="chapter_plan_materialization", stage_key="launch_arc_1"),
        )

        self.assertEqual(result, '{"source":"ordinary"}')
        self.assertEqual(len(codex.calls), 0)
        self.assertEqual(len(ordinary.calls), 1)
        self.assertEqual(ordinary.calls[0]["kwargs"]["task_family"], "chapter_plan_materialization")
        self.assertEqual(ordinary.calls[0]["kwargs"]["stage_key"], "launch_arc_1")

    def test_codex_primary_for_genesis_when_enabled(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        router = LLMCallRouter(ordinary_adapter=ordinary, codex_client=codex, codex_enabled=True)

        result = router.chat(
            [{"role": "user", "content": "world"}],
            intent=LLMCallIntent(task_family="genesis", stage_key="world"),
        )

        self.assertEqual(result, '{"source":"codex"}')
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(len(ordinary.calls), 0)

    def test_codex_failure_falls_back_to_ordinary_and_records_event(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient(fail=True)
        router = LLMCallRouter(ordinary_adapter=ordinary, codex_client=codex, codex_enabled=True)

        result = router.chat(
            [{"role": "user", "content": "review"}],
            intent=LLMCallIntent(task_family="reviewer", stage_key="chapter_review"),
        )

        self.assertEqual(result, '{"source":"ordinary"}')
        self.assertEqual(ordinary.calls[0]["kwargs"]["task_family"], "reviewer")
        self.assertEqual(ordinary.calls[0]["kwargs"]["stage_key"], "chapter_review")
        events = router.drain_model_fallback_events()
        self.assertEqual(events[0]["from_backend"], "codex_bridge")
        self.assertEqual(events[0]["to_backend"], "ordinary")

    def test_routed_adapter_accepts_intent_kwargs_and_defaults_to_ordinary(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        adapter = RoutedModelAdapter(LLMCallRouter(ordinary_adapter=ordinary, codex_client=codex, codex_enabled=True))

        ordinary_result = adapter.chat([{"role": "user", "content": "no intent"}])
        codex_result = adapter.chat(
            [{"role": "user", "content": "with intent"}],
            task_family="writer",
            stage_key="chapter_draft",
        )

        self.assertEqual(ordinary_result, '{"source":"ordinary"}')
        self.assertEqual(codex_result, '{"source":"codex"}')
        self.assertEqual(len(codex.calls), 1)

    def test_config_exposes_codex_bridge_defaults(self) -> None:
        config = Config(db_path=":memory:")

        self.assertFalse(config.codex_enabled)
        self.assertEqual(config.codex_bridge_url, "http://host.docker.internal:8897")
        self.assertEqual(config.codex_max_concurrent, 1)
        self.assertEqual(config.codex_sync_timeout_seconds, 90.0)
        self.assertEqual(config.codex_job_timeout_seconds, 900.0)

    def test_api_genesis_service_uses_routed_adapter_when_codex_enabled(self) -> None:
        old_config = api_module._config
        old_runtime_settings = api_module._runtime_settings
        try:
            with TemporaryDirectory() as tmpdir:
                api_module._config = Config(
                    db_path=str(Path(tmpdir) / "forwin.db"),
                    minimax_api_key="ordinary-key",
                    minimax_base_url="http://ordinary.invalid/v1",
                    minimax_model="ordinary-model",
                    codex_enabled=True,
                    codex_bridge_url="http://codex.invalid",
                )
                api_module._runtime_settings = RuntimeSettingsStore(
                    str(Path(tmpdir) / "runtime.json"),
                    default_api_key="ordinary-key",
                    default_base_url="http://ordinary.invalid/v1",
                    default_model="ordinary-model",
                )

                service = api_module._build_genesis_service()

                self.assertIsInstance(service.llm_client, RoutedModelAdapter)
        finally:
            api_module._config = old_config
            api_module._runtime_settings = old_runtime_settings

    def test_orchestrator_uses_routed_adapter_when_codex_enabled(self) -> None:
        with TemporaryDirectory() as tmpdir:
            config = Config(
                db_path=str(Path(tmpdir) / "forwin.db"),
                minimax_api_key="ordinary-key",
                minimax_base_url="http://ordinary.invalid/v1",
                minimax_model="ordinary-model",
                codex_enabled=True,
                codex_bridge_url="http://codex.invalid",
            )
            orchestrator = WritingOrchestrator(config)
            try:
                self.assertIsInstance(orchestrator.llm_client, RoutedModelAdapter)
            finally:
                orchestrator.llm_client.close()

    def test_book_genesis_marks_stage_calls_for_codex_and_excludes_launch_arc(self) -> None:
        client = CapturingLLM()
        service = BookGenesisService(llm_client=client)

        service._call_json_with_trace(
            messages=[{"role": "user", "content": "world"}],
            fallback={},
            stage_key="world",
        )
        service._call_json_with_trace(
            messages=[{"role": "user", "content": "arc chapters"}],
            fallback={"chapters": []},
            stage_key="launch_arc_1",
        )

        self.assertEqual(client.calls[0]["kwargs"]["task_family"], "genesis")
        self.assertEqual(client.calls[0]["kwargs"]["stage_key"], "world")
        self.assertTrue(client.calls[0]["kwargs"]["codex_allowed"])
        self.assertEqual(client.calls[1]["kwargs"]["task_family"], "chapter_plan_materialization")
        self.assertFalse(client.calls[1]["kwargs"]["codex_allowed"])


if __name__ == "__main__":
    unittest.main()
