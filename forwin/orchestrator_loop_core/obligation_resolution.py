from __future__ import annotations

from sqlalchemy.orm import Session

from forwin.canon_quality.obligation_verifier import (
    expire_unresolved_obligations_after_acceptance,
    verify_active_obligations_after_acceptance,
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
        "resolution": verify_active_obligations_after_acceptance(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
            accepted_text=accepted_text,
        ),
        "expiry": expire_unresolved_obligations_after_acceptance(
            session=session,
            project_id=project_id,
            chapter_number=chapter_number,
        ),
    }


__all__ = ["_verify_obligations_after_acceptance"]
