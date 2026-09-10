"""Project -> Chapter -> Job -> Attempt is the publication/Canon lock order."""

from __future__ import annotations

import json

from sqlalchemy import select

from forwin.candidate_drafts import candidate_body_hash
from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
from forwin.models.draft import CandidateDraftRecord
from forwin.models.project import ChapterPlan, Project
from forwin.models.publisher import PublisherChapterBinding, PublisherUploadJob


class PublicationConflictError(ValueError):
    reason = "publication_conflict"

    def __init__(self, conflicting_job_id: str) -> None:
        self.conflicting_job_id = conflicting_job_id
        super().__init__(
            f"chapter publication blocked by existing protection from job {conflicting_job_id}"
        )


class PublicationPrefixWait(ValueError):
    reason = "publication_prefix_wait"

    def __init__(self) -> None:
        super().__init__("confirmed publication prefix is incomplete on this platform")


def lock_job_chapter(session, job_id: str) -> None:
    # Discover identities without row locks; revalidate after acquiring the owner.
    identity = session.execute(
        select(PublisherUploadJob.project_id, PublisherUploadJob.chapter_number).where(
            PublisherUploadJob.id == job_id
        )
    ).one_or_none()
    if identity and identity.project_id:
        lock_project_chapters(session, identity.project_id)


def lock_project_chapters(session, project_id: str) -> None:
    session.execute(
        select(Project).where(Project.id == project_id).with_for_update()
    ).scalar_one_or_none()
    list(
        session.scalars(
            select(ChapterPlan)
            .where(ChapterPlan.project_id == project_id)
            .order_by(ChapterPlan.chapter_number, ChapterPlan.id)
            .with_for_update()
        )
    )


def require_active_job(session, job: PublisherUploadJob) -> CanonCommitRecord | None:
    if not job.canon_commit_id:
        if job.project_id and job.task_kind == "chapter_upload":
            raise ValueError("project chapter publication requires Canon identity")
        return None  # Standalone publisher tasks do not replace a local Canon chapter.
    commit = session.get(CanonCommitRecord, job.canon_commit_id)
    chapter = session.get(ChapterPlan, commit.chapter_plan_id) if commit else None
    candidate = (
        session.get(CandidateDraftRecord, commit.candidate_id) if commit else None
    )
    if (
        commit is None
        or chapter is None
        or candidate is None
        or chapter.active_commit_id != commit.id
        or chapter.status != "accepted"
        or commit.project_id != job.project_id
        or chapter.project_id != job.project_id
        or commit.chapter_number != job.chapter_number
        or chapter.chapter_number != job.chapter_number
        or commit.candidate_id != job.candidate_id
        or candidate.body_hash != job.body_sha256
        or job.chapter_title != commit.chapter_title
        or candidate_body_hash(job.body_text) != job.body_sha256
    ):
        raise ValueError("stale Canon publisher identity")
    if commit.production_mode in {"factory_batch", "soak_test"}:
        raise ValueError("offline Canon content cannot enter real publication")
    return commit


def reserve_publication(session, job: PublisherUploadJob) -> None:
    commit = require_active_job(session, job)
    if commit is None:
        return
    # A new queue identity does not erase an earlier external effect. This
    # also covers protections whose original upload job has been deleted.
    conflict = session.scalar(
        select(CanonPublicationProtection.upload_job_id)
        .where(
            CanonPublicationProtection.project_id == job.project_id,
            CanonPublicationProtection.chapter_number == job.chapter_number,
            CanonPublicationProtection.platform_id == job.platform_id,
            CanonPublicationProtection.upload_job_id != job.id,
            CanonPublicationProtection.state.in_(["reserved", "published"]),
        )
        .order_by(CanonPublicationProtection.id)
        .limit(1)
    )
    if conflict is not None:
        raise PublicationConflictError(conflict)
    if job.publish:
        published = set(
            session.scalars(
                select(CanonPublicationProtection.chapter_number).where(
                    CanonPublicationProtection.project_id == job.project_id,
                    CanonPublicationProtection.platform_id == job.platform_id,
                    CanonPublicationProtection.state == "published",
                    CanonPublicationProtection.chapter_number < job.chapter_number,
                )
            )
        )
        if published != set(range(1, job.chapter_number)):
            raise PublicationPrefixWait()
    row = session.scalar(
        select(CanonPublicationProtection).where(
            CanonPublicationProtection.upload_job_id == job.id
        )
    )
    if row is None:
        session.add(
            CanonPublicationProtection(
                project_id=job.project_id,
                chapter_plan_id=commit.chapter_plan_id,
                chapter_number=job.chapter_number,
                canon_commit_id=commit.id,
                upload_job_id=job.id,
                platform_id=job.platform_id,
                content_sha256=job.body_sha256,
                state="reserved",
            )
        )
    elif row.state != "published":
        row.state = "reserved"
    session.flush()


