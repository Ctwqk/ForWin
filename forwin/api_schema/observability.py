from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.governance import (
    BandCheckpointDetail,
    BlockingReasonInfo,
    DecisionEventInfo,
    NarrativeConstraintInfo,
    PlanTaskItem,
    ProjectGovernanceSettings,
)
from forwin.protocol.subworld import SubWorldSummary
from .genesis import PromptTraceInfo


class DecisionEventsResponse(BaseModel):
    items: list[DecisionEventInfo] = Field(default_factory=list)


class StageDurationAggregate(BaseModel):
    stage: str = ""
    event_count: int = 0
    total_duration_ms: int = 0
    max_duration_ms: int = 0
    last_duration_ms: int = 0


class ArtifactManifestItem(BaseModel):
    uri: str = ""
    kind: str = ""
    redaction_state: str = ""
    source_event_id: str = ""
    trace_id: str = ""
    hash: str = ""
    size: int = 0


class TaskTimelineResponse(BaseModel):
    task_id: str
    project_id: str = ""
    events: list[DecisionEventInfo] = Field(default_factory=list)
    stage_durations: list[StageDurationAggregate] = Field(default_factory=list)
    operation_ids: list[str] = Field(default_factory=list)


class ChapterLedgerResponse(BaseModel):
    project_id: str
    chapter_number: int
    plan_status: str = ""
    events: list[DecisionEventInfo] = Field(default_factory=list)
    prompt_trace_ids: list[str] = Field(default_factory=list)
    artifact_uris: list[str] = Field(default_factory=list)
    stage_durations: list[StageDurationAggregate] = Field(default_factory=list)
    operation_ids: list[str] = Field(default_factory=list)
    artifact_manifest: list[ArtifactManifestItem] = Field(default_factory=list)


class PromptTraceDetailResponse(PromptTraceInfo):
    pass


class ArtifactReadResponse(BaseModel):
    uri: str
    content_type: str = "text/plain; charset=utf-8"
    size: int = 0
    hash: str = ""
    preview: str = ""
    truncated: bool = False


class PerformanceSpanInfo(BaseModel):
    span_id: str = ""
    parent_span_id: str = ""
    trace_id: str = ""
    span_name: str = ""
    span_kind: str = ""
    component: str = ""
    stage: str = ""
    status: str = "ok"
    project_id: str = ""
    task_id: str = ""
    operation_id: str = ""
    chapter_number: int = 0
    duration_ms: int = 0
    self_duration_ms: int = 0
    tags: dict[str, Any] = Field(default_factory=dict)
    metrics: dict[str, Any] = Field(default_factory=dict)
    error: dict[str, Any] = Field(default_factory=dict)
    created_at: str = ""


class PerformanceBreakdownItem(BaseModel):
    key: str
    count: int = 0
    total_duration_ms: int = 0
    avg_duration_ms: float = 0.0
    p50_ms: int = 0
    p95_ms: int = 0
    p99_ms: int = 0
    max_ms: int = 0
    error_count: int = 0
    error_rate: float = 0.0


class PerformanceReportResponse(BaseModel):
    project_id: str = ""
    task_id: str = ""
    chapter_number: int = 0
    total_duration_ms: int = 0
    top_slow_spans: list[PerformanceSpanInfo] = Field(default_factory=list)
    critical_path: list[PerformanceSpanInfo] = Field(default_factory=list)
    component_breakdown: list[PerformanceBreakdownItem] = Field(default_factory=list)
    stage_breakdown: list[PerformanceBreakdownItem] = Field(default_factory=list)
    llm_breakdown: list[PerformanceBreakdownItem] = Field(default_factory=list)
    db_breakdown: list[PerformanceBreakdownItem] = Field(default_factory=list)
    recommendations: list[str] = Field(default_factory=list)


class CausalReplayResponse(BaseModel):
    root_event: DecisionEventInfo | None = None
    timeline: list[DecisionEventInfo] = Field(default_factory=list)
    branches: dict[str, list[DecisionEventInfo]] = Field(default_factory=dict)
    current_outcome: str = ""
    linked_review_refs: list[DecisionEventInfo] = Field(default_factory=list)
    linked_checkpoint_refs: list[DecisionEventInfo] = Field(default_factory=list)


class GovernanceInsightsResponse(BaseModel):
    top_override_rule_types: list[dict[str, Any]] = Field(default_factory=list)
    top_override_reasons: list[dict[str, Any]] = Field(default_factory=list)
    top_warn_but_allowed_issue_types: list[dict[str, Any]] = Field(default_factory=list)
    top_constraint_false_positive_types: list[dict[str, Any]] = Field(default_factory=list)
    forced_accept_frequency: int = 0
    most_common_blocking_reasons: list[dict[str, Any]] = Field(default_factory=list)
    recent_band_checkpoint_distribution: list[dict[str, Any]] = Field(default_factory=list)
    issue_group_distribution: list[dict[str, Any]] = Field(default_factory=list)
    recent_action_effectiveness: list[dict[str, Any]] = Field(default_factory=list)
    recommended_adjustments: list[dict[str, Any]] = Field(default_factory=list)
    recent_examples: list[dict[str, Any]] = Field(default_factory=list)


__all__ = [
    'DecisionEventsResponse',
    'StageDurationAggregate',
    'ArtifactManifestItem',
    'TaskTimelineResponse',
    'ChapterLedgerResponse',
    'PromptTraceDetailResponse',
    'ArtifactReadResponse',
    'PerformanceSpanInfo',
    'PerformanceBreakdownItem',
    'PerformanceReportResponse',
    'CausalReplayResponse',
    'GovernanceInsightsResponse',
]
