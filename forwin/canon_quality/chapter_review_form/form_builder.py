from __future__ import annotations

from typing import Any

from sqlalchemy.orm import Session

from forwin.book_state.query import BookStateQuery
from forwin.protocol.book_state import WorldNode

from . import FORM_SCHEMA_VERSION
from .form_schema import (
    CharacterReviewAsk,
    ChapterReviewForm,
    CountdownReviewAsk,
    FinalChapterAsk,
    ObligationReviewAsk,
    OpenSignalReviewAsk,
)
from .pruning import (
    fit_form_budget,
    row_value,
    select_characters_to_ask,
    select_countdowns_to_ask,
    select_obligations_to_ask,
    select_signals_to_ask,
)


# BookState restraint.state -> form custody; a character without restraint is free, as in the canon gate.
_CUSTODY_BY_RESTRAINT = {"free": "free", "restrained": "captured", "imprisoned": "captured"}
# BookState clock state.status -> form countdown status.
_COUNTDOWN_STATUS_BY_CLOCK = {"running": "active", "expired": "closed", "resolved": "resolved"}


def build_form(
    *,
    project_id: str,
    chapter_number: int,
    chapter_text: str,
    character_rows: list[Any] | None = None,
    countdown_rows: list[Any] | None = None,
    open_signal_rows: list[Any] | None = None,
    obligations: list[Any] | None = None,
    target_total_chapters: int = 0,
    token_budget_chars: int = 8000,
) -> ChapterReviewForm:
    characters = [
        _character_ask(row)
        for row in select_characters_to_ask(rows=list(character_rows or []), chapter_text=chapter_text)
    ]
    countdowns = [
        _countdown_ask(row)
        for row in select_countdowns_to_ask(rows=list(countdown_rows or []), chapter_text=chapter_text)
    ]
    open_signals = [
        _signal_ask(row, current_chapter=chapter_number)
        for row in select_signals_to_ask(rows=list(open_signal_rows or []), chapter_number=chapter_number)
    ]
    obligation_asks = [
        _obligation_ask(item, current_chapter=chapter_number)
        for item in select_obligations_to_ask(obligations=list(obligations or []), chapter_number=chapter_number)
    ]
    final_chapter = FinalChapterAsk() if target_total_chapters and int(chapter_number or 0) >= int(target_total_chapters) else None
    form = ChapterReviewForm(
        project_id=project_id,
        chapter_number=int(chapter_number or 0),
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=characters,
        countdowns=countdowns,
        obligations=obligation_asks,
        open_signals=open_signals,
        final_chapter=final_chapter,
    )
    return fit_form_budget(form, max_chars=max(1000, int(token_budget_chars or 8000)))


