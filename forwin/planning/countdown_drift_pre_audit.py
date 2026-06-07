from typing import Any

from forwin.planning.ledger_state_drift_pre_audit import select_countdown_compat_drift_targets


def select_countdown_drift_targets(
    signals: list[Any],
    *,
    project_id: str = "",
    as_of_chapter: int = 0,
    book_state_query: Any | None = None,
) -> list[dict[str, Any]]:
    return select_countdown_compat_drift_targets(
        signals,
        project_id=project_id,
        as_of_chapter=as_of_chapter,
        book_state_query=book_state_query,
    )


__all__ = ["select_countdown_drift_targets"]
