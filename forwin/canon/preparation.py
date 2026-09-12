from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.book_state.extraction.contract import (
    BookStateExtractionRequest,
    BookStateExtractionResult,
)
from forwin.book_state.extraction.graph_delta import BookStateGraphDeltaExtractor
from forwin.book_state.reviewer import BookStateReviewGate, BookStateReviewVerdict
from forwin.candidate_drafts import (
    CandidateDraftRepository,
    candidate_body_hash,
    candidate_writer_output_admission_fingerprint,
)
from forwin.canon.eligibility import candidate_ineligibility_reason
from forwin.model_adapter import ModelAdapter
from forwin.models.book_state import GraphDeltaRow
from forwin.models.draft import ChapterDraft
from forwin.models.project import ChapterPlan, Project
from forwin.naming import EntityAdmissionPlan, EntityRegistrar
from forwin.narrative_obligations.resolution_evidence import (
    ObligationResolutionPlan,
    build_resolution_plan,
    context_obligations,
    validate_resolution_plan,
)
from forwin.observability.pipeline_trace import PipelineTraceRecorder
from forwin.planning.world_contracts import WorldContractRepository
from forwin.protocol.book_state import ApprovedGraphDeltaSet
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import RuntimePolicy
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore

from .outbox_events import publisher_binding_snapshot
from .plan import CanonAuditEvent, CanonCommitPlan
from .quality_preparation import CanonQualityPreparer
from .types import CanonPreparationOutcome


@dataclass(frozen=True)
class BookStatePreparationOutcome:
    approved_changes: ApprovedGraphDeltaSet | None = None
    blocked_path: str = ""
    extraction: BookStateExtractionResult | None = None
    review_verdict: BookStateReviewVerdict | None = None

    @property
    def blocked(self) -> bool:
        return self.approved_changes is None


@dataclass(frozen=True, slots=True)
class CanonPreparationRequest:
    candidate_id: str
    project_id: str
    chapter_number: int
    writer_output: WriterOutput
    verdict: ReviewVerdict
    acceptance_mode: str
    repair_attempt_count: int
    residual_review_issues: list[dict[str, Any]]
    canon_risk_level: str


class BookStateCanonPreparer:
    def prepare(
        self,
        *,
        policy: RuntimePolicy,
        recorder: PipelineTraceRecorder,
        session: Session,
        candidate_id: str,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
        verdict: ReviewVerdict,
    ) -> BookStatePreparationOutcome:
        del verdict
        chapter_intent = WorldContractRepository(session).get_chapter_intent(
            project_id,
            chapter_number,
        )
        extraction = BookStateGraphDeltaExtractor(
            layers=set(policy.canon.book_state_layers),
            session=session,
        ).extract(
            BookStateExtractionRequest(
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output.model_copy(
                    update={"project_id": project_id}
                ),
                chapter_intent=chapter_intent,
                review_verdict_id=(
                    f"book_state_direct_review_{project_id}_{chapter_number}"
                ),
            )
        )
        if not extraction.accepted or extraction.changes is None:
            _record_book_state_block(
                recorder=recorder,
                session=session,
                candidate_id=candidate_id,
                project_id=project_id,
                chapter_number=chapter_number,
                gate_id="book_state_extraction",
                blocked_path="book-state-direct-extraction-blocked",
                issues=extraction.issues,
                related_object_type="candidate_draft",
                related_object_id=candidate_id,
            )
            return BookStatePreparationOutcome(
                blocked_path="book-state-direct-extraction-blocked",
                extraction=extraction,
            )
        review = BookStateReviewGate(session).review(extraction.changes)
        if not review.accepted or review.approved_changes is None:
            _record_book_state_block(
                recorder=recorder,
                session=session,
                candidate_id=candidate_id,
                project_id=project_id,
                chapter_number=chapter_number,
                gate_id="book_state_review",
                blocked_path="book-state-review-gate-blocked",
                issues=review.issues,
                related_object_type="book_state_review",
                related_object_id=review.verdict_id,
            )
            return BookStatePreparationOutcome(
                blocked_path="book-state-review-gate-blocked",
                extraction=extraction,
                review_verdict=review,
            )
        return BookStatePreparationOutcome(
            approved_changes=review.approved_changes,
            extraction=extraction,
            review_verdict=review,
        )


