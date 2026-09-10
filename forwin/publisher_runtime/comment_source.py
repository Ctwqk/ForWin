"""Resolve comment origins from explicit remote identities, never display titles."""

from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256

from sqlalchemy import select

from forwin.models.base import new_id
from forwin.models.canon import CanonPublicationProtection
from forwin.models.project import ChapterPlan, Project
from forwin.models.publisher import PublisherChapterBinding, PublisherWorkBinding


def body_hash(body: str) -> str:
    return sha256(body.encode("utf-8")).hexdigest()


def observed_time(value) -> datetime | None:
    try:
        value = datetime.fromisoformat(str(value))
        return value.astimezone(UTC).replace(tzinfo=None) if value.tzinfo else value
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class CommentSource:
    project_id: str
    source_scope: str
    account_id: str
    work_id: str
    chapter_id: str
    work_binding_id: str = ""
    source_status: str = "unknown"
    source_chapter_number: int | None = None
    source_chapter_plan_id: str = ""
    source_canon_commit_id: str = ""
    source_publication_id: str = ""


def resolve_comment_source(session, *, platform, item, job=None) -> CommentSource:
    if job is not None:
        if job.platform_id != platform:
            raise ValueError("comment sync job platform does not match observation")
        if (
            job.work_id
            and item.get("work_id")
            and job.work_id != str(item["work_id"]).strip()
        ):
            raise ValueError("comment sync job work does not match observation")
    project_id = str(
        getattr(job, "project_id", "") or item.get("project_id", "")
    ).strip()
    if project_id and session.get(Project, project_id) is None:
        project_id = ""
    account_id = str(item.get("account_id", "") or "").strip()
    work_id = str(item.get("work_id") or getattr(job, "work_id", "") or "").strip()
    chapter_id = str(
        item.get("chapter_id") or getattr(job, "chapter_id", "") or ""
    ).strip()
    # An observation with no account/binding scope or no work identity must not
    # collapse unrelated sources into one global empty-string identity.
    scope = f"account:{account_id}" if account_id and work_id else f"unknown:{new_id()}"
    binding = None
    if work_id:
        stmt = select(PublisherWorkBinding).where(
            PublisherWorkBinding.platform_id == platform,
            PublisherWorkBinding.remote_book_id == work_id,
        )
        if project_id:
            stmt = stmt.where(PublisherWorkBinding.project_id == project_id)
        bindings = session.scalars(stmt).all()
        if len(bindings) == 1:
            binding = bindings[0]
            project_id = binding.project_id
            if not account_id:
                scope = f"binding:{binding.id}"
    values = {
        "project_id": project_id,
        "source_scope": scope,
        "account_id": account_id,
        "work_id": work_id,
        "chapter_id": chapter_id,
    }
    if binding is None:
        return CommentSource(**values)
    values["work_binding_id"] = binding.id
    if not chapter_id:
        return CommentSource(**values)
    chapters = session.scalars(
        select(PublisherChapterBinding).where(
            PublisherChapterBinding.work_binding_id == binding.id,
            PublisherChapterBinding.platform_id == platform,
            PublisherChapterBinding.project_id == project_id,
            PublisherChapterBinding.remote_chapter_id == chapter_id,
            PublisherChapterBinding.chapter_number > 0,
        )
    ).all()
    if len(chapters) != 1:
        return CommentSource(**values)
    number = chapters[0].chapter_number
    plans = session.scalars(
        select(ChapterPlan).where(
            ChapterPlan.project_id == project_id,
            ChapterPlan.chapter_number == number,
        )
    ).all()
    if len(plans) != 1:
        return CommentSource(**values)
    publications = session.scalars(
        select(CanonPublicationProtection).where(
            CanonPublicationProtection.project_id == project_id,
            CanonPublicationProtection.platform_id == platform,
            CanonPublicationProtection.remote_book_id == work_id,
            CanonPublicationProtection.remote_chapter_id == chapter_id,
            CanonPublicationProtection.state == "published",
        )
    ).all()
    if any(
        row.chapter_plan_id != plans[0].id or row.chapter_number != number
        for row in publications
    ):
        return CommentSource(**values)
    values.update(
        source_chapter_number=number,
        source_chapter_plan_id=plans[0].id,
        source_status="chapter_known",
    )
    if len({row.canon_commit_id for row in publications}) == 1:
        values.update(
            source_status="confirmed",
            source_canon_commit_id=publications[0].canon_commit_id,
            source_publication_id=publications[0].id,
        )
    return CommentSource(**values)


SOURCE_FIELDS = (
    "project_id",
    "platform_id",
    "source_scope",
    "account_id",
    "work_id",
    "chapter_id",
    "remote_comment_id",
    "work_binding_id",
    "source_status",
    "source_chapter_number",
    "source_chapter_plan_id",
    "source_canon_commit_id",
    "source_publication_id",
    "author_id",
)


def source_identity(comment):
    return {field: getattr(comment, field) for field in SOURCE_FIELDS}


def source_hash(comment):
    return body_hash(
        json.dumps(
            source_identity(comment),
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
        )
    )
