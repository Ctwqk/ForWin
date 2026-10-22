"""Supersede what conflict resolutions left behind once canon drops the delta that carried them.

A historical rewrite that drops a world_engine delta calls ``supersede_resolutions`` inside its own
transaction. It is the inverse of ``conflict_resolution.record_option_side_effects``.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.project import ChapterPlan
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativePlanPatch
from forwin.planning.future_plan_audit.patches import FuturePlanPatchMixin


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

    Those are the resolved conflicts whose result_refs list the delta; they stay resolved. The open
    obligations their plan patches bind are cancelled with ``reason``. A patch on a plan still to
    write is reverted and the revert recorded as a ``plan_patch_revert`` patch; a patch on a plan
    already accepted stays.
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
    cancelled: list[str] = []
    reverted: list[str] = []
    for row in rows:
        refs = _result_refs(row)
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
            FuturePlanPatchMixin.revert_plan_patch(plan, patch)
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
        cancelled_obligation_ids=tuple(cancelled),
        reverted_plan_patch_ids=tuple(reverted),
    )


def _result_refs(row: WorldPlanConflictRow) -> list[str]:
    try:
        refs = json.loads(row.result_refs_json or "[]")
    except ValueError:
        return []
    return [ref for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []


__all__ = ["PLAN_PATCH_REVERT_TYPE", "SupersededResolutions", "supersede_resolutions"]
