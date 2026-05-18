from __future__ import annotations

from forwin.canon_quality.chapter_review_form.cost_estimator import (
    CostEstimate,
    estimate_tokens_for_text,
    should_abort_for_cost_cap,
    usage_from_llm_client,
)


class ClientWithAttempts:
    llm_attempt_events = [
        {
            "status": "succeeded",
            "input_text": "ASCII prompt with 主倒计时",
            "output_text": "JSON answer with 主倒计时",
        },
        {"status": "failed", "input_chars": 9999, "output_chars": 9999},
    ]


def test_estimate_tokens_for_chinese_text_uses_half_char_ratio() -> None:
    assert estimate_tokens_for_text("主倒计时还有五十九分钟。") >= 6


def test_usage_from_llm_client_uses_last_successful_attempt() -> None:
    usage = usage_from_llm_client(ClientWithAttempts())

    assert usage.input_tokens == estimate_tokens_for_text("ASCII prompt with 主倒计时")
    assert usage.output_tokens == estimate_tokens_for_text("JSON answer with 主倒计时")
    assert usage.estimated is True


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