def _record_book_state_block(
    *,
    recorder: PipelineTraceRecorder,
    session: Session,
    candidate_id: str,
    project_id: str,
    chapter_number: int,
    gate_id: str,
    blocked_path: str,
    issues: list[Any],
    related_object_type: str,
    related_object_id: str,
) -> None:
    issue_payloads = [
        issue.model_dump(mode="json")
        for issue in issues
        if hasattr(issue, "model_dump")
    ]
    issue_keys = [
        str(item.get("code") or "").strip()
        for item in issue_payloads
        if str(item.get("code") or "").strip()
    ]
    reason = "; ".join(
        str(item.get("message") or item.get("code") or "").strip()
        for item in issue_payloads
        if str(item.get("message") or item.get("code") or "").strip()
    )
    recorder.record_event(
        updater=StateUpdater(session),
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
        scope="chapter",
        summary=f"第{chapter_number}章 {gate_id} 阻止 Canon 写入。",
        reason=reason or blocked_path,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        payload=attach_gate_outcome(
            {
                "blocked_path": blocked_path,
                "issues": issue_payloads,
            },
            GateOutcome(
                gate_id=gate_id,
                responsibility_domain="book_state_admission",
                scope="chapter",
                candidate_id=candidate_id,
                chapter_number=chapter_number,
                fired=True,
                decision="block",
                blocked=True,
                issue_keys=list(dict.fromkeys(issue_keys)),
            ),
        ),
    )