def record_publication_receipt(
    session,
    job,
    *,
    state: str,
    remote_book_id: str,
    remote_chapter_id: str,
    evidence: dict,
) -> None:
    if not job.canon_commit_id:
        return
    row = session.scalar(
        select(CanonPublicationProtection).where(
            CanonPublicationProtection.upload_job_id == job.id
        )
    )
    if row is None:
        # This is an observed external fact, not permission for a new action.
        # Old/out-of-order and late receipts must freeze their original identity.
        commit = session.get(CanonCommitRecord, job.canon_commit_id)
        if (
            commit is None
            or commit.project_id != job.project_id
            or commit.chapter_number != job.chapter_number
            or commit.candidate_id != job.candidate_id
        ):
            raise ValueError("receipt Canon publication identity is invalid")
        row = CanonPublicationProtection(
            project_id=job.project_id,
            chapter_plan_id=commit.chapter_plan_id,
            chapter_number=job.chapter_number,
            canon_commit_id=commit.id,
            upload_job_id=job.id,
            platform_id=job.platform_id,
            content_sha256=job.body_sha256,
            state="reserved",
        )
        session.add(row)
        session.flush()
    if row.state != "published":
        # Pending platform review can become public without another local action.
        row.state = "published" if state == "published" else "reserved"
    row.remote_book_id = remote_book_id
    row.remote_chapter_id = remote_chapter_id
    row.evidence_json = json.dumps(evidence, ensure_ascii=False, sort_keys=True)


def release_absent_publication(session, job) -> None:
    row = session.scalar(
        select(CanonPublicationProtection).where(
            CanonPublicationProtection.upload_job_id == job.id
        )
    )
    if row is not None and row.state != "published":
        row.state = "confirmed_absent"


def require_revision_unprotected(
    session, *, project_id: str, from_chapter: int
) -> None:
    protected = session.scalar(
        select(CanonPublicationProtection.id)
        .where(
            CanonPublicationProtection.project_id == project_id,
            CanonPublicationProtection.chapter_number >= from_chapter,
            CanonPublicationProtection.state.in_(["reserved", "published"]),
        )
        .limit(1)
    )
    # Historical platform observations may lack a job identity: never silently ignore them.
    bound = session.scalar(
        select(PublisherChapterBinding.id)
        .where(
            PublisherChapterBinding.project_id == project_id,
            PublisherChapterBinding.chapter_number >= from_chapter,
            PublisherChapterBinding.publish_state.in_(
                ["published", "review_pending", "submitted", "drafted", "unknown"]
            ),
        )
        .limit(1)
    )
    if protected or bound:
        raise ValueError("chapter revision blocked by publication protection")


def lock_publisher_projects(session, job_ids: list[str]) -> None:
    # Batch claim/recovery can touch several books. Acquire their owners in one
    # deterministic order before taking any job locks (including lease expiry).
    for project_id in session.scalars(
        select(PublisherUploadJob.project_id)
        .where(PublisherUploadJob.project_id != "", PublisherUploadJob.id.in_(job_ids))
        .distinct()
        .order_by(PublisherUploadJob.project_id)
    ):
        lock_project_chapters(session, project_id)
