"""Accepted-chapter revision request use case shared by the existing review API."""

from fastapi import HTTPException

from forwin.canon.historical_rewrite import (
    HistoricalCanonRewriteRepository,
    HistoricalRewriteInvalid,
)
from forwin.canon.revision_service import save_revision_proposal


def record_revision_retry(session, *, project, chapter, request, reason):
    """Keep accepted content active while recording a request or real proposal."""
    if (
        request.expected_book_revision is not None
        and project.book_revision != request.expected_book_revision
    ):
        raise HTTPException(409, "revision proposal book revision is stale")
    candidate_id = ""
    if request.replacement_body is not None:
        if chapter.status != "accepted":
            raise HTTPException(
                409, "replacement body requires an active accepted chapter"
            )
        try:
            candidate = save_revision_proposal(
                session,
                project_id=project.id,
                chapter_number=chapter.chapter_number,
                body=request.replacement_body,
                title=request.replacement_title,
                expected_book_revision=request.expected_book_revision,
            )
            candidate_id = candidate.id
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
    if chapter.status == "accepted":
        try:
            marker = HistoricalCanonRewriteRepository(session).mark_pending(
                project_id=project.id,
                chapter_number=chapter.chapter_number,
                reason=reason,
            )
            marker.related_object_id = str(chapter.id)
        except HistoricalRewriteInvalid as exc:
            raise HTTPException(409, str(exc)) from exc
    return candidate_id
