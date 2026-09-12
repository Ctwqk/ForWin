from __future__ import annotations

from sqlalchemy.orm import Session

from forwin.canon_quality.obligation_verifier import (
    expire_unresolved_obligations_after_acceptance,
    read_committed_obligation_resolutions,
)


def _verify_obligations_after_acceptance(
    self,
    *,
    session: Session,
    project_id: str,
    chapter_number: int,
    accepted_text: str,
) -> dict[str, object]:
    if not self.policy.review.allows_repair_scope("obligation"):
        return {}
    return {
        "resolution": read_committed_obligation_resolutions(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        ),
        "expiry": expire_unresolved_obligations_after_acceptance(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        ),
    }


__all__ = ["_verify_obligations_after_acceptance"]
