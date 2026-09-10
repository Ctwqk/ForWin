"""Finite proposal and validation use cases; live acceptance stays in CanonAdmissionService."""

from __future__ import annotations

import json

from sqlalchemy import func, select

from forwin.candidate_drafts import candidate_body_hash, candidate_plan_revision
from forwin.models.canon import CanonCommitRecord, CanonRevisionValidationRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft, ChapterReview
from forwin.models.project import ChapterPlan, Project
from forwin.publisher_runtime.protection import (
    lock_project_chapters,
    require_revision_unprotected,
)
from forwin.runtime.policy_store import ProjectPolicyStore

from .revision_validation import (
    assess_revision,
    revision_digest,
    validate_chapter_sequence,
)


def revision_model_identity(writer) -> dict:
    from .revision_model import model_identity

    return model_identity(writer)


def revision_policy_fingerprint(project) -> str:
    return revision_digest(
        {
            "policy": project.runtime_policy_json,
            "version": project.runtime_policy_version,
            "automation": project.automation_json,
            "genesis_revision": project.active_genesis_revision_id,
            "title": project.title,
            "genre": project.genre,
            "premise": project.premise,
            "setting": project.setting_summary,
            "target": project.target_total_chapters,
        }
    )


class RevisionValidationService:
    def __init__(self, *, session_factory, writer, policy):
        self.session_factory = session_factory
        self.writer = writer
        self.policy = policy

    def prepare(self, *, project_id: str, candidate_id: str):
        from .revision_evaluator import HistoricalBodyEvaluator
        from .revision_replica import (
            CandidateReplica,
            PrefixProvenanceUnknown,
            capture_revision,
        )
        from .types import CanonPreparationOutcome

        model = revision_model_identity(self.writer)
        with self.session_factory.begin() as source:
            # All live Canon/publication writers share this owner lock. Release
            # immediately after capture, before any extraction or model request.
            lock_project_chapters(source, project_id)
            project = source.get(Project, project_id)
            candidate = source.get(CandidateDraftRecord, candidate_id)
            metadata = json.loads(candidate.metadata_json or "{}") if candidate else {}
            if candidate is None or not metadata.get("revision_proposal"):
                raise ValueError(
                    "historical acceptance requires a real revision proposal"
                )
            if metadata.get("revision_base_book_revision") != project.book_revision:
                raise ValueError("revision proposal book revision is stale")
            if ProjectPolicyStore(source).load(project).policy != self.policy:
                raise ValueError("revision runtime policy differs from project policy")
            require_revision_unprotected(
                source, project_id=project_id, from_chapter=candidate.chapter_number
            )
            try:
                snapshot = capture_revision(
                    source,
                    project_id=project_id,
                    candidate_id=candidate_id,
                    model_identity=model,
                    policy_fingerprint=revision_policy_fingerprint(project),
                )
            except PrefixProvenanceUnknown as exc:
                # No complete manifest exists yet. Persist the observed failure
                # identity explicitly instead of inventing an empty valid prefix.
                evidence = {
                    "status": "unknown",
                    "stage": "source_capture",
                    "project_id": project_id,
                    "candidate_id": candidate_id,
                    "candidate_body_sha256": candidate.body_hash,
                    "base_book_revision": project.book_revision,
                    "policy_fingerprint": revision_policy_fingerprint(project),
                    "model_identity": model,
                    "uncovered": [str(exc)],
                    "observed_chapters": [
                        dict(row)
                        for row in source.execute(
                            select(
                                ChapterPlan.id,
                                ChapterPlan.chapter_number,
                                ChapterPlan.active_commit_id,
                                ChapterPlan.status,
                            )
                            .where(ChapterPlan.project_id == project_id)
                            .order_by(ChapterPlan.chapter_number)
                        ).mappings()
                    ],
                }
                validation_id = "revision-unknown-" + revision_digest(evidence)
                if source.get(CanonRevisionValidationRecord, validation_id) is None:
                    source.add(
                        CanonRevisionValidationRecord(
                            id=validation_id,
                            project_id=project_id,
                            candidate_id=candidate_id,
                            base_book_revision=project.book_revision,
                            status="unknown",
                            result_json=json.dumps(
                                evidence, ensure_ascii=False, sort_keys=True
                            ),
                        )
                    )
                return CanonPreparationOutcome(
                    blocked_path=validation_id, block_kind="revision_unknown"
                )

        try:
            if model["provider"] == "unavailable" or model["model"] == "unavailable":
                raise ValueError(
                    "historical validation model identity/capability unavailable"
                )
            with CandidateReplica(snapshot) as replica:
                from .revision_model import isolated_writer

                evaluator = HistoricalBodyEvaluator(
                    writer=isolated_writer(self.writer, model), policy=self.policy
                )

                def evaluate(chapter):
                    from .revision_validation import (
                        RevisionChapterAssessment,
                        RevisionCheck,
                    )

                    start = len(evaluator.llm_client.evidence)
                    try:
                        assessment = evaluator.evaluate(
                            replica.session, snapshot.manifest, chapter
                        )
                    except Exception as exc:  # noqa: BLE001 — owner boundary preserves evidence and rejects incomplete validation.
                        assessment = RevisionChapterAssessment(
                            chapter_number=chapter.chapter_number,
                            body_sha256=chapter.body_sha256,
                            checks=(
                                RevisionCheck(
                                    dimension="body_extraction",
                                    status="unknown",
                                    explanation=f"Historical owner could not complete: {type(exc).__name__}",
                                ),
                            ),
                        )
                    return assessment.model_copy(
                        update={
                            "model_calls": tuple(evaluator.llm_client.evidence[start:])
                        }
                    )

                result = validate_chapter_sequence(snapshot.manifest, evaluate=evaluate)
            if revision_model_identity(self.writer) != model:
                result = assess_revision(
                    snapshot.manifest,
                    result.chapters,
                    uncovered=("model_identity_changed",),
                )
        except Exception as exc:  # noqa: BLE001 — owner boundary preserves evidence and rejects incomplete validation.
            result = assess_revision(
                snapshot.manifest,
                (),
                uncovered=(f"prefix_or_runtime:{type(exc).__name__}:{exc}",),
            )
        payload = result.model_dump(mode="json")
        # Draft content is already immutable in ChapterDraft. Persist references,
        # hashes and evidence rather than duplicate every unchanged successor body.
        for chapter in payload["manifest"]["chapters"]:
            chapter.pop("body")
        with self.session_factory.begin() as session:
            lock_project_chapters(session, project_id)
            project = session.get(Project, project_id)
            candidate = session.scalar(
                select(CandidateDraftRecord)
                .where(CandidateDraftRecord.id == candidate_id)
                .with_for_update()
            )
            record = session.get(CanonRevisionValidationRecord, result.validation_id)
            if record is None:
                record = CanonRevisionValidationRecord(
                    id=result.validation_id,
                    project_id=project_id,
                    candidate_id=candidate_id,
                    base_book_revision=snapshot.manifest.base_book_revision,
                    status=result.status,
                    result_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                )
                session.add(record)
            if result.status != "pass":
                return CanonPreparationOutcome(
                    blocked_path=result.validation_id,
                    block_kind="revision_" + result.status,
                )
            if any(not chapter.prepared_changes for chapter in result.chapters):
                raise ValueError(
                    "passing historical coverage lacks freshly prepared changes"
                )
            # The model ran without live locks. A later result is still evidence,
            # but cannot downgrade an accepted candidate or review a changed base.
            active = session.get(ChapterPlan, candidate.chapter_plan_id)
            if (
                project.book_revision != snapshot.manifest.base_book_revision
                or revision_policy_fingerprint(project)
                != snapshot.manifest.policy_fingerprint
                or candidate.status == "accepted"
                or (
                    active.active_commit_id
                    and session.get(
                        CanonCommitRecord, active.active_commit_id
                    ).candidate_id
                    == candidate_id
                )
                or record.accepted_book_revision is not None
            ):
                return CanonPreparationOutcome(
                    blocked_path=result.validation_id, block_kind="revision_stale"
                )
            review = ChapterReview(
                draft_id=candidate.candidate_draft_id,
                verdict="pass",
                review_meta_json=json.dumps(
                    {"verdict": "pass", "revision_validation_id": result.validation_id}
                ),
            )
            session.add(review)
            session.flush()
            candidate.review_id = review.id
            candidate.review_result_json = review.review_meta_json
            candidate.status = "reviewed"
            plan = revision_commit_plan(session, result, 0)
            return CanonPreparationOutcome(plan=plan)


