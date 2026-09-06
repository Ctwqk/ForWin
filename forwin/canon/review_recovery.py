"""Narrow manual re-review of an unchanged, rolled-back historical candidate."""

from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.candidate_drafts import (
    CandidateDraftRepository,
    CandidateTransitionError,
    candidate_body_hash,
    candidate_plan_revision,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan, Project
from forwin.naming import writer_output_admission_fingerprint
from forwin.outbox.store import any_outbox_events_exist
from forwin.protocol.writer import WriterOutput

from .historical_rewrite import HistoricalCanonRewriteService
from .plan import CanonCommitPlan


def reopen_failed_historical_candidate_for_review(
    session: Session,
    *,
    project_id: str,
    chapter_number: int,
    candidate_id: str,
    draft_id: str,
    review_id: str,
    actor_type: str,
    source: str,
    writer_output: WriterOutput,
    artifact_path: str,
) -> None:
    """Restore only reviewability; normal preparation must rerun every gate.

    Failed writes predate a dedicated failure audit record. Their durable
    evidence is the prior eligible plan, unchanged reviewed version,
    pending API rewrite authorization, and intact committed predecessor.
    Neither a retry marker nor a new Canon plan is written by this operation.
    """
    error = "failed candidate is not an authorized historical re-review"
    if actor_type != "api" or source != "chapter_review_approve_api":
        raise CandidateTransitionError(error)
    project = session.scalar(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    chapter = session.scalar(
        select(ChapterPlan)
        .where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number == chapter_number,
        )
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    candidate = session.scalar(
        select(CandidateDraftRecord)
        .where(CandidateDraftRecord.id == candidate_id)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    if (
        project is None
        or chapter is None
        or chapter.status != "needs_review"
        or candidate is None
        or candidate.status != "failed"
        or candidate.canon_status != "candidate"
        or candidate.canon_commit_id
        or not str(candidate.failure_reason or "").strip()
    ):
        raise CandidateTransitionError(error)
    try:
        plan_payload = json.loads(candidate.canon_commit_plan_json)
        if not isinstance(plan_payload, dict) or plan_payload.get(
            "acceptance_mode"
        ) not in {"normal", "human_approved"}:
            raise ValueError(error)
        plan = CanonCommitPlan.model_validate_json(candidate.canon_commit_plan_json)
        eligibility = json.loads(candidate.eligibility_decision_json)
        metadata = json.loads(candidate.metadata_json)
        if not isinstance(eligibility, dict) or not isinstance(metadata, dict):
            raise ValueError(error)
    except (TypeError, ValueError) as exc:
        raise CandidateTransitionError(error) from exc
    draft = session.scalar(
        select(ChapterDraft)
        .where(ChapterDraft.chapter_plan_id == chapter.id)
        .order_by(ChapterDraft.version.desc(), ChapterDraft.id.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    review = session.scalar(
        select(ChapterReview)
        .where(ChapterReview.draft_id == draft_id)
        .order_by(ChapterReview.created_at.desc(), ChapterReview.id.desc())
        .limit(1)
        .with_for_update()
        .execution_options(populate_existing=True)
    )
    latest = CandidateDraftRepository(session).latest_for_chapter(
        project_id=project_id, chapter_number=chapter_number
    )
    if (
        latest is None
        or latest.id != candidate_id
        or draft is None
        or draft.id != draft_id
        or review is None
        or review.id != review_id
        or candidate.candidate_draft_id != draft_id
        or candidate.review_id != review_id
        or candidate.chapter_plan_id != chapter.id
        or candidate.version != draft.version
        or candidate.project_id != project_id
        or plan.project_id != project_id
        or candidate.chapter_number != chapter_number
        or plan.chapter_number != chapter_number
        or plan.candidate_id != candidate_id
        or candidate.idempotency_key != plan.idempotency_key
        or candidate.body_hash != plan.candidate_body_hash
        or candidate_body_hash(draft.body_text) != plan.candidate_body_hash
        or candidate.plan_revision != plan.plan_revision
        or candidate_plan_revision(chapter) != plan.plan_revision
        or candidate.policy_version != plan.policy_version
        or project.runtime_policy_version != plan.policy_version
        or not isinstance(artifact_path, str)
        or not artifact_path.strip()
        or draft.llm_raw_response != artifact_path
        or candidate.writer_artifact_ref != artifact_path
        or writer_output.project_id != project_id
        or writer_output.chapter_number != chapter_number
        or writer_output.title != metadata.get("title")
        or writer_output.body != draft.body_text
        or eligibility.get("eligible") is not True
        or eligibility.get("candidate_id") != candidate_id
        or eligibility.get("body_hash") != plan.candidate_body_hash
        or eligibility.get("plan_revision") != plan.plan_revision
        or candidate.review_result_json != review.review_meta_json
        or review.verdict not in {"pass", "warn"}
    ):
        raise CandidateTransitionError(error)
    artifact_fingerprint = writer_output_admission_fingerprint(writer_output)
    fingerprint_key = "writer_output_admission_fingerprint"
    if artifact_fingerprint != plan.entity_admission_plan.candidate_fingerprint or (
        fingerprint_key in metadata
        and metadata[fingerprint_key] != artifact_fingerprint
    ):
        raise CandidateTransitionError(error)
    if session.scalar(
        select(CanonCommitRecord.id).where(
            (CanonCommitRecord.candidate_id == candidate_id)
            | (CanonCommitRecord.idempotency_key == plan.idempotency_key)
        )
    ) or any_outbox_events_exist(
        session, event_ids=[event.event_id for event in plan.outbox_events]
    ):
        raise CandidateTransitionError(error)
    replacement = HistoricalCanonRewriteService(session).prepare_replacement(
        plan, persist_marker_backfill=False
    )
    if replacement is None:
        raise CandidateTransitionError(error)
    predecessor = session.get(CandidateDraftRecord, replacement.previous.candidate_id)
    if (
        predecessor is None
        or predecessor.status != "accepted"
        or predecessor.canon_commit_id != replacement.previous.id
        or predecessor.chapter_plan_id != chapter.id
        or predecessor.version >= candidate.version
        or candidate.created_at <= replacement.marker.created_at
    ):
        raise CandidateTransitionError(error)
    if fingerprint_key not in metadata:
        # Preserve the exact proof needed by preparation and locked admission.
        # This is permitted only after every historical recovery guard succeeds.
        metadata[fingerprint_key] = artifact_fingerprint
        candidate.metadata_json = json.dumps(
            metadata, ensure_ascii=False, sort_keys=True
        )
    # This exception to failed's terminal state exists only at manual re-review.
    # Keep the prior failure reason and plan until normal preparation succeeds.
    candidate.status = "needs_review"
    session.flush()
