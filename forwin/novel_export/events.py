"""Durable post-Canon export requests using the existing outbox lease owner."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from forwin.models.outbox import OutboxEvent
from forwin.outbox.store import enqueue_outbox_event

from .exporter import write_export
from .snapshot import BookExport, capture_snapshot, lock_export_project, render_snapshot

NOVEL_EXPORT_REQUESTED = "novel.export.requested"


class ExportRequest(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    schema_version: Literal[1] = 1
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]+$")
    book_revision: int = Field(ge=1)
    display_title: str
    snapshot: BookExport | None = None


def enqueue_book_export(session, project) -> None:
    # Caller already owns the Canon transaction and Project lock. No history
    # traversal, publication action, filesystem write or service initialization.
    request = ExportRequest(
        project_id=project.id,
        book_revision=project.book_revision,
        display_title=project.title,
    )
    enqueue_outbox_event(
        session,
        aggregate_type="project",
        aggregate_id=project.id,
        event_type=NOVEL_EXPORT_REQUESTED,
        event_id=f"novel-export:{project.id}:{project.book_revision}",
        payload=request.model_dump(mode="json"),
    )


def export_event(session_factory, root: Path, claim) -> None:
    # Discover only scope before locking. Lock order stays Project -> Outbox.
    request = ExportRequest.model_validate(dict(claim.payload))
    if (
        claim.event_type != NOVEL_EXPORT_REQUESTED
        or claim.aggregate_type != "project"
        or claim.aggregate_id != request.project_id
    ):
        raise ValueError("export claim identity mismatch")
    with session_factory.begin() as session:
        lock_export_project(session, request.project_id)
        row = session.scalar(
            select(OutboxEvent)
            .where(OutboxEvent.id == claim.row_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        now = datetime.now(UTC)
        expires = row.lease_expires_at if row else None
        if expires is not None and expires.tzinfo is None:
            expires = expires.replace(tzinfo=UTC)
        if (
            row is None
            or row.status != "running"
            or row.worker_id != claim.worker_id
            or row.lease_epoch != claim.lease_epoch
            or expires is None
            or expires <= now
            or row.event_id != claim.event_id
            or row.event_type != claim.event_type
            or row.aggregate_type != claim.aggregate_type
            or row.aggregate_id != claim.aggregate_id
        ):
            raise ValueError("export claim lease is stale")
        persisted = ExportRequest.model_validate_json(row.payload_json)
        if (
            persisted.project_id != request.project_id
            or persisted.book_revision != request.book_revision
            or persisted.display_title != request.display_title
        ):
            raise ValueError("export persisted request identity mismatch")
        snapshot = persisted.snapshot
        if snapshot is None:
            snapshot = capture_snapshot(
                session,
                project_id=persisted.project_id,
                book_revision=persisted.book_revision,
                display_title=persisted.display_title,
            )
            row.payload_json = persisted.model_copy(
                update={"snapshot": snapshot}
            ).model_dump_json()
        if (
            snapshot.project_id != persisted.project_id
            or snapshot.book_revision != persisted.book_revision
            or snapshot.display_title != persisted.display_title
        ):
            raise ValueError("export frozen snapshot identity mismatch")
        book = render_snapshot(session, snapshot)
    # First freeze commits before any IO. A later lease loss can only replay the
    # same immutable files; the file owner also refuses pointer regression.
    write_export(root, snapshot, book)


def build_novel_export_handlers(*, session_factory, root: Path):
    return {
        NOVEL_EXPORT_REQUESTED: lambda claim: export_event(session_factory, root, claim)
    }


def rebuild_export(
    session_factory, root: Path, *, project_id: str, book_revision: int
) -> None:
    """Restore files from an already frozen request without changing queue truth."""
    with session_factory.begin() as session:
        lock_export_project(session, project_id)
        row = session.scalar(
            select(OutboxEvent).where(
                OutboxEvent.event_id == f"novel-export:{project_id}:{book_revision}",
                OutboxEvent.event_type == NOVEL_EXPORT_REQUESTED,
                OutboxEvent.aggregate_type == "project",
                OutboxEvent.aggregate_id == project_id,
            )
        )
        if row is None:
            raise ValueError("export has no retained frozen request")
        request = ExportRequest.model_validate_json(row.payload_json)
        snapshot = request.snapshot
        if (
            snapshot is None
            or request.project_id != project_id
            or request.book_revision != book_revision
            or snapshot.project_id != project_id
            or snapshot.book_revision != book_revision
            or snapshot.display_title != request.display_title
        ):
            raise ValueError("export has no matching frozen snapshot")
        book = render_snapshot(session, snapshot)
    write_export(root, snapshot, book)
