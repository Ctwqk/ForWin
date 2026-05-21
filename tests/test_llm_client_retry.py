from __future__ import annotations

import time
import unittest
from unittest.mock import patch

import httpx

from forwin.protocol.experience import ArcPayoffMap, ReaderPromise, RevelationLayer
from forwin.orchestrator.phase24 import _coerce_unit_float
from forwin.writer.llm_client import LLMClient


class LLMClientRetryTests(unittest.TestCase):
    def test_wall_timeout_interrupts_hung_http_post(self) -> None:
        class HangingHTTPClient:
            def __init__(self) -> None:
                self.closed = False

            def post(self, *_args, **_kwargs):  # noqa: ANN002, ANN003
                time.sleep(5)
                return httpx.Response(
                    200,
                    json={"choices": [{"message": {"content": "too-late"}}]},
                )

            def close(self) -> None:
                self.closed = True

        client = LLMClient(
            api_key="test-key",
            base_url="https://primary.example/v1",
            model="primary-model",
        )
        hanging_client = HangingHTTPClient()
        client.client = hanging_client  # type: ignore[assignment]

        try:
            started_at = time.perf_counter()
            with self.assertRaises(httpx.ReadTimeout):
                client._post_with_wall_timeout(  # noqa: SLF001
                    "https://primary.example/v1/chat/completions",
                    json={"model": "primary-model", "messages": []},
                    headers={},
                    timeout=httpx.Timeout(0.2, connect=0.2),
                )
            elapsed = time.perf_counter() - started_at
        finally:
            client.close()

        self.assertLess(elapsed, 1.5)
        self.assertTrue(hanging_client.closed)

    def test_reader_promise_coerces_unknown_ambiguity_mode_to_managed(self) -> None:
        promise = ReaderPromise.model_validate({"ambiguity_mode": "suggestive_opaque"})

        self.assertEqual(promise.ambiguity_mode, "managed")

    def test_reader_promise_coerces_numeric_tuning_fields_to_strings(self) -> None:
        promise = ReaderPromise.model_validate(
            {
                "acceptable_drag_level": 0.2,
                "acceptable_exposition_density": 0.3,
                "cliffhanger_aggressiveness": 0.8,
                "world_legibility_target": 0.7,
            }
        )

        self.assertEqual(promise.acceptable_drag_level, "0.2")
        self.assertEqual(promise.acceptable_exposition_density, "0.3")
        self.assertEqual(promise.cliffhanger_aggressiveness, "0.8")
        self.assertEqual(promise.world_legibility_target, "0.7")

    def test_arc_payoff_map_coerces_llm_flexible_shapes(self) -> None:
        payoff_map = ArcPayoffMap.model_validate(
            {
                "macro_payoffs": [
                    {
                        "payoff_id": "p1",
                        "category": "item_acquisition",
                        "target_chapter_hint": 1,
                    },
                    {
                        "payoff_id": "p2",
                        "category": "threat_established",
                        "target_chapter_hint": 2,
                    },
                ],
                "awe_kit": [{"awe_type": "temporal_vastness", "summary": "钟声震颤"}],
                "ambiguity_constraints": [{"aspect": "罗盘来源", "rule": "只暗示"}],
            }
        )

        self.assertEqual(payoff_map.macro_payoffs[0].category, "power")
        self.assertEqual(payoff_map.macro_payoffs[0].target_chapter_hint, "1")
        self.assertEqual(payoff_map.macro_payoffs[1].category, "mystery")
        self.assertEqual(payoff_map.awe_kit[0], "temporal_vastness：钟声震颤")
        self.assertEqual(payoff_map.ambiguity_constraints[0], "罗盘来源：只暗示")

    def test_phase24_coerces_confidence_words(self) -> None:
        self.assertEqual(_coerce_unit_float("high", default=0.65), 0.85)
        self.assertEqual(_coerce_unit_float("medium", default=0.65), 0.65)
        self.assertEqual(_coerce_unit_float("low", default=0.65), 0.35)

    def test_revelation_layer_coerces_window_list_to_text(self) -> None:
        layer = RevelationLayer.model_validate(
            {
                "layer_id": "r1",
                "layer_type": "clue",
                "summary": "罗盘来源只暗示",
                "chapter_window": [1, 1],
            }
        )

        self.assertEqual(layer.chapter_window, "1-1")

    def test_retries_minimax_529_then_returns_content(self) -> None:
        client = LLMClient(
            api_key="test-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=2,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
        )
        calls = {"count": 0}

        def fake_post(url, **_kwargs):  # noqa: ANN001
            calls["count"] += 1
            request = httpx.Request("POST", url)
            if calls["count"] == 1:
                return httpx.Response(
                    529,
                    json={"error": "overloaded"},
                    request=request,
                )
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "ok"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post), patch(
                "forwin.writer.llm_client.time.sleep",
                return_value=None,
            ) as sleep:
                result = client.chat([{"role": "user", "content": "hello"}])
        finally:
            client.close()

        self.assertEqual(result, "ok")
        self.assertEqual(calls["count"], 2)
        sleep.assert_called_once_with(0.0)

    def test_records_attempt_telemetry_without_secrets(self) -> None:
        client = LLMClient(
            api_key="test-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=2,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
        )
        calls = {"count": 0}

        def fake_post(url, **kwargs):  # noqa: ANN001
            self.assertNotIn("Authorization", jsonable(kwargs["json"]))
            calls["count"] += 1
            request = httpx.Request("POST", url)
            if calls["count"] == 1:
                return httpx.Response(529, json={"error": "overloaded"}, request=request)
            return httpx.Response(
                200,
                headers={"x-request-id": "provider-123"},
                json={"choices": [{"message": {"content": "ok"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post), patch(
                "forwin.writer.llm_client.time.sleep",
                return_value=None,
            ):
                result = client.chat([{"role": "user", "content": "hello"}])
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "ok")
        self.assertEqual([item["http_status"] for item in attempts], [529, 200])
        self.assertEqual(attempts[0]["attempt_no"], 1)
        self.assertEqual(attempts[1]["provider_request_id"], "provider-123")
        self.assertEqual(attempts[1]["output_chars"], 2)
        self.assertEqual(attempts[0]["base_url_host"], "primary.example")
        serialized = str(attempts)
        self.assertNotIn("test-key", serialized)
        self.assertNotIn("Authorization", serialized)

    def test_attempt_telemetry_records_group_profile_category_and_final_success(self) -> None:
        client = LLMClient(
            api_key="primary-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=1,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            fallback_profiles=[
                {
                    "id": "backup-profile",
                    "name": "Backup",
                    "api_key": "backup-key",
                    "base_url": "https://backup.example/v1",
                    "model": "backup-model",
                }
            ],
        )

        def fake_post(url, **kwargs):  # noqa: ANN001
            request = httpx.Request("POST", url)
            if kwargs["json"]["model"] == "primary-model":
                return httpx.Response(529, json={"error": "overloaded"}, request=request)
            return httpx.Response(
                200,
                headers={"x-request-id": "backup-request"},
                json={"choices": [{"message": {"content": "backup-ok"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post), patch(
                "forwin.writer.llm_client.time.sleep",
                return_value=None,
            ):
                result = client.chat([{"role": "user", "content": "hello"}])
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "backup-ok")
        self.assertEqual(len({item["attempt_group_id"] for item in attempts}), 1)
        self.assertEqual(attempts[0]["error_category"], "provider_overload")
        self.assertTrue(attempts[0]["retryable"])
        self.assertTrue(attempts[0]["fallback_eligible"])
        self.assertTrue(attempts[0]["final_failure"])
        self.assertEqual(attempts[1]["profile_id"], "backup-profile")
        self.assertEqual(attempts[1]["profile_name"], "Backup")
        self.assertEqual(attempts[1]["model"], "backup-model")
        self.assertEqual(attempts[1]["provider_request_id"], "backup-request")
        self.assertEqual(attempts[1]["output_chars"], len("backup-ok"))
        self.assertEqual(attempts[1]["error_category"], "")
        self.assertFalse(attempts[1]["final_failure"])

    def test_does_not_retry_timeout_when_disabled(self) -> None:
        client = LLMClient(
            api_key="test-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=2,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
        )

        try:
            with patch.object(
                client.client,
                "post",
                side_effect=httpx.ReadTimeout("read timed out"),
            ):
                with self.assertRaises(httpx.ReadTimeout):
                    client.chat(
                        [{"role": "user", "content": "hello"}],
                        retry_on_timeout=False,
                    )
        finally:
            client.close()

    def test_does_not_fallback_after_timeout_when_retry_disabled(self) -> None:
        client = LLMClient(
            api_key="primary-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=2,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            fallback_profiles=[
                {
                    "api_key": "backup-key",
                    "base_url": "https://backup.example/v1",
                    "model": "backup-model",
                }
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            if kwargs["json"]["model"] == "primary-model":
                raise httpx.ReadTimeout("read timed out")
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "backup-ok"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                with self.assertRaises(httpx.ReadTimeout):
                    client.chat(
                        [{"role": "user", "content": "hello"}],
                        retry_on_timeout=False,
                    )
        finally:
            client.close()

        self.assertEqual(calls, ["primary-model"])
        self.assertEqual(client.drain_model_fallback_events(), [])

    def test_wall_timeout_interrupts_hung_http_post(self) -> None:
        client = LLMClient(
            api_key="test-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=1,
        )
        original_client = client.client

        def hung_post(url, **_kwargs):  # noqa: ANN001
            time.sleep(0.2)
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "late"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=hung_post):
                started = time.monotonic()
                with self.assertRaises(httpx.ReadTimeout):
                    client._post_with_wall_timeout(
                        "https://primary.example/v1/chat/completions",
                        json={},
                        headers={},
                        timeout=httpx.Timeout(0.05),
                    )
                elapsed = time.monotonic() - started
        finally:
            client.close()

        self.assertLess(elapsed, 0.15)
        self.assertIsNot(client.client, original_client)

    def test_falls_back_to_next_profile_after_retryable_failures(self) -> None:
        client = LLMClient(
            api_key="primary-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=2,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            fallback_profiles=[
                {
                    "api_key": "backup-key",
                    "base_url": "https://backup.example/v1",
                    "model": "backup-model",
                }
            ],
        )
        calls: list[tuple[str, str]] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append((url, kwargs["json"]["model"]))
            request = httpx.Request("POST", url)
            if kwargs["json"]["model"] == "primary-model":
                return httpx.Response(529, json={"error": "overloaded"}, request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "backup-ok"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post), patch(
                "forwin.writer.llm_client.time.sleep",
                return_value=None,
            ):
                result = client.chat([{"role": "user", "content": "hello"}])
        finally:
            client.close()

        self.assertEqual(result, "backup-ok")
        self.assertEqual([model for _url, model in calls], ["primary-model", "primary-model", "backup-model"])
        events = client.drain_model_fallback_events()
        self.assertEqual(events[0]["from_model"], "primary-model")
        self.assertEqual(events[0]["to_model"], "backup-model")

    def test_falls_back_after_timeout_retries_are_exhausted(self) -> None:
        client = LLMClient(
            api_key="primary-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=2,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            fallback_profiles=[
                {
                    "api_key": "backup-key",
                    "base_url": "https://backup.example/v1",
                    "model": "backup-model",
                }
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            if kwargs["json"]["model"] == "primary-model":
                raise httpx.ReadTimeout("read timed out")
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "timeout-backup-ok"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post), patch(
                "forwin.writer.llm_client.time.sleep",
                return_value=None,
            ):
                result = client.chat([{"role": "user", "content": "hello"}])
        finally:
            client.close()

        self.assertEqual(result, "timeout-backup-ok")
        self.assertEqual(calls, ["primary-model", "primary-model", "backup-model"])
        self.assertEqual(client.drain_model_fallback_events()[0]["to_model"], "backup-model")

    def test_does_not_fallback_for_non_retryable_http_error(self) -> None:
        client = LLMClient(
            api_key="primary-key",
            base_url="https://primary.example/v1",
            model="primary-model",
            retry_attempts=2,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            fallback_profiles=[
                {
                    "api_key": "backup-key",
                    "base_url": "https://backup.example/v1",
                    "model": "backup-model",
                }
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            request = httpx.Request("POST", url)
            return httpx.Response(400, json={"error": "bad request"}, request=request)

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                with self.assertRaises(httpx.HTTPStatusError):
                    client.chat([{"role": "user", "content": "hello"}])
        finally:
            client.close()

        self.assertEqual(calls, ["primary-model"])

    def test_kimi_k25_disables_thinking_and_omits_temperature(self) -> None:
        client = LLMClient(
            api_key="kimi-key",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k2.5",
            retry_attempts=1,
        )
        payloads: list[dict] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            payloads.append(kwargs["json"])
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                result = client.chat(
                    [{"role": "user", "content": "只输出 JSON"}],
                    temperature=0.2,
                    response_format={"type": "json_object"},
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "{\"ok\":true}")
        self.assertNotIn("temperature", payloads[0])
        self.assertEqual(payloads[0]["thinking"], {"type": "disabled"})
        self.assertEqual(attempts[0]["temperature"], 0.6)
        self.assertEqual(attempts[0]["requested_temperature"], 0.2)

    def test_kimi_k25_raises_small_max_tokens_for_reasoning_budget(self) -> None:
        client = LLMClient(
            api_key="kimi-key",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k2.5",
            retry_attempts=1,
        )
        payloads: list[dict] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            payloads.append(kwargs["json"])
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                client.chat(
                    [{"role": "user", "content": "只输出 JSON"}],
                    temperature=1.0,
                    max_tokens=900,
                    response_format={"type": "json_object"},
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(payloads[0]["max_tokens"], 1800)
        self.assertEqual(attempts[0]["max_tokens"], 1800)

    def test_kimi_k25_respects_explicit_stage_timeout(self) -> None:
        client = LLMClient(
            api_key="kimi-key",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k2.5",
            retry_attempts=1,
        )
        timeouts: list[float] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            timeouts.append(float(kwargs["timeout"].read))
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                client.chat(
                    [{"role": "user", "content": "只输出 JSON"}],
                    timeout_seconds=45,
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(timeouts[0], 45.0)
        self.assertEqual(attempts[0]["timeout_seconds"], 45.0)

    def test_minimax_payload_uses_current_openai_compatible_parameters(self) -> None:
        client = LLMClient(
            api_key="minimax-key",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            retry_attempts=1,
        )
        payloads: list[dict] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            payloads.append(kwargs["json"])
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{\"ok\":true}"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                result = client.chat(
                    [
                        {"role": "system", "content": "只输出 JSON。"},
                        {"role": "system", "content": "不要解释。"},
                        {"role": "user", "content": "返回 {\"ok\": true}。"},
                    ],
                    temperature=0.0,
                    max_tokens=5000,
                    response_format={"type": "json_object"},
                    task_family="writer",
                    stage_key="scene_breakdown",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "{\"ok\":true}")
        self.assertEqual(payloads[0]["max_completion_tokens"], 2048)
        self.assertNotIn("max_tokens", payloads[0])
        self.assertNotIn("response_format", payloads[0])
        self.assertEqual(payloads[0]["temperature"], 0.1)
        self.assertEqual(
            payloads[0]["messages"],
            [
                {"role": "system", "content": "只输出 JSON。\n\n不要解释。"},
                {"role": "user", "content": "返回 {\"ok\": true}。"},
            ],
        )
        self.assertEqual(attempts[0]["max_tokens"], 2048)
        self.assertEqual(attempts[0]["requested_max_tokens"], 5000)
        self.assertEqual(attempts[0]["temperature"], 0.1)
        self.assertEqual(attempts[0]["requested_temperature"], 0.0)
        self.assertEqual(attempts[0]["response_format"], {})

    def test_non_retryable_http_error_records_provider_body_preview(self) -> None:
        client = LLMClient(
            api_key="test-key",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k2.5",
            retry_attempts=1,
        )

        def fake_post(url, **_kwargs):  # noqa: ANN001
            request = httpx.Request("POST", url)
            return httpx.Response(
                400,
                json={
                    "error": {
                        "message": "invalid temperature: only 1 is allowed for this model",
                        "type": "invalid_request_error",
                    }
                },
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                with self.assertRaises(httpx.HTTPStatusError):
                    client.chat(
                        [{"role": "user", "content": "hello"}],
                        temperature=0.2,
                    )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertIn("invalid temperature", attempts[0]["error_message"])
        self.assertNotIn("test-key", attempts[0]["error_message"])

    def test_critical_extraction_routes_to_spark_then_kimi_and_skips_minimax(self) -> None:
        client = LLMClient(
            api_key="minimax-key",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            retry_attempts=1,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            fallback_profiles=[
                {
                    "id": "spark",
                    "name": "GPT-5.3-Codex-Spark",
                    "api_key": "spark-key",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5.3-codex-spark",
                },
                {
                    "id": "kimi",
                    "name": "Kimi K2.5",
                    "api_key": "kimi-key",
                    "base_url": "https://api.moonshot.cn/v1",
                    "model": "kimi-k2.5",
                },
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            request = httpx.Request("POST", url)
            if kwargs["json"]["model"] == "gpt-5.3-codex-spark":
                return httpx.Response(529, json={"error": "overloaded"}, request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{\"ok\": true}"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post), patch(
                "forwin.writer.llm_client.time.sleep",
                return_value=None,
            ):
                result = client.chat(
                    [{"role": "user", "content": "抽取 canon 事件"}],
                    response_format={"type": "json_object"},
                    task_family="writer",
                    stage_key="state_event_extraction",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "{\"ok\": true}")
        self.assertEqual(calls, ["gpt-5.3-codex-spark", "kimi-k2.5"])
        self.assertEqual([item["stage_key"] for item in attempts], ["state_event_extraction", "state_event_extraction"])
        self.assertEqual(attempts[0]["llm_task_route"], "canon_extraction")

    def test_feedback_analysis_routes_spark_then_kimi_then_minimax(self) -> None:
        client = LLMClient(
            api_key="kimi-key",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k2.5",
            retry_attempts=1,
            fallback_profiles=[
                {
                    "id": "minimax",
                    "name": "MiniMax",
                    "api_key": "minimax-key",
                    "base_url": "https://api.minimaxi.com/v1",
                    "model": "MiniMax-M2.7",
                },
                {
                    "id": "spark",
                    "name": "GPT-5.3-Codex-Spark",
                    "api_key": "spark-key",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5.3-codex-spark",
                },
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{\"signals\": []}"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                result = client.chat(
                    [{"role": "user", "content": "分析评论信号"}],
                    response_format={"type": "json_object"},
                    task_family="phase4",
                    stage_key="comment_analysis",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "{\"signals\": []}")
        self.assertEqual(calls, ["gpt-5.3-codex-spark"])
        self.assertEqual(attempts[0]["llm_task_route"], "feedback_analysis")
        self.assertEqual(attempts[0]["candidate_chain"][0]["provider_kind"], "kimi")

    def test_chapter_prose_routes_to_spark_then_kimi_and_skips_minimax(self) -> None:
        client = LLMClient(
            api_key="minimax-key",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            retry_attempts=1,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            fallback_profiles=[
                {
                    "api_key": "kimi-key",
                    "base_url": "https://api.moonshot.cn/v1",
                    "model": "kimi-k2.5",
                },
                {
                    "api_key": "spark-key",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5.3-codex-spark",
                },
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            request = httpx.Request("POST", url)
            if kwargs["json"]["model"] == "gpt-5.3-codex-spark":
                return httpx.Response(503, json={"error": "busy"}, request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "章节正文"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post), patch(
                "forwin.writer.llm_client.time.sleep",
                return_value=None,
            ):
                result = client.chat(
                    [{"role": "user", "content": "写第 1 章"}],
                    task_family="writer",
                    stage_key="chapter_draft",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "章节正文")
        self.assertEqual(calls, ["gpt-5.3-codex-spark", "kimi-k2.5"])
        self.assertEqual(attempts[0]["llm_task_route"], "prose_generation")

    def test_chapter_prose_keeps_deepseek_as_fallback_when_kimi_is_rate_limited(self) -> None:
        client = LLMClient(
            api_key="minimax-key",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            retry_attempts=1,
            retry_initial_delay_seconds=0,
            retry_max_delay_seconds=0,
            fallback_profiles=[
                {
                    "id": "kimi",
                    "name": "Kimi",
                    "api_key": "kimi-key",
                    "base_url": "https://api.moonshot.cn/v1",
                    "model": "kimi-k2.5",
                },
                {
                    "id": "deepseek",
                    "name": "DeepSeek",
                    "api_key": "deepseek-key",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                },
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            request = httpx.Request("POST", url)
            if kwargs["json"]["model"] == "kimi-k2.5":
                return httpx.Response(429, json={"error": "rate limited"}, request=request)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "DeepSeek 正文"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                result = client.chat(
                    [{"role": "user", "content": "写第 1 章"}],
                    task_family="writer",
                    stage_key="chapter_draft",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "DeepSeek 正文")
        self.assertEqual(calls, ["kimi-k2.5", "deepseek-chat"])
        self.assertEqual(attempts[0]["error_category"], "rate_limit")
        self.assertEqual(attempts[1]["profile_id"], "deepseek")
        skipped_reasons = [
            item["reason"]
            for attempt in attempts
            for item in attempt["skipped_profiles"]
        ]
        self.assertNotIn("replaced_by_kimi", skipped_reasons)

    def test_writer_preview_allows_minimax_as_low_risk_fallback(self) -> None:
        client = LLMClient(
            api_key="minimax-key",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            retry_attempts=1,
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "标题\n\n正文"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                result = client.chat(
                    [{"role": "user", "content": "预览"}],
                    task_family="writer",
                    stage_key="writer_preview_fallback",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(result, "标题\n\n正文")
        self.assertEqual(calls, ["MiniMax-M2.7"])
        self.assertEqual(attempts[0]["llm_task_route"], "writer_preview")

    def test_launch_arc_routes_as_low_risk_planning_for_minimax_fallback(self) -> None:
        client = LLMClient(
            api_key="minimax-key",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            retry_attempts=1,
        )
        try:
            route = client._llm_task_route(
                task_family="genesis",
                stage_key="launch_arc_1",
                response_format={"type": "json_object"},
            )
            routed = client._route_profiles(
                client._request_profiles(),
                task_family="genesis",
                stage_key="launch_arc_1",
                response_format={"type": "json_object"},
            )
        finally:
            client.close()

        self.assertEqual(route, "planning_json_low_risk")
        self.assertEqual(routed[0]["model"], "MiniMax-M2.7")

    def test_minimax_only_canon_route_fails_without_call(self) -> None:
        client = LLMClient(
            api_key="minimax-key",
            base_url="https://api.minimaxi.com/v1",
            model="MiniMax-M2.7",
            retry_attempts=1,
        )
        try:
            with self.assertRaises(RuntimeError):
                client.chat(
                    [{"role": "user", "content": "抽 canon"}],
                    response_format={"type": "json_object"},
                    task_family="writer",
                    stage_key="state_event_extraction",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(attempts[0]["error_category"], "no_usable_profile")
        self.assertEqual(attempts[0]["skipped_profiles"][0]["reason"], "route_not_allowed")

    def test_primary_deepseek_is_honored_even_when_kimi_fallback_exists(self) -> None:
        client = LLMClient(
            api_key="deepseek-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            retry_attempts=1,
            fallback_profiles=[
                {
                    "id": "gemini",
                    "name": "Gemini",
                    "api_key": "gemini-key",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    "model": "gemini-2.5-pro",
                },
                {
                    "id": "kimi",
                    "name": "Kimi",
                    "api_key": "kimi-key",
                    "base_url": "https://api.moonshot.cn/v1",
                    "model": "kimi-k2.5",
                },
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{\"ok\": true}"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                client.chat(
                    [{"role": "user", "content": "规划"}],
                    response_format={"type": "json_object"},
                    task_family="planning",
                    stage_key="chapter_plan",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(calls, ["deepseek-chat"])
        skipped = attempts[0]["skipped_profiles"]
        self.assertIn("replaced_by_deepseek", [item["reason"] for item in skipped])

    def test_primary_deepseek_excludes_kimi_fallback(self) -> None:
        client = LLMClient(
            api_key="deepseek-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            retry_attempts=1,
            fallback_profiles=[
                {
                    "id": "kimi",
                    "name": "Kimi",
                    "api_key": "kimi-key",
                    "base_url": "https://api.moonshot.cn/v1",
                    "model": "kimi-k2.5",
                },
            ],
        )
        try:
            route_result = client._route_profiles_with_metadata(
                client._request_profiles(),
                response_format={"type": "json_object"},
                task_family="planning",
                stage_key="chapter_plan",
            )
        finally:
            client.close()

        self.assertEqual([profile["model"] for profile in route_result["profiles"]], ["deepseek-chat"])
        self.assertEqual(route_result["skipped_profiles"][0]["reason"], "primary_deepseek_no_kimi_fallback")

    def test_repair_route_can_prefer_deepseek_over_spark(self) -> None:
        client = LLMClient(
            api_key="kimi-key",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k2.5",
            retry_attempts=1,
            fallback_profiles=[
                {
                    "id": "spark",
                    "name": "Codex Spark",
                    "api_key": "spark-key",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5.3-codex-spark",
                },
                {
                    "id": "deepseek",
                    "name": "DeepSeek",
                    "api_key": "deepseek-key",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-reasoner",
                },
            ],
        )
        try:
            route_result = client._route_profiles_with_metadata(
                client._request_profiles(),
                task_family="writer",
                stage_key="chapter_rewrite",
                preferred_provider_kind="deepseek",
                preferred_model="deepseek-reasoner",
            )
        finally:
            client.close()

        self.assertEqual(route_result["profiles"][0]["model"], "deepseek-reasoner")

    def test_repair_route_can_prefer_codex_spark_for_final_attempt(self) -> None:
        client = LLMClient(
            api_key="deepseek-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-reasoner",
            retry_attempts=1,
            fallback_profiles=[
                {
                    "id": "spark",
                    "name": "Codex Spark",
                    "api_key": "spark-key",
                    "base_url": "https://api.openai.com/v1",
                    "model": "gpt-5.3-codex-spark",
                },
            ],
        )
        try:
            route_result = client._route_profiles_with_metadata(
                client._request_profiles(),
                task_family="writer",
                stage_key="chapter_rewrite",
                preferred_provider_kind="spark",
                preferred_model="gpt-5.3-codex-spark",
            )
        finally:
            client.close()

        self.assertEqual(route_result["profiles"][0]["model"], "gpt-5.3-codex-spark")

    def test_deepseek_timeout_is_long_enough_for_generation_calls(self) -> None:
        client = LLMClient(
            api_key="deepseek-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            retry_attempts=1,
        )
        try:
            timeout = client._effective_timeout_for_profile(
                client._request_profiles()[0],
                httpx.Timeout(45.0, connect=10.0),
            )
        finally:
            client.close()

        self.assertGreaterEqual(timeout.read or 0.0, 120.0)

    def test_deepseek_generation_respects_explicit_scene_timeout(self) -> None:
        client = LLMClient(
            api_key="deepseek-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            retry_attempts=1,
        )
        try:
            timeout = client._effective_timeout_for_profile(
                client._request_profiles()[0],
                httpx.Timeout(45.0, connect=10.0),
                llm_task_route="prose_generation",
                explicit_timeout=True,
            )
        finally:
            client.close()

        self.assertEqual(timeout.read, 45.0)

    def test_deepseek_review_json_respects_explicit_short_timeout(self) -> None:
        client = LLMClient(
            api_key="deepseek-key",
            base_url="https://api.deepseek.com/v1",
            model="deepseek-chat",
            retry_attempts=1,
        )
        try:
            timeout = client._effective_timeout_for_profile(
                client._request_profiles()[0],
                httpx.Timeout(30.0, connect=10.0),
                llm_task_route="review_json",
                explicit_timeout=True,
            )
        finally:
            client.close()

        self.assertEqual(timeout.read, 30.0)

    def test_kimi_primary_still_replaces_deepseek_fallback(self) -> None:
        client = LLMClient(
            api_key="kimi-key",
            base_url="https://api.moonshot.cn/v1",
            model="kimi-k2.5",
            retry_attempts=1,
            fallback_profiles=[
                {
                    "id": "deepseek",
                    "name": "DeepSeek",
                    "api_key": "deepseek-key",
                    "base_url": "https://api.deepseek.com/v1",
                    "model": "deepseek-chat",
                },
                {
                    "id": "gemini",
                    "name": "Gemini",
                    "api_key": "gemini-key",
                    "base_url": "https://generativelanguage.googleapis.com/v1beta/openai",
                    "model": "gemini-2.5-pro",
                },
            ],
        )
        calls: list[str] = []

        def fake_post(url, **kwargs):  # noqa: ANN001
            calls.append(kwargs["json"]["model"])
            request = httpx.Request("POST", url)
            return httpx.Response(
                200,
                json={"choices": [{"message": {"content": "{\"ok\": true}"}}]},
                request=request,
            )

        try:
            with patch.object(client.client, "post", side_effect=fake_post):
                client.chat(
                    [{"role": "user", "content": "规划"}],
                    response_format={"type": "json_object"},
                    task_family="planning",
                    stage_key="chapter_plan",
                )
            attempts = client.drain_llm_attempt_events()
        finally:
            client.close()

        self.assertEqual(calls, ["kimi-k2.5"])
        skipped = attempts[0]["skipped_profiles"]
        skipped_reasons = [item["reason"] for item in skipped]
        self.assertIn("replaced_by_kimi", skipped_reasons)
        self.assertIn("replaced_by_deepseek", skipped_reasons)


def jsonable(value):  # noqa: ANN001
    return str(value)


if __name__ == "__main__":
    unittest.main()