def book_state_review_rows(
    session: Session,
    *,
    project_id: str,
    chapter_number: int,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Character and countdown rows for chapter N's form, read from BookState as of chapter N−1."""
    as_of_chapter = max(int(chapter_number or 0) - 1, 0)
    query = BookStateQuery(session)
    world = query.runtime(project_id, as_of_chapter=as_of_chapter).world
    story_minute = query.current_story_minute(project_id, as_of_chapter=as_of_chapter)
    character_rows: list[dict[str, Any]] = []
    countdown_rows: list[dict[str, Any]] = []
    for node in sorted(world.nodes_by_id.values(), key=lambda item: item.id):
        if not node.is_active:
            continue
        state = world.get_state(node.id)
        if node.node_type == "character" and node.name.strip():
            character_rows.append(_character_row(node, state))
        elif node.node_type == "clock":
            countdown_rows.append(
                _clock_row(node, state, story_minute=story_minute, as_of_chapter=as_of_chapter)
            )
    return character_rows, countdown_rows


def _character_row(node: WorldNode, state: dict[str, Any]) -> dict[str, Any]:
    restraint = state.get("restraint")
    restraint_state = (restraint.get("state") if isinstance(restraint, dict) else None) or "free"
    last_seen = state.get("last_seen_chapter")
    return {
        "character_name": node.name.strip(),
        "aliases": list(node.aliases),
        "chapter_number": last_seen if type(last_seen) is int else node.created_at_chapter,
        "life_state": state.get("alive_status") or "unknown",
        "custody_state": _CUSTODY_BY_RESTRAINT.get(str(restraint_state), "unknown"),
        "location_id": str(state.get("location_id") or ""),
    }


def _clock_row(
    node: WorldNode,
    state: dict[str, Any],
    *,
    story_minute: int | None,
    as_of_chapter: int,
) -> dict[str, Any]:
    due_at = state.get("due_at")
    remaining = due_at - story_minute if type(due_at) is int and story_minute is not None else None
    return {
        "countdown_key": node.id,
        "label": str(node.profile.get("label") or node.name or node.id),
        "remaining_minutes": remaining,
        "status": _COUNTDOWN_STATUS_BY_CLOCK.get(str(state.get("status") or "running"), "active"),
        "chapter_number": as_of_chapter,
    }


def _character_ask(row: Any) -> CharacterReviewAsk:
    return CharacterReviewAsk(
        name=str(row_value(row, "character_name") or row_value(row, "name") or "").strip(),
        aliases=[str(item).strip() for item in row_value(row, "aliases", []) or [] if str(item).strip()],
        prior_life_state=_life_state(row_value(row, "life_state") or "unknown"),
        prior_custody_state=_custody_state(row_value(row, "custody_state") or "unknown"),
        prior_location_id=str(row_value(row, "location_id") or "").strip(),
        last_seen_chapter=int(row_value(row, "chapter_number", 0) or 0),
    )


def _countdown_ask(row: Any) -> CountdownReviewAsk:
    key = str(row_value(row, "countdown_key") or row_value(row, "key") or "").strip()
    label = str(row_value(row, "label") or key).strip() or key
    return CountdownReviewAsk(
        key=key,
        label=label,
        prior_value_minutes=_optional_int(row_value(row, "remaining_minutes", None)),
        prior_status=_countdown_status(row_value(row, "status") or "active"),
        last_updated_chapter=int(row_value(row, "chapter_number", 0) or 0),
    )


def _signal_ask(row: Any, *, current_chapter: int) -> OpenSignalReviewAsk:
    signal_id = str(row_value(row, "signal_id") or row_value(row, "id") or "").strip()
    return OpenSignalReviewAsk(
        id=signal_id,
        description=str(row_value(row, "description") or row_value(row, "signal_type") or "").strip(),
        severity=str(row_value(row, "severity") or "warning").strip(),
        age_chapters=max(0, int(current_chapter or 0) - int(row_value(row, "chapter_number", current_chapter) or current_chapter)),
    )


def _obligation_ask(obligation: Any, *, current_chapter: int) -> ObligationReviewAsk:
    obligation_id = str(row_value(obligation, "id") or row_value(obligation, "obligation_id") or "").strip()
    deadline = int(row_value(obligation, "deadline_chapter", current_chapter) or current_chapter)
    return ObligationReviewAsk(
        id=obligation_id,
        summary=str(row_value(obligation, "summary") or row_value(obligation, "description") or "").strip(),
        deadline_chapter=deadline,
        must_resolve_now=bool(row_value(obligation, "must_resolve_now", False)) or deadline <= int(current_chapter or 0),
        payoff_test=str(row_value(obligation, "payoff_test") or "").strip(),
    )


def _life_state(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized in {"alive", "wounded", "dead"}:
        return normalized
    return "unknown"


def _custody_state(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized in {"free", "captured"}:
        return normalized
    return "unknown"


def _countdown_status(value: Any) -> str:
    normalized = str(value or "").strip()
    if normalized in {"active", "paused", "closed", "fulfilled", "reopened", "consistent", "warning", "conflict", "resolved"}:
        return normalized
    return "active"


def _optional_int(value: Any) -> int | None:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None
