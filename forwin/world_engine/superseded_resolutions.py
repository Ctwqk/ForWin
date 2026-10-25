"""Supersede what conflict resolutions left behind once canon drops the delta that carried them.

A historical rewrite that drops a world_engine delta calls ``supersede_resolutions`` inside its own
transaction. It is the inverse of ``conflict_resolution.record_option_side_effects``.
"""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.project import ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativePlanPatch
from forwin.planning.future_plan_audit.patches import FuturePlanPatchMixin
from forwin.world_engine.conflict_repository import PLAN_PATCH_REF_PREFIX, WorldConflictRepository, conflict_result_refs


PLAN_PATCH_REVERT_TYPE = "plan_patch_revert"
# Plans still to write, as PlanningQuery.future_chapters selects them; any other plan keeps its goals.
_UNACCEPTED_PLAN_STATUSES = frozenset({"planned", "failed"})


@dataclass(frozen=True)
class SupersededResolutions:
    conflict_ids: tuple[str, ...] = ()
    cancelled_obligation_ids: tuple[str, ...] = ()
    # The resolutions' own plan patches that a plan_patch_revert patch took back out.
    reverted_plan_patch_ids: tuple[str, ...] = ()


def supersede_resolutions(
    session: Session, *, project_id: str, graph_delta_id: str, reason: str,
) -> SupersededResolutions:
    """Undo the side effects of the conflicts whose resolution committed through ``graph_delta_id``.

    Those are the resolved conflicts whose result_refs list the delta; they stay resolved, with the delta
    recorded as ``superseded_by_delta`` so that a superseded veto no longer counts as one. The open
    obligations referenced by the resolution or its patches are cancelled with ``reason``; pending
    responses are retired. A patch on a plan still to
    write is reverted and the revert recorded as a ``plan_patch_revert`` patch; a patch on a plan
    already accepted stays. A goal or marker another still-active resolution also put into the plan
    stays with it (Ruling P34).
    """
    rows = [
        row
        for row in session.scalars(
            select(WorldPlanConflictRow)
            .where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.status == "resolved",
                WorldPlanConflictRow.result_refs_json.contains(graph_delta_id, autoescape=True),
            )
            .order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
        )
        if graph_delta_id in _result_refs(row)
    ]
    ledger = NarrativeObligationRepository(session)
    superseded_ids = {row.id for row in rows}
    cancelled: list[str] = []
    reverted: list[str] = []
    repo = WorldConflictRepository(session)
    for row in rows:
        refs = _result_refs(row)
        # The canon range rewind already removed the hold's status patch. Retire the
        # response even when a planner attached a patch, so it can never be rebound.
        repo.clear_pending_plan_response(row)
        repo.mark_superseded(row, graph_delta_id=graph_delta_id)
        for ref in refs:
            if ledger.cancel_obligation(ref, reason=reason) is not None:
                cancelled.append(ref)
        for patch in sorted(ledger.list_patches_by_ids(refs), key=lambda item: refs.index(item.id)):
            for obligation_id in patch.source_obligation_ids:
                if obligation_id in refs and ledger.cancel_obligation(obligation_id, reason=reason) is not None:
                    cancelled.append(obligation_id)
            plan = (
                session.get(ChapterPlan, patch.target_plan_id)
                if patch.target_scope == "chapter" and patch.target_plan_id
                else None
            )
            if plan is None or plan.status not in _UNACCEPTED_PLAN_STATUSES:
                continue
            FuturePlanPatchMixin.revert_plan_patch(plan, patch, still_applied=_active_resolution_patches(
                session, ledger, project_id=project_id, plan_id=plan.id, superseded_conflict_ids=superseded_ids,
            ))
            session.add(plan)
            ledger.create_plan_patch(NarrativePlanPatch(
                project_id=project_id,
                patch_type=PLAN_PATCH_REVERT_TYPE,
                target_scope="chapter",
                target_plan_id=plan.id,
                target_arc_id=plan.arc_plan_id,
                affected_chapters=[plan.chapter_number],
                new_contract={"reverted_plan_patch_id": patch.id},
                diff_summary=f"Revert plan patch {patch.id}: {reason}",
                validation_status="passed",
                applied=True,
                metadata={"reverted_plan_patch_id": patch.id, "conflict_id": row.id, "graph_delta_id": graph_delta_id},
            ))
            reverted.append(patch.id)
    session.flush()
    return SupersededResolutions(
        conflict_ids=tuple(row.id for row in rows),
        cancelled_obligation_ids=tuple(dict.fromkeys(cancelled)),
        reverted_plan_patch_ids=tuple(reverted),
    )


