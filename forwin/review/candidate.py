"""Candidate review and persistence, independent of generation coordination."""

from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.candidate_drafts import CandidateDraftRepository, candidate_plan_revision
from forwin.checker.rules import ContinuityChecker
from forwin.llm.compat import filter_supported_kwargs
from forwin.models.draft import ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan, Project
from forwin.naming.entity_registrar import EntityRegistrar, LLMEntityAdmissionClassifier
from forwin.observability.pipeline_trace import PipelineTraceRecorder
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.draft_service import DraftReviewService
from forwin.review.repair.verification import RepairVerifier
from forwin.skills import SkillPromptLayerBuilder, SkillRouter
from forwin.state.repo import StateRepository
from forwin.state.updater import StateUpdater
from forwin.storage import ArtifactStore

from .candidate_autofix import (
    apply_canon_name_drift_autofix,
)
from .results import merge_repair_verification


@dataclass(frozen=True, slots=True)
class CandidateReviewRequest:
    session: Session
    repo: StateRepository
    checker: ContinuityChecker
    project_id: str
    chapter_number: int
    context: ChapterContextPack
    output: WriterOutput


@dataclass(frozen=True, slots=True)
class CandidateRepairVerification:
    original_output: WriterOutput
    before_review: ReviewVerdict
    instruction: RepairInstruction


@dataclass(frozen=True, slots=True)
class CandidateReviewEvaluation:
    output: WriterOutput
    review: ReviewVerdict


@dataclass(frozen=True, slots=True)
class PersistedCandidateReview:
    output: WriterOutput
    draft: ChapterDraft
    review_row: ChapterReview
    candidate_id: str


def _call_compatible(function, **kwargs):
    return function(**filter_supported_kwargs(function, kwargs))


class CandidateReviewService:
    def __init__(
        self,
        *,
        draft_review: DraftReviewService,
        repair_verifier: RepairVerifier,
        model_client,
        skill_router: SkillRouter,
        skill_prompt_layer_builder: SkillPromptLayerBuilder,
        artifact_store: ArtifactStore,
        trace_recorder: PipelineTraceRecorder,
    ):
        self.draft_review = draft_review
        self.repair_verifier = repair_verifier
        self.model_client = model_client
        self.skill_router = skill_router
        self.skill_prompt_layer_builder = skill_prompt_layer_builder
        self.artifact_store = artifact_store
        self.trace_recorder = trace_recorder

    def evaluate(
        self,
        request: CandidateReviewRequest,
        *,
        verification: CandidateRepairVerification | None = None,
    ) -> CandidateReviewEvaluation:
        output = self._plan_entities(
            session=request.session,
            project_id=request.project_id,
            chapter_number=request.chapter_number,
            writer_output=request.output,
        )
        review = self._review(
            repo=request.repo,
            checker=request.checker,
            project_id=request.project_id,
            context=request.context,
            writer_output=output,
        )
        fixed = apply_canon_name_drift_autofix(output, review)
        if fixed is not None:
            output = self._plan_entities(
                session=request.session,
                project_id=request.project_id,
                chapter_number=request.chapter_number,
                writer_output=fixed,
            )
            review = self._review(
                repo=request.repo,
                checker=request.checker,
                project_id=request.project_id,
                context=request.context,
                writer_output=output,
            )
        if verification is not None:
            result = self.repair_verifier.verify(
                original_output=verification.original_output,
                repaired_output=output,
                before_review=verification.before_review,
                after_review=review,
                repair_instruction=verification.instruction,
            )
            review = merge_repair_verification(review, result, verification.instruction)
        return CandidateReviewEvaluation(output, review)

    def persist(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        chapter_plan: ChapterPlan,
        evaluation: CandidateReviewEvaluation,
    ) -> PersistedCandidateReview:
        chapter_number = chapter_plan.chapter_number
        writer_output = evaluation.output
        review = evaluation.review
        artifact_paths = self.artifact_store.save_writer_output(
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
        )
        self.trace_recorder.record_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.WRITER_OUTPUT_ARTIFACT_SAVED,
            scope="chapter",
            summary=f"第{chapter_number}章 writer output artifact 已保存。",
            payload={
                "draft_blob_path": artifact_paths.get("draft_blob_path", ""),
                "artifact_meta_path": artifact_paths.get("meta_path", ""),
                "char_count": int(getattr(writer_output, "char_count", 0) or 0),
            },
        )
        persisted_output = artifact_paths["writer_output"].model_copy(
            update={
                "generation_meta": {
                    **writer_output.generation_meta,
                    "artifact_meta_path": artifact_paths["meta_path"],
                },
            }
        )
        draft = updater.save_draft(
            chapter_plan_id=chapter_plan.id,
            writer_output=persisted_output,
            raw_response=artifact_paths["meta_path"],
            model_name=str(getattr(self.model_client, "model", "") or ""),
        )
        review_row = updater.save_review(draft.id, review)
        candidate_repository = CandidateDraftRepository(session)
        previous_candidate = candidate_repository.latest_for_chapter(
            project_id=project_id,
            chapter_number=chapter_number,
        )
        project = session.get(Project, project_id)
        candidate = candidate_repository.create_reviewed_version(
            project_id=project_id,
            chapter_plan=chapter_plan,
            draft=draft,
            review=review_row,
            writer_output=persisted_output,
            plan_revision=candidate_plan_revision(chapter_plan),
            policy_version=max(
                1, int(getattr(project, "runtime_policy_version", 1) or 1)
            ),
            parent_candidate_id=(
                str(previous_candidate.id)
                if previous_candidate is not None
                and previous_candidate.candidate_draft_id != draft.id
                else ""
            ),
            repair_attempt_count=max(
                int(chapter_plan.repair_attempt_count or 0),
                int(previous_candidate.repair_attempt_count or 0)
                if previous_candidate
                else 0,
            ),
            repair_history=json.loads(previous_candidate.repair_history_json or "[]")
            if previous_candidate
            else [],
        )
        updater.mark_chapter_status(
            project_id,
            chapter_number,
            "drafted",
            repair_attempt_count=candidate.repair_attempt_count,
        )
        session.flush()
        return PersistedCandidateReview(
            persisted_output, draft, review_row, str(candidate.id)
        )

    def _review(
        self,
        *,
        repo: StateRepository,
        checker: ContinuityChecker,
        project_id: str,
        context,
        writer_output: WriterOutput,
    ) -> ReviewVerdict:
        reviewer_skill_layers = self.skill_prompt_layer_builder.build(
            self.skill_router.select(
                scope="reviewer",
                stage_key="chapter_review",
                task_family="review_chapter",
            )
        )
        review = _call_compatible(
            self.draft_review.review,
            project_id=project_id,
            repo=repo,
            context=context,
            writer_output=writer_output,
            continuity_checker=checker,
            reviewer_skill_layers=reviewer_skill_layers,
        )
        return review

    def _plan_entities(
        self,
        *,
        session: Session,
        project_id: str,
        chapter_number: int,
        writer_output: WriterOutput,
    ) -> WriterOutput:
        llm_client = self.model_client
        classifier = (
            LLMEntityAdmissionClassifier(llm_client) if llm_client is not None else None
        )
        result = EntityRegistrar(
            session=session, classifier=classifier
        ).plan_writer_output(
            project_id=project_id,
            chapter_number=chapter_number,
            writer_output=writer_output,
        )
        return result.writer_output
