from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.llm.compat import call_chat_compat
from forwin.utils import LLMJSONParseError, parse_llm_json

from .signals import CanonQualitySignal, CountdownLedgerEntry


logger = logging.getLogger(__name__)


TemporalReferenceKind = Literal[
    "current_timer",
    "upper_bound_current_timer",
    "wall_clock_deadline",
    "local_tactical_deadline",
    "duration_cost",
    "frequency",
    "elapsed_time",
    "hypothetical",
    "static_policy",
    "retrospective",
    "other_non_countdown",
    "unknown",
]


TemporalDecisionAction = Literal[
    "keep_conflict",
    "ignore_as_non_countdown",
    "downgrade_to_warning",
    "mark_reset_or_branch",
    "reassign_countdown_key",
]


class TemporalMentionDecision(BaseModel):
    signal_id: str = ""
    action: TemporalDecisionAction = "keep_conflict"
    reference_kind: TemporalReferenceKind = "unknown"
    confidence: float = Field(default=0.0, ge=0.0, le=1.0)
    reason: str = ""
    countdown_key: str = ""
    span_start: int = 0
    span_end: int = 0
    replacement_countdown_key: str = ""


class TemporalReconciliationResult(BaseModel):
    decisions: list[TemporalMentionDecision] = Field(default_factory=list)
    reviewer_summary: str = ""
    status: Literal["not_run", "passed", "failed"] = "not_run"
    error: str = ""


TemporalReconciler = Callable[..., TemporalReconciliationResult | dict[str, Any] | None]


class LLMTemporalReconciler:
    """Classify ambiguous time mentions without embedding story-specific keywords in analyzers."""

    def __init__(
        self,
        llm_client,
        *,
        confidence_threshold: float = 0.82,
    ) -> None:
        self.llm_client = llm_client
        self.confidence_threshold = float(confidence_threshold)

    def __call__(
        self,
        *,
        project_id: str,
        chapter_number: int,
        draft_id: str = "",
        body: str,
        previous_entries: list[dict[str, Any] | CountdownLedgerEntry] | None = None,
        signals: list[CanonQualitySignal] | None = None,
        entries: list[CountdownLedgerEntry] | None = None,
    ) -> TemporalReconciliationResult:
        candidate_signals = [
            signal
            for signal in list(signals or [])
            if signal.signal_type in {"countdown_non_monotonic"}
            and signal.status == "open"
        ]
        candidate_entries = list(entries or [])
        if not self.llm_client:
            return TemporalReconciliationResult()
        if not candidate_signals and not candidate_entries:
            return TemporalReconciliationResult()
        payload = {
            "project_id": project_id,
            "chapter_number": int(chapter_number or 0),
            "draft_id": draft_id,
            "body_excerpt": _body_excerpt_for_candidates(
                body=str(body or ""),
                signals=candidate_signals,
                entries=list(entries or []),
            ),
            "previous_countdown_entries": [
                _entry_payload(item) for item in list(previous_entries or [])
            ],
            "candidate_signals": [
                _signal_payload(signal, body=str(body or "")) for signal in candidate_signals
            ],
            "candidate_countdown_entries": [
                _countdown_entry_payload(entry) for entry in candidate_entries
            ],
        }
        messages = _temporal_classifier_messages(payload)
        try:
            raw = call_chat_compat(
                self.llm_client,
                messages,
                temperature=0.0,
                max_tokens=4096,
                response_format={"type": "json_object"},
                timeout_seconds=35,
                retry_on_timeout=True,
                task_family="reviewer",
                stage_key="temporal_reference_classification",
                output_schema={"type": "object"},
            )
            parsed = parse_llm_json(raw, error_prefix="TEMPORAL")
            result = TemporalReconciliationResult.model_validate(parsed)
        except (LLMJSONParseError, ValueError, Exception) as exc:
            logger.warning(
                "temporal_reconciliation_failed project_id=%s chapter=%s error=%s",
                project_id,
                chapter_number,
                exc,
            )
            return TemporalReconciliationResult(status="failed", error=f"{exc.__class__.__name__}: {exc}")
        return _trusted_decisions_only(
            result.model_copy(update={"status": "passed"}),
            allowed_signal_ids={signal.signal_id for signal in candidate_signals},
            confidence_threshold=self.confidence_threshold,
        )


