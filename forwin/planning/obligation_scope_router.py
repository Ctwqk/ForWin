from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ScopeAction = Literal[
    "defer_with_chapter_plan_patch",
    "defer_with_band_plan_patch",
    "manual_review_required",
    "block",
]


@dataclass(frozen=True, slots=True)
class BandScopeCandidate:
    band_id: str
    arc_id: str = ""
    chapter_start: int = 0
    chapter_end: int = 0
    planned_chapters: list[int] = field(default_factory=list)


@dataclass(frozen=True, slots=True)
class ObligationScopeDecision:
    action: ScopeAction
    target_scope: Literal["chapter", "band", "arc", "book", "manual"]
    target_band_id: str = ""
    target_arc_id: str = ""
    affected_chapters: list[int] = field(default_factory=list)
    deadline_chapter: int = 0
    reason: str = ""


_CHAPTER_TYPES = {
    "motivation_gap",
    "transition_bridge_needed",
    "foreshadowing_payoff",
}
_BAND_TYPES = {
    "reader_promise_payoff",
    "reveal_escalation_needed",
    "style_repetition_pressure",
    "repeated_scene_pattern",
}
_ARC_TYPES = {
    "identity_ambiguity",
    "countdown_explanation",
    "artifact_count_explanation",
    "world_rule_explanation",
    "terminal_state_active_conflict",
}


class ObligationScopeRouter:
    def route(
        self,
        *,
        issue_type: str,
        priority: str,
        current_chapter: int,
        target_total_chapters: int,
        bands: list[BandScopeCandidate],
    ) -> ObligationScopeDecision:
        del priority
        normalized = str(issue_type or "").strip()
        current = int(current_chapter or 0)
        target_total = int(target_total_chapters or 0)
        if normalized in _CHAPTER_TYPES:
            chapter = current + 1
            if target_total and chapter > target_total:
                return ObligationScopeDecision(
                    action="manual_review_required",
                    target_scope="manual",
                    reason="no future chapter available for chapter-level obligation",
                )
            return ObligationScopeDecision(
                action="defer_with_chapter_plan_patch",
                target_scope="chapter",
                affected_chapters=[chapter],
                deadline_chapter=chapter,
                reason=f"{normalized} can be resolved by a future chapter plan patch",
            )
        if normalized in _BAND_TYPES:
            band = _select_band(current_chapter=current, bands=bands)
            if band is None:
                return ObligationScopeDecision(
                    action="manual_review_required",
                    target_scope="band",
                    reason="no future band plan available for band-level obligation",
                )
            affected = _future_chapters_for_band(band=band, current_chapter=current)
            return ObligationScopeDecision(
                action="defer_with_band_plan_patch",
                target_scope="band",
                target_band_id=band.band_id,
                target_arc_id=band.arc_id,
                affected_chapters=affected,
                deadline_chapter=max(affected) if affected else int(band.chapter_end or 0),
                reason=f"{normalized} requires band-level payoff planning",
            )
        if normalized in _ARC_TYPES:
            return ObligationScopeDecision(
                action="manual_review_required",
                target_scope="arc",
                reason=f"{normalized} requires arc-level planning",
            )
        return ObligationScopeDecision(
            action="manual_review_required",
            target_scope="manual",
            reason=f"{normalized or 'unknown_issue'} has no automatic obligation scope route",
        )


def _select_band(*, current_chapter: int, bands: list[BandScopeCandidate]) -> BandScopeCandidate | None:
    current = int(current_chapter or 0)
    normalized = sorted(bands, key=lambda item: (int(item.chapter_start or 0), int(item.chapter_end or 0)))
    for band in normalized:
        affected = _future_chapters_for_band(band=band, current_chapter=current)
        if affected and int(band.chapter_start or 0) <= current <= int(band.chapter_end or 0):
            return band
    for band in normalized:
        affected = _future_chapters_for_band(band=band, current_chapter=current)
        if affected and int(band.chapter_start or 0) > current:
            return band
    for band in normalized:
        if _future_chapters_for_band(band=band, current_chapter=current):
            return band
    return None


def _future_chapters_for_band(*, band: BandScopeCandidate, current_chapter: int) -> list[int]:
    planned = [int(item) for item in band.planned_chapters if int(item or 0) > int(current_chapter or 0)]
    if planned:
        return sorted(dict.fromkeys(planned))
    start = max(int(band.chapter_start or 0), int(current_chapter or 0) + 1)
    end = int(band.chapter_end or 0)
    if start <= 0 or end < start:
        return []
    return list(range(start, end + 1))
