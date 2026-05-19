from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.narrative_obligations.budget import ObligationBudgetResult
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.review import ReviewVerdict

OperationMode = Literal["blackbox", "copilot", "checkpoint"]
DecisionOutcome = Literal[
    "auto_approve",
    "local_repair",
    "chapter_patch",
    "band_patch",
    "arc_patch",
    "book_patch",
    "commit_with_obligation",
    "manual_review",
    "system_block",
]


@dataclass(frozen=True)
class PlanLayerHealth:
    active_chapter_patch_count: int = 0
    active_band_patch_count: int = 0
    active_arc_patch_count: int = 0
    active_book_patch_count: int = 0
    overdue_obligation_count: int = 0
    missing_layers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionInput:
    project_id: str
    chapter_number: int
    review: ReviewVerdict
    signals: list[CanonQualitySignal]
    open_obligations: list[NarrativeObligation]
    operation_mode: OperationMode
    attempts_completed: int
    prior_scope_history: list[str]
    budget: ObligationBudgetResult | None
    target_total_chapters: int
    plan_layer_health: PlanLayerHealth


@dataclass(frozen=True)
class Decision:
    outcome: DecisionOutcome
    reason: str
    rule_id: str
    missing_evidence: list[str]
    routed_from: str
    sub_action: dict[str, Any]


@dataclass(frozen=True)
class DecisionRule:
    rule_id: str
    source_dispatcher: str
    priority: int
    matches: Callable[[DecisionInput], bool]
    decide: Callable[[DecisionInput], Decision]