def apply_temporal_reconciliation(
    *,
    signals: list[CanonQualitySignal],
    entries: list[CountdownLedgerEntry],
    reconciliation: TemporalReconciliationResult | dict[str, Any] | None,
    confidence_threshold: float = 0.82,
) -> tuple[list[CanonQualitySignal], list[CountdownLedgerEntry], TemporalReconciliationResult]:
    result = _coerce_result(reconciliation)
    if not result.decisions:
        return list(signals), list(entries), result

    ignored_signal_ids: set[str] = set()
    warning_signal_ids: set[str] = set()
    resolved_signal_ids: set[str] = set()
    ignored_spans: list[tuple[int, int]] = []
    reset_spans: list[tuple[int, int, TemporalMentionDecision]] = []
    reassign_spans: list[tuple[int, int, TemporalMentionDecision]] = []
    for decision in result.decisions:
        if decision.confidence < confidence_threshold:
            continue
        if decision.action == "ignore_as_non_countdown":
            if decision.signal_id:
                ignored_signal_ids.add(decision.signal_id)
            if decision.span_end > decision.span_start:
                ignored_spans.append((decision.span_start, decision.span_end))
        elif decision.action == "downgrade_to_warning" and decision.signal_id:
            warning_signal_ids.add(decision.signal_id)
        elif decision.action == "mark_reset_or_branch":
            if decision.signal_id:
                resolved_signal_ids.add(decision.signal_id)
            if decision.span_end > decision.span_start:
                reset_spans.append((decision.span_start, decision.span_end, decision))
        elif decision.action == "reassign_countdown_key":
            if not decision.replacement_countdown_key:
                continue
            if decision.signal_id:
                resolved_signal_ids.add(decision.signal_id)
            if decision.span_end > decision.span_start:
                reassign_spans.append((decision.span_start, decision.span_end, decision))

    reconciled_signals: list[CanonQualitySignal] = []
    for signal in signals:
        if signal.signal_id in ignored_signal_ids or signal.signal_id in resolved_signal_ids:
            continue
        if signal.signal_id in warning_signal_ids and signal.severity == "error":
            payload = dict(signal.payload)
            payload["temporal_semantics"] = _decision_payload_for_signal(result, signal.signal_id)
            reconciled_signals.append(signal.model_copy(update={"severity": "warning", "payload": payload}))
            continue
        reconciled_signals.append(signal)

    if ignored_spans:
        reconciled_entries = [
            entry
            for entry in entries
            if not _entry_overlaps_any_span(entry, ignored_spans)
        ]
    else:
        reconciled_entries = list(entries)
    if reset_spans:
        reconciled_entries = [
            _entry_marked_reset(entry, reset_spans)
            for entry in reconciled_entries
        ]
    if reassign_spans:
        reconciled_entries = [
            _entry_reassigned(entry, reassign_spans)
            for entry in reconciled_entries
        ]
    return reconciled_signals, reconciled_entries, result


def _trusted_decisions_only(
    result: TemporalReconciliationResult,
    *,
    allowed_signal_ids: set[str],
    confidence_threshold: float,
) -> TemporalReconciliationResult:
    decisions: list[TemporalMentionDecision] = []
    for decision in result.decisions:
        if decision.signal_id and decision.signal_id not in allowed_signal_ids:
            continue
        if decision.action != "keep_conflict" and decision.confidence < confidence_threshold:
            continue
        decisions.append(decision)
    return result.model_copy(update={"decisions": decisions})


def _coerce_result(value: TemporalReconciliationResult | dict[str, Any] | None) -> TemporalReconciliationResult:
    if isinstance(value, TemporalReconciliationResult):
        return value
    if isinstance(value, dict):
        return TemporalReconciliationResult.model_validate(value)
    return TemporalReconciliationResult()


def _temporal_classifier_messages(payload: dict[str, Any]) -> list[dict[str, str]]:
    return [
        {
            "role": "system",
            "content": (
                "你是 ForWin 的时间语义分类器。只输出 JSON。"
                "你的任务不是审美评价，而是判断候选时间表达是否真的是当前主倒计时读数。"
                "不要按单个关键词裁决；必须结合正文 span、前后文、已有 countdown ledger 和候选冲突。"
            ),
        },
        {
            "role": "user",
            "content": (
                "请分类 candidate_signals 中的时间冲突，也可以分类 candidate_countdown_entries 中"
                "明显不是倒计时读数的候选条目。只返回 JSON 对象："
                '{"decisions":[{"signal_id":"","action":"keep_conflict|ignore_as_non_countdown|'
                'downgrade_to_warning|mark_reset_or_branch|reassign_countdown_key",'
                '"reference_kind":"current_timer|upper_bound_current_timer|wall_clock_deadline|'
                'local_tactical_deadline|duration_cost|frequency|elapsed_time|hypothetical|'
                'static_policy|retrospective|other_non_countdown|unknown",'
                '"confidence":0.0,"reason":"","countdown_key":"","span_start":0,"span_end":0,'
                '"replacement_countdown_key":""}],"reviewer_summary":""}。\n'
                "如果 decision 针对 candidate_countdown_entries 而不是 signal，signal_id 可以为空，"
                "但必须填写 span_start/span_end。"
                "规则：如果表达是墙钟期限、行动耗时、等待时间、频率、回忆、规则阈值、假设推演，"
                "且不是当前倒计时读数，使用 ignore_as_non_countdown。"
                "如果它是同一 countdown_key 的当前读数且比 ledger 回升，使用 keep_conflict。"
                "只有文本明确 reset、branch clock 或新倒计时来源时，才使用 mark_reset_or_branch 或 reassign_countdown_key。"
                "如果 candidate signal 的 subject_key/inferred countdown_key 与 span 附近文本明确命名的倒计时对象不一致，"
                "使用 reassign_countdown_key，并把 replacement_countdown_key 写成 main、memory_reset、"
                "terminal_audit_window、archive_cleanup、core_access_window、public_countdown 或一个简短语义 key。"
                "置信度低于 0.82 时保守 keep_conflict 或 downgrade_to_warning。\n"
                f"输入数据：{json.dumps(payload, ensure_ascii=False)}"
            ),
        },
    ]


