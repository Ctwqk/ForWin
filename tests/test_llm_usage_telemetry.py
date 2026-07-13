from __future__ import annotations

import json
import time
from concurrent.futures import ThreadPoolExecutor
from threading import Event
from unittest.mock import patch

import httpx
import pytest

from forwin.llm.codex_client import CodexBridgeClient
from forwin.llm.router import LLMCallIntent, LLMCallRouter, RoutedModelAdapter
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure
from forwin.writer.llm import LLMClient


class _OrdinaryAdapter:
    def chat(self, _messages, **_kwargs) -> str:
        return "ordinary"

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        return []

    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        return []


class _FailingOrdinaryAdapter(_OrdinaryAdapter):
    def __init__(self) -> None:
        self._attempts: list[dict[str, object]] = []

    def chat(self, _messages, **_kwargs) -> str:
        self._attempts.append(
            {
                "attempt_group_id": "ordinary-first",
                "attempt_no": 1,
                "provider": "openai_compatible",
                "model": "ordinary-model",
                "error_class": "RuntimeError",
                "final_failure": True,
            }
        )
        raise RuntimeError("ordinary failed")

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        attempts = list(self._attempts)
        self._attempts.clear()
        return attempts


class _SlowOrdinaryAdapter(_OrdinaryAdapter):
    def __init__(self) -> None:
        self._attempts: list[dict[str, object]] = []

    def chat(self, messages, **kwargs) -> str:  # noqa: ANN001
        label = str(messages[0]["content"])
        self._attempts.append(
            {
                "attempt_group_id": f"backend-{label}",
                "attempt_no": 1,
                "provider": "openai_compatible",
                "model": "ordinary-model",
                "task_family": str(kwargs.get("task_family") or ""),
                "stage_key": str(kwargs.get("stage_key") or ""),
                "http_status": 200,
                "output_chars": len(label),
            }
        )
        time.sleep(0.05)
        return label

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        attempts = list(self._attempts)
        self._attempts.clear()
        return attempts


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


def test_openai_compatible_parse_failure_still_records_provider_usage() -> None:
    client = LLMClient(
        api_key="test-key",
        base_url="https://provider.example/v1",
        model="provider-model",
        retry_attempts=1,
    )

    def fake_post(url: str, **_kwargs) -> httpx.Response:
        return httpx.Response(
            200,
            json={
                "choices": [],
                "usage": {
                    "prompt_tokens": 19,
                    "completion_tokens": 3,
                    "total_tokens": 22,
                },
            },
            request=httpx.Request("POST", url),
        )

    try:
        with patch.object(client.client, "post", side_effect=fake_post):
            with pytest.raises(IndexError):
                client.chat([{"role": "user", "content": "hello"}])
        attempt = client.drain_llm_attempt_events()[0]
    finally:
        client.close()

    assert attempt["prompt_tokens"] == 19
    assert attempt["completion_tokens"] == 3
    assert attempt["total_tokens"] == 22
    assert attempt["usage_source"] == "provider"
    assert int(attempt["output_chars"]) > 0


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


def test_ordinary_to_codex_fallback_preserves_attempt_order() -> None:
    payload = {
        "ok": True,
        "content": "codex fallback",
        "raw_events": [
            {
                "type": "turn.completed",
                "usage": {"input_tokens": 11, "output_tokens": 4},
            }
        ],
        "returncode": 0,
        "actual_model": "gpt-5.3-codex-spark",
        "thread_id": "thread-fallback",
    }
    with patch(
        "forwin.llm.codex_client.httpx.Client",
        return_value=_BridgeHTTPClient(payload),
    ):
        router = LLMCallRouter(
            ordinary_adapter=_FailingOrdinaryAdapter(),
            codex_client=CodexBridgeClient(bridge_url="http://bridge"),
            codex_enabled=True,
            codex_default_model="gpt-5.3-codex-spark",
        )
        router.chat(
            [{"role": "user", "content": "write"}],
            intent=LLMCallIntent(task_family="writer", stage_key="chapter_draft"),
        )
        attempts = router.drain_llm_attempt_events()

    assert [item["provider"] for item in attempts] == [
        "openai_compatible",
        "codex_bridge",
    ]
    assert len({str(item["attempt_group_id"]) for item in attempts}) == 1


def test_routed_parse_failure_marker_mutates_the_drained_attempt() -> None:
    adapter = RoutedModelAdapter(
        LLMCallRouter(ordinary_adapter=_SlowOrdinaryAdapter(), codex_enabled=False)
    )
    adapter.chat(
        [{"role": "user", "content": "parse"}],
        task_family="chapter_review_form",
        stage_key="chapter_review_form",
    )

    mark_latest_attempt_parse_failure(
        adapter,
        parser_name="ChapterReviewAnswers",
        stage_key="chapter_review_form",
        schema_name="review_json",
        error="invalid schema",
    )

    attempt = adapter.drain_llm_attempt_events()[0]
    assert attempt["parse_error"] == "invalid schema"
    assert attempt["schema_ok"] is False


def test_router_serializes_shared_ordinary_attempt_capture() -> None:
    router = LLMCallRouter(
        ordinary_adapter=_SlowOrdinaryAdapter(),
        codex_enabled=False,
    )

    def invoke(label: str) -> str:
        return router.chat(
            [{"role": "user", "content": label}],
            intent=LLMCallIntent(task_family="review", stage_key="chapter_review"),
        )

    with ThreadPoolExecutor(max_workers=2) as executor:
        outputs = list(executor.map(invoke, ["first", "second"]))
    attempts = router.drain_llm_attempt_events()

    assert outputs == ["first", "second"]
    assert len(attempts) == 2
    assert len({str(item["attempt_group_id"]) for item in attempts}) == 2
    assert {str(item["backend_attempt_group_id"]) for item in attempts} == {
        "backend-first",
        "backend-second",
    }


def test_parse_failure_marker_targets_calling_threads_route_group() -> None:
    adapter = RoutedModelAdapter(
        LLMCallRouter(ordinary_adapter=_SlowOrdinaryAdapter(), codex_enabled=False)
    )
    first_returned = Event()
    second_marked = Event()

    def first_call() -> None:
        adapter.chat(
            [{"role": "user", "content": "first"}],
            task_family="chapter_review_form",
            stage_key="chapter_review_form",
        )
        first_returned.set()
        assert second_marked.wait(timeout=2)
        mark_latest_attempt_parse_failure(
            adapter,
            parser_name="review",
            stage_key="chapter_review_form",
            error="first-error",
        )

    def second_call() -> None:
        assert first_returned.wait(timeout=2)
        adapter.chat(
            [{"role": "user", "content": "second"}],
            task_family="chapter_review_form",
            stage_key="chapter_review_form",
        )
        mark_latest_attempt_parse_failure(
            adapter,
            parser_name="review",
            stage_key="chapter_review_form",
            error="second-error",
        )
        second_marked.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(executor.map(lambda fn: fn(), [first_call, second_call]))

    attempts = adapter.drain_llm_attempt_events()
    errors_by_backend_group = {
        str(item["backend_attempt_group_id"]): str(item.get("parse_error") or "")
        for item in attempts
    }
    assert errors_by_backend_group == {
        "backend-first": "first-error",
        "backend-second": "second-error",
    }
