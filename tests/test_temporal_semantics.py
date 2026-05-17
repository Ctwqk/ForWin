from __future__ import annotations

import json

from forwin.canon_quality.signals import CanonQualitySignal, CountdownLedgerEntry
from forwin.canon_quality.temporal_semantics import (
    LLMTemporalReconciler,
    TemporalMentionDecision,
    TemporalReconciliationResult,
    apply_temporal_reconciliation,
)


class _FakeLLM:
    def __init__(self, payload: dict) -> None:
        self.payload = payload
        self.calls: list[dict] = []

    def chat(self, messages, **kwargs):  # noqa: ANN001, ANN201
        self.calls.append({"messages": messages, "kwargs": kwargs})
        return json.dumps(self.payload, ensure_ascii=False)


def test_llm_temporal_reconciler_returns_structured_trusted_decisions_only() -> None:
    llm = _FakeLLM(
        {
            "decisions": [
                {
                    "signal_id": "sig-1",
                    "action": "ignore_as_non_countdown",
                    "reference_kind": "wall_clock_deadline",
                    "confidence": 0.91,
                    "reason": "墙钟期限，不是当前倒计时读数。",
                    "span_start": 10,
                    "span_end": 14,
                },
                {
                    "signal_id": "foreign-sig",
                    "action": "ignore_as_non_countdown",
                    "reference_kind": "other_non_countdown",
                    "confidence": 0.99,
                    "reason": "不允许引用不在候选列表里的 signal。",
                    "span_start": 20,
                    "span_end": 24,
                },
            ],
            "reviewer_summary": "分类完成",
        }
    )
    reconciler = LLMTemporalReconciler(llm)

    result = reconciler(
        project_id="p1",
        chapter_number=3,
        body="记忆重置窗口剩余74分钟，距离天亮还有三个小时。",
        signals=[
            CanonQualitySignal(
                signal_id="sig-1",
                project_id="p1",
                chapter_number=3,
                signal_type="countdown_non_monotonic",
                severity="error",
                span_start=10,
                span_end=14,
            )
        ],
        entries=[],
        previous_entries=[],
    )

    assert [decision.signal_id for decision in result.decisions] == ["sig-1"]
    assert llm.calls[0]["kwargs"]["stage_key"] == "temporal_reference_classification"
    assert llm.calls[0]["kwargs"]["task_family"] == "reviewer"


def test_apply_temporal_reconciliation_removes_ignored_span_entries_and_signal() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-1",
        project_id="p1",
        chapter_number=3,
        signal_type="countdown_non_monotonic",
        severity="error",
        span_start=12,
        span_end=15,
    )
    kept_entry = CountdownLedgerEntry(
        project_id="p1",
        countdown_key="memory_reset",
        chapter_number=3,
        normalized_remaining_minutes=74,
        raw_mention="74分钟",
        evidence_refs=["body:0-4"],
        payload={"span_start": 0, "span_end": 4},
    )
    ignored_entry = CountdownLedgerEntry(
        project_id="p1",
        countdown_key="memory_reset",
        chapter_number=3,
        normalized_remaining_minutes=180,
        raw_mention="三个小时",
        evidence_refs=["body:12-15"],
        payload={"span_start": 12, "span_end": 15},
    )

    signals, entries, _ = apply_temporal_reconciliation(
        signals=[signal],
        entries=[kept_entry, ignored_entry],
        reconciliation=TemporalReconciliationResult(
            decisions=[
                TemporalMentionDecision(
                    signal_id="sig-1",
                    action="ignore_as_non_countdown",
                    reference_kind="wall_clock_deadline",
                    confidence=0.9,
                    span_start=12,
                    span_end=15,
                )
            ]
        ),
    )

    assert signals == []
    assert [entry.raw_mention for entry in entries] == ["74分钟"]


def test_apply_temporal_reconciliation_can_mark_llm_confirmed_reset() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-reset",
        project_id="p1",
        chapter_number=3,
        signal_type="countdown_non_monotonic",
        severity="error",
        span_start=30,
        span_end=38,
    )
    entry = CountdownLedgerEntry(
        project_id="p1",
        countdown_key="terminal_audit_window",
        chapter_number=3,
        normalized_remaining_minutes=72,
        raw_mention="00:72:00",
        previous_remaining_minutes=68,
        status="conflict",
        payload={"span_start": 30, "span_end": 38},
    )

    signals, entries, _ = apply_temporal_reconciliation(
        signals=[signal],
        entries=[entry],
        reconciliation=TemporalReconciliationResult(
            decisions=[
                TemporalMentionDecision(
                    signal_id="sig-reset",
                    action="mark_reset_or_branch",
                    reference_kind="current_timer",
                    confidence=0.93,
                    reason="正文明确说明该窗口被系统重设。",
                    span_start=30,
                    span_end=38,
                )
            ]
        ),
    )

    assert signals == []
    assert entries[0].is_reset_event is True
    assert entries[0].status == "consistent"
    assert entries[0].payload["temporal_semantics"]["action"] == "mark_reset_or_branch"


def test_apply_temporal_reconciliation_can_reassign_llm_confirmed_clock_key() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-key",
        project_id="p1",
        chapter_number=3,
        signal_type="countdown_non_monotonic",
        severity="error",
        span_start=12,
        span_end=20,
    )
    entry = CountdownLedgerEntry(
        project_id="p1",
        countdown_key="main",
        label="main",
        chapter_number=3,
        normalized_remaining_minutes=72,
        raw_mention="00:72:18",
        previous_remaining_minutes=40,
        status="conflict",
        payload={"span_start": 12, "span_end": 20},
    )

    signals, entries, _ = apply_temporal_reconciliation(
        signals=[signal],
        entries=[entry],
        reconciliation=TemporalReconciliationResult(
            decisions=[
                TemporalMentionDecision(
                    signal_id="sig-key",
                    action="reassign_countdown_key",
                    reference_kind="current_timer",
                    confidence=0.94,
                    reason="正文语境指向另一个独立倒计时。",
                    span_start=12,
                    span_end=20,
                    replacement_countdown_key="memory_reset",
                )
            ]
        ),
    )

    assert signals == []
    assert entries[0].countdown_key == "memory_reset"
    assert entries[0].label == "memory_reset"
    assert entries[0].previous_remaining_minutes is None
    assert entries[0].status == "consistent"
