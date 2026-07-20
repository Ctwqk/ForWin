from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Iterator

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forwin.models.canon import CanonCommitRecord
from forwin.models.project import Project
from forwin.models.projection import ProjectionCheckpoint


PROJECTION_COMPONENTS = ("obsidian", "llm_kb", "chapter_memory")
_PROJECTION_COMPONENT_SET = frozenset(PROJECTION_COMPONENTS)
_MAX_PROJECTION_ERROR_CHARS = 1000
_SECRET_ASSIGNMENT_RE = re.compile(
    r"(?i)[\"']?\b(api[_-]?key|authorization|password|secret|token)\b[\"']?"
    r"\s*[:=]\s*"
    r"(?:(?:bearer|basic)\s+[^\s,;]+|\"[^\"]*\"|'[^']*'|[^\s,;]+)"
)


@dataclass(frozen=True, slots=True)
class ProjectionTarget:
    project_id: str
    chapter_number: int
    canon_commit_id: str | None = None
    candidate_id: str = ""
    canon_idempotency_key: str = ""


@dataclass(frozen=True, slots=True)
class ProjectionEventIdentity:
    canon_commit_id: str
    canon_idempotency_key: str
    project_id: str
    chapter_number: int
    candidate_id: str


@dataclass(frozen=True, slots=True)
class ProjectionRunTicket:
    project_id: str
    projection_kind: str
    target_canon_commit_id: str | None
    target_chapter_number: int
    event_id: str
    started_at: datetime


def validate_projection_component(projection_kind: str) -> str:
    normalized = str(projection_kind or "").strip().lower()
    if normalized not in _PROJECTION_COMPONENT_SET:
        raise ValueError(
            "projection component must be obsidian, llm_kb, or chapter_memory"
        )
    return normalized


def sanitize_projection_error(error: BaseException | str) -> str:
    if isinstance(error, BaseException):
        prefix = error.__class__.__name__
        message = str(error)
    else:
        prefix = "ProjectionError"
        message = str(error)
    message = " ".join(message.split())
    message = _SECRET_ASSIGNMENT_RE.sub(lambda match: f"{match.group(1)}=[REDACTED]", message)
    return f"{prefix}: {message}"[:_MAX_PROJECTION_ERROR_CHARS]


@contextmanager
def session_transaction(session_factory: Any) -> Iterator[Any]:
    begin = getattr(session_factory, "begin", None)
    if callable(begin):
        with begin() as session:
            yield session
        return
    with session_factory() as session:
        with session.begin():
            yield session


def checkpoint_for_update_statement(project_id: str, projection_kind: str):
    return (
        select(ProjectionCheckpoint)
        .where(
            ProjectionCheckpoint.project_id == project_id,
            ProjectionCheckpoint.projection_kind == projection_kind,
        )
        .with_for_update()
    )


def latest_projection_target(session: Any, project_id: str) -> ProjectionTarget:
    if session.get(Project, project_id) is None:
        raise ValueError("project not found")
    commit = session.execute(
        select(CanonCommitRecord)
        .where(
            CanonCommitRecord.project_id == project_id,
            CanonCommitRecord.status == "committed",
        )
        .order_by(
            CanonCommitRecord.chapter_number.desc(),
            CanonCommitRecord.created_at.desc(),
            CanonCommitRecord.id.desc(),
        )
        .limit(1)
    ).scalar_one_or_none()
    if commit is None:
        return ProjectionTarget(project_id=project_id, chapter_number=0)
    return ProjectionTarget(
        project_id=project_id,
        chapter_number=int(commit.chapter_number or 0),
        canon_commit_id=commit.id,
        candidate_id=commit.candidate_id,
        canon_idempotency_key=commit.idempotency_key,
    )


