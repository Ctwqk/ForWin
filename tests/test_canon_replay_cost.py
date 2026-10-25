from __future__ import annotations

import json

import httpx

from forwin.canon_quality.chapter_review_form.cost_estimator import (
    CostEstimate,
    estimate_tokens_for_text,
    should_abort_for_cost_cap,
    usage_from_llm_client,
)
from forwin.config import ModelProfileConfig
from forwin.writer.llm import LLMClient


class ClientWithAttempts:
    llm_attempt_events = [
        {
            "status": "succeeded",
            "input_text": "ASCII prompt with 主倒计时",
            "output_text": "JSON answer with 主倒计时",
        },
        {"status": "failed", "input_chars": 9999, "output_chars": 9999},
    ]


class ClientWithRawPayloadAttempts:
    request_payload = {
        "messages": [
            {
                "role": "system",
                "content": "ASCII-heavy system prompt with 主倒计时",
            }
        ]
    }
    response_text = "ASCII-heavy JSON answer with 主倒计时"
    llm_attempt_events = [
        {
            "status": "succeeded",
            "input_chars": 9999,
            "output_chars": 9999,
            "_raw_request_payload": request_payload,
            "_raw_response_text": response_text,
        }
    ]


class ClientWithCanonicalUsageAttempts:
    llm_attempt_events = [
        {
            "http_status": 429,
            "prompt_tokens": 4,
            "completion_tokens": 0,
            "error_class": "HTTPStatusError",
        },
        {
            "http_status": 200,
            "output_chars": 12,
            "prompt_tokens": 23,
            "completion_tokens": 7,
            "total_tokens": 30,
        },
    ]


def test_estimate_tokens_for_chinese_text_uses_half_char_ratio() -> None:
    assert estimate_tokens_for_text("主倒计时还有五十九分钟。") >= 6


def test_usage_from_llm_client_counts_successful_and_failed_attempts() -> None:
    usage = usage_from_llm_client(ClientWithAttempts())

    assert usage.input_tokens == (
        estimate_tokens_for_text("ASCII prompt with 主倒计时") + int(9999 * 0.5)
    )
    assert usage.output_tokens == (
        estimate_tokens_for_text("JSON answer with 主倒计时") + int(9999 * 0.5)
    )
    assert usage.estimated is True


def test_usage_from_llm_client_prefers_raw_payload_over_char_count() -> None:
    usage = usage_from_llm_client(ClientWithRawPayloadAttempts())

    assert usage.input_tokens == estimate_tokens_for_text(
        json.dumps(ClientWithRawPayloadAttempts.request_payload, ensure_ascii=False, sort_keys=True)
    )
    assert usage.output_tokens == estimate_tokens_for_text(ClientWithRawPayloadAttempts.response_text)
    assert usage.input_tokens < int(9999 * 0.5)
    assert usage.output_tokens < int(9999 * 0.5)


def test_usage_from_llm_client_reads_canonical_attempt_usage() -> None:
    usage = usage_from_llm_client(ClientWithCanonicalUsageAttempts())

    assert usage.input_tokens == 27
    assert usage.output_tokens == 7
    assert usage.estimated is False


def test_usage_from_llm_client_reads_attempts_without_draining_them(monkeypatch) -> None:
    client = LLMClient(
        profiles={"provider": ModelProfileConfig(
            id="provider", name="Provider", api_key="test-key",
            base_url="https://provider.example/v1", model="provider-model",
        )},
        chains={"default": ("provider",)},
        retry_attempts=1,
    )

    def fake_post(url, **_kwargs):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "ok"}}],
                "usage": {"prompt_tokens": 29, "completion_tokens": 8, "total_tokens": 37},
            },
            request=httpx.Request("POST", url),
        )

    monkeypatch.setattr(client.client, "post", fake_post)
    try:
        client.chat([{"role": "user", "content": "review"}])
        usage = usage_from_llm_client(client)

        assert usage.input_tokens == 29
        assert usage.output_tokens == 8
        assert client.drain_llm_attempt_events()[0]["total_tokens"] == 37
    finally:
        client.close()


def test_cost_cap_aborts_before_next_chapter_estimate_exceeds_cap() -> None:
    current = CostEstimate(total_input_tokens=100, total_output_tokens=100, total_usd=0.90, chapters={})
    next_chapter = CostEstimate(total_input_tokens=20, total_output_tokens=20, total_usd=0.20, chapters={})

    decision = should_abort_for_cost_cap(
        current_cost=current,
        next_chapter_estimate=next_chapter,
        cap_usd=1.00,
    )

    assert decision.abort is True
    assert decision.reason == "cost_cap"
