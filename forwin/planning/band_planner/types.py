"""A band plan: one chapter plan draft per chapter of a beat-sheet band, and the planner's outcome."""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Literal, Protocol, get_args

from pydantic import BaseModel, ConfigDict, Field

from forwin.planning.beat_sheet.types import BandSpan

if TYPE_CHECKING:
    from sqlalchemy.orm import Session

    from forwin.book_state.runtime import BookStateRuntime
    from forwin.models.project import ChapterPlan
    from forwin.planning.beat_sheet.types import ArcBeatSheetData
    from forwin.runtime.policy import RuntimePolicy


WorldBindingKind = Literal["intent", "event", "news", "clock", "relation", "secret"]
WORLD_BINDING_KINDS: tuple[str, ...] = get_args(WorldBindingKind)
PlanIssueSeverity = Literal["error", "warning"]
# Plan rows still to write: the only ones a band plan may rewrite (accepted, drafted and needs_review never).
REWRITABLE_PLAN_STATUSES = ("planned", "failed")


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True)


class WorldBinding(_Frozen):
    """A world element the chapter takes up: ``ref`` is the intent, event, clock or relation id, or a fact ref."""

    kind: WorldBindingKind
    ref: str
    how: str = ""


class RevealDirective(_Frozen):
    id: str
    fact_ref: str
    observer: str
    target_state: str


class HintDirective(_Frozen):
    id: str
    fact_ref: str
    hint_text: str


class ExpectedSpan(_Frozen):
    amount: float
    unit: str


class IntendedChange(_Frozen):
    ref: str
    field: str
    value: Any = None


class ChapterPlanDraft(_Frozen):
    chapter_number: int
    title: str
    one_line: str
    goals: list[str]
    pov_ref: str
    onstage_refs: list[str]
    location_refs: list[str] = Field(default_factory=list)
    expected_span: ExpectedSpan
    world_bindings: list[WorldBinding] = Field(default_factory=list)
    reveal_directives: list[RevealDirective] = Field(default_factory=list)
    hint_directives: list[HintDirective] = Field(default_factory=list)
    intended_changes: list[IntendedChange] = Field(default_factory=list)
    milestone_ids: list[str] = Field(default_factory=list)
    # The schedule's reward category for this chapter and the trope templates it uses.
    reward_slot: str = ""
    trope_ids: list[str] = Field(default_factory=list)
    hook: str = ""


class BandPlanOutput(_Frozen):
    """What ``plan.band`` returns: the drafts of the chapters it was asked to plan."""

    chapters: list[ChapterPlanDraft]


class BandPlanDraft(_Frozen):
    band: BandSpan
    chapters: list[ChapterPlanDraft]


class PlanIssue(_Frozen):
    code: str
    severity: PlanIssueSeverity
    chapter_number: int | None = None
    ref: str = ""
    message: str


@dataclass(frozen=True)
class BandPlanOutcome:
    # The band plan version written; None when nothing was written.
    band_plan_id: str | None
    chapter_plan_ids: list[str] = field(default_factory=list)
    issues: list[PlanIssue] = field(default_factory=list)
    conflict_id: str | None = None


class FeasibilityAuditor(Protocol):
    """The feasibility audit the planner runs on every valid band plan; an error is retried, then fails it."""

    def audit_plans(
        self,
        session: Session,
        *,
        project_id: str,
        plans: Sequence[ChapterPlanDraft | ChapterPlan],
        beat_sheet: ArcBeatSheetData | None,
        policy: RuntimePolicy,
        runtime: BookStateRuntime | None = None,
    ) -> list[PlanIssue]: ...


def band_id(band: BandSpan) -> str:
    """The band's id in band experience plans, checkpoints, ledgers and trope records."""
    return f"band:{band.chapter_start}:{band.chapter_end}"


__all__ = [
    "REWRITABLE_PLAN_STATUSES",
    "WORLD_BINDING_KINDS",
    "BandPlanDraft",
    "BandPlanOutcome",
    "BandPlanOutput",
    "ChapterPlanDraft",
    "ExpectedSpan",
    "FeasibilityAuditor",
    "HintDirective",
    "IntendedChange",
    "PlanIssue",
    "RevealDirective",
    "WorldBinding",
    "band_id",
]
