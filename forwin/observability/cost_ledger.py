from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.models.audit import DecisionEvent
from forwin.models.draft import CandidateDraftRecord
from forwin.models.genesis import PromptTrace
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import Project


CostDimension = Literal[
    "project",
    "chapter",
    "band",
    "candidate",
    "task_family",
    "stage_key",
    "model",
    "provider",
]

_DIMENSION_ORDER: tuple[CostDimension, ...] = (
    "project",
    "chapter",
    "band",
    "candidate",
    "task_family",
    "stage_key",
    "model",
    "provider",
)
_MANUAL_ACTOR_TYPES = frozenset({"manual_ui", "api", "extension"})
_EXTERNAL_REQUEST_EVENT_TYPES = frozenset(
    {
        DecisionEventType.GENERATION_REQUESTED,
        DecisionEventType.CONTINUE_REQUESTED,
        DecisionEventType.PROJECT_CREATED,
        DecisionEventType.GENESIS_CREATED,
        DecisionEventType.GENESIS_UPDATED,
        DecisionEventType.GENESIS_STAGE_GENERATED,
        DecisionEventType.GENESIS_STAGE_LOCKED,
        DecisionEventType.GENESIS_STAGE_RERUN,
        DecisionEventType.GENESIS_STAGE_REFINED,
        DecisionEventType.START_WRITING_REQUESTED,
        DecisionEventType.RUNTIME_POLICY_UPDATED,
        DecisionEventType.MANUAL_CHECKPOINT_CREATED,
        DecisionEventType.CONSTRAINT_CREATED,
        DecisionEventType.CONSTRAINT_UPDATED,
        DecisionEventType.CONSTRAINT_ARCHIVED,
        DecisionEventType.PLAN_TASK_CONTRACT_UPDATED,
        DecisionEventType.PAUSE_REQUESTED,
        DecisionEventType.TERMINATE_REQUESTED,
        DecisionEventType.REVIEW_APPROVED,
        DecisionEventType.FORCED_ACCEPT_APPLIED,
        DecisionEventType.BAND_CHECKPOINT_APPROVED,
        DecisionEventType.BAND_CHECKPOINT_OVERRIDDEN,
        DecisionEventType.PROJECT_DELETE_REQUESTED,
        DecisionEventType.UPLOAD_JOB_CREATED,
        DecisionEventType.UPLOAD_JOB_CANCELLED,
        DecisionEventType.COMMENT_SYNC_JOB_CREATED,
        DecisionEventType.PERSONALITY_LOADOUT_MANUAL_OVERRIDE,
    }
)
_ATTEMPT_SIGNAL_KEYS = frozenset(
    {
        "attempt_no",
        "input_chars",
        "output_chars",
        "duration_ms",
        "prompt_tokens",
        "completion_tokens",
        "total_tokens",
        "http_status",
        "error_class",
    }
)


class CostMetrics(BaseModel):
    model_config = ConfigDict(frozen=True)

    attempts: int = 0
    successes: int = 0
    retries: int = 0
    fallbacks: int = 0
    input_chars: int = 0
    output_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    provider_usage_attempts: int = 0
    codex_usage_attempts: int = 0
    estimated_usage_attempts: int = 0
    missing_usage_attempts: int = 0


class CostDimensionMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    dimension: CostDimension
    value: str
    metrics: CostMetrics = Field(default_factory=CostMetrics)


class GateCostMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    gate_id: str
    metrics: CostMetrics = Field(default_factory=CostMetrics)


class ManualActionMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    action_type: str
    actor_type: str
    actor_id: str = ""
    source: str = ""
    count: int = 0
    duration_ms: int = 0
    unknown_duration_count: int = 0


class CostLedgerReport(BaseModel):
    model_config = ConfigDict(frozen=True)

    schema_version: Literal[1] = 1
    project_id: str = ""
    chapter_number: int = 0
    band_id: str = ""
    candidate_id: str = ""
    project_count: int = 0
    trace_count: int = 0
    event_count: int = 0
    totals: CostMetrics = Field(default_factory=CostMetrics)
    dimensions: list[CostDimensionMetric] = Field(default_factory=list)
    gate_costs: list[GateCostMetric] = Field(default_factory=list)
    manual_action_count: int = 0
    manual_action_duration_ms: int = 0
    unknown_manual_duration_count: int = 0
    manual_actions: list[ManualActionMetric] = Field(default_factory=list)