def retire_response_patches(session: Session, row: WorldPlanConflictRow, *, reason: str) -> None:
    """Tombstone obsolete response patches even when their plan was deleted or already written."""
    ledger = NarrativeObligationRepository(session)
    for patch in WorldConflictRepository(session).response_patches(row):
        plan = session.get(ChapterPlan, patch.target_plan_id)
        if plan is not None and plan.status in _UNACCEPTED_PLAN_STATUSES:
            FuturePlanPatchMixin.revert_plan_patch(plan, patch, still_applied=_active_resolution_patches(
                session, ledger, project_id=row.project_id, plan_id=plan.id, superseded_conflict_ids={row.id},
            ))
        ledger.create_plan_patch(NarrativePlanPatch(
            project_id=row.project_id, patch_type=PLAN_PATCH_REVERT_TYPE, target_scope="chapter",
            target_plan_id=patch.target_plan_id, target_arc_id=patch.target_arc_id,
            affected_chapters=patch.affected_chapters, new_contract={"reverted_plan_patch_id": patch.id},
            diff_summary=f"Revert plan patch {patch.id}: {reason}", validation_status="passed", applied=True,
            metadata={"reverted_plan_patch_id": patch.id, "conflict_id": row.id},
        ))


def _active_resolution_patches(
    session: Session,
    ledger: NarrativeObligationRepository,
    *,
    project_id: str,
    plan_id: str,
    superseded_conflict_ids: set[str],
) -> list[NarrativePlanPatch]:
    """The plan's patches that still answer a resolved conflict: the conflict's own (its result_refs list
    the patch), not reverted, and the conflict not among those being superseded."""
    patches = ledger.list_patches_for_plan(project_id, target_plan_id=plan_id)
    reverted = {
        str(patch.metadata.get("reverted_plan_patch_id") or "")
        for patch in patches
        if patch.patch_type == PLAN_PATCH_REVERT_TYPE
    }
    conflict_by_patch = {
        patch.id: conflict_id
        for patch in patches
        if patch.patch_type != PLAN_PATCH_REVERT_TYPE and patch.id not in reverted
        and (conflict_id := str(patch.metadata.get("conflict_id") or ""))
        and conflict_id not in superseded_conflict_ids
    }
    if not conflict_by_patch:
        return []
    refs_by_conflict = {
        row.id: _result_refs(row)
        for row in session.scalars(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == project_id,
            WorldPlanConflictRow.status == "resolved",
            WorldPlanConflictRow.id.in_(sorted(set(conflict_by_patch.values()))),
        ))
    }
    return [
        patch for patch in patches
        if patch.id in conflict_by_patch and patch.id in refs_by_conflict.get(conflict_by_patch[patch.id], ())
    ]


def _result_refs(row: WorldPlanConflictRow) -> list[str]:
    """The row's result_refs, a band plan's ``plan_patch:<id>`` read as the bare patch id P3 refs use."""
    return [ref.removeprefix(PLAN_PATCH_REF_PREFIX) for ref in conflict_result_refs(row)]


__all__ = ["PLAN_PATCH_REVERT_TYPE", "SupersededResolutions", "supersede_resolutions"]
