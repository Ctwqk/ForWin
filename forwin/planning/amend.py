"""Carry out amend_plan: plan the conflict's chapters again, revising the arc's beat sheet first when it must change."""

from __future__ import annotations

import json
from collections.abc import Sequence
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.repository import BookStateRepository
from forwin.models.planning_v6 import ArcBeatSheetRow, BandPlanRow
from forwin.models.project import ArcPlanVersion
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.band_planner.service import BandPlanner
from forwin.planning.band_planner.types import BandPlanOutcome, PlanIssue
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.service import ArcBeatSheetService
from forwin.planning.beat_sheet.types import BandSpan
from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_resolution import ConflictOptionUnavailable
from forwin.world_engine.conflict_routing import PLAN_CONFLICT_SOURCES
from forwin.world_engine.conflict_types import ConflictDecision


# The result_refs an amendment adds to its conflict.
BAND_PLAN_REF_PREFIX = "band_plan:"
BEAT_SHEET_REF_PREFIX = "beat_sheet:"
# A plan conflict over a milestone, or one whose issues leave the exit state unreached, needs a new beat sheet.
_REVISING_VIOLATIONS = frozenset({"milestone"})
_REVISING_ISSUE_CODES = frozenset({"exit_state_unaddressed"})
# The issue when the beat sheet has no band holding the chapters to plan.
NO_BAND = "no_band"


@dataclass(frozen=True)
class AmendResult:
    # The band plan holding the first chapter planned again, written or kept; None when none could be made.
    band_plan_id: str | None
    # The beat sheet version the amendment wrote; None when the sheet stood.
    beat_sheet_id: str | None = None
    # Why no band plan was made: the planner's errors.
    issues: tuple[PlanIssue, ...] = ()