def validate_projection_event_identity(
    session: Any,
    identity: ProjectionEventIdentity,
) -> None:
    commit = session.get(CanonCommitRecord, identity.canon_commit_id)
    if commit is None or commit.status != "committed":
        raise ValueError("canon projection event references no committed Canon record")
    expected = {
        "canon_idempotency_key": str(commit.idempotency_key or ""),
        "project_id": str(commit.project_id or ""),
        "chapter_number": int(commit.chapter_number or 0),
        "candidate_id": str(commit.candidate_id or ""),
    }
    actual = {
        "canon_idempotency_key": identity.canon_idempotency_key,
        "project_id": identity.project_id,
        "chapter_number": int(identity.chapter_number),
        "candidate_id": identity.candidate_id,
    }
    mismatches = [key for key in expected if expected[key] != actual[key]]
    if mismatches:
        raise ValueError(
            "canon projection event identity mismatch: " + ", ".join(mismatches)
        )


class ProjectionCheckpointStore:
    def __init__(self, session_factory: Any) -> None:
        self.session_factory = session_factory

    def resolve_target(
        self,
        project_id: str,
        *,
        event_identity: ProjectionEventIdentity | None = None,
    ) -> ProjectionTarget:
        with session_transaction(self.session_factory) as session:
            if event_identity is not None:
                validate_projection_event_identity(session, event_identity)
            return latest_projection_target(session, project_id)

    def begin_component(
        self,
        target: ProjectionTarget,
        projection_kind: str,
        *,
        event_id: str = "",
        force: bool = False,
    ) -> ProjectionRunTicket | None:
        kind = validate_projection_component(projection_kind)
        for attempt in range(2):
            try:
                with session_transaction(self.session_factory) as session:
                    row = session.execute(
                        checkpoint_for_update_statement(target.project_id, kind)
                    ).scalar_one_or_none()
                    if row is None:
                        row = ProjectionCheckpoint(
                            project_id=target.project_id,
                            projection_kind=kind,
                        )
                        session.add(row)
                        session.flush()

                    current_target = int(row.target_chapter_number or 0)
                    if current_target > target.chapter_number:
                        return None
                    if current_target < target.chapter_number:
                        row.target_chapter_number = target.chapter_number
                        row.target_canon_commit_id = target.canon_commit_id
                    elif target.canon_commit_id is not None:
                        existing_commit_id = row.target_canon_commit_id
                        if existing_commit_id not in {None, target.canon_commit_id}:
                            raise RuntimeError(
                                "projection checkpoint target identity is inconsistent"
                            )
                        row.target_canon_commit_id = target.canon_commit_id

                    projected = int(row.projected_chapter_number or 0)
                    if (
                        not force
                        and row.status == "healthy"
                        and projected >= target.chapter_number
                    ):
                        return None

                    started_at = _next_started_at(row.started_at)
                    row.status = "running"
                    row.started_at = started_at
                    row.completed_at = None
                    return ProjectionRunTicket(
                        project_id=target.project_id,
                        projection_kind=kind,
                        target_canon_commit_id=target.canon_commit_id,
                        target_chapter_number=target.chapter_number,
                        event_id=str(event_id or ""),
                        started_at=started_at,
                    )
            except IntegrityError:
                if attempt:
                    raise
        raise RuntimeError("projection checkpoint creation retry exhausted")

    def complete_component(
        self,
        ticket: ProjectionRunTicket,
        *,
        source_digest: str = "",
    ) -> None:
        with session_transaction(self.session_factory) as session:
            row = session.execute(
                checkpoint_for_update_statement(
                    ticket.project_id,
                    ticket.projection_kind,
                )
            ).scalar_one()
            projected = int(row.projected_chapter_number or 0)
            if ticket.target_chapter_number > projected:
                row.projected_chapter_number = ticket.target_chapter_number
                row.projected_canon_commit_id = ticket.target_canon_commit_id

            if not _ticket_is_current(row, ticket):
                return
            row.status = "healthy"
            row.projected_chapter_number = max(
                int(row.projected_chapter_number or 0),
                ticket.target_chapter_number,
            )
            if row.projected_chapter_number == ticket.target_chapter_number:
                row.projected_canon_commit_id = ticket.target_canon_commit_id
            row.last_event_id = ticket.event_id
            row.source_digest = str(source_digest or "")
            row.last_error = ""
            row.completed_at = _utcnow()

    def fail_component(
        self,
        ticket: ProjectionRunTicket,
        error: BaseException | str,
    ) -> None:
        with session_transaction(self.session_factory) as session:
            row = session.execute(
                checkpoint_for_update_statement(
                    ticket.project_id,
                    ticket.projection_kind,
                )
            ).scalar_one()
            if not _ticket_is_current(row, ticket):
                return
            if int(row.projected_chapter_number or 0) > ticket.target_chapter_number:
                return
            row.status = "degraded"
            row.last_event_id = ticket.event_id
            row.last_error = sanitize_projection_error(error)
            row.completed_at = _utcnow()

    def status(self, project_id: str) -> dict[str, Any]:
        with session_transaction(self.session_factory) as session:
            target = latest_projection_target(session, project_id)
            rows = {
                row.projection_kind: row
                for row in session.execute(
                    select(ProjectionCheckpoint).where(
                        ProjectionCheckpoint.project_id == project_id,
                        ProjectionCheckpoint.projection_kind.in_(
                            PROJECTION_COMPONENTS
                        ),
                    )
                )
                .scalars()
                .all()
            }

            components = []
            for kind in PROJECTION_COMPONENTS:
                row = rows.get(kind)
                projected_chapter = int(
                    getattr(row, "projected_chapter_number", 0) or 0
                )
                lag = max(target.chapter_number - projected_chapter, 0)
                status = str(getattr(row, "status", "never") or "never")
                if row is not None and lag > 0 and status == "healthy":
                    status = "degraded"
                component_target_chapter = max(
                    target.chapter_number,
                    int(getattr(row, "target_chapter_number", 0) or 0),
                )
                component_target_commit = (
                    target.canon_commit_id
                    if component_target_chapter == target.chapter_number
                    else getattr(row, "target_canon_commit_id", None)
                )
                components.append(
                    {
                        "projection_kind": kind,
                        "status": status,
                        "healthy": status == "healthy" and lag == 0,
                        "target_canon_commit_id": component_target_commit,
                        "target_chapter_number": component_target_chapter,
                        "projected_canon_commit_id": getattr(
                            row, "projected_canon_commit_id", None
                        ),
                        "projected_chapter_number": projected_chapter,
                        "lag": lag,
                        "last_event_id": str(
                            getattr(row, "last_event_id", "") or ""
                        ),
                        "source_digest": str(
                            getattr(row, "source_digest", "") or ""
                        ),
                        "last_error": str(
                            getattr(row, "last_error", "") or ""
                        )[:_MAX_PROJECTION_ERROR_CHARS],
                        "started_at": getattr(row, "started_at", None),
                        "completed_at": getattr(row, "completed_at", None),
                        "updated_at": getattr(row, "updated_at", None),
                    }
                )

        statuses = {item["status"] for item in components}
        overall = next(
            (
                candidate
                for candidate in ("degraded", "running", "never", "healthy")
                if candidate in statuses
            ),
            "never",
        )
        return {
            "project_id": project_id,
            "status": overall,
            "healthy": overall == "healthy",
            "target_canon_commit_id": target.canon_commit_id,
            "target_chapter_number": target.chapter_number,
            "components": components,
        }


def _ticket_is_current(
    row: ProjectionCheckpoint,
    ticket: ProjectionRunTicket,
) -> bool:
    return (
        int(row.target_chapter_number or 0) == ticket.target_chapter_number
        and row.target_canon_commit_id == ticket.target_canon_commit_id
        and row.started_at == ticket.started_at
    )


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _next_started_at(previous: datetime | None) -> datetime:
    current = _utcnow()
    if previous is not None and current <= previous:
        return previous + timedelta(microseconds=1)
    return current


__all__ = [
    "PROJECTION_COMPONENTS",
    "ProjectionCheckpointStore",
    "ProjectionEventIdentity",
    "ProjectionRunTicket",
    "ProjectionTarget",
    "checkpoint_for_update_statement",
    "latest_projection_target",
    "sanitize_projection_error",
    "session_transaction",
    "validate_projection_component",
    "validate_projection_event_identity",
]
