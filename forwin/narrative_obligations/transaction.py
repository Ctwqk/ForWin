from __future__ import annotations

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.canon_quality.signals import CanonAdmissionGateResult
from forwin.models.base import new_id
from forwin.models.project import ChapterPlan
from forwin.planning.future_plan_auditor import FuturePlanAuditor
from forwin.planning.plan_patch_validator import PlanPatchValidator

from .repository import NarrativeObligationRepository
from .types import NarrativeObligation, NarrativePlanPatch


class DeferAcceptanceTransactionResult(BaseModel):
    success: bool
    errors: list[str] = Field(default_factory=list)
    obligation: NarrativeObligation | None = None
    plan_patch: NarrativePlanPatch | None = None
    gate_result: CanonAdmissionGateResult | None = None


class _DeferAcceptanceAbort(Exception):
    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors


class DeferAcceptanceTransaction:
    def __init__(
        self,
        session: Session,
        *,
        validator: PlanPatchValidator | None = None,
    ) -> None:
        self.session = session
        self.validator = validator or PlanPatchValidator()

    def run(
        self,
        *,
        obligation: NarrativeObligation,
        plan_patch: NarrativePlanPatch,
        current_chapter: int,
        target_total_chapters: int,
    ) -> DeferAcceptanceTransactionResult:
        obligation_id = obligation.id or new_id()
        prepared_obligation = obligation.model_copy(
            update={"id": obligation_id, "status": "proposed"}
        )
        source_obligation_ids = list(plan_patch.source_obligation_ids or [])
        if obligation_id not in source_obligation_ids:
            source_obligation_ids.append(obligation_id)
        prepared_patch = plan_patch.model_copy(
            update={"source_obligation_ids": source_obligation_ids}
        )

        validation = self.validator.validate(
            patch=prepared_patch,
            obligations=[prepared_obligation],
            current_chapter=current_chapter,
            target_total_chapters=target_total_chapters,
        )
        if not validation.passed:
            return DeferAcceptanceTransactionResult(success=False, errors=validation.errors)

        target_plan = None
        if prepared_patch.target_scope == "chapter" and prepared_patch.target_plan_id:
            target_plan = self.session.get(ChapterPlan, prepared_patch.target_plan_id)
            if target_plan is None:
                return DeferAcceptanceTransactionResult(
                    success=False,
                    errors=[f"target_plan_not_found:{prepared_patch.target_plan_id}"],
                )
            if str(target_plan.status or "") == "accepted":
                return DeferAcceptanceTransactionResult(
                    success=False,
                    errors=[f"target_plan_already_accepted:{target_plan.chapter_number}"],
                )

        repo = NarrativeObligationRepository(self.session)
        try:
            with self.session.begin_nested():
                stored_obligation = repo.create_obligation(prepared_obligation)
                applied_patch = prepared_patch.model_copy(
                    update={
                        "validation_status": "passed",
                        "validation_errors": [],
                        "applied": True,
                    }
                )
                if target_plan is not None:
                    FuturePlanAuditor().apply_plan_patch(target_plan, applied_patch)
                    self.session.add(target_plan)
                stored_patch = repo.create_plan_patch(applied_patch)
                planned = repo.mark_obligation_planned(
                    stored_obligation.id,
                    linked_plan_patch_ids=[stored_patch.id],
                )
                if planned is None:
                    raise _DeferAcceptanceAbort(["obligation_not_persisted"])
                gate_result = evaluate_canon_admission(
                    project_id=planned.project_id,
                    chapter_number=current_chapter,
                    draft_id=planned.origin_draft_id,
                    review_id=planned.origin_review_id,
                    review_verdict="warn",
                    obligations=[planned],
                    plan_patches=[stored_patch],
                    mode="strict",
                    is_final_chapter=current_chapter >= int(target_total_chapters or 0)
                    if int(target_total_chapters or 0)
                    else False,
                )
                if not gate_result.commit_allowed:
                    raise _DeferAcceptanceAbort(gate_result.blocking_reasons)
        except _DeferAcceptanceAbort as exc:
            return DeferAcceptanceTransactionResult(success=False, errors=exc.errors)

        return DeferAcceptanceTransactionResult(
            success=True,
            obligation=planned,
            plan_patch=stored_patch,
            gate_result=gate_result,
        )