@dataclass(slots=True)
class _MutableMetrics:
    attempts: int = 0
    successes: int = 0
    retries: int = 0
    fallbacks: int = 0
    input_chars: int = 0
    output_chars: int = 0
    prompt_tokens: int = 0
    completion_tokens: int = 0
    total_tokens: int = 0
    duration_ms: int = 0
    provider_usage_attempts: int = 0
    codex_usage_attempts: int = 0
    estimated_usage_attempts: int = 0
    missing_usage_attempts: int = 0

    def add(self, record: _CostRecord) -> None:
        self.attempts += 1
        self.successes += int(record.success)
        self.retries += int(record.retry)
        self.fallbacks += int(record.fallback)
        self.input_chars += record.input_chars
        self.output_chars += record.output_chars
        self.prompt_tokens += record.prompt_tokens
        self.completion_tokens += record.completion_tokens
        self.total_tokens += record.total_tokens
        self.duration_ms += record.duration_ms
        self.provider_usage_attempts += int(record.usage_source == "provider")
        self.codex_usage_attempts += int(record.usage_source == "codex_bridge")
        self.estimated_usage_attempts += int(record.usage_source == "estimated")
        self.missing_usage_attempts += int(record.usage_source == "missing")

    def freeze(self) -> CostMetrics:
        return CostMetrics(**asdict(self))


@dataclass(slots=True)
class _TraceContext:
    project_id: str
    chapter_number: int = 0
    band_id: str = ""
    candidate_id: str = ""
    gate_id: str = ""
    default_stage_key: str = ""
    default_model: str = ""
    default_provider: str = ""


@dataclass(slots=True)
class _CostRecord:
    project_id: str
    chapter_number: int
    band_id: str
    candidate_id: str
    gate_id: str
    task_family: str
    stage_key: str
    model: str
    provider: str
    success: bool
    retry: bool
    fallback: bool
    input_chars: int
    output_chars: int
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    duration_ms: int
    usage_source: str


