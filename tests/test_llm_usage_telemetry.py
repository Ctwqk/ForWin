from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import httpx
import pytest

from forwin.config import ModelProfileConfig
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure
from forwin.writer.llm import LLMClient
from forwin.writer.llm.errors import EmptyLLMResponse


@pytest.fixture
def llm_client(monkeypatch):
    client = LLMClient(
        profiles={"provider": ModelProfileConfig(
            id="provider", name="Provider", api_key="test-key",
            base_url="https://provider.example/v1", model="provider-model",
        )},
        chains={"default": ("provider",)},
        retry_attempts=1,
    )

    def fake_post(url, *, json, **_kwargs):
        time.sleep(0.01)
        return httpx.Response(
            200,
            json={"choices": [{"message": {"content": json["messages"][0]["content"]}}]},
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(client.client, "post", fake_post)
    try:
        yield client
    finally:
        client.close()


def _provider_attempt(response_payload: dict[str, object]) -> dict[str, object]:
    client = LLMClient(
        profiles={"provider": ModelProfileConfig(
            id="provider", name="Provider", api_key="test-key",
            base_url="https://provider.example/v1", model="provider-model",
        )},
        chains={"default": ("provider",)},
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


def test_openai_compatible_empty_response_still_records_provider_usage() -> None:
    client = LLMClient(
        profiles={"provider": ModelProfileConfig(
            id="provider", name="Provider", api_key="test-key",
            base_url="https://provider.example/v1", model="provider-model",
        )},
        chains={"default": ("provider",)},
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
            with pytest.raises(EmptyLLMResponse):
                client.chat([{"role": "user", "content": "hello"}])
        attempt = client.drain_llm_attempt_events()[0]
    finally:
        client.close()

    assert attempt["prompt_tokens"] == 19
    assert attempt["completion_tokens"] == 3
    assert attempt["total_tokens"] == 22
    assert attempt["usage_source"] == "provider"
    assert int(attempt["output_chars"]) > 0


def test_parse_failure_marker_mutates_the_drained_attempt(llm_client) -> None:
    adapter = llm_client
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


def test_concurrent_client_calls_have_distinct_attempt_groups(llm_client) -> None:
    def invoke(label: str):
        output = llm_client.chat(
            [{"role": "user", "content": label}],
            task_family="review", stage_key="chapter_review",
        )
        return output, llm_client.drain_llm_attempt_events()

    with ThreadPoolExecutor(max_workers=2) as executor:
        results = list(executor.map(invoke, ["first", "second"]))
    outputs = [output for output, _ in results]
    attempts = [attempt for _, events in results for attempt in events]

    assert outputs == ["first", "second"]
    assert llm_client.drain_llm_attempt_events() == []
    assert len(attempts) == 2
    assert len({str(item["attempt_group_id"]) for item in attempts}) == 2
    assert {item["_raw_request_payload"]["messages"][0]["content"] for item in attempts} == {"first", "second"}
