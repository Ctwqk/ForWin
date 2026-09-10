"""Commit one bounded comment batch independently of later maintenance work."""

from __future__ import annotations

from sqlalchemy import select

from forwin.maintenance.trace_upload import enqueue_trace_upload
from forwin.models.publisher import CommentAnalysisRecord, PublisherRawComment
from forwin.observability.llm_trace import safe_prompt_trace_attempts
from forwin.simulation.world import CommentAnalyzer


def consume_post_canon_comment_batch(
    *,
    session_factory,
    llm_client,
    project_id: str,
    canon_commit_id: str,
    chapter_number: int,
    limit: int = 8,
) -> dict[str, object]:
    """Use the existing analyzer; no plan, Canon or maintenance lease writes.

    The post-Canon caller has drained its preceding model attempts. Completion,
    ordinary bounded failures and their usage trace commit before aggregation.
    A process crash before this commit can repeat a model call; this does not
    claim exactly-once external execution.
    """
    analyzer = CommentAnalyzer(llm_client=llm_client)
    with session_factory.begin() as session:
        comments = analyzer.pending_comments(
            session=session, project_id=project_id, limit=min(max(0, limit), 8)
        )
        comment_ids = [row.id for row in comments]
        analyzer.analyze_and_store(
            session=session,
            project_id=project_id,
            comments=comments,
            chapter_number=chapter_number,
        )
        records = (
            session.scalars(
                select(CommentAnalysisRecord)
                .join(
                    PublisherRawComment,
                    PublisherRawComment.id == CommentAnalysisRecord.source_comment_id,
                )
                .where(
                    PublisherRawComment.id.in_(comment_ids),
                    CommentAnalysisRecord.analyzer_version == analyzer.store.version,
                    CommentAnalysisRecord.content_sha256
                    == PublisherRawComment.content_sha256,
                    CommentAnalysisRecord.source_sha256
                    == PublisherRawComment.source_sha256,
                )
                .order_by(CommentAnalysisRecord.id)
            ).all()
            if comment_ids
            else []
        )
        result = {
            "selected_count": len(comment_ids),
            "analysis_status": analyzer.store.status(session, project_id=project_id),
            "analyses": [
                {
                    "analysis_id": row.id,
                    "attempt_count": row.attempt_count,
                    "status": row.status,
                    "signal_count": row.signal_count,
                }
                for row in records
            ],
        }
        drain = getattr(llm_client, "drain_llm_attempt_events", None)
        attempts = drain() if comment_ids and callable(drain) else []
        if attempts:
            result["trace"] = enqueue_trace_upload(
                session,
                trace={
                    "schema_version": "post-canon-trace-v1",
                    "canon_commit_id": canon_commit_id,
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "step_name": "feedback",
                    "comment_analyses": result["analyses"],
                    "attempts": safe_prompt_trace_attempts(
                        [dict(item) for item in attempts if isinstance(item, dict)]
                    ),
                },
            )
    return result
