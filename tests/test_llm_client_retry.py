from __future__ import annotations

import unittest
from unittest.mock import patch

import httpx

from forwin.writer.llm_client import LLMClient


class LLMClientRetryTests(unittest.TestCase):
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


def jsonable(value):  # noqa: ANN001
    return str(value)


if __name__ == "__main__":
    unittest.main()
