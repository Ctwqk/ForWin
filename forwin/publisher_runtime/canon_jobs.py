from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from typing import Any

from sqlalchemy import select

from forwin.candidate_drafts import candidate_body_hash
from forwin.canon.outbox_events import (
    CANON_PUBLISHER_REQUESTED,
    CanonPublisherBindingSnapshot,
    CanonPublisherEventPayload,
    parse_canon_event_envelope,
)
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan, Project
from forwin.outbox.worker import OutboxClaim

from .idempotency import publisher_job_idempotency_key
from .protection import lock_project_chapters


class CanonPublisherJobService:
    def __init__(self, *, session_factory, upload_jobs) -> None:
        self.session_factory = session_factory
        self.upload_jobs = upload_jobs

    def materialize(
        self,
        *,
        canon_commit_id: str,
        canon_idempotency_key: str,
        project_id: str,
        chapter_number: int,
        candidate_id: str,
        chapter_title: str,
        body_sha256: str,
        bindings: Sequence[Mapping[str, Any] | CanonPublisherBindingSnapshot],
        publish: bool = True,
    ) -> list[dict[str, Any]]:
        normalized_bindings = self._bindings(bindings)
        normalized_title = str(chapter_title or "").strip()
        if not normalized_title:
            raise ValueError("chapter_title must be non-empty")
        normalized_project_id = str(project_id or "").strip()
        normalized_candidate_id = str(candidate_id or "").strip()
        normalized_commit_id = str(canon_commit_id or "").strip()
        normalized_canon_key = str(canon_idempotency_key or "").strip()
        normalized_body_hash = str(body_sha256 or "").strip()
        normalized_chapter = int(chapter_number or 0)

        with self.session_factory() as session:
            project, commit, candidate, draft = self._load_canon_rows(
                session,
                canon_commit_id=normalized_commit_id,
                canon_idempotency_key=normalized_canon_key,
                project_id=normalized_project_id,
                chapter_number=normalized_chapter,
                candidate_id=normalized_candidate_id,
                body_sha256=normalized_body_hash,
            )
            if commit.production_mode in {"factory_batch", "soak_test"}:
                raise ValueError("offline Canon content cannot enter real publication")
            if normalized_title != commit.chapter_title:
                raise ValueError("Canon publisher accepted title mismatch")
            jobs = []
            for binding in normalized_bindings:
                identity = publisher_job_idempotency_key(
                    canon_idempotency_key=commit.idempotency_key,
                    project_id=project.id,
                    chapter_number=normalized_chapter,
                    candidate_id=candidate.id,
                    platform_id=binding.platform,
                )
                job, _created = self.upload_jobs.create_idempotent_canon_job(
                    session,
                    idempotency_key=identity,
                    canon_commit_id=commit.id,
                    canon_idempotency_key=commit.idempotency_key,
                    project_id=project.id,
                    candidate_id=candidate.id,
                    chapter_number=normalized_chapter,
                    chapter_title=normalized_title,
                    body=str(draft.body_text or ""),
                    binding=binding.model_dump(mode="json"),
                    publish=bool(publish),
                )
                jobs.append(job)
            session.commit()
            for job in jobs:
                session.refresh(job)
            return [self.upload_jobs.serialize_upload_job(job) for job in jobs]

    def materialize_event(self, parsed: CanonPublisherEventPayload) -> list[dict[str, Any]]:
        request = {
            "canon_commit_id": parsed.canon_commit_id,
            "canon_idempotency_key": parsed.canon_idempotency_key,
            "project_id": parsed.project_id,
            "chapter_number": parsed.chapter_number,
            "candidate_id": parsed.candidate_id,
            "body_sha256": parsed.body_sha256,
        }
        with self.session_factory() as session:
            commit = session.get(CanonCommitRecord, parsed.canon_commit_id)
            if commit is not None and commit.production_mode in {"factory_batch", "soak_test"}:
                # Offline chapter recovery still has a canonical publisher event.
                # Consume it successfully after validating identity; emit no jobs.
                self._load_canon_rows(session, **request)
                if parsed.chapter_title != commit.chapter_title:
                    raise ValueError("Canon publisher accepted title mismatch")
                return []
        return self.materialize(
            **request, chapter_title=parsed.chapter_title,
            bindings=[binding.model_dump(mode="json") for binding in parsed.publisher_bindings],
            publish=parsed.publish,
        )

    def release(
        self,
        *,
        project_id: str,
        job_ids: Sequence[str],
        publish: bool,
        actor_type: str,
        session=None,
    ) -> list[dict[str, Any]]:
        return self.upload_jobs.release_scheduled_canon_jobs(
            project_id=project_id,
            job_ids=list(job_ids),
            publish=publish,
            actor_type=actor_type,
            session=session,
        )

    def find(
        self,
        *,
        canon_idempotency_key: str,
        project_id: str,
        chapter_number: int,
        candidate_id: str,
        platform: str,
    ) -> dict[str, Any] | None:
        identity = publisher_job_idempotency_key(
            canon_idempotency_key=canon_idempotency_key,
            project_id=project_id,
            chapter_number=chapter_number,
            candidate_id=candidate_id,
            platform_id=platform,
        )
        with self.session_factory() as session:
            from forwin.models.publisher import PublisherUploadJob

            job = session.execute(
                select(PublisherUploadJob).where(
                    PublisherUploadJob.idempotency_key == identity
                )
            ).scalar_one_or_none()
            if job is None:
                return None
            if (
                job.project_id != str(project_id or "").strip()
                or job.candidate_id != str(candidate_id or "").strip()
                or int(job.chapter_number or 0) != int(chapter_number or 0)
                or job.platform_id != str(platform or "").strip()
                or not job.canon_commit_id
            ):
                raise ValueError("stored Canon publisher identity mismatch")
            return self.upload_jobs.serialize_upload_job(job)

    @staticmethod
    def _bindings(
        bindings: Sequence[Mapping[str, Any] | CanonPublisherBindingSnapshot],
    ) -> list[CanonPublisherBindingSnapshot]:
        normalized: list[CanonPublisherBindingSnapshot] = []
        seen_platforms: set[str] = set()
        for item in bindings:
            binding = (
                item
                if isinstance(item, CanonPublisherBindingSnapshot)
                else CanonPublisherBindingSnapshot.model_validate(item)
            )
            if binding.platform in seen_platforms:
                raise ValueError(
                    f"duplicate publisher platform in Canon snapshot: {binding.platform}"
                )
            seen_platforms.add(binding.platform)
            normalized.append(binding)
        return normalized

    @staticmethod
    def _load_canon_rows(
        session,
        *,
        canon_commit_id: str,
        canon_idempotency_key: str,
        project_id: str,
        chapter_number: int,
        candidate_id: str,
        body_sha256: str,
    ) -> tuple[Project, CanonCommitRecord, CandidateDraftRecord, ChapterDraft]:
        if not canon_commit_id or not canon_idempotency_key:
            raise ValueError("Canon publisher identity is incomplete")
        if not project_id or not candidate_id or chapter_number <= 0:
            raise ValueError("Canon publisher chapter identity is incomplete")
        lock_project_chapters(session, project_id)
        project = session.get(Project, project_id)
        commit = session.get(CanonCommitRecord, canon_commit_id)
        candidate = session.get(CandidateDraftRecord, candidate_id)
        if project is None:
            raise ValueError(f"project not found: {project_id}")
        if (
            commit is None
            or commit.status != "committed"
            or commit.idempotency_key != canon_idempotency_key
            or commit.project_id != project_id
            or commit.candidate_id != candidate_id
            or int(commit.chapter_number or 0) != chapter_number
        ):
            raise ValueError("Canon commit identity mismatch")
        if (
            candidate is None
            or candidate.status != "accepted"
            or candidate.project_id != project_id
            or int(candidate.chapter_number or 0) != chapter_number
        ):
            raise ValueError("accepted candidate identity mismatch")
        chapter = session.get(ChapterPlan, candidate.chapter_plan_id)
        draft = session.get(ChapterDraft, candidate.candidate_draft_id)
        if (
            chapter is None
            or chapter.status != "accepted"
            or chapter.active_commit_id != commit.id
            or commit.chapter_plan_id != chapter.id
            or chapter.project_id != project_id
            or int(chapter.chapter_number or 0) != chapter_number
        ):
            raise ValueError("accepted chapter identity mismatch")
        if draft is None or draft.chapter_plan_id != chapter.id:
            raise ValueError("accepted candidate draft not found")
        if candidate_body_hash(draft.body_text) != candidate.body_hash:
            raise ValueError("accepted candidate body hash mismatch")
        if not body_sha256 or body_sha256 != candidate.body_hash:
            raise ValueError("Canon publisher body hash mismatch")
        return project, commit, candidate, draft


def build_canon_publisher_outbox_handlers(
    *,
    service_provider: Callable[[], CanonPublisherJobService],
) -> dict[str, Callable[[OutboxClaim], None]]:
    def handle(event: OutboxClaim) -> None:
        parsed = parse_canon_event_envelope(
            event_type=event.event_type,
            event_id=event.event_id,
            aggregate_type=event.aggregate_type,
            aggregate_id=event.aggregate_id,
            payload=event.payload,
        )
        if not isinstance(parsed, CanonPublisherEventPayload):
            raise TypeError("Canon publisher event payload has the wrong type")
        service_provider().materialize_event(parsed)

    return {CANON_PUBLISHER_REQUESTED: handle}


__all__ = [
    "CanonPublisherJobService",
    "build_canon_publisher_outbox_handlers",
]