class CanonPreparationService:
    def __init__(
        self,
        *,
        quality_preparer: CanonQualityPreparer | None = None,
        book_state_preparer: BookStateCanonPreparer | None = None,
    ) -> None:
        self.quality_preparer = quality_preparer or CanonQualityPreparer()
        self.book_state_preparer = book_state_preparer or BookStateCanonPreparer()

    def prepare(
        self,
        *,
        request: CanonPreparationRequest,
        session: Session,
        updater: StateUpdater,
        policy: RuntimePolicy,
        llm_client: ModelAdapter,
        artifact_store: ArtifactStore,
        recorder: PipelineTraceRecorder,
    ) -> CanonPreparationOutcome:
        candidate_id = request.candidate_id
        project_id = request.project_id
        chapter_number = request.chapter_number
        writer_output = request.writer_output
        verdict = request.verdict
        candidate = CandidateDraftRepository(session).get(candidate_id)
        if candidate is None:
            raise LookupError("candidate draft not found")
        if candidate.project_id != project_id:
            raise ValueError("candidate project mismatch")
        if int(candidate.chapter_number or 0) != int(chapter_number or 0):
            raise ValueError("candidate chapter mismatch")
        if candidate.body_hash != candidate_body_hash(writer_output.body):
            raise ValueError("candidate body changed after review")
        recorder.record_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.CANON_COMMIT_STARTED,
            scope="chapter",
            summary=f"第{chapter_number}章 candidate 开始 Canon 准入评估。",
            related_object_type="candidate_draft",
            related_object_id=candidate.id,
            payload=attach_gate_outcome(
                {},
                GateOutcome(
                    gate_id="canon_quality",
                    responsibility_domain="canon_admission",
                    scope="chapter",
                    candidate_id=candidate.id,
                    chapter_number=chapter_number,
                    policy_version=int(candidate.policy_version or 0),
                    evaluated=False,
                    fired=False,
                    decision="pass",
                    blocked=False,
                ),
            ),
        )
        ineligible_reason = candidate_ineligibility_reason(verdict)
        if ineligible_reason:
            _mark_candidate_needs_review(
                CandidateDraftRepository(session),
                candidate.id,
                reason=ineligible_reason,
            )
            return CanonPreparationOutcome(
                blocked_path=ineligible_reason,
                block_kind="candidate_ineligible",
            )

        try:
            quality_outcome = self.quality_preparer.evaluate(
                policy=policy,
                llm_client=llm_client,
                artifact_store=artifact_store,
                recorder=recorder,
                session=session,
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
                candidate_id=candidate.id,
                policy_version=int(candidate.policy_version or 0),
            )
        except Exception as exc:  # noqa: BLE001
            recorder.record_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.CANON_COMMIT_BLOCKED,
                scope="chapter",
                summary=f"第{chapter_number}章 Canon 准入评估异常。",
                reason=str(exc),
                related_object_type="candidate_draft",
                related_object_id=candidate.id,
                payload=attach_gate_outcome(
                    {"error": str(exc)},
                    GateOutcome(
                        gate_id="canon_quality",
                        responsibility_domain="canon_admission",
                        scope="chapter",
                        candidate_id=candidate.id,
                        chapter_number=chapter_number,
                        policy_version=int(candidate.policy_version or 0),
                        fired=True,
                        decision="error",
                        blocked=True,
                        issue_keys=[exc.__class__.__name__],
                        issue_groups=["runtime_observation"],
                    ),
                ),
            )
            _mark_candidate_failed(
                CandidateDraftRepository(session),
                candidate.id,
                reason=str(exc),
            )
            return CanonPreparationOutcome(
                blocked_path=str(exc),
                block_kind="canon_preparation_error",
            )
        if quality_outcome.blocked:
            _mark_candidate_needs_review(
                CandidateDraftRepository(session),
                candidate.id,
                reason=quality_outcome.blocked_path or "canon quality blocked",
            )
            return CanonPreparationOutcome(
                blocked_path=quality_outcome.blocked_path,
                block_kind="canon_quality",
                canon_gate_result=quality_outcome.gate_result,
            )
        try:
            entity_admission_plan = EntityRegistrar(
                session=session
            ).verify_writer_output_admission(
                project_id=project_id,
                writer_output=writer_output,
            )
        except Exception as exc:  # noqa: BLE001
            _mark_candidate_needs_review(
                CandidateDraftRepository(session),
                candidate.id,
                reason=str(exc),
            )
            return CanonPreparationOutcome(
                blocked_path=str(exc),
                block_kind="entity_admission",
            )
        try:
            book_state_outcome = self.book_state_preparer.prepare(
                policy=policy,
                recorder=recorder,
                session=session,
                candidate_id=candidate_id,
                project_id=project_id,
                chapter_number=chapter_number,
                writer_output=writer_output,
                verdict=verdict,
            )
        except Exception as exc:  # noqa: BLE001
            _mark_candidate_failed(
                CandidateDraftRepository(session),
                candidate.id,
                reason=str(exc),
            )
            return CanonPreparationOutcome(
                blocked_path=str(exc),
                block_kind="canon_preparation_error",
            )
        if book_state_outcome.blocked:
            _mark_candidate_needs_review(
                CandidateDraftRepository(session),
                candidate.id,
                reason=book_state_outcome.blocked_path or "BookState blocked",
            )
            return CanonPreparationOutcome(
                blocked_path=book_state_outcome.blocked_path,
                block_kind="book_state",
            )
        return self.prepare_from_approved(
            session=session,
            candidate_id=candidate_id,
            approved_book_state_changes=book_state_outcome.approved_changes,
            entity_admission_plan=entity_admission_plan,
            acceptance_mode=request.acceptance_mode,
            repair_attempt_count=request.repair_attempt_count,
            residual_review_issues=request.residual_review_issues,
            canon_risk_level=request.canon_risk_level,
            quality_admission_run_id=getattr(
                quality_outcome, "quality_admission_run_id", ""
            ),
            obligation_resolution_plan=getattr(
                quality_outcome, "obligation_resolution_plan", None
            ),
        )

    def prepare_from_approved(
        self,
        *,
        session: Session,
        candidate_id: str,
        approved_book_state_changes: ApprovedGraphDeltaSet,
        entity_admission_plan: EntityAdmissionPlan,
        acceptance_mode: str,
        repair_attempt_count: int,
        residual_review_issues: list[dict[str, Any]],
        canon_risk_level: str,
        quality_admission_run_id: str = "",
        obligation_resolution_plan: ObligationResolutionPlan | None = None,
    ) -> CanonPreparationOutcome:
        repository = CandidateDraftRepository(session)
        candidate = repository.get(candidate_id, for_update=True)
        if candidate is None:
            raise LookupError("candidate draft not found")
        project_id = str(candidate.project_id or "")
        chapter_number = int(candidate.chapter_number or 0)
        if approved_book_state_changes.project_id != project_id:
            raise ValueError("BookState project mismatch")
        if int(approved_book_state_changes.chapter_number or 0) != chapter_number:
            raise ValueError("BookState chapter mismatch")
        admission_fingerprint = candidate_writer_output_admission_fingerprint(candidate)
        if (
            not admission_fingerprint
            or entity_admission_plan.candidate_fingerprint != admission_fingerprint
        ):
            _mark_candidate_needs_review(
                repository,
                candidate.id,
                reason="entity admission candidate fingerprint mismatch",
            )
            return CanonPreparationOutcome(
                block_kind="entity_admission",
                blocked_path="entity admission candidate fingerprint mismatch",
            )
        if entity_admission_plan.blocked:
            _mark_candidate_needs_review(
                repository,
                candidate.id,
                reason="; ".join(entity_admission_plan.plan_conflicts),
            )
            return CanonPreparationOutcome(
                block_kind="entity_admission",
                blocked_path="; ".join(entity_admission_plan.plan_conflicts),
            )
        if entity_admission_plan.project_id != project_id:
            raise ValueError("Entity admission project mismatch")
        if int(entity_admission_plan.chapter_number or 0) != chapter_number:
            raise ValueError("Entity admission chapter mismatch")

        expected_previous_accepted_chapter = int(
            session.scalar(
                select(func.max(ChapterPlan.chapter_number)).where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.status == "accepted",
                    ChapterPlan.chapter_number < chapter_number,
                )
            )
            or 0
        )
        expected_book_state_chapter = int(
            session.scalar(
                select(func.max(GraphDeltaRow.chapter_number)).where(
                    GraphDeltaRow.project_id == project_id,
                    GraphDeltaRow.chapter_number < chapter_number,
                )
            )
            or 0
        )
        project = session.get(Project, project_id)
        chapter = session.get(ChapterPlan, candidate.chapter_plan_id)
        if project is None or chapter is None:
            raise ValueError("Canon publisher snapshot source is missing")
        publisher_bindings = publisher_binding_snapshot(
            project.automation_json,
            default_book_name=project.title,
        )
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        if draft is None:
            raise ValueError("obligation candidate draft missing")
        obligation_context = context_obligations(
            session, project_id, chapter_number, draft_id=draft.id, for_admission=True
        )
        identity = {
            "project_id": project_id,
            "chapter_number": chapter_number,
            "candidate_id": candidate.id,
            "draft_id": draft.id,
            "chapter_body": draft.body_text,
        }
        if obligation_resolution_plan is None:
            obligation_resolution_plan = build_resolution_plan(
                obligations=obligation_context, form=None, answers=None, **identity
            )
        validate_resolution_plan(
            obligation_resolution_plan, obligations=obligation_context, **identity
        )
        plan = CanonCommitPlan.build(
            project_id=project_id,
            chapter_number=chapter_number,
            candidate_id=candidate.id,
            candidate_body_hash=candidate.body_hash,
            plan_revision=candidate.plan_revision,
            policy_version=candidate.policy_version,
            expected_previous_accepted_chapter=expected_previous_accepted_chapter,
            expected_book_state_chapter=expected_book_state_chapter,
            expected_book_revision=project.book_revision,
            quality_admission_run_id=quality_admission_run_id,
            obligation_resolution_plan=obligation_resolution_plan,
            approved_book_state_changes=approved_book_state_changes,
            entity_admission_plan=entity_admission_plan,
            acceptance_mode=acceptance_mode,
            repair_attempt_count=repair_attempt_count,
            residual_review_issues=residual_review_issues,
            canon_risk_level=canon_risk_level,
            chapter_title=str(chapter.title or "").strip() or f"第{chapter_number}章",
            publisher_bindings=publisher_bindings,
            audit_events=(
                CanonAuditEvent(
                    event_type=DecisionEventType.CANON_COMMIT,
                    event_family="business_event",
                    summary=f"第{chapter_number}章 canon 原子提交完成。",
                    payload={"candidate_id": candidate.id},
                    related_object_type="candidate_draft",
                    related_object_id=candidate.id,
                ),
            ),
        )
        repository.attach_entity_admission_plan(
            candidate.id,
            plan_payload=entity_admission_plan.model_dump(mode="json"),
        )
        repository.attach_canon_plan(
            candidate.id,
            plan_payload=plan.model_dump(mode="json"),
            eligibility_payload={
                "eligible": True,
                "candidate_id": candidate.id,
                "body_hash": candidate.body_hash,
                "plan_revision": candidate.plan_revision,
            },
            idempotency_key=plan.idempotency_key,
        )
        if candidate.status != "ready_for_canon":
            repository.transition(candidate.id, "ready_for_canon")
        return CanonPreparationOutcome(plan=plan)


def _mark_candidate_needs_review(
    repository: CandidateDraftRepository,
    candidate_id: str,
    *,
    reason: str,
) -> None:
    candidate = repository.get(candidate_id, for_update=True)
    if candidate is None or candidate.status in {"needs_review", "failed", "accepted"}:
        return
    repository.transition(
        candidate.id,
        "needs_review",
        failure_reason=reason,
    )


def _mark_candidate_failed(
    repository: CandidateDraftRepository,
    candidate_id: str,
    *,
    reason: str,
) -> None:
    candidate = repository.get(candidate_id, for_update=True)
    if candidate is None or candidate.status in {"failed", "accepted"}:
        return
    repository.transition(
        candidate.id,
        "failed",
        failure_reason=reason,
    )


__all__ = [
    "BookStateCanonPreparer",
    "BookStatePreparationOutcome",
    "CanonPreparationService",
]