def revision_commit_plan(session, result, index):
    from forwin.naming import EntityAdmissionPlan
    from forwin.protocol.book_state import ApprovedGraphDeltaSet

    from .outbox_events import CanonPublisherBindingSnapshot
    from .plan import CanonCommitPlan

    chapter = result.manifest.chapters[index]
    prepared = result.chapters[index].prepared_changes
    candidate = session.get(CandidateDraftRecord, chapter.candidate_id)
    project = session.get(Project, result.manifest.project_id)
    return CanonCommitPlan.build(
        project_id=project.id,
        chapter_number=chapter.chapter_number,
        candidate_id=candidate.id,
        candidate_body_hash=chapter.body_sha256,
        plan_revision=next(
            row["plan_revision"]
            for row in result.manifest.baseline_identity
            if row["chapter_plan_id"] == chapter.chapter_plan_id
        ),
        policy_version=result.manifest.policy_version,
        expected_previous_accepted_chapter=chapter.chapter_number - 1,
        expected_book_state_chapter=chapter.chapter_number - 1,
        expected_book_revision=result.manifest.base_book_revision,
        revision_validation_id=result.validation_id,
        approved_book_state_changes=ApprovedGraphDeltaSet.model_validate(
            prepared["approved_changes"]
        ),
        entity_admission_plan=EntityAdmissionPlan.model_validate(
            prepared["entity_plan"]
        ),
        chapter_title=chapter.title,
        publisher_bindings=tuple(
            CanonPublisherBindingSnapshot.model_validate(row)
            for row in result.manifest.publisher_bindings
        ),
    )


