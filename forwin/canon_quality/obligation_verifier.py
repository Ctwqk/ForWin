from __future__ import annotations

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.narrative_obligation import NarrativeObligationRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository


def read_committed_obligation_resolutions(*, session: Session, project_id: str, chapter_number: int) -> dict[str, list[str]]:
    """Read Canon's result; post-acceptance maintenance has no resolution authority."""
    return {"resolved_obligation_ids": list(session.scalars(select(NarrativeObligationRow.id).where(
        NarrativeObligationRow.project_id == project_id,
        NarrativeObligationRow.status == "resolved",
        NarrativeObligationRow.resolution_chapter == chapter_number,
    )))}


def expire_unresolved_obligations_after_acceptance(
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
) -> dict[str, list[str]]:
    repo = NarrativeObligationRepository(session)
    expired_ids: list[str] = []
    blocked_ids: list[str] = []
    for obligation in repo.list_active_for_context(project_id, chapter_number=chapter_number + 1):
        if obligation.status != "active":
            continue
        if int(obligation.deadline_chapter or 0) > int(chapter_number or 0):
            continue
        expired = repo.expire_obligation(
            obligation.id,
            reason="deadline passed after accepted chapter",
            chapter_number=chapter_number,
        )
        if expired is None:
            continue
        expired_ids.append(expired.id)
        if expired.blocking_policy == "block_at_deadline":
            blocked = repo.block_expired_obligation(expired.id, chapter_number=chapter_number)
            if blocked is not None:
                blocked_ids.append(blocked.id)
    return {
        "expired_obligation_ids": expired_ids,
        "blocked_obligation_ids": blocked_ids,
    }
