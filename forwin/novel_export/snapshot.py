"""Freeze only retained accepted content and narrowly scoped identity metadata."""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forwin.models.canon import CanonCommitRecord, CanonPublicationProtection
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan, Project
from forwin.models.publisher import PublisherUploadReceipt


class _Frozen(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class PublishedReference(_Frozen):
    publication_id: str
    platform_id: str
    remote_book_id: str
    remote_chapter_id: str
    receipt_ids: tuple[str, ...] = ()
    receipt_status: Literal["retained", "unavailable"]


class FrozenPlan(_Frozen):
    status: Literal["known", "unknown"] = "unknown"
    policy_version: int | None = None
    acceptance_mode: str | None = None
    expected_book_revision: int | None = None


class ChapterExport(_Frozen):
    chapter_plan_id: str
    chapter_number: int = Field(ge=1)
    canon_commit_id: str
    candidate_id: str
    draft_id: str
    acceptance_revision: int = Field(ge=1)
    base_book_revision: int = Field(ge=0)
    chapter_title: str
    body_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")
    plan_revision: str | None
    plan_revision_semantics: Literal["candidate_original_plan"] = (
        "candidate_original_plan"
    )
    frozen_plan: FrozenPlan
    published_receipts: tuple[PublishedReference, ...] = ()


class BookExport(_Frozen):
    schema_version: Literal[1] = 1
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    book_revision: int = Field(ge=1)
    base_book_revision: int = Field(ge=0)
    display_title: str
    captured_at: str
    publication_reference_semantics: Literal["observed_at_capture"] = (
        "observed_at_capture"
    )
    chapters: tuple[ChapterExport, ...]
    book_sha256: str = Field(pattern=r"^[a-f0-9]{64}$")


def digest(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def lock_export_project(session, project_id: str) -> Project:
    project = session.scalar(
        select(Project)
        .where(Project.id == project_id)
        .with_for_update(read=True)
        .execution_options(populate_existing=True)
    )
    if project is None:
        raise ValueError("export project is missing")
    return project


def _content(session, commit: CanonCommitRecord):
    candidate = session.get(
        CandidateDraftRecord, commit.candidate_id, populate_existing=True
    )
    draft = (
        session.get(ChapterDraft, candidate.candidate_draft_id, populate_existing=True)
        if candidate
        else None
    )
    if (
        candidate is None
        or draft is None
        or candidate.project_id != commit.project_id
        or candidate.chapter_plan_id != commit.chapter_plan_id
        or candidate.chapter_number != commit.chapter_number
        or draft.chapter_plan_id != commit.chapter_plan_id
        or not candidate.body_hash
        or digest(draft.body_text) != candidate.body_hash
    ):
        raise ValueError("export body identity is unproven")
    try:
        stored_plan = json.loads(candidate.canon_commit_plan_json or "{}")
    except (TypeError, ValueError):
        stored_plan = None
    if isinstance(stored_plan, dict):
        accepted_hash = stored_plan.get("candidate_body_hash")
        if accepted_hash and accepted_hash != candidate.body_hash:
            raise ValueError("export body hash contradicts the retained accepted plan")
    return candidate, draft


def _plan(candidate, commit) -> FrozenPlan:
    # Current ChapterPlan may have changed since acceptance. Never fill gaps
    # with its mutable goals, context, or current policy.
    try:
        raw = json.loads(candidate.canon_commit_plan_json or "{}")
    except (TypeError, ValueError):
        return FrozenPlan()
    if not isinstance(raw, dict) or (
        raw.get("canon_commit_id") != commit.id
        or raw.get("candidate_id") != candidate.id
        or raw.get("candidate_body_hash") != candidate.body_hash
        or raw.get("plan_revision") != candidate.plan_revision
    ):
        return FrozenPlan()
    return FrozenPlan(
        status="known",
        policy_version=raw.get("policy_version"),
        acceptance_mode=raw.get("acceptance_mode"),
        expected_book_revision=raw.get("expected_book_revision"),
    )


def _published(session, commit, body_hash):
    refs = []
    for protection in session.scalars(
        select(CanonPublicationProtection)
        .where(
            CanonPublicationProtection.canon_commit_id == commit.id,
            CanonPublicationProtection.state == "published",
        )
        .order_by(CanonPublicationProtection.id)
        .execution_options(populate_existing=True)
    ):
        if (
            protection.project_id != commit.project_id
            or protection.chapter_plan_id != commit.chapter_plan_id
            or protection.chapter_number != commit.chapter_number
            or protection.content_sha256 != body_hash
            or not protection.platform_id
            or not protection.remote_book_id
            or not protection.remote_chapter_id
        ):
            raise ValueError("export publication identity is unproven")
        receipt_ids = tuple(
            session.scalars(
                select(PublisherUploadReceipt.id)
                .where(
                    PublisherUploadReceipt.upload_job_id == protection.upload_job_id,
                    PublisherUploadReceipt.platform_id == protection.platform_id,
                    PublisherUploadReceipt.remote_book_id == protection.remote_book_id,
                    PublisherUploadReceipt.remote_chapter_id
                    == protection.remote_chapter_id,
                    PublisherUploadReceipt.content_sha256 == body_hash,
                    PublisherUploadReceipt.official_state == "published",
                )
                .order_by(PublisherUploadReceipt.id)
            )
        )
        refs.append(
            PublishedReference(
                publication_id=protection.id,
                platform_id=protection.platform_id,
                remote_book_id=protection.remote_book_id,
                remote_chapter_id=protection.remote_chapter_id,
                receipt_ids=receipt_ids,
                receipt_status="retained" if receipt_ids else "unavailable",
            )
        )
    return tuple(refs)


def capture_snapshot(
    session, *, project_id: str, book_revision: int, display_title: str
) -> BookExport:
    project = lock_export_project(session, project_id)
    if not 1 <= book_revision <= project.book_revision:
        raise ValueError("export book revision is outside retained history")
    # Every acceptance in a historical suffix shares base R-1. Selecting each
    # stable chapter at R includes the entire atomic set; world edits leave the
    # selected accepted bodies unchanged without inventing new acceptances.
    selected, previous_by_chapter = {}, {}
    for commit in session.scalars(
        select(CanonCommitRecord)
        .where(
            CanonCommitRecord.project_id == project_id,
            CanonCommitRecord.status == "committed",
        )
        .order_by(CanonCommitRecord.acceptance_revision)
        .execution_options(populate_existing=True)
    ):
        previous = previous_by_chapter.get(commit.chapter_plan_id)
        if (
            commit.acceptance_revision
            != (previous.acceptance_revision + 1 if previous else 1)
            or commit.base_book_revision < 0
            or commit.base_book_revision >= project.book_revision
            or (
                previous
                and (
                    commit.base_book_revision <= previous.base_book_revision
                    or commit.chapter_number != previous.chapter_number
                )
            )
        ):
            raise ValueError("export acceptance revision history is ambiguous")
        previous_by_chapter[commit.chapter_plan_id] = commit
        if commit.base_book_revision < book_revision:
            selected[commit.chapter_plan_id] = commit
    ordered = sorted(selected.values(), key=lambda row: row.chapter_number)
    if [row.chapter_number for row in ordered] != list(range(1, len(ordered) + 1)):
        raise ValueError("export accepted chapter sequence is incomplete")
    if book_revision == project.book_revision:
        active = dict(
            session.execute(
                select(ChapterPlan.id, ChapterPlan.active_commit_id).where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.status == "accepted",
                )
            ).all()
        )
        if active != {row.chapter_plan_id: row.id for row in ordered}:
            raise ValueError("export current active identity is incomplete")
    chapters, bodies = [], []
    for commit in ordered:
        candidate, draft = _content(session, commit)
        chapters.append(
            ChapterExport(
                chapter_plan_id=commit.chapter_plan_id,
                chapter_number=commit.chapter_number,
                canon_commit_id=commit.id,
                candidate_id=candidate.id,
                draft_id=draft.id,
                acceptance_revision=commit.acceptance_revision,
                base_book_revision=commit.base_book_revision,
                chapter_title=commit.chapter_title,
                body_sha256=candidate.body_hash,
                plan_revision=candidate.plan_revision or None,
                frozen_plan=_plan(candidate, commit),
                published_receipts=_published(session, commit, candidate.body_hash),
            )
        )
        bodies.append(draft.body_text)
    return BookExport(
        project_id=project_id,
        book_revision=book_revision,
        base_book_revision=book_revision - 1,
        display_title=display_title,
        captured_at=datetime.now(UTC).isoformat(),
        chapters=tuple(chapters),
        book_sha256=digest(_markdown(display_title, chapters, bodies)),
    )


def _markdown(title, chapters, bodies):
    # Preserve accepted body bytes, including their whitespace. Identity is in
    # the manifest, not invisible metadata or prompt contents in Markdown.
    parts = ["# " + " ".join(title.splitlines()) + "\n\n"]
    for chapter, body in zip(chapters, bodies, strict=True):
        heading = " ".join(chapter.chapter_title.splitlines())
        parts.append(f"## {chapter.chapter_number}. {heading}\n\n{body}\n\n")
    return "".join(parts)


def render_snapshot(session, snapshot: BookExport) -> str:
    bodies = []
    for chapter in snapshot.chapters:
        commit = session.get(
            CanonCommitRecord, chapter.canon_commit_id, populate_existing=True
        )
        if (
            commit is None
            or commit.status != "committed"
            or commit.project_id != snapshot.project_id
            or commit.chapter_plan_id != chapter.chapter_plan_id
            or commit.candidate_id != chapter.candidate_id
            or commit.chapter_number != chapter.chapter_number
            or commit.chapter_title != chapter.chapter_title
            or commit.acceptance_revision != chapter.acceptance_revision
            or commit.base_book_revision != chapter.base_book_revision
            or commit.base_book_revision >= snapshot.book_revision
        ):
            raise ValueError(
                "export frozen body identity does not match retained Canon"
            )
        candidate, draft = _content(session, commit)
        if draft.id != chapter.draft_id or candidate.body_hash != chapter.body_sha256:
            raise ValueError(
                "export frozen body identity does not match retained draft"
            )
        bodies.append(draft.body_text)
    text = _markdown(snapshot.display_title, snapshot.chapters, bodies)
    if digest(text) != snapshot.book_sha256:
        raise ValueError("export book body identity digest mismatch")
    return text