def _signal_payload(signal: CanonQualitySignal, *, body: str) -> dict[str, Any]:
    span_start = int(signal.span_start or 0)
    span_end = int(signal.span_end or 0)
    return {
        "signal_id": signal.signal_id,
        "signal_type": signal.signal_type,
        "severity": signal.severity,
        "subject_key": signal.subject_key,
        "description": signal.description,
        "span_start": signal.span_start,
        "span_end": signal.span_end,
        "text_span": body[max(0, span_start - 80) : min(len(body), span_end + 80)],
        "payload": signal.payload,
    }


def _body_excerpt_for_candidates(
    *,
    body: str,
    signals: list[CanonQualitySignal],
    entries: list[CountdownLedgerEntry],
    window: int = 260,
) -> str:
    text = str(body or "")
    spans: list[tuple[int, int]] = []
    for signal in signals:
        if signal.span_start is not None and signal.span_end is not None:
            spans.append((int(signal.span_start), int(signal.span_end)))
    for entry in entries:
        span = _entry_span(entry)
        if span is not None:
            spans.append(span)
    if not text or not spans:
        return text[:1800]
    start = max(0, min(span_start for span_start, _ in spans) - window)
    end = min(len(text), max(span_end for _, span_end in spans) + window)
    prefix = "..." if start > 0 else ""
    suffix = "..." if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"


def _countdown_entry_payload(entry: CountdownLedgerEntry) -> dict[str, Any]:
    return {
        "countdown_key": entry.countdown_key,
        "chapter_number": entry.chapter_number,
        "normalized_remaining_minutes": entry.normalized_remaining_minutes,
        "raw_mention": entry.raw_mention,
        "previous_remaining_minutes": entry.previous_remaining_minutes,
        "status": entry.status,
        "evidence_refs": list(entry.evidence_refs),
        "payload": dict(entry.payload),
    }


def _entry_payload(item: dict[str, Any] | CountdownLedgerEntry) -> dict[str, Any]:
    if isinstance(item, CountdownLedgerEntry):
        return _countdown_entry_payload(item)
    return dict(item)


def _entry_overlaps_any_span(entry: CountdownLedgerEntry, spans: list[tuple[int, int]]) -> bool:
    span = _entry_span(entry)
    if span is None:
        return False
    start, end = span
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def _entry_marked_reset(
    entry: CountdownLedgerEntry,
    spans: list[tuple[int, int, TemporalMentionDecision]],
) -> CountdownLedgerEntry:
    decision = _decision_for_entry_span(entry, spans)
    if decision is None:
        return entry
    payload = dict(entry.payload)
    payload["temporal_semantics"] = decision.model_dump(mode="json")
    return entry.model_copy(
        update={
            "is_reset_event": True,
            "is_branch_clock": decision.reference_kind == "current_timer",
            "status": "consistent",
            "payload": payload,
        }
    )


def _entry_reassigned(
    entry: CountdownLedgerEntry,
    spans: list[tuple[int, int, TemporalMentionDecision]],
) -> CountdownLedgerEntry:
    decision = _decision_for_entry_span(entry, spans)
    if decision is None or not decision.replacement_countdown_key:
        return entry
    payload = dict(entry.payload)
    payload["temporal_semantics"] = decision.model_dump(mode="json")
    replacement = decision.replacement_countdown_key
    return entry.model_copy(
        update={
            "countdown_key": replacement,
            "label": replacement,
            "previous_remaining_minutes": None,
            "status": "consistent",
            "payload": payload,
        }
    )


def _decision_for_entry_span(
    entry: CountdownLedgerEntry,
    spans: list[tuple[int, int, TemporalMentionDecision]],
) -> TemporalMentionDecision | None:
    entry_span = _entry_span(entry)
    if entry_span is None:
        return None
    start, end = entry_span
    for span_start, span_end, decision in spans:
        if start < span_end and end > span_start:
            return decision
    return None


def _entry_span(entry: CountdownLedgerEntry) -> tuple[int, int] | None:
    payload = dict(entry.payload or {})
    start = payload.get("span_start")
    end = payload.get("span_end")
    if isinstance(start, int) and isinstance(end, int) and end > start:
        return start, end
    for ref in entry.evidence_refs:
        if not str(ref).startswith("body:") or "-" not in str(ref):
            continue
        left, right = str(ref)[5:].split("-", 1)
        try:
            parsed_start = int(left)
            parsed_end = int(right)
        except ValueError:
            continue
        if parsed_end > parsed_start:
            return parsed_start, parsed_end
    return None


def _decision_payload_for_signal(result: TemporalReconciliationResult, signal_id: str) -> dict[str, Any]:
    for decision in result.decisions:
        if decision.signal_id == signal_id:
            return decision.model_dump(mode="json")
    return {}
