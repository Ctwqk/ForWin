from __future__ import annotations

import json
from unittest.mock import patch

import httpx

from forwin.llm.codex_client import CodexBridgeClient
from forwin.llm.router import LLMCallIntent, LLMCallRouter
from forwin.writer.llm import LLMClient


class _OrdinaryAdapter:
    def chat(self, _messages, **_kwargs) -> str:
        return "ordinary"

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        return []

    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        return []


class _BridgeResponse:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload
        self.status_code = 200
        self.text = json.dumps(payload)

    def json(self) -> dict[str, object]:
        return self._payload

    def raise_for_status(self) -> None:
        return None


class _BridgeHTTPClient:
    def __init__(self, payload: dict[str, object]) -> None:
        self._payload = payload

    def post(self, *_args, **_kwargs) -> _BridgeResponse:
        return _BridgeResponse(self._payload)

    def close(self) -> None:
        return None


def _provider_attempt(response_payload: dict[str, object]) -> dict[str, object]:
    client = LLMClient(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="provider-model",
        retry_attempts=1,
    )

    def fake_post(url: str, **_kwargs) -> httpx.Response:
        return httpx.Response(
            200,
            json=response_payload,
            request=httpx.Request("POST", url),
        )

    try:
        with patch.object(client.client, "post", side_effect=fake_post):
            client.chat([{"role": "user", "content": "hello"}])
        return client.drain_llm_attempt_events()[0]
    finally:
        client.close()


def _codex_attempt(*, raw_events: list[dict[str, object]]) -> dict[str, object]:
    payload = {
        "ok": True,
        "content": "codex answer",
        "raw_events": raw_events,
        "returncode": 0,
        "actual_model": "gpt-5.3-codex-spark",
        "thread_id": "thread-usage",
    }
    fake_http = _BridgeHTTPClient(payload)
    with patch("forwin.llm.codex_client.httpx.Client", return_value=fake_http):
        client = CodexBridgeClient(bridge_url="http://bridge")
        router = LLMCallRouter(
            ordinary_adapter=_OrdinaryAdapter(),
            codex_client=client,
            codex_enabled=True,
            codex_default_model="gpt-5.3-codex-spark",
        )
        router.chat(
            [{"role": "user", "content": "review this chapter"}],
            intent=LLMCallIntent(task_family="review", stage_key="chapter_review"),
        )
        return router.drain_llm_attempt_events()[0]


def test_openai_compatible_attempt_records_provider_usage() -> None:
    attempt = _provider_attempt(
        {
            "choices": [{"message": {"content": "ok"}}],
            "usage": {
                "prompt_tokens": 17,
                "completion_tokens": 5,
                "total_tokens": 22,
            },
        }
    )

    assert attempt["prompt_tokens"] == 17
    assert attempt["completion_tokens"] == 5
    assert attempt["total_tokens"] == 22
    assert attempt["usage_source"] == "provider"


def test_openai_compatible_attempt_marks_missing_provider_usage() -> None:
    attempt = _provider_attempt({"choices": [{"message": {"content": "ok"}}]})

    assert attempt["prompt_tokens"] is None
    assert attempt["completion_tokens"] is None
    assert attempt["total_tokens"] is None
    assert attempt["usage_source"] == "missing"


def test_codex_usage_is_normalized_into_routed_attempt() -> None:
    attempt = _codex_attempt(
        raw_events=[
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 31, "output_tokens": 9},
            }
        ]
    )

    assert attempt["prompt_tokens"] == 31
    assert attempt["completion_tokens"] == 9
    assert attempt["total_tokens"] == 40
    assert attempt["usage_source"] == "codex_bridge"
    assert attempt["task_family"] == "review"
    assert attempt["stage_key"] == "chapter_review"


def test_codex_missing_usage_uses_character_estimate() -> None:
    attempt = _codex_attempt(raw_events=[{"type": "turn.completed"}])

    assert int(attempt["prompt_tokens"]) > 0
    assert int(attempt["completion_tokens"]) > 0
    assert attempt["total_tokens"] == (
        int(attempt["prompt_tokens"]) + int(attempt["completion_tokens"])
    )
    assert attempt["usage_source"] == "estimated"
