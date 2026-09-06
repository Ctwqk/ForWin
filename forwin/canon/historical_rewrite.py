from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.repository import BookStateRepository
from forwin.models.audit import DecisionEvent
from forwin.models.canon import CanonCommitRecord
from forwin.models.project import ChapterPlan

from .plan import CanonCommitPlan


class HistoricalRewriteInvalid(ValueError):
    """The retry audit trail does not authorize replacement of this commit."""


def _payload(event: DecisionEvent) -> dict:
    try:
        value = json.loads(event.payload_json or "{}")
    except (TypeError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


class HistoricalCanonRewriteRepository:
    """Historical rewrite authorization lives in the existing API retry event."""

    def __init__(self, session: Session) -> None:
        self.session = session

    def committed_records(
        self, project_id: str, chapter_number: int
    ) -> list[CanonCommitRecord]:
        return list(
            self.session.scalars(
                select(CanonCommitRecord)
                .where(
                    CanonCommitRecord.project_id == project_id,
                    CanonCommitRecord.chapter_number == chapter_number,
                    CanonCommitRecord.status == "committed",
                )
                .with_for_update()
            )
        )

    def mark_pending(
        self, *, project_id: str, chapter_number: int, reason: str
    ) -> DecisionEvent:
        records = self.committed_records(project_id, chapter_number)
        if len(records) != 1:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        event = DecisionEvent(
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.RETRY_ATTEMPT,
            actor_type="api",
            scope="chapter",
            summary=f"第{chapter_number}章 review 候选已重置为 planned，等待重写。",
            reason=reason,
            payload_json=json.dumps(
                {
                    "chapter_number": chapter_number,
                    "previous_status": "accepted",
                    "previous_commit_id": records[0].id,
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            related_object_type="chapter",
        )
        self.session.add(event)
        self.session.flush()
        return event

    def pending_marker(self, previous: CanonCommitRecord) -> DecisionEvent:
        events = list(
            self.session.scalars(
                select(DecisionEvent)
                .where(
                    DecisionEvent.project_id == previous.project_id,
                    DecisionEvent.chapter_number == previous.chapter_number,
                    DecisionEvent.event_family == "audit_action",
                    DecisionEvent.event_type == DecisionEventType.RETRY_ATTEMPT,
                    DecisionEvent.actor_type == "api",
                )
                .with_for_update()
            )
        )
        pending = [
            event
            for event in events
            if _payload(event).get("previous_status") == "accepted"
            and not _payload(event).get("replacement_commit_id")
        ]
        if len(pending) != 1:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        event = pending[0]
        payload = _payload(event)
        if payload.get("chapter_number") != previous.chapter_number:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        if "previous_commit_id" not in payload:
            # Legacy retries predate the durable marker. A single accepted API
            # retry plus exactly one committed record is the only backfill path.
            if (
                len(
                    [
                        item
                        for item in events
                        if _payload(item).get("previous_status") == "accepted"
                    ]
                )
                != 1
            ):
                raise HistoricalRewriteInvalid(
                    "historical rewrite marker missing or invalid"
                )
            records = self.committed_records(
                previous.project_id, previous.chapter_number
            )
            if len(records) != 1 or records[0].id != previous.id:
                raise HistoricalRewriteInvalid(
                    "historical rewrite marker missing or invalid"
                )
            payload["previous_commit_id"] = previous.id
            event.payload_json = json.dumps(payload, ensure_ascii=False, sort_keys=True)
            self.session.flush()
        if payload.get("previous_commit_id") != previous.id:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        return event


@dataclass
class HistoricalCanonReplacement:
    session: Session
    plan: CanonCommitPlan
    previous: CanonCommitRecord
    marker: DecisionEvent
    through_chapter: int
    retired_delta_ids: list[str] = field(default_factory=list)
    replayed_chapters: list[int] = field(default_factory=list)

    def retire_old_contribution(self) -> None:
        repo = BookStateRepository(self.session)
        self.retired_delta_ids = [
            delta.id
            for delta in repo.list_graph_deltas(
                self.plan.project_id,
                after_chapter=self.plan.chapter_number - 1,
                through_chapter=self.plan.chapter_number,
            )
        ]
        result = json.loads(self.previous.result_json or "{}")
        result["retired_graph_delta_ids"] = self.retired_delta_ids
        self.previous.result_json = json.dumps(
            result, ensure_ascii=False, sort_keys=True
        )
        self.session.flush()
        repo.invalidate_project_range(
            self.plan.project_id,
            from_chapter=self.plan.chapter_number,
            through_chapter=self.through_chapter,
        )
        repo.retire_chapter_deltas(
            self.plan.project_id,
            self.plan.chapter_number,
        )

    def rebuild_successor_projections(self) -> None:
        self.replayed_chapters = BookStateRepository(
            self.session
        ).rebuild_project_range(
            self.plan.project_id,
            from_chapter=self.plan.chapter_number + 1,
            through_chapter=self.through_chapter,
        )

    def mark_prior_commit_superseded(self) -> None:
        # Project locking in admission serializes allocation. Keep the original
        # chapter in the audit result while freeing the existing unique key.
        minimum = self.session.scalar(
            select(func.min(CanonCommitRecord.chapter_number)).where(
                CanonCommitRecord.project_id == self.plan.project_id,
            )
        )
        archive_chapter = min(int(minimum or 0), 0) - 1
        result = json.loads(self.previous.result_json or "{}")
        result.update(
            {
                "original_chapter_number": self.previous.chapter_number,
                "superseded_by_commit_id": self.plan.canon_commit_id,
                "replayed_chapters": self.replayed_chapters,
            }
        )
        self.previous.result_json = json.dumps(
            result, ensure_ascii=False, sort_keys=True
        )
        self.previous.chapter_number = archive_chapter
        self.previous.status = "superseded"
        payload = _payload(self.marker)
        payload["replacement_commit_id"] = self.plan.canon_commit_id
        self.marker.payload_json = json.dumps(
            payload, ensure_ascii=False, sort_keys=True
        )
        self.session.flush()


class HistoricalCanonRewriteService:
    def __init__(self, session: Session) -> None:
        self.session = session

    def prepare_replacement(
        self, plan: CanonCommitPlan
    ) -> HistoricalCanonReplacement | None:
        records = HistoricalCanonRewriteRepository(self.session).committed_records(
            plan.project_id,
            plan.chapter_number,
        )
        if not records:
            return None
        if len(records) != 1 or records[0].candidate_id == plan.candidate_id:
            raise HistoricalRewriteInvalid(
                "historical rewrite marker missing or invalid"
            )
        previous = records[0]
        marker = HistoricalCanonRewriteRepository(self.session).pending_marker(previous)
        through_chapter = BookStateRepository(self.session).latest_available_chapter(
            plan.project_id
        )
        successors = list(
            self.session.execute(
                select(CanonCommitRecord, ChapterPlan.status)
                .join(
                    ChapterPlan,
                    (ChapterPlan.project_id == CanonCommitRecord.project_id)
                    & (ChapterPlan.chapter_number == CanonCommitRecord.chapter_number),
                )
                .where(
                    CanonCommitRecord.project_id == plan.project_id,
                    CanonCommitRecord.chapter_number > plan.chapter_number,
                    CanonCommitRecord.status == "committed",
                )
            )
        )
        if any(status != "accepted" for _, status in successors):
            raise HistoricalRewriteInvalid(
                "historical rewrite has a nonaccepted successor"
            )
        return HistoricalCanonReplacement(
            self.session, plan, previous, marker, through_chapter
        )
