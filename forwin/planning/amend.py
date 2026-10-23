"""Carry out amend_plan: plan the conflict's chapters again, revising the arc's beat sheet first when it must change."""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.repository import BookStateRepository
from forwin.models.audit import DecisionEvent
from forwin.models.planning_v6 import ArcBeatSheetRow, BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.band_planner.service import BandPlanner
from forwin.planning.band_planner.types import REWRITABLE_PLAN_STATUSES, BandPlanOutcome, PlanIssue
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.service import ArcBeatSheetService
from forwin.planning.beat_sheet.types import BandSpan
from forwin.planning.feasibility import band_for_chapter, open_plan_feasibility_conflict
from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.conflict_repository import WorldConflictRepository, conflict_result_refs
from forwin.world_engine.conflict_resolution import ConflictOptionUnavailable
from forwin.world_engine.conflict_routing import PLAN_CONFLICT_SOURCES
from forwin.world_engine.conflict_types import ConflictDecision


# The result_refs an amendment adds to its conflict.
BAND_PLAN_REF_PREFIX = "band_plan:"
BEAT_SHEET_REF_PREFIX = "beat_sheet:"
# A failed world-tick amendment: its WORLD_CONFLICT_AMEND_FAILED event and its follow-up plan conflict.
AMEND_FAILED_REF_PREFIX = "amend_failed:"
PLAN_CONFLICT_REF_PREFIX = "plan_conflict:"
# A plan conflict over a milestone, or one whose issues leave the exit state unreached, needs a new beat sheet.
_REVISING_VIOLATIONS = frozenset({"milestone"})
_REVISING_ISSUE_CODES = frozenset({"exit_state_unaddressed"})
# The issues an amendment raises itself: no sheet band holds the chapters, a band came out without a plan,
# and an amendment that raised.
NO_BAND = "no_band"
NO_BAND_PLAN = "no_band_plan"
AMEND_ERROR = "amend_error"


@dataclass(frozen=True)
class AmendResult:
    # The band plan holding the first chapter planned again, written or kept; None when none could be made.
    band_plan_id: str | None
    # The beat sheet version the amendment wrote, or this conflict's earlier revision it planned from.
    beat_sheet_id: str | None = None
    # Why no band plan was made: the planner's errors. Empty with no band plan when nothing was left to plan.
    issues: tuple[PlanIssue, ...] = ()

    @property
    def failed(self) -> bool:
        return self.band_plan_id is None and bool(self.issues)


@dataclass(frozen=True)
class AmendFailure:
    event_id: str
    # The plan_feasibility conflict a human amends the plan through instead.
    followup_conflict_id: str