def save_revision_proposal(
    session,
    *,
    project_id: str,
    chapter_number: int,
    body: str,
    title: str | None = None,
    expected_book_revision: int | None = None,
):
    lock_project_chapters(session, project_id)
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError("revision project is missing")
    if (
        expected_book_revision is not None
        and project.book_revision != expected_book_revision
    ):
        raise ValueError("revision proposal book revision is stale")
    chapter = session.scalar(
        select(ChapterPlan).where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number == chapter_number,
        )
    )
    if chapter is None or chapter.status != "accepted" or not chapter.active_commit_id:
        raise ValueError("revision proposal requires an active accepted chapter")
    require_revision_unprotected(
        session, project_id=project_id, from_chapter=chapter_number
    )
    commit = session.get(CanonCommitRecord, chapter.active_commit_id)
    body = str(body)
    if not body.strip():
        raise ValueError("revision proposal body is empty")
    title = commit.chapter_title if title is None else str(title).strip()
    if not title:
        raise ValueError("revision proposal title is empty")
    digest = candidate_body_hash(body)
    for existing in session.scalars(
        select(CandidateDraftRecord)
        .where(
            CandidateDraftRecord.chapter_plan_id == chapter.id,
            CandidateDraftRecord.body_hash == digest,
        )
        .order_by(CandidateDraftRecord.version.desc())
    ):
        metadata = json.loads(existing.metadata_json or "{}")
        if (
            metadata.get("revision_base_book_revision") == project.book_revision
            and metadata.get("title") == title
        ):
            return existing
    version = (
        int(
            session.scalar(
                select(func.max(ChapterDraft.version)).where(
                    ChapterDraft.chapter_plan_id == chapter.id
                )
            )
            or 0
        )
        + 1
    )
    draft = ChapterDraft(
        chapter_plan_id=chapter.id,
        version=version,
        body_text=body,
        summary="",
        char_count=len(body),
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(
        draft_id=draft.id,
        verdict="unknown",
        review_meta_json='{"verdict":"unknown","reason":"revision awaiting full-suffix review"}',
    )
    session.add(review)
    session.flush()
    candidate = CandidateDraftRecord(
        project_id=project_id,
        chapter_plan_id=chapter.id,
        chapter_number=chapter_number,
        candidate_draft_id=draft.id,
        review_id=review.id,
        version=version,
        parent_candidate_id=commit.candidate_id,
        body_hash=digest,
        plan_revision=candidate_plan_revision(chapter),
        policy_version=max(1, project.runtime_policy_version),
        status="drafted",
        canon_status="candidate",
        metadata_json=json.dumps(
            {
                "title": title,
                "revision_base_book_revision": project.book_revision,
                "revision_base_commit_id": commit.id,
                "revision_proposal": True,
            },
            ensure_ascii=False,
            sort_keys=True,
        ),
    )
    session.add(candidate)
    session.flush()
    return candidate
