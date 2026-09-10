from __future__ import annotations

import json

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.models.audit import DecisionEvent
from forwin.models.canon import CanonCommitRecord
from forwin.models.project import ChapterPlan, Project

from .plan import CanonCommitPlan


class HistoricalRewriteInvalid(ValueError):
    """A revision request cannot authorize an unvalidated mainline replacement."""


def _payload(event: DecisionEvent) -> dict:
    try:
        value = json.loads(event.payload_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class HistoricalCanonRewriteRepository:
    """Pending revisions retain the accepted chapter and immutable Canon evidence."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def committed_records(
        self, project_id: str, chapter_number: int
    ) -> list[CanonCommitRecord]:
        return list(
            self.session.scalars(
                select(CanonCommitRecord)
                .join(ChapterPlan, ChapterPlan.active_commit_id == CanonCommitRecord.id)
                .where(
                    ChapterPlan.project_id == project_id,
                    ChapterPlan.chapter_number == chapter_number,
                )
            )
        )

    def mark_pending(
        self, *, project_id: str, chapter_number: int, reason: str
    ) -> DecisionEvent:
        project = self.session.scalar(
            select(Project).where(Project.id == project_id).with_for_update()
        )
        chapter = self.session.scalar(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == chapter_number,
            )
            .with_for_update()
        )
        if project is None or chapter is None or not chapter.active_commit_id:
            raise HistoricalRewriteInvalid(
                "historical rewrite active identity missing or invalid"
            )
        # Publication guards use the same Project -> Chapter serialization boundary.
        from forwin.publisher_runtime.protection import require_revision_unprotected

        try:
            require_revision_unprotected(
                self.session, project_id=project_id, from_chapter=chapter_number
            )
        except ValueError as exc:
            raise HistoricalRewriteInvalid(str(exc)) from exc
        for event in self.session.scalars(
            select(DecisionEvent).where(
                DecisionEvent.project_id == project_id,
                DecisionEvent.chapter_number == chapter_number,
                DecisionEvent.event_type == DecisionEventType.RETRY_ATTEMPT,
                DecisionEvent.actor_type == "api",
            )
        ):
            payload = _payload(event)
            if payload.get(
                "previous_commit_id"
            ) == chapter.active_commit_id and not payload.get("replacement_commit_id"):
                return event
        event = DecisionEvent(
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.RETRY_ATTEMPT,
            actor_type="api",
            scope="chapter",
            reason=reason,
            summary=f"第{chapter_number}章修订请求已记录；原接纳版本继续生效。",
            payload_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "chapter_plan_id": chapter.id,
                    "previous_status": "accepted",
                    "previous_commit_id": chapter.active_commit_id,
                    "base_book_revision": project.book_revision,
                    "revision_status": "pending_validation",
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            related_object_type="chapter",
            related_object_id=chapter.id,
        )
        self.session.add(event)
        self.session.flush()
        return event


class HistoricalCanonRewriteService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def prepare_replacement(
        self, plan: CanonCommitPlan, *, persist_marker_backfill: bool = True
    ):
        # Replaying old deltas is not semantic validation of successor bodies.
        # Never invalidate snapshots or mutate original evidence to make a retry pass.
        if self.session.scalar(
            select(CanonCommitRecord.id)
            .where(
                CanonCommitRecord.project_id == plan.project_id,
                CanonCommitRecord.chapter_number == plan.chapter_number,
            )
            .limit(1)
        ):
            raise HistoricalRewriteInvalid(
                "chapter already accepted; historical rewrite requires reliable full-suffix validation"
            )
