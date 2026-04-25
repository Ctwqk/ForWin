from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from forwin.protocol.experience import ArcPayoffMap, ReaderPromise, RevelationLayer
from forwin.orchestrator.phase24 import _coerce_unit_float
from forwin.writer.llm_client import LLMClient


class LLMClientRetryTests(unittest.TestCase):
    def test_reader_promise_coerces_unknown_ambiguity_mode_to_managed(self) -> None:
        promise = ReaderPromise.model_validate({"ambiguity_mode": "suggestive_opaque"})

        self.assertEqual(promise.ambiguity_mode, "managed")

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

    def test_kimi_k25_raises_short_stage_timeout(self) -> None:
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

        self.assertEqual(timeouts[0], 120.0)
        self.assertEqual(attempts[0]["timeout_seconds"], 120.0)

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

    def test_feedback_analysis_routes_minimax_before_spark_and_kimi(self) -> None:
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
        self.assertEqual(calls, ["MiniMax-M2.7"])
        self.assertEqual(attempts[0]["llm_task_route"], "feedback_analysis")

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


def jsonable(value):  # noqa: ANN001
    return str(value)


if __name__ == "__main__":
    unittest.main()