@dataclass(slots=True)
class _MutableManualAction:
    count: int = 0
    duration_ms: int = 0
    unknown_duration_count: int = 0


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        value = json.loads(str(raw or "{}"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _json_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(str(raw or "[]"))
    except (TypeError, ValueError, json.JSONDecodeError):
        return []
    return value if isinstance(value, list) else []


def _nonnegative_int(value: object) -> int:
    if value is None or isinstance(value, bool):
        return 0
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def _optional_nonnegative_int(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return None


def _event_candidate(
    row: DecisionEvent,
    payload: dict[str, Any],
    *,
    candidate_by_review: dict[str, str] | None = None,
) -> str:
    outcome = parse_gate_outcome(payload)
    if outcome is not None and outcome.candidate_id:
        return outcome.candidate_id
    candidate_id = str(
        payload.get("candidate_id") or payload.get("gate_related_object_id") or ""
    ).strip()
    if candidate_id:
        return candidate_id
    if str(row.related_object_type or "") == "chapter_review":
        review_id = str(row.related_object_id or "").strip()
        mapped = str((candidate_by_review or {}).get(review_id) or "").strip()
        if mapped:
            return mapped
    if str(row.related_object_type or "") in {
        "candidate_draft",
        "band_checkpoint",
        "future_plan_audit_run",
        "scenario_rehearsal_run",
        "generation_audit_checkpoint",
    }:
        return str(row.related_object_id or "").strip()
    return ""


def _snapshot_value(*payloads: dict[str, Any], key: str) -> str:
    for payload in payloads:
        value = str(payload.get(key) or "").strip()
        if value:
            return value
    return ""


def _snapshot_chapter(*payloads: dict[str, Any]) -> int:
    for payload in payloads:
        chapter_number = _nonnegative_int(payload.get("chapter_number"))
        if chapter_number:
            return chapter_number
    return 0


def _parse_timestamp(value: object) -> datetime | None:
    text = str(value or "").strip()
    if not text:
        return None
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None


def _manual_duration(payload: dict[str, Any]) -> int | None:
    if "duration_ms" in payload:
        return _optional_nonnegative_int(payload.get("duration_ms"))
    if "duration_seconds" in payload:
        seconds = payload.get("duration_seconds")
        if isinstance(seconds, bool) or seconds is None:
            return None
        try:
            return max(0, int(float(seconds) * 1000))
        except (TypeError, ValueError):
            return None
    started_at = _parse_timestamp(payload.get("started_at"))
    finished_at = _parse_timestamp(payload.get("finished_at"))
    if started_at is None or finished_at is None:
        return None
    try:
        return max(0, int((finished_at - started_at).total_seconds() * 1000))
    except TypeError:
        return None


class CostLedgerService:
    def __init__(self, session) -> None:
        self.session = session

    def report(
        self,
        *,
        project_id: str = "",
        chapter_number: int = 0,
        band_id: str = "",
        candidate_id: str = "",
    ) -> CostLedgerReport:
        project_id = str(project_id or "").strip()
        chapter_number = _nonnegative_int(chapter_number)
        band_id = str(band_id or "").strip()
        candidate_id = str(candidate_id or "").strip()

        events = self._events(project_id=project_id)
        candidate_by_review = self._candidate_by_review(project_id=project_id)
        event_by_id = {str(row.id or ""): row for row in events}
        trace_event = {
            str(row.related_object_id or ""): row
            for row in events
            if str(row.related_object_type or "") == "prompt_trace"
            and str(row.related_object_id or "")
        }
        traces = self._traces(project_id=project_id)
        trace_by_id = {str(row.id or ""): row for row in traces}
        explicit_trace_event = self._explicit_trace_events(events)
        band_ranges = self._band_ranges(project_id=project_id)
        context_cache: dict[str, _TraceContext] = {}

        def resolve_context(
            trace: PromptTrace, *, visiting: frozenset[str] = frozenset()
        ) -> _TraceContext:
            trace_id = str(trace.id or "")
            if trace_id in context_cache:
                return context_cache[trace_id]
            parent_context = None
            parent_id = str(trace.parent_trace_id or "")
            if parent_id and parent_id not in visiting and parent_id in trace_by_id:
                parent_context = resolve_context(
                    trace_by_id[parent_id],
                    visiting=visiting | {trace_id},
                )
            context = self._trace_context(
                trace,
                event_by_id=event_by_id,
                related_trace_event=trace_event.get(trace_id),
                explicit_trace_event=explicit_trace_event.get(trace_id),
                parent_context=parent_context,
                candidate_by_review=candidate_by_review,
            )
            if not context.band_id and context.chapter_number:
                context.band_id = self._band_for_chapter(
                    project_id=context.project_id,
                    chapter_number=context.chapter_number,
                    band_ranges=band_ranges,
                )
            context_cache[trace_id] = context
            return context

        matched_traces: list[PromptTrace] = []
        records: list[_CostRecord] = []
        for trace in traces:
            context = resolve_context(trace)
            if not self._matches(
                project_id=context.project_id,
                chapter_number=context.chapter_number,
                band_id=context.band_id,
                candidate_id=context.candidate_id,
                project_filter=project_id,
                chapter_filter=chapter_number,
                band_filter=band_id,
                candidate_filter=candidate_id,
            ):
                continue
            matched_traces.append(trace)
            records.extend(self._trace_records(trace, context=context))

        total = _MutableMetrics()
        dimension_totals: dict[tuple[CostDimension, str], _MutableMetrics] = (
            defaultdict(_MutableMetrics)
        )
        gate_totals: dict[str, _MutableMetrics] = defaultdict(_MutableMetrics)
        for record in records:
            total.add(record)
            for dimension, value in self._record_dimensions(record):
                dimension_totals[(dimension, value)].add(record)
            if record.gate_id:
                gate_totals[record.gate_id].add(record)

        matched_events = [
            row
            for row in events
            if self._matches_event(
                row,
                project_filter=project_id,
                chapter_filter=chapter_number,
                band_filter=band_id,
                candidate_filter=candidate_id,
                candidate_by_review=candidate_by_review,
                band_ranges=band_ranges,
            )
        ]
        manual_actions = self._manual_actions(matched_events)
        project_ids = set(
            self.session.scalars(
                select(Project.id).where(Project.id == project_id)
                if project_id
                else select(Project.id)
            ).all()
        )
        return CostLedgerReport(
            project_id=project_id,
            chapter_number=chapter_number,
            band_id=band_id,
            candidate_id=candidate_id,
            project_count=len(project_ids),
            trace_count=len(matched_traces),
            event_count=len(matched_events),
            totals=total.freeze(),
            dimensions=[
                CostDimensionMetric(
                    dimension=dimension,
                    value=value,
                    metrics=metrics.freeze(),
                )
                for (dimension, value), metrics in sorted(
                    dimension_totals.items(),
                    key=lambda item: (
                        _DIMENSION_ORDER.index(item[0][0]),
                        item[0][1],
                    ),
                )
            ],
            gate_costs=[
                GateCostMetric(gate_id=gate_id, metrics=metrics.freeze())
                for gate_id, metrics in sorted(gate_totals.items())
            ],
            manual_action_count=sum(item.count for item in manual_actions),
            manual_action_duration_ms=sum(item.duration_ms for item in manual_actions),
            unknown_manual_duration_count=sum(
                item.unknown_duration_count for item in manual_actions
            ),
            manual_actions=manual_actions,
        )

    def _events(self, *, project_id: str) -> list[DecisionEvent]:
        statement = select(DecisionEvent).order_by(
            DecisionEvent.created_at.asc(), DecisionEvent.id.asc()
        )
        if project_id:
            statement = statement.where(DecisionEvent.project_id == project_id)
        return list(self.session.scalars(statement).all())

    def _traces(self, *, project_id: str) -> list[PromptTrace]:
        statement = select(PromptTrace).order_by(
            PromptTrace.created_at.asc(), PromptTrace.id.asc()
        )
        if project_id:
            statement = statement.where(PromptTrace.project_id == project_id)
        return list(self.session.scalars(statement).all())

    def _candidate_by_review(self, *, project_id: str) -> dict[str, str]:
        statement = select(CandidateDraftRecord)
        if project_id:
            statement = statement.where(CandidateDraftRecord.project_id == project_id)
        return {
            str(row.review_id or ""): str(row.id or "")
            for row in self.session.scalars(statement).all()
            if str(row.review_id or "").strip()
        }

    def _band_ranges(self, *, project_id: str) -> list[tuple[str, str, int, int]]:
        statement = select(BandExperiencePlan)
        if project_id:
            statement = statement.where(BandExperiencePlan.project_id == project_id)
        return [
            (
                str(row.project_id or ""),
                str(row.band_id or ""),
                int(row.chapter_start or 0),
                int(row.chapter_end or 0),
            )
            for row in self.session.scalars(statement).all()
            if str(row.band_id or "").strip()
        ]

    @staticmethod
    def _band_for_chapter(
        *,
        project_id: str,
        chapter_number: int,
        band_ranges: list[tuple[str, str, int, int]],
    ) -> str:
        matches = {
            band_id
            for row_project_id, band_id, chapter_start, chapter_end in band_ranges
            if row_project_id == project_id
            and chapter_start > 0
            and chapter_start <= chapter_number <= chapter_end
        }
        return next(iter(matches)) if len(matches) == 1 else ""

    @staticmethod
    def _explicit_trace_events(
        events: list[DecisionEvent],
    ) -> dict[str, DecisionEvent]:
        result: dict[str, DecisionEvent] = {}
        for row in events:
            payload = _json_object(row.payload_json)
            outcome = parse_gate_outcome(payload)
            trace_ids = list(outcome.trace_ids) if outcome is not None else []
            if outcome is not None:
                trace_ids.extend(
                    str(ref).removeprefix("prompt_trace:")
                    for ref in outcome.evidence_refs
                    if str(ref).startswith("prompt_trace:")
                )
            payload_trace_id = str(payload.get("trace_id") or "").strip()
            if payload_trace_id:
                trace_ids.append(payload_trace_id)
            for trace_id in trace_ids:
                normalized = str(trace_id or "").strip()
                if normalized and normalized not in result:
                    result[normalized] = row
        return result

    @staticmethod
    def _matches(
        *,
        project_id: str,
        chapter_number: int,
        band_id: str,
        candidate_id: str,
        project_filter: str,
        chapter_filter: int,
        band_filter: str,
        candidate_filter: str,
    ) -> bool:
        return bool(
            (not project_filter or project_id == project_filter)
            and (not chapter_filter or chapter_number == chapter_filter)
            and (not band_filter or band_id == band_filter)
            and (not candidate_filter or candidate_id == candidate_filter)
        )

    @classmethod
    def _matches_event(
        cls,
        row: DecisionEvent,
        *,
        project_filter: str,
        chapter_filter: int,
        band_filter: str,
        candidate_filter: str,
        candidate_by_review: dict[str, str],
        band_ranges: list[tuple[str, str, int, int]],
    ) -> bool:
        payload = _json_object(row.payload_json)
        row_project_id = str(row.project_id or "")
        row_chapter_number = int(row.chapter_number or 0)
        row_band_id = str(row.band_id or "")
        if not row_band_id and row_chapter_number:
            row_band_id = cls._band_for_chapter(
                project_id=row_project_id,
                chapter_number=row_chapter_number,
                band_ranges=band_ranges,
            )
        return cls._matches(
            project_id=row_project_id,
            chapter_number=row_chapter_number,
            band_id=row_band_id,
            candidate_id=_event_candidate(
                row, payload, candidate_by_review=candidate_by_review
            ),
            project_filter=project_filter,
            chapter_filter=chapter_filter,
            band_filter=band_filter,
            candidate_filter=candidate_filter,
        )

    @staticmethod
    def _trace_context(
        trace: PromptTrace,
        *,
        event_by_id: dict[str, DecisionEvent],
        related_trace_event: DecisionEvent | None,
        explicit_trace_event: DecisionEvent | None,
        parent_context: _TraceContext | None,
        candidate_by_review: dict[str, str],
    ) -> _TraceContext:
        input_snapshot = _json_object(trace.input_snapshot_json)
        output_summary = _json_object(trace.output_summary_json)
        model_profile = _json_object(trace.model_profile_json)
        chapter_number = _snapshot_chapter(input_snapshot, output_summary)
        band_id = _snapshot_value(input_snapshot, output_summary, key="band_id")
        candidate_id = _snapshot_value(
            input_snapshot, output_summary, key="candidate_id"
        )
        gate_id = ""
        current = event_by_id.get(str(trace.decision_event_id or ""))
        if current is None:
            current = explicit_trace_event
        if current is None:
            current = related_trace_event
        visited: set[str] = set()
        while current is not None and str(current.id or "") not in visited:
            visited.add(str(current.id or ""))
            payload = _json_object(current.payload_json)
            outcome = parse_gate_outcome(payload)
            chapter_number = chapter_number or int(current.chapter_number or 0)
            band_id = band_id or str(current.band_id or "")
            candidate_id = candidate_id or _event_candidate(
                current,
                payload,
                candidate_by_review=candidate_by_review,
            )
            if outcome is not None and not gate_id:
                gate_id = outcome.gate_id
                chapter_number = chapter_number or int(outcome.chapter_number or 0)
                band_id = band_id or outcome.band_id
                candidate_id = candidate_id or outcome.candidate_id
            parent_id = str(current.parent_event_id or "")
            if not parent_id:
                root_id = str(current.causal_root_id or "")
                parent_id = root_id if root_id != str(current.id or "") else ""
            current = event_by_id.get(parent_id) if parent_id else None
        if parent_context is not None:
            chapter_number = chapter_number or parent_context.chapter_number
            band_id = band_id or parent_context.band_id
            candidate_id = candidate_id or parent_context.candidate_id
            gate_id = gate_id or parent_context.gate_id
        return _TraceContext(
            project_id=str(trace.project_id or ""),
            chapter_number=chapter_number,
            band_id=band_id,
            candidate_id=candidate_id,
            gate_id=gate_id,
            default_stage_key=str(trace.stage_key or ""),
            default_model=str(model_profile.get("model") or ""),
            default_provider=str(
                model_profile.get("provider")
                or model_profile.get("provider_kind")
                or trace.backend
                or ""
            ),
        )

    @classmethod
    def _trace_records(
        cls, trace: PromptTrace, *, context: _TraceContext
    ) -> list[_CostRecord]:
        attempts = [
            item
            for item in _json_list(trace.attempts_json)
            if isinstance(item, dict)
            and str(item.get("source") or "") != "router_trace"
            and any(key in item for key in _ATTEMPT_SIGNAL_KEYS)
        ]
        records: list[_CostRecord] = []
        seen_by_group: dict[str, int] = defaultdict(int)
        identity_by_group: dict[str, tuple[str, str, str]] = {}
        fallback_count = 0
        for index, attempt in enumerate(attempts):
            group_id = str(attempt.get("attempt_group_id") or f"ungrouped-{index}")
            model = str(attempt.get("model") or context.default_model or "")
            provider = str(
                attempt.get("provider")
                or attempt.get("provider_kind")
                or context.default_provider
                or ""
            )
            identity = (
                provider,
                model,
                str(attempt.get("profile_id") or ""),
            )
            retry = seen_by_group[group_id] > 0
            fallback = bool(
                retry
                and identity_by_group.get(group_id)
                and identity_by_group[group_id] != identity
            )
            fallback_count += int(fallback)
            seen_by_group[group_id] += 1
            identity_by_group[group_id] = identity
            prompt_tokens_raw = _optional_nonnegative_int(
                attempt.get("prompt_tokens", attempt.get("input_tokens"))
            )
            completion_tokens_raw = _optional_nonnegative_int(
                attempt.get("completion_tokens", attempt.get("output_tokens"))
            )
            total_tokens_raw = _optional_nonnegative_int(attempt.get("total_tokens"))
            known_component = (
                prompt_tokens_raw is not None or completion_tokens_raw is not None
            )
            total_tokens = (
                total_tokens_raw
                if total_tokens_raw is not None
                else (
                    (prompt_tokens_raw or 0) + (completion_tokens_raw or 0)
                    if known_component
                    else 0
                )
            )
            usage_source = str(attempt.get("usage_source") or "").strip()
            if not usage_source:
                usage_source = "provider" if known_component else "missing"
            records.append(
                _CostRecord(
                    project_id=context.project_id,
                    chapter_number=context.chapter_number,
                    band_id=context.band_id,
                    candidate_id=context.candidate_id,
                    gate_id=context.gate_id,
                    task_family=str(attempt.get("task_family") or ""),
                    stage_key=str(
                        attempt.get("stage_key") or context.default_stage_key or ""
                    ),
                    model=model,
                    provider=provider,
                    success=cls._attempt_succeeded(attempt),
                    retry=retry,
                    fallback=fallback,
                    input_chars=_nonnegative_int(attempt.get("input_chars")),
                    output_chars=_nonnegative_int(attempt.get("output_chars")),
                    prompt_tokens=prompt_tokens_raw or 0,
                    completion_tokens=completion_tokens_raw or 0,
                    total_tokens=total_tokens,
                    duration_ms=_nonnegative_int(attempt.get("duration_ms")),
                    usage_source=usage_source,
                )
            )
        if trace.fallback_used and records and fallback_count == 0:
            records[-1].fallback = True
            if len(records) > 1:
                records[-1].retry = True
        return records

    @staticmethod
    def _attempt_succeeded(attempt: dict[str, Any]) -> bool:
        status = str(attempt.get("status") or "").strip().lower()
        if status in {"failed", "error"}:
            return False
        if bool(
            attempt.get("error_class")
            or attempt.get("final_failure")
            or attempt.get("parse_error")
        ):
            return False
        http_status = _nonnegative_int(attempt.get("http_status"))
        if http_status >= 400:
            return False
        return bool(
            status in {"succeeded", "success", "ok"}
            or _nonnegative_int(attempt.get("output_chars")) > 0
            or 200 <= http_status < 300
        )

    @staticmethod
    def _record_dimensions(
        record: _CostRecord,
    ) -> list[tuple[CostDimension, str]]:
        candidates: list[tuple[CostDimension, str]] = [
            ("project", record.project_id),
            ("chapter", str(record.chapter_number) if record.chapter_number else ""),
            ("band", record.band_id),
            ("candidate", record.candidate_id),
            ("task_family", record.task_family),
            ("stage_key", record.stage_key),
            ("model", record.model),
            ("provider", record.provider),
        ]
        return [(dimension, value) for dimension, value in candidates if value]

    @staticmethod
    def _manual_actions(events: list[DecisionEvent]) -> list[ManualActionMetric]:
        grouped: dict[tuple[str, str, str, str], _MutableManualAction] = defaultdict(
            _MutableManualAction
        )
        for row in events:
            actor_type = str(row.actor_type or "").strip()
            if actor_type not in _MANUAL_ACTOR_TYPES:
                continue
            payload = _json_object(row.payload_json)
            if not CostLedgerService._is_manual_action(row, payload=payload):
                continue
            key = (
                str(row.event_type or "").strip(),
                actor_type,
                str(row.actor_id or "").strip(),
                str(
                    payload.get("source")
                    or payload.get("actor_source")
                    or payload.get("channel")
                    or ""
                ).strip(),
            )
            metric = grouped[key]
            metric.count += 1
            duration_ms = _manual_duration(payload)
            if duration_ms is None:
                metric.unknown_duration_count += 1
            else:
                metric.duration_ms += duration_ms
        return [
            ManualActionMetric(
                action_type=key[0],
                actor_type=key[1],
                actor_id=key[2],
                source=key[3],
                count=metric.count,
                duration_ms=metric.duration_ms,
                unknown_duration_count=metric.unknown_duration_count,
            )
            for key, metric in sorted(grouped.items())
        ]

    @staticmethod
    def _is_manual_action(row: DecisionEvent, *, payload: dict[str, Any]) -> bool:
        actor_type = str(row.actor_type or "").strip()
        if actor_type == "manual_ui":
            return True
        if any(
            bool(payload.get(key))
            for key in ("manual_action", "human_origin", "initiated_by_human")
        ):
            return True
        if str(row.event_family or "") == "audit_action":
            return True
        return str(row.event_type or "") in _EXTERNAL_REQUEST_EVENT_TYPES


def render_cost_ledger_markdown(report: CostLedgerReport) -> str:
    totals = report.totals
    lines = [
        "# Cost And Intervention Ledger",
        "",
        f"- Project: `{report.project_id or 'all'}`",
        f"- Chapter: `{report.chapter_number or 'all'}`",
        f"- Band: `{report.band_id or 'all'}`",
        f"- Candidate: `{report.candidate_id or 'all'}`",
        f"- Prompt traces: {report.trace_count}",
        f"- Attempts: {totals.attempts}",
        f"- Successes: {totals.successes}",
        f"- Retries: {totals.retries}",
        f"- Fallbacks: {totals.fallbacks}",
        f"- Input/output chars: {totals.input_chars}/{totals.output_chars}",
        f"- Prompt/completion/total tokens: {totals.prompt_tokens}/{totals.completion_tokens}/{totals.total_tokens}",
        f"- LLM duration ms: {totals.duration_ms}",
        f"- Manual actions: {report.manual_action_count}",
        f"- Manual duration ms: {report.manual_action_duration_ms}",
        f"- Unknown manual duration: {report.unknown_manual_duration_count}",
        "",
        "| dimension | value | attempts | successes | retries | fallbacks | chars in/out | tokens total | duration ms |",
        "|---|---|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for item in report.dimensions:
        metric = item.metrics
        lines.append(
            f"| {item.dimension} | {item.value} | {metric.attempts} | "
            f"{metric.successes} | {metric.retries} | {metric.fallbacks} | "
            f"{metric.input_chars}/{metric.output_chars} | {metric.total_tokens} | "
            f"{metric.duration_ms} |"
        )
    lines.extend(
        [
            "",
            "| gate | attempts | retries | fallbacks | total tokens | duration ms |",
            "|---|---:|---:|---:|---:|---:|",
        ]
    )
    for item in report.gate_costs:
        metric = item.metrics
        lines.append(
            f"| {item.gate_id} | {metric.attempts} | {metric.retries} | "
            f"{metric.fallbacks} | {metric.total_tokens} | {metric.duration_ms} |"
        )
    lines.extend(
        [
            "",
            "| action type | actor type | actor id | source | count | duration ms | unknown duration |",
            "|---|---|---|---|---:|---:|---:|",
        ]
    )
    for item in report.manual_actions:
        lines.append(
            f"| {item.action_type} | {item.actor_type} | {item.actor_id} | "
            f"{item.source} | {item.count} | {item.duration_ms} | "
            f"{item.unknown_duration_count} |"
        )
    return "\n".join(lines) + "\n"


__all__ = [
    "CostDimensionMetric",
    "CostLedgerReport",
    "CostLedgerService",
    "CostMetrics",
    "GateCostMetric",
    "ManualActionMetric",
    "render_cost_ledger_markdown",
]
