from __future__ import annotations

import json
from collections import Counter, defaultdict
from dataclasses import asdict, dataclass
from datetime import datetime
from typing import Any, Literal, get_args

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import parse_gate_outcome
from forwin.models.audit import DecisionEvent
from forwin.models.draft import CandidateDraftRecord
from forwin.models.genesis import PromptTrace
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.world_engine.conflict_types import ConflictOption


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
        "workflow_attempt_no",
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


class RoleCostMetric(BaseModel):
    model_config = ConfigDict(frozen=True)

    role: str
    metrics: CostMetrics = Field(default_factory=CostMetrics)


class ChapterCostAutonomy(BaseModel):
    model_config = ConfigDict(frozen=True)

    project_id: str
    chapter_number: int
    roles: list[RoleCostMetric] = Field(default_factory=list)
    world_conflicts: int = 0
    human_decided_conflicts: int = 0
    band_checkpoint_pauses_by_code: dict[str, int] = Field(default_factory=dict)
    band_payload_trim_runs: int = 0
    band_payload_trim_events: int = 0
    band_payload_removed_chars: int = 0
    band_payload_removed_items: dict[str, int] = Field(default_factory=dict)
    oversized_intent_deferrals: dict[str, int] = Field(default_factory=dict)
    repeatedly_deferred_intents: dict[str, int] = Field(default_factory=dict)


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
    chapters: list[ChapterCostAutonomy] = Field(default_factory=list)


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
        represented_trace_ids = {
            str(trace.id) for trace in traces
            if any(isinstance(item, dict) and item.get("source") != "router_trace"
                   and any(key in item for key in _ATTEMPT_SIGNAL_KEYS) for item in _json_list(trace.attempts_json))
        }
        explicit_trace_event = self._explicit_trace_events(events)
        band_ranges = self._band_ranges(project_id=project_id)
        chapter_arc_ids = self._chapter_arc_ids(project_id=project_id)
        descendant_candidates = self._descendant_candidates(
            traces,
            event_by_id=event_by_id,
            trace_event=trace_event,
            explicit_trace_event=explicit_trace_event,
            candidate_by_review=candidate_by_review,
        )
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
                descendant_candidate=descendant_candidates.get(trace_id, ""),
            )
            if not context.band_id and context.chapter_number:
                context.band_id = self._band_for_chapter(
                    project_id=context.project_id,
                    chapter_number=context.chapter_number,
                    band_ranges=band_ranges,
                    chapter_arc_ids=chapter_arc_ids,
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

        for event in events:
            if event.event_type not in {DecisionEventType.LLM_REQUEST_SUCCEEDED, DecisionEventType.LLM_REQUEST_FAILED}:
                continue
            payload = _json_object(event.payload_json)
            trace_id = str(payload.get("prompt_trace_id") or "")
            related_trace_id = str(event.related_object_id or "") if event.related_object_type == "prompt_trace" else ""
            # Writer workflow envelopes describe a whole operation, not an additional model call.
            if not (trace_id or related_trace_id or payload.get("stage_key") or payload.get("attempt_group_id")):
                continue
            if trace_id in represented_trace_ids or related_trace_id in represented_trace_ids:
                continue
            linked_trace = trace_by_id.get(trace_id) or trace_by_id.get(related_trace_id)
            trace_context = (resolve_context(linked_trace) if linked_trace is not None
                             else _TraceContext(project_id=event.project_id))
            outcome = parse_gate_outcome(payload)
            event_chapter = int(event.chapter_number or 0) or _snapshot_chapter(payload) or trace_context.chapter_number
            context = _TraceContext(
                project_id=event.project_id, chapter_number=event_chapter,
                band_id=(event.band_id or _snapshot_value(payload, key="band_id") or trace_context.band_id
                         or self._band_for_chapter(
                             project_id=event.project_id, chapter_number=event_chapter,
                             band_ranges=band_ranges, chapter_arc_ids=chapter_arc_ids)),
                candidate_id=(_event_candidate(event, payload, candidate_by_review=candidate_by_review)
                              or trace_context.candidate_id),
                gate_id=((outcome.gate_id if outcome is not None else "")
                         or _snapshot_value(payload, key="gate_id") or trace_context.gate_id),
                default_stage_key=str(payload.get("stage_key") or trace_context.default_stage_key),
                default_model=trace_context.default_model,
                default_provider=trace_context.default_provider,
            )
            if not self._matches(
                project_id=context.project_id, chapter_number=context.chapter_number, band_id=context.band_id,
                candidate_id=context.candidate_id, project_filter=project_id, chapter_filter=chapter_number,
                band_filter=band_id, candidate_filter=candidate_id,
            ):
                continue
            attempt = {**payload, "attempt_no": payload.get("attempt_no", 1),
                       "status": "success" if event.event_type == DecisionEventType.LLM_REQUEST_SUCCEEDED else "failed"}
            records.extend(self._trace_records(PromptTrace(attempts_json=json.dumps([attempt])), context=context))

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
                chapter_arc_ids=chapter_arc_ids,
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
            chapters=self._chapter_reports(
                records=records, traces=matched_traces, contexts=context_cache, events=matched_events,
                all_events=events, project_id=project_id, chapter_number=chapter_number,
                band_id=band_id, candidate_id=candidate_id, band_ranges=band_ranges, chapter_arc_ids=chapter_arc_ids,
            ),
        )

    def _chapter_reports(
        self, *, records: list[_CostRecord], traces: list[PromptTrace], contexts: dict[str, _TraceContext],
        events: list[DecisionEvent], all_events: list[DecisionEvent], project_id: str, chapter_number: int,
        band_id: str, candidate_id: str, band_ranges: list[tuple[str, str, str, int, int]],
        chapter_arc_ids: dict[tuple[str, int], str],
    ) -> list[ChapterCostAutonomy]:
        chapters: dict[tuple[str, int], ChapterCostAutonomy] = {}
        roles: dict[tuple[str, int], dict[str, _MutableMetrics]] = defaultdict(lambda: defaultdict(_MutableMetrics))

        def chapter(project: str, number: int | None) -> ChapterCostAutonomy:
            key = (project, int(number or 0))
            if key not in chapters:
                chapters[key] = ChapterCostAutonomy(project_id=key[0], chapter_number=key[1])
            return chapters[key]

        for record in records:
            chapter(record.project_id, record.chapter_number)
            roles[(record.project_id, record.chapter_number)][_record_role(record)].add(record)
        for trace in traces:
            context = contexts[str(trace.id)]
            row = chapter(context.project_id, context.chapter_number)
            trim = _json_object(trace.output_summary_json).get("band_payload_trim")
            if not isinstance(trim, dict) or not trim:
                continue
            removed = trim.get("removed")
            removed = removed if isinstance(removed, dict) else {}
            chars = max(0, _nonnegative_int(trim.get("before_chars")) - _nonnegative_int(trim.get("after_chars")))
            counts = Counter(row.band_payload_removed_items)
            counts.update({str(key): _nonnegative_int(value) for key, value in removed.items()})
            chapters[(row.project_id, row.chapter_number)] = row.model_copy(update={
                "band_payload_trim_runs": row.band_payload_trim_runs + 1,
                "band_payload_trim_events": row.band_payload_trim_events + int(bool(chars or trim.get("overflow"))),
                "band_payload_removed_chars": row.band_payload_removed_chars + chars,
                "band_payload_removed_items": dict(sorted(counts.items())),
            })

        deferrals: dict[tuple[str, str], Counter[int]] = defaultdict(Counter)
        for event in all_events:
            payload = _json_object(event.payload_json)
            if (event.event_type == DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED
                    and payload.get("reason", event.reason) == "prompt_too_large_deferred"
                    and payload.get("intent_id")):
                deferrals[(event.project_id, str(payload["intent_id"]))][int(event.chapter_number or 0)] += 1
        for event in events:
            row = chapter(event.project_id, event.chapter_number)
            payload = _json_object(event.payload_json)
            if event.event_type == DecisionEventType.BAND_CHECKPOINT_HIT:
                outcome = parse_gate_outcome(payload)
                codes = outcome.issue_keys if outcome is not None else payload.get("issue_codes", [])
                codes = codes if isinstance(codes, list) else []
                for code in sorted({str(code) for code in codes if code} or {"unknown"}):
                    row.band_checkpoint_pauses_by_code[code] = row.band_checkpoint_pauses_by_code.get(code, 0) + 1
            if (event.event_type == DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED
                    and payload.get("reason", event.reason) == "prompt_too_large_deferred" and payload.get("intent_id")):
                intent = str(payload["intent_id"])
                row.oversized_intent_deferrals[intent] = row.oversized_intent_deferrals.get(intent, 0) + 1
                count = sum(count for number, count in deferrals[(event.project_id, intent)].items()
                            if number <= row.chapter_number)
                if count > 1:
                    row.repeatedly_deferred_intents[intent] = count
        statement = select(WorldPlanConflictRow).order_by(WorldPlanConflictRow.id)
        if project_id:
            statement = statement.where(WorldPlanConflictRow.project_id == project_id)
        for conflict in self.session.scalars(statement).all():
            number = (conflict.blocks_from_chapter if conflict.source == "chapter_extraction"
                      else conflict.detected_after_chapter)
            conflict_band = self._band_for_chapter(
                project_id=conflict.project_id, chapter_number=number,
                band_ranges=band_ranges, chapter_arc_ids=chapter_arc_ids,
            )
            refs = _json_list(conflict.result_refs_json)
            if not self._matches(
                project_id=conflict.project_id, chapter_number=number, band_id=conflict_band,
                candidate_id=candidate_id if candidate_id and f"candidate:{candidate_id}" in refs else "",
                project_filter=project_id, chapter_filter=chapter_number, band_filter=band_id, candidate_filter=candidate_id,
            ):
                continue
            row = chapter(conflict.project_id, number)
            human = (conflict.status == "resolved" and conflict.decision_option in get_args(ConflictOption)
                     and _json_object(conflict.decision_json).get("decided_by") == "human")
            chapters[(row.project_id, number)] = row.model_copy(update={
                "world_conflicts": row.world_conflicts + 1,
                "human_decided_conflicts": row.human_decided_conflicts + int(human),
            })
        return [row.model_copy(update={
            "roles": [RoleCostMetric(role=role, metrics=metrics.freeze())
                      for role, metrics in sorted(roles[key].items())],
            "band_checkpoint_pauses_by_code": dict(sorted(row.band_checkpoint_pauses_by_code.items())),
            "oversized_intent_deferrals": dict(sorted(row.oversized_intent_deferrals.items())),
            "repeatedly_deferred_intents": dict(sorted(row.repeatedly_deferred_intents.items())),
        }) for key, row in sorted(chapters.items())]

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

    def _band_ranges(
        self, *, project_id: str
    ) -> list[tuple[str, str, str, int, int]]:
        statement = select(BandExperiencePlan)
        if project_id:
            statement = statement.where(BandExperiencePlan.project_id == project_id)
        return [
            (
                str(row.project_id or ""),
                str(row.arc_id or ""),
                str(row.band_id or ""),
                int(row.chapter_start or 0),
                int(row.chapter_end or 0),
            )
            for row in self.session.scalars(statement).all()
            if str(row.band_id or "").strip()
        ]

    def _chapter_arc_ids(self, *, project_id: str) -> dict[tuple[str, int], str]:
        statement = select(ChapterPlan)
        if project_id:
            statement = statement.where(ChapterPlan.project_id == project_id)
        selected: dict[tuple[str, int], tuple[tuple[int, datetime, str], str]] = {}
        for row in self.session.scalars(statement).all():
            key = (str(row.project_id or ""), int(row.chapter_number or 0))
            rank = (
                int(str(row.status or "") == "accepted"),
                row.created_at or datetime.min,
                str(row.id or ""),
            )
            current = selected.get(key)
            if current is None or rank > current[0]:
                selected[key] = (rank, str(row.arc_plan_id or ""))
        return {key: value[1] for key, value in selected.items() if value[1]}

    @staticmethod
    def _band_for_chapter(
        *,
        project_id: str,
        chapter_number: int,
        band_ranges: list[tuple[str, str, str, int, int]],
        chapter_arc_ids: dict[tuple[str, int], str],
    ) -> str:
        matching_rows = [
            (arc_id, band_id)
            for (
                row_project_id,
                arc_id,
                band_id,
                chapter_start,
                chapter_end,
            ) in band_ranges
            if row_project_id == project_id
            and chapter_start > 0
            and chapter_start <= chapter_number <= chapter_end
        ]
        chapter_arc_id = chapter_arc_ids.get((project_id, chapter_number), "")
        if chapter_arc_id:
            matching_rows = [
                row for row in matching_rows if row[0] == chapter_arc_id
            ]
        matches = {band_id for _arc_id, band_id in matching_rows}
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

    @classmethod
    def _descendant_candidates(
        cls,
        traces: list[PromptTrace],
        *,
        event_by_id: dict[str, DecisionEvent],
        trace_event: dict[str, DecisionEvent],
        explicit_trace_event: dict[str, DecisionEvent],
        candidate_by_review: dict[str, str],
    ) -> dict[str, str]:
        trace_by_id = {str(row.id or ""): row for row in traces}
        candidates_by_ancestor: dict[str, set[str]] = defaultdict(set)
        for trace in traces:
            trace_id = str(trace.id or "")
            direct = cls._trace_context(
                trace,
                event_by_id=event_by_id,
                related_trace_event=trace_event.get(trace_id),
                explicit_trace_event=explicit_trace_event.get(trace_id),
                parent_context=None,
                candidate_by_review=candidate_by_review,
            ).candidate_id
            if not direct:
                continue
            parent_id = str(trace.parent_trace_id or "")
            visited: set[str] = set()
            while parent_id and parent_id not in visited:
                visited.add(parent_id)
                parent = trace_by_id.get(parent_id)
                if parent is None:
                    break
                candidates_by_ancestor[parent_id].add(direct)
                parent_id = str(parent.parent_trace_id or "")
        return {
            trace_id: next(iter(candidate_ids))
            for trace_id, candidate_ids in candidates_by_ancestor.items()
            if len(candidate_ids) == 1
        }

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
        band_ranges: list[tuple[str, str, str, int, int]],
        chapter_arc_ids: dict[tuple[str, int], str],
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
                chapter_arc_ids=chapter_arc_ids,
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
        descendant_candidate: str = "",
    ) -> _TraceContext:
        input_snapshot = _json_object(trace.input_snapshot_json)
        output_summary = _json_object(trace.output_summary_json)
        model_profile = _json_object(trace.model_profile_json)
        chapter_number = _snapshot_chapter(input_snapshot, output_summary)
        band_id = _snapshot_value(input_snapshot, output_summary, key="band_id")
        candidate_id = _snapshot_value(
            input_snapshot, output_summary, key="candidate_id"
        )
        gate_id = _snapshot_value(input_snapshot, output_summary, key="gate_id")
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
        candidate_id = candidate_id or str(descendant_candidate or "")
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
        workflow_retry_pending: set[tuple[str, str]] = set()
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
            task_family = str(attempt.get("task_family") or "")
            stage_key = str(
                attempt.get("stage_key") or context.default_stage_key or ""
            )
            workflow_key = (task_family, stage_key)
            retry = bool(
                attempt.get("workflow_retry")
                or _nonnegative_int(attempt.get("workflow_attempt_no")) > 1
                or workflow_key in workflow_retry_pending
                or seen_by_group[group_id] > 0
            )
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
            success = cls._attempt_succeeded(attempt)
            records.append(
                _CostRecord(
                    project_id=context.project_id,
                    chapter_number=context.chapter_number,
                    band_id=context.band_id,
                    candidate_id=context.candidate_id,
                    gate_id=context.gate_id,
                    task_family=task_family,
                    stage_key=stage_key,
                    model=model,
                    provider=provider,
                    success=success,
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
            if bool(
                attempt.get("parse_error")
                or attempt.get("final_failure")
                or str(attempt.get("status") or "").strip().lower()
                in {"failed", "error"}
            ):
                workflow_retry_pending.add(workflow_key)
            elif retry and success:
                workflow_retry_pending.discard(workflow_key)
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


def _record_role(record: _CostRecord) -> str:
    stage, family = record.stage_key, record.task_family
    if stage in {"review.world_conflict", "spark_pause_gate"} or stage.startswith("gate_") or family == "codex_delegate":
        return "codex_delegate"
    if stage.startswith("world.") or family == "world_engine":
        return "world_engine"
    if stage.startswith("plan.") or family in {"planner", "planning", "arc_planning", "chapter_plan_materialization"}:
        return "planner"
    if "extract" in stage or "chapter_delta" in stage or family == "extraction":
        return "extraction"
    if stage.startswith("review") or stage in {"chapter_review", "chapter_review_form", "chapter_review_json_repair", "repair_verification"} or family in {"review", "reviewer", "review_chapter", "chapter_review_form"}:
        return "reviewer"
    if family in {"writer", "write_chapter", "repair"} or stage in {"chapter_draft", "chapter_rewrite", "writer_preview", "writer_preview_fallback", "chapter_generation", "chapter_repair", "scene_generation", "scene_breakdown", "scene_stitch"}:
        return "writer"
    return "other"


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
    for chapter in report.chapters:
        lines.extend(["", f"## Chapter {chapter.chapter_number} ({chapter.project_id})", "",
                      "| role | calls | prompt tokens | completion tokens | total tokens |",
                      "|---|---:|---:|---:|---:|"])
        for role in chapter.roles:
            metrics = role.metrics
            lines.append(f"| {role.role} | {metrics.attempts} | {metrics.prompt_tokens} | {metrics.completion_tokens} | {metrics.total_tokens} |")
        lines.extend(["", f"- World conflicts: {chapter.world_conflicts}; human-decided: {chapter.human_decided_conflicts}",
                      f"- Band checkpoint pauses by code: {json.dumps(chapter.band_checkpoint_pauses_by_code, sort_keys=True)}",
                      f"- Band payload trims: {chapter.band_payload_trim_events}/{chapter.band_payload_trim_runs} runs; removed chars: {chapter.band_payload_removed_chars}; removed items: {json.dumps(chapter.band_payload_removed_items, sort_keys=True)}",
                      f"- Oversized intent deferrals: {json.dumps(chapter.oversized_intent_deferrals, sort_keys=True)}",
                      f"- Repeatedly deferred intents (through chapter): {json.dumps(chapter.repeatedly_deferred_intents, sort_keys=True)}"])
    return "\n".join(lines) + "\n"


__all__ = [
    "ChapterCostAutonomy",
    "RoleCostMetric",
    "CostDimensionMetric",
    "CostLedgerReport",
    "CostLedgerService",
    "CostMetrics",
    "GateCostMetric",
    "ManualActionMetric",
    "render_cost_ledger_markdown",
]