class PlanAmender:
    """amend_plan's planning, for plan conflicts and world-tick conflicts alike.

    No lock is held through a model call: the caller holds none, and the amender commits the session after every
    planning step, which ends the project row lock the step took before the next call. Call it outside a
    ``Session.begin()`` block. What it wrote goes into the conflict's result_refs.
    """

    def __init__(self, band_planner: BandPlanner, beat_sheet_service: ArcBeatSheetService) -> None:
        self.band_planner = band_planner
        self.beat_sheet_service = beat_sheet_service

    def amend(
        self, session: Session, *, conflict: WorldPlanConflictRow, decision: ConflictDecision, policy: RuntimePolicy,
    ) -> AmendResult:
        """A plan conflict's band is planned again with the decision's rationale as a note, after a beat-sheet
        revision when it broke a milestone or the exit state; ``max_replans_per_band`` does not stop it.

        For a world-tick conflict, once its change committed, the unwritten chapters of the band holding the next
        chapter are planned again with the conflict's summary as a note.
        """
        if conflict.source in PLAN_CONFLICT_SOURCES:
            result = self._amend_plan_conflict(session, conflict, decision, policy)
        else:
            result = self._replan_after_world_change(session, conflict, policy)
        refs = [
            *([f"{BEAT_SHEET_REF_PREFIX}{result.beat_sheet_id}"] if result.beat_sheet_id else []),
            *([f"{BAND_PLAN_REF_PREFIX}{result.band_plan_id}"] if result.band_plan_id else []),
        ]
        if refs:
            WorldConflictRepository(session).attach_result_refs(conflict, refs)
        session.commit()
        return result

    def _amend_plan_conflict(
        self, session: Session, conflict: WorldPlanConflictRow, decision: ConflictDecision, policy: RuntimePolicy,
    ) -> AmendResult:
        proposed = _proposed_change(conflict)
        band = proposed.get("band")
        if not (isinstance(band, list) and len(band) == 2 and all(type(number) is int for number in band)):
            raise ConflictOptionUnavailable(f"Conflict {conflict.id} names no band to amend")
        start, end = band
        project_id = conflict.project_id
        arc_plan, sheet_row = _arc_with_sheet(session, project_id, start)
        first = max(start, (BookStateRepository(session).latest_canon_chapter(project_id) or 0) + 1)
        notes = [text for text in (decision.rationale.strip(),) if text]
        codes = {str(item.get("code") or "") for item in proposed.get("issues") or [] if isinstance(item, dict)}
        beat_sheet_id = None
        if conflict.violated in _REVISING_VIOLATIONS or codes & _REVISING_ISSUE_CODES:
            sheet_row = self.beat_sheet_service.revise(
                session, project_id=project_id, arc_plan=arc_plan, policy=policy, reason=f"conflict:{conflict.id}",
                conflict_summary="\n".join(text for text in (conflict.summary, decision.rationale) if text.strip()),
            )
            beat_sheet_id = sheet_row.id
            session.commit()
        outcome = self._plan_unwritten(session, arc_plan=arc_plan, sheet_row=sheet_row, first=first, last=end,
                                       policy=policy, notes=notes)
        return AmendResult(band_plan_id=outcome.band_plan_id, beat_sheet_id=beat_sheet_id, issues=tuple(outcome.issues))

    def _plan_unwritten(
        self, session: Session, *, arc_plan: ArcPlanVersion, sheet_row: ArcBeatSheetRow, first: int, last: int,
        policy: RuntimePolicy, notes: Sequence[str],
    ) -> BandPlanOutcome:
        """Plan again the chapters ``first``-``last`` still to write, band by band of the sheet; returns the first
        band's outcome.

        A band whose span has an active band plan is planned again from ``first``. A revision may split the arc
        differently, so a band without one is planned whole, as the band planner plans a new band.
        """
        low = min(first, last)
        bands = sorted((band for band in ArcBeatSheetRepository(session).load(sheet_row).band_breakdown
                        if band.chapter_start <= last and band.chapter_end >= low),
                       key=lambda band: band.chapter_start)
        if not bands:
            return BandPlanOutcome(band_plan_id=None, issues=[PlanIssue(
                code=NO_BAND, severity="error", chapter_number=low,
                message=f"beat sheet {sheet_row.id} has no band holding chapters {low}-{last}",
            )])
        outcomes = []
        for band in bands:
            existing = _band_plan_for_span(session, arc_plan.project_id, arc_plan.id, band)
            if existing is not None:
                outcome = self.band_planner.replan_remaining(
                    session, project_id=arc_plan.project_id, band_plan_id=existing.id, from_chapter=first,
                    policy=policy, notes=notes,
                )
            else:
                outcome = self.band_planner.plan_band(
                    session, project_id=arc_plan.project_id, arc_plan=arc_plan, beat_sheet_row=sheet_row, band=band,
                    policy=policy, extra_notes=notes,
                )
            session.commit()
            outcomes.append(outcome)
        return outcomes[0]

    def _replan_after_world_change(
        self, session: Session, conflict: WorldPlanConflictRow, policy: RuntimePolicy,
    ) -> AmendResult:
        project_id = conflict.project_id
        first = (BookStateRepository(session).latest_canon_chapter(project_id) or 0) + 1
        band_plan = session.scalar(
            select(BandPlanRow)
            .join(ArcPlanVersion, ArcPlanVersion.id == BandPlanRow.arc_plan_id)
            .where(BandPlanRow.project_id == project_id, BandPlanRow.status == "active",
                   BandPlanRow.chapter_start <= first, BandPlanRow.chapter_end >= first,
                   ArcPlanVersion.status != "superseded")
            .order_by(BandPlanRow.version.desc()).limit(1)
        )
        if band_plan is None:
            # The next chapter's band is not planned yet; it will be planned from the changed world.
            return AmendResult(band_plan_id=None)
        outcome = self.band_planner.replan_remaining(
            session, project_id=project_id, band_plan_id=band_plan.id, from_chapter=first, policy=policy,
            notes=[conflict.summary],
        )
        return AmendResult(band_plan_id=outcome.band_plan_id, issues=tuple(outcome.issues))


def _proposed_change(conflict: WorldPlanConflictRow) -> dict:
    try:
        proposed = json.loads(conflict.proposed_change_json or "{}")
    except ValueError:
        return {}
    return proposed if isinstance(proposed, dict) else {}


def _arc_with_sheet(session: Session, project_id: str, chapter: int) -> tuple[ArcPlanVersion, ArcBeatSheetRow]:
    """The arc holding ``chapter`` and its active beat sheet."""
    for row in ArcBeatSheetRepository(session).active_for_project(project_id):
        arc = session.get(ArcPlanVersion, row.arc_plan_id)
        if arc is not None and arc.chapter_start <= chapter <= arc.chapter_end:
            return arc, row
    raise ConflictOptionUnavailable(f"No arc with an active beat sheet holds chapter {chapter}")


def _band_plan_for_span(session: Session, project_id: str, arc_plan_id: str, band: BandSpan) -> BandPlanRow | None:
    return session.scalar(
        select(BandPlanRow)
        .where(BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == arc_plan_id,
               BandPlanRow.band_index == band.band_index, BandPlanRow.chapter_start == band.chapter_start,
               BandPlanRow.chapter_end == band.chapter_end, BandPlanRow.status == "active")
        .order_by(BandPlanRow.version.desc()).limit(1)
    )


__all__ = ["BAND_PLAN_REF_PREFIX", "BEAT_SHEET_REF_PREFIX", "NO_BAND", "AmendResult", "PlanAmender"]
