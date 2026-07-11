from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, Field

from forwin.audit.events import DecisionEventInfo
from forwin.planning.contracts import PlanTaskItem


CheckpointStatus = Literal["pending", "pass", "warn", "fail", "error", "overridden"]


CHECKPOINT_STATUS_VALUES = {"pending", "pass", "warn", "fail", "error", "overridden"}


def normalize_checkpoint_status(value: object) -> str:
    raw = str(value or "").strip()
    if raw in CHECKPOINT_STATUS_VALUES:
        return raw
    return "error"


BlockingReasonCode = Literal[
    "",
    "chapter_not_canon",
    "band_checkpoint_pending",
    "band_checkpoint_warn",
    "band_checkpoint_fail",
    "future_constraint_block",
]


OverClosureRiskCategory = Literal[
    "",
    "character_locked_out",
    "thread_closed_too_early",
    "relationship_closed_too_early",
    "secret_over_explained",
    "growth_arc_completed_too_early",
]


IssueGroup = Literal[
    "",
    "fact_conflict",
    "director_imbalance",
    "runtime_observation",
    "operator_action",
]


class BlockingReasonInfo(BaseModel):
    code: BlockingReasonCode = ""
    message: str = ""
    chapter_number: int = 0
    band_id: str = ""
    decision_event_id: str = ""
    detail: str = ""


class BandCheckpointIssueInfo(BaseModel):
    code: str = ""
    severity: str = "info"
    category: OverClosureRiskCategory = ""
    issue_group: IssueGroup = ""
    description: str = ""
    detail: str = ""


class BandCheckpointDetail(BaseModel):
    id: str = ""
    project_id: str = ""
    arc_id: str = ""
    band_id: str = ""
    chapter_start: int = 0
    chapter_end: int = 0
    trigger_source: str = ""
    boundary_kind: str = ""
    boundary_chapter: int = 0
    status: CheckpointStatus = "pending"
    summary: str = ""
    reason: str = ""
    issues: list[BandCheckpointIssueInfo] = Field(default_factory=list)
    decision_refs: list[DecisionEventInfo] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str = ""
    resolved_at: str = ""


class NextBandSummary(BaseModel):
    band_id: str = ""
    chapter_start: int = 0
    chapter_end: int = 0
    chapter_titles: list[str] = Field(default_factory=list)
    band_task_contract: list[PlanTaskItem] = Field(default_factory=list)


def band_is_first_chapter(band_start: int, chapter_number: int) -> bool:
    return int(chapter_number or 0) == int(band_start or 0)


def chapter_blocking_message(
    reason: BlockingReasonCode, *, chapter_number: int = 0, band_id: str = ""
) -> str:
    if reason == "chapter_not_canon":
        return f"前序章节尚未进入 canon，暂不能开启第{chapter_number}章。"
    if reason == "band_checkpoint_pending":
        return f"{band_id or '上一 band'} 尚未完成 checkpoint 放行。"
    if reason == "band_checkpoint_warn":
        return f"{band_id or '上一 band'} checkpoint 出现警告，需人工确认后继续。"
    if reason == "band_checkpoint_fail":
        return f"{band_id or '上一 band'} checkpoint 未通过，需修复或 override 后继续。"
    if reason == "future_constraint_block":
        return "存在未来叙事约束冲突，需先处理后才能继续。"
    return ""


__all__ = [
    "BandCheckpointDetail",
    "BandCheckpointIssueInfo",
    "BlockingReasonCode",
    "BlockingReasonInfo",
    "CheckpointStatus",
    "IssueGroup",
    "NextBandSummary",
    "OverClosureRiskCategory",
    "band_is_first_chapter",
    "chapter_blocking_message",
    "normalize_checkpoint_status",
]
