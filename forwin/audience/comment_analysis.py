"""Durable consumption state for the existing comment analyzer."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, func, or_, select

from forwin.models.publisher import (
    CommentAnalysisRecord,
    CommentSignalCandidate,
    PublisherRawComment,
)
from forwin.publisher_runtime.comment_source import (
    body_hash,
    source_hash,
    source_identity,
)


def now_utc():
    return datetime.now(UTC).replace(tzinfo=None)


class CommentAnalysisStore:
    def __init__(self, *, analyzer_version, max_attempts=3, retry_delay_seconds=60):
        if not analyzer_version or max_attempts < 1 or retry_delay_seconds < 0:
            raise ValueError(
                "comment analyzer version and bounded retry policy required"
            )
        self.version = analyzer_version
        self.max_attempts = max_attempts
        self.retry_delay = retry_delay_seconds

    def pending(self, session, *, project_id, limit):
        terminal_or_waiting = exists().where(
            CommentAnalysisRecord.source_comment_id == PublisherRawComment.id,
            CommentAnalysisRecord.content_sha256 == PublisherRawComment.content_sha256,
            CommentAnalysisRecord.source_sha256 == PublisherRawComment.source_sha256,
            CommentAnalysisRecord.analyzer_version == self.version,
            or_(
                (CommentAnalysisRecord.status == "completed")
                & (PublisherRawComment.active_analysis_id == CommentAnalysisRecord.id),
                (CommentAnalysisRecord.status != "completed")
                & or_(
                    CommentAnalysisRecord.attempt_count >= self.max_attempts,
                    CommentAnalysisRecord.next_retry_at > now_utc(),
                ),
            ),
        )
        return session.scalars(
            select(PublisherRawComment)
            .where(
                PublisherRawComment.project_id == project_id,
                ~terminal_or_waiting,
            )
            .order_by(
                PublisherRawComment.ingested_at.asc(), PublisherRawComment.id.asc()
            )
            .limit(max(0, limit))
            .with_for_update(skip_locked=True)
        ).all()

    def begin(self, session, *, project_id, comments, generation_chapter_number):
        records = []
        for comment_id in dict.fromkeys(row.id for row in comments):
            comment = session.scalar(
                select(PublisherRawComment)
                .where(
                    PublisherRawComment.id == comment_id,
                    PublisherRawComment.project_id == project_id,
                )
                .with_for_update(skip_locked=True)
                .execution_options(populate_existing=True)
            )
            if comment is None:
                continue
            comment.content_sha256 = body_hash(comment.body_text or "")
            comment.source_sha256 = source_hash(comment)
            record = session.scalar(
                select(CommentAnalysisRecord).where(
                    CommentAnalysisRecord.source_comment_id == comment.id,
                    CommentAnalysisRecord.content_sha256 == comment.content_sha256,
                    CommentAnalysisRecord.source_sha256 == comment.source_sha256,
                    CommentAnalysisRecord.analyzer_version == self.version,
                )
            )
            now = now_utc()
            if record is not None and record.status == "completed":
                # A reverted body/source can reuse its exact immutable evidence.
                comment.active_analysis_id = record.id
                continue
            if record is not None and (
                record.attempt_count >= self.max_attempts
                or (record.next_retry_at and record.next_retry_at > now)
            ):
                continue
            if record is None:
                identity = source_identity(comment)
                identity["remote_created_at"] = comment.remote_created_at
                identity.update(
                    observed_at=str(comment.observed_at or ""),
                    ingested_at=str(comment.ingested_at or ""),
                )
                record = CommentAnalysisRecord(
                    project_id=project_id,
                    source_comment_id=comment.id,
                    content_sha256=comment.content_sha256,
                    source_sha256=comment.source_sha256,
                    analyzer_version=self.version,
                    input_body=comment.body_text or "",
                    source_identity_json=json.dumps(identity, ensure_ascii=False),
                    attempt_count=0,
                )
                session.add(record)
            record.attempt_count += 1
            record.status = "pending"
            record.attempted_at = now
            record.generation_chapter_number = generation_chapter_number
            record.next_retry_at = None
            records.append((comment, record))
        session.flush()
        return records

    def status(self, session, *, project_id):
        current = (
            select(CommentAnalysisRecord)
            .join(
                PublisherRawComment,
                PublisherRawComment.id == CommentAnalysisRecord.source_comment_id,
            )
            .where(
                PublisherRawComment.project_id == project_id,
                CommentAnalysisRecord.analyzer_version == self.version,
                CommentAnalysisRecord.content_sha256
                == PublisherRawComment.content_sha256,
                CommentAnalysisRecord.source_sha256
                == PublisherRawComment.source_sha256,
                or_(
                    CommentAnalysisRecord.status != "completed",
                    PublisherRawComment.active_analysis_id == CommentAnalysisRecord.id,
                ),
            )
        )
        counts = dict(
            session.execute(
                select(CommentAnalysisRecord.status, func.count())
                .where(
                    CommentAnalysisRecord.id.in_(
                        current.with_only_columns(CommentAnalysisRecord.id)
                    )
                )
                .group_by(CommentAnalysisRecord.status)
            ).all()
        )
        total = session.scalar(
            select(func.count())
            .select_from(PublisherRawComment)
            .where(PublisherRawComment.project_id == project_id)
        )
        errors = session.scalars(
            current.where(CommentAnalysisRecord.status.in_(("failed", "exhausted")))
            .order_by(CommentAnalysisRecord.attempted_at.desc())
            .limit(20)
        ).all()
        return {
            "analyzer_version": self.version,
            "pending_count": max(
                0,
                total
                - sum(
                    counts.get(key, 0) for key in ("completed", "failed", "exhausted")
                ),
            ),
            "completed_count": counts.get("completed", 0),
            "failed_count": counts.get("failed", 0),
            "exhausted_count": counts.get("exhausted", 0),
            "errors": [
                {
                    "comment_id": row.source_comment_id,
                    "analysis_id": row.id,
                    "attempt_count": row.attempt_count,
                    "last_error": row.last_error,
                    "next_retry_at": str(row.next_retry_at or ""),
                }
                for row in errors
            ],
        }

    def failed(self, record, error):
        record.status = (
            "exhausted" if record.attempt_count >= self.max_attempts else "failed"
        )
        record.last_error = f"{type(error).__name__}: {error}"[:2000]
        record.next_retry_at = (
            None
            if record.status == "exhausted"
            else now_utc() + timedelta(seconds=self.retry_delay)
        )

    @staticmethod
    def complete(record, signal_count):
        record.status = "completed"
        record.signal_count = signal_count
        record.analyzed_at = now_utc()
        record.last_error = ""
        record.next_retry_at = None


def current_signal_condition():
    """Older results stay inspectable; current reads use the explicitly completed version."""
    known_source = exists().where(
        PublisherRawComment.id == CommentSignalCandidate.source_comment_id,
        PublisherRawComment.project_id == CommentSignalCandidate.project_id,
        PublisherRawComment.source_status.in_(("chapter_known", "confirmed")),
        PublisherRawComment.source_chapter_number
        == CommentSignalCandidate.chapter_number,
        PublisherRawComment.source_chapter_number > 0,
    )
    return known_source & exists().where(
        CommentAnalysisRecord.id == CommentSignalCandidate.analysis_id,
        CommentAnalysisRecord.status == "completed",
        CommentAnalysisRecord.source_comment_id == PublisherRawComment.id,
        CommentAnalysisRecord.project_id == PublisherRawComment.project_id,
        PublisherRawComment.id == CommentSignalCandidate.source_comment_id,
        PublisherRawComment.active_analysis_id == CommentAnalysisRecord.id,
        PublisherRawComment.content_sha256 == CommentAnalysisRecord.content_sha256,
        PublisherRawComment.source_sha256 == CommentAnalysisRecord.source_sha256,
    )
