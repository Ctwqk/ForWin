from __future__ import annotations

import unittest

import forwin.api as api_module
from forwin.config import InfrastructureConfig
from forwin.genesis import BookGenesisService
from forwin.llm.router import LLMCallIntent, LLMCallRouter, RoutedModelAdapter
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.generation.pipeline import ChapterPipeline
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy


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


class FailingOrdinaryAdapter(OrdinaryAdapter):
    def chat(self, messages, **kwargs) -> str:
        self.calls.append({"messages": messages, "kwargs": kwargs})
        raise RuntimeError("ordinary unavailable")


class FakeCodexClient:
    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail
        self.calls: list[dict[str, object]] = []
        self.last_call_trace: dict[str, object] = {}

    def chat(self, messages, *, intent: LLMCallIntent, **kwargs) -> str:
        self.calls.append({"messages": messages, "intent": intent, "kwargs": kwargs})
        if self.fail:
            raise RuntimeError("codex bridge unavailable")
        actual_model = str(kwargs.get("model") or "codex-default")
        self.last_call_trace = {
            "actual_model": actual_model,
            "raw_events": [{"type": "turn.completed", "usage": {"output_tokens": 12}}],
            "returncode": 0,
        }
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

    def test_codex_primary_for_structured_planning_when_enabled(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        router = LLMCallRouter(ordinary_adapter=ordinary, codex_client=codex, codex_enabled=True)

        result = router.chat(
            [{"role": "user", "content": "plan arc"}],
            intent=LLMCallIntent(
                task_family="planning",
                stage_key="arc_plan",
                output_schema={"type": "object"},
            ),
        )

        self.assertEqual(result, '{"source":"codex"}')
        self.assertEqual(len(codex.calls), 1)
        self.assertEqual(len(ordinary.calls), 0)

    def test_codex_primary_for_chapter_review_form_when_enabled(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        router = LLMCallRouter(ordinary_adapter=ordinary, codex_client=codex, codex_enabled=True)

        result = router.chat(
            [{"role": "user", "content": "review form"}],
            intent=LLMCallIntent(
                task_family="chapter_review_form",
                stage_key="chapter_review_form",
                output_schema={"type": "object"},
            ),
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
            task_family="reviewer",
            stage_key="chapter_review",
        )

        self.assertEqual(ordinary_result, '{"source":"ordinary"}')
        self.assertEqual(codex_result, '{"source":"codex"}')
        self.assertEqual(len(codex.calls), 1)

    def test_writer_prose_uses_ordinary_before_codex_fallback(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        adapter = RoutedModelAdapter(LLMCallRouter(ordinary_adapter=ordinary, codex_client=codex, codex_enabled=True))

        result = adapter.chat(
            [{"role": "user", "content": "write chapter"}],
            task_family="writer",
            stage_key="chapter_draft",
        )

        self.assertEqual(result, '{"source":"ordinary"}')
        self.assertEqual(len(ordinary.calls), 1)
        self.assertEqual(len(codex.calls), 0)

    def test_write_chapter_family_uses_ordinary_before_codex_fallback(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        adapter = RoutedModelAdapter(LLMCallRouter(ordinary_adapter=ordinary, codex_client=codex, codex_enabled=True))

        result = adapter.chat(
            [{"role": "user", "content": "write chapter"}],
            task_family="write_chapter",
            stage_key="chapter_draft",
        )

        self.assertEqual(result, '{"source":"ordinary"}')
        self.assertEqual(len(ordinary.calls), 1)
        self.assertEqual(len(codex.calls), 0)

    def test_writer_prose_falls_back_to_codex_when_ordinary_fails(self) -> None:
        ordinary = FailingOrdinaryAdapter()
        codex = FakeCodexClient()
        adapter = RoutedModelAdapter(
            LLMCallRouter(
                ordinary_adapter=ordinary,
                codex_client=codex,
                codex_enabled=True,
                codex_default_model="gpt-5.3-codex-spark",
            )
        )

        result = adapter.chat(
            [{"role": "user", "content": "write chapter"}],
            task_family="writer",
            stage_key="chapter_draft",
        )

        self.assertEqual(result, '{"source":"codex"}')
        self.assertEqual(adapter.last_call_result.backend, "codex_bridge")
        self.assertTrue(adapter.last_call_result.fallback_used)
        self.assertEqual(codex.calls[0]["kwargs"]["model"], "gpt-5.3-codex-spark")
        event = adapter.drain_model_fallback_events()[0]
        self.assertEqual(event["from_backend"], "ordinary")
        self.assertEqual(event["to_backend"], "codex_bridge")

    def test_codex_primary_tasks_use_configured_codex_model(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        router = LLMCallRouter(
            ordinary_adapter=ordinary,
            codex_client=codex,
            codex_enabled=True,
            codex_default_model="gpt-5.3-codex-spark",
        )

        result = router.chat(
            [{"role": "user", "content": "review"}],
            intent=LLMCallIntent(task_family="reviewer", stage_key="chapter_review"),
        )

        self.assertEqual(result, '{"source":"codex"}')
        self.assertEqual(codex.calls[0]["kwargs"]["model"], "gpt-5.3-codex-spark")

    def test_codex_result_trace_keeps_model_and_raw_bridge_events(self) -> None:
        router = LLMCallRouter(
            ordinary_adapter=OrdinaryAdapter(),
            codex_client=FakeCodexClient(),
            codex_enabled=True,
            codex_default_model="gpt-5.3-codex-spark",
        )

        result = router.chat_with_result(
            [{"role": "user", "content": "review"}],
            intent=LLMCallIntent(task_family="review", stage_key="spark_pause_gate"),
        )

        self.assertEqual(result.backend, "codex_bridge")
        self.assertEqual(result.trace["model"], "gpt-5.3-codex-spark")
        self.assertEqual(result.trace["actual_model"], "gpt-5.3-codex-spark")
        self.assertEqual(result.trace["raw_events"][0]["type"], "turn.completed")
        self.assertEqual(result.trace["returncode"], 0)

    def test_total_failure_replaces_prior_success_trace(self) -> None:
        ordinary = FailingOrdinaryAdapter()
        codex = FakeCodexClient()
        adapter = RoutedModelAdapter(
            LLMCallRouter(
                ordinary_adapter=ordinary,
                codex_client=codex,
                codex_enabled=True,
                codex_default_model="gpt-5.3-codex-spark",
            )
        )
        adapter.chat(
            [{"role": "user", "content": "first"}],
            task_family="review",
            stage_key="spark_pause_gate",
        )
        self.assertEqual(adapter.last_call_result.backend, "codex_bridge")

        codex.fail = True
        with self.assertRaisesRegex(RuntimeError, "ordinary unavailable"):
            adapter.chat(
                [{"role": "user", "content": "second"}],
                task_family="review",
                stage_key="spark_pause_gate",
            )

        self.assertIsNotNone(adapter.last_call_result)
        self.assertEqual(adapter.last_call_result.backend, "failed")
        self.assertIn("ordinary unavailable", adapter.last_call_result.trace["ordinary_error"])
        failed_codex_trace = adapter.last_call_result.trace["failed_codex_trace"]
        self.assertNotIn("raw_events", failed_codex_trace)

    def test_preferred_codex_model_keeps_bridge_enabled_for_repair(self) -> None:
        ordinary = OrdinaryAdapter()
        codex = FakeCodexClient()
        adapter = RoutedModelAdapter(
            LLMCallRouter(
                ordinary_adapter=ordinary,
                codex_client=codex,
                codex_enabled=True,
                codex_default_model="gpt-5.3-codex-spark",
            )
        )

        result = adapter.chat(
            [{"role": "user", "content": "rewrite"}],
            task_family="writer",
            stage_key="chapter_rewrite",
            preferred_provider_kind="spark",
            preferred_model="gpt-5.3-codex-spark",
        )

        self.assertEqual(result, '{"source":"codex"}')
        self.assertEqual(len(ordinary.calls), 0)
        self.assertEqual(codex.calls[0]["kwargs"]["model"], "gpt-5.3-codex-spark")

    def test_config_exposes_codex_bridge_defaults(self) -> None:
        config = InfrastructureConfig(database_url=postgres_test_url())

        self.assertFalse(config.codex_enabled)
        self.assertEqual(config.codex_bridge_url, "http://host.docker.internal:8897")
        self.assertEqual(config.codex_max_concurrent, 1)
        self.assertEqual(config.codex_sync_timeout_seconds, 90.0)
        self.assertEqual(config.codex_job_timeout_seconds, 900.0)

    def test_api_genesis_service_uses_routed_adapter_when_codex_enabled(self) -> None:
        old_config = api_module._config
        try:
            api_module._config = InfrastructureConfig(
                database_url=postgres_test_url("forwin"),
                minimax_api_key="ordinary-key",
                minimax_base_url="http://ordinary.invalid/v1",
                minimax_model="ordinary-model",
                codex_enabled=True,
                codex_bridge_url="http://codex.invalid",
            )
            service = api_module._build_genesis_service()

            self.assertIsInstance(service.llm_client, RoutedModelAdapter)
        finally:
            api_module._config = old_config

    def test_pipeline_uses_routed_adapter_when_codex_enabled(self) -> None:
        config = InfrastructureConfig(
            database_url=postgres_test_url("forwin"),
            minimax_api_key="ordinary-key",
            minimax_base_url="http://ordinary.invalid/v1",
            minimax_model="ordinary-model",
            codex_enabled=True,
            codex_bridge_url="http://codex.invalid",
        )
        container = RuntimeContainer.from_config(
            config,
            policy=RuntimePolicy.for_profile("standard"),
            role="api",
        )
        services = container.services()
        pipeline = container.build_chapter_pipeline()
        try:
            self.assertIsInstance(pipeline.llm_client, RoutedModelAdapter)
        finally:
            services.llm_client.close()
            services.engine.dispose()

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
