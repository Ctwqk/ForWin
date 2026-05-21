from __future__ import annotations

from pydantic import BaseModel, Field

from .types import NarrativeObligation


_STRUCTURAL_P1_TYPES = {
    "identity_ambiguity",
    "countdown_explanation",
    "artifact_count_explanation",
}
_OPEN_STATUSES = {"proposed", "planned", "active", "expired"}


class ObligationBudgetPolicy(BaseModel):
    max_new_p1_p2_per_chapter: int = 2
    max_open_p1_p2_per_band: int = 5
    max_open_arc_structural_p1: int = 2
    arc_max_p0_p1_per_arc: int = 2
    arc_max_p1_p2_per_arc: int = 4
    book_max_p0_per_book: int = 1
    book_max_p1_p2_per_book: int = 3


class ObligationBudgetResult(BaseModel):
    allowed: bool
    over_budget: bool = False
    reasons: list[str] = Field(default_factory=list)


def evaluate_obligation_budget(
    *,
    open_obligations: list[NarrativeObligation],
    new_obligations: list[NarrativeObligation],
    current_chapter: int,
    band_start: int,
    band_end: int,
    arc_start: int,
    arc_end: int,
    final_band_start_chapter: int = 0,
    policy: ObligationBudgetPolicy | None = None,
) -> ObligationBudgetResult:
    resolved_policy = policy or ObligationBudgetPolicy()
    reasons: list[str] = []
    current = int(current_chapter or 0)
    active_open = [item for item in open_obligations if item.status in _OPEN_STATUSES]

    new_p1_p2_count = sum(1 for item in new_obligations if item.priority in {"P1", "P2"})
    if new_p1_p2_count > resolved_policy.max_new_p1_p2_per_chapter:
        reasons.append(
            "chapter_new_p1_p2_budget_exceeded:"
            f"{new_p1_p2_count}>{resolved_policy.max_new_p1_p2_per_chapter}"
        )

    band_items = [
        item
        for item in [*active_open, *new_obligations]
        if item.priority in {"P1", "P2"}
        and int(band_start or 0) <= int(item.origin_chapter_number or 0) <= int(band_end or 0)
    ]
    if len(band_items) > resolved_policy.max_open_p1_p2_per_band:
        reasons.append(
            "band_open_p1_p2_budget_exceeded:"
            f"{len(band_items)}>{resolved_policy.max_open_p1_p2_per_band}"
        )

    structural_p1 = [
        item
        for item in [*active_open, *new_obligations]
        if item.priority == "P1"
        and item.obligation_type in _STRUCTURAL_P1_TYPES
        and int(arc_start or 0) <= int(item.origin_chapter_number or 0) <= int(arc_end or 0)
    ]
    if len(structural_p1) > resolved_policy.max_open_arc_structural_p1:
        reasons.append(
            "arc_structural_p1_budget_exceeded:"
            f"{len(structural_p1)}>{resolved_policy.max_open_arc_structural_p1}"
        )

    arc_p0_p1 = [
        item
        for item in [*active_open, *new_obligations]
        if item.priority in {"P0", "P1"}
        and int(arc_start or 0) <= int(item.origin_chapter_number or 0) <= int(arc_end or 0)
    ]
    if len(arc_p0_p1) > resolved_policy.arc_max_p0_p1_per_arc:
        reasons.append(
            "arc_p0_p1_budget_exceeded:"
            f"{len(arc_p0_p1)}>{resolved_policy.arc_max_p0_p1_per_arc}"
        )

    arc_p1_p2 = [
        item
        for item in [*active_open, *new_obligations]
        if item.priority in {"P1", "P2"}
        and int(arc_start or 0) <= int(item.origin_chapter_number or 0) <= int(arc_end or 0)
    ]
    if len(arc_p1_p2) > resolved_policy.arc_max_p1_p2_per_arc:
        reasons.append(
            "arc_p1_p2_budget_exceeded:"
            f"{len(arc_p1_p2)}>{resolved_policy.arc_max_p1_p2_per_arc}"
        )

    book_p0 = [
        item
        for item in [*active_open, *new_obligations]
        if item.priority == "P0"
    ]
    if len(book_p0) > resolved_policy.book_max_p0_per_book:
        reasons.append(
            "book_p0_budget_exceeded:"
            f"{len(book_p0)}>{resolved_policy.book_max_p0_per_book}"
        )

    book_p1_p2 = [
        item
        for item in [*active_open, *new_obligations]
        if item.priority in {"P1", "P2"}
    ]
    if len(book_p1_p2) > resolved_policy.book_max_p1_p2_per_book:
        reasons.append(
            "book_p1_p2_budget_exceeded:"
            f"{len(book_p1_p2)}>{resolved_policy.book_max_p1_p2_per_book}"
        )

    if int(final_band_start_chapter or 0) and current >= int(final_band_start_chapter or 0):
        for item in active_open:
            if item.priority == "P0":
                reasons.append(f"final_band_open_p0_obligation:{item.id}")

    return ObligationBudgetResult(
        allowed=not reasons,
        over_budget=bool(reasons),
        reasons=sorted(set(reasons)),
    )