class PlanAmender:
    """amend_plan's planning, for plan conflicts and world-tick conflicts alike.

    No lock is held through a model call: the caller holds none, and the amender commits the session after every
    planning step, which ends the project row lock the step took before the next call. Call it with a plain
    session, outside any ``Session.begin()`` / ``sessionmaker.begin()`` or ``begin_nested()`` block. What it wrote
    goes into the conflict's result_refs, attached under the conflict row lock.
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
            _attach_refs(session, conflict, refs)
        session.commit()
        return result

    def record_world_amendment_failure(
        self, session: Session, *, conflict: WorldPlanConflictRow, issues: Sequence[PlanIssue], policy: RuntimePolicy,
    ) -> AmendFailure:
        """A world-tick amendment that failed once its resolution committed: the resolution stands, and a
        plan_feasibility conflict over the next chapter's band lets a human plan it again. A
        WORLD_CONFLICT_AMEND_FAILED event and result refs on the resolved conflict record the failure. Commits.
        """
        project_id = conflict.project_id
        latest = BookStateRepository(session).latest_canon_chapter(project_id) or 0
        repo = WorldConflictRepository(session)
        # The conflict row first, then the project row the follow-up conflict takes.
        row = repo.get(project_id, conflict.id, for_update=True)
        band, band_plan_id = band_for_chapter(session, project_id, latest + 1)
        placed = [issue if issue.chapter_number is not None else issue.model_copy(update={"chapter_number": latest + 1})
                  for issue in issues]
        if not any(issue.severity == "error" for issue in placed):
            placed.append(PlanIssue(code=NO_BAND_PLAN, severity="error", chapter_number=latest + 1,
                                    message="the band could not be planned again"))
        followup = open_plan_feasibility_conflict(session, project_id=project_id, band=band, issues=placed,
                                                  policy=policy, band_plan_id=band_plan_id)
        event = DecisionEvent(
            project_id=project_id,
            chapter_number=latest,
            scope="chapter",
            event_family="business_event",
            event_type=DecisionEventType.WORLD_CONFLICT_AMEND_FAILED,
            actor_type="system",
            summary=f"World conflict {row.id} stays resolved, but its chapters could not be planned again.",
            reason="; ".join(f"{issue.code}: {issue.message}" for issue in placed),
            payload_json=json.dumps({
                "conflict_id": row.id, "followup_conflict_id": followup.id, "band_plan_id": band_plan_id,
                "band": [band.chapter_start, band.chapter_end],
                "issues": [issue.model_dump(mode="json") for issue in placed],
            }, ensure_ascii=False, sort_keys=True),
            related_object_type="world_conflict",
            related_object_id=row.id,
        )
        session.add(event)
        session.flush()
        repo.attach_result_refs(row, [
            f"{AMEND_FAILED_REF_PREFIX}{event.id}", f"{PLAN_CONFLICT_REF_PREFIX}{followup.id}",
        ])
        session.commit()
        return AmendFailure(event_id=event.id, followup_conflict_id=followup.id)

    @staticmethod
    def error_issue(exc: BaseException) -> PlanIssue:
        """The issue for an amendment that raised."""
        return PlanIssue(code=AMEND_ERROR, severity="error", message=f"{type(exc).__name__}: {exc}")

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
        chapters = set(range(first, end + 1)) or {end}
        beat_sheet_id = None
        if conflict.violated in _REVISING_VIOLATIONS or codes & _REVISING_ISSUE_CODES:
            session.refresh(conflict)
            if f"{BEAT_SHEET_REF_PREFIX}{sheet_row.id}" not in conflict_result_refs(conflict):
                sheet_row = self.beat_sheet_service.revise(
                    session, project_id=project_id, arc_plan=arc_plan, policy=policy,
                    reason=f"conflict:{conflict.id}",
                    conflict_summary="\n".join(text for text in (conflict.summary, decision.rationale) if text.strip()),
                )
                # End the revision's project row lock, then record it, so a retry plans from it.
                session.commit()
                _attach_refs(session, conflict, [f"{BEAT_SHEET_REF_PREFIX}{sheet_row.id}"])
            beat_sheet_id = sheet_row.id
            chapters |= _retire_stale_band_plans(session, arc_plan=arc_plan, sheet_row=sheet_row, first=first)
            session.commit()
        outcome = self._plan_bands(session, arc_plan=arc_plan, sheet_row=sheet_row, chapters=chapters, first=first,
                                   policy=policy, notes=notes)
        return AmendResult(band_plan_id=outcome.band_plan_id, beat_sheet_id=beat_sheet_id, issues=tuple(outcome.issues))

    def _plan_bands(
        self, session: Session, *, arc_plan: ArcPlanVersion, sheet_row: ArcBeatSheetRow, chapters: Iterable[int],
        first: int, policy: RuntimePolicy, notes: Sequence[str],
    ) -> BandPlanOutcome:
        """Plan again, band by band of the sheet, the bands holding ``chapters``; the first band's outcome when
        every band came out with a plan, else the failing band's, and no later band is planned.

        A band whose span has an active band plan is planned again from ``first``. A band without one, which a
        revision that split the arc differently leaves, is planned whole, as the band planner plans a new band.
        """
        wanted = sorted(set(chapters))
        bands = sorted((band for band in ArcBeatSheetRepository(session).load(sheet_row).band_breakdown
                        if any(band.chapter_start <= number <= band.chapter_end for number in wanted)),
                       key=lambda band: band.chapter_start)
        if not bands:
            return BandPlanOutcome(band_plan_id=None, issues=[PlanIssue(
                code=NO_BAND, severity="error", chapter_number=wanted[0] if wanted else first,
                message=f"beat sheet {sheet_row.id} has no band holding chapters {wanted}",
            )])
        planned: BandPlanOutcome | None = None
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
            if outcome.band_plan_id is None:
                return BandPlanOutcome(band_plan_id=None, issues=outcome.issues or [PlanIssue(
                    code=NO_BAND_PLAN, severity="error", chapter_number=max(first, band.chapter_start),
                    message=f"chapters {band.chapter_start}-{band.chapter_end} came out without a band plan",
                )])
            planned = planned or outcome
        return planned

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
        if outcome.band_plan_id is None:
            return AmendResult(band_plan_id=None, issues=tuple(outcome.issues) or (PlanIssue(
                code=NO_BAND_PLAN, severity="error", chapter_number=first,
                message=f"band plan {band_plan.id} could not be planned again",
            ),))
        return AmendResult(band_plan_id=outcome.band_plan_id)


def _retire_stale_band_plans(
    session: Session, *, arc_plan: ArcPlanVersion, sheet_row: ArcBeatSheetRow, first: int,
) -> set[int]:
    """Supersede the arc's active band plans whose span the revised sheet no longer has.

    Returns the chapters from ``first`` on whose rows are still to write but no longer belong to an active band
    plan, a retry's included, for the sheet's bands to plan. Rows without a band plan are left as they are.
    """
    spans = {(band.band_index, band.chapter_start, band.chapter_end)
             for band in ArcBeatSheetRepository(session).load(sheet_row).band_breakdown}
    same_arc = (BandPlanRow.project_id == arc_plan.project_id, BandPlanRow.arc_plan_id == arc_plan.id)
    stale = [row.id for row in session.scalars(select(BandPlanRow).where(*same_arc, BandPlanRow.status == "active"))
             if (row.band_index, row.chapter_start, row.chapter_end) not in spans]
    if stale:
        session.execute(update(BandPlanRow).where(BandPlanRow.id.in_(stale)).values(status="superseded")
                        .execution_options(synchronize_session="fetch"))
    active = select(BandPlanRow.id).where(*same_arc, BandPlanRow.status == "active")
    return set(session.scalars(select(ChapterPlan.chapter_number).where(
        ChapterPlan.project_id == arc_plan.project_id,
        ChapterPlan.chapter_number.between(max(first, arc_plan.chapter_start), arc_plan.chapter_end),
        ChapterPlan.status.in_(REWRITABLE_PLAN_STATUSES), ChapterPlan.band_plan_id.is_not(None),
        ChapterPlan.band_plan_id.not_in(active),
    )))


def _attach_refs(session: Session, conflict: WorldPlanConflictRow, refs: Sequence[str]) -> None:
    """Add ``refs`` to the conflict's result_refs as the database holds them, under the conflict row lock."""
    repo = WorldConflictRepository(session)
    row = repo.get(conflict.project_id, conflict.id, for_update=True)
    if row is None:
        raise LookupError(f"World conflict {conflict.id} vanished during its amendment")
    repo.attach_result_refs(row, refs)


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


__all__ = [
    "AMEND_ERROR",
    "AMEND_FAILED_REF_PREFIX",
    "BAND_PLAN_REF_PREFIX",
    "BEAT_SHEET_REF_PREFIX",
    "NO_BAND",
    "NO_BAND_PLAN",
    "PLAN_CONFLICT_REF_PREFIX",
    "AmendFailure",
    "AmendResult",
    "PlanAmender",
]
