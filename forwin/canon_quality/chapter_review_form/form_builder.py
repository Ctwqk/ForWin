from __future__ import annotations

from typing import Any

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
    row_value,
    select_characters_to_ask,
    select_countdowns_to_ask,
    select_obligations_to_ask,
    select_signals_to_ask,
)


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
    return _fit_budget(form, max_chars=max(1000, int(token_budget_chars or 8000)))


def _character_ask(row: Any) -> CharacterReviewAsk:
    name = str(row_value(row, "character_name") or row_value(row, "name") or "").strip()
    payload = row_value(row, "payload", {}) or {}
    aliases = []
    if isinstance(payload, dict) and isinstance(payload.get("aliases"), list):
        aliases = [str(item).strip() for item in payload.get("aliases", []) if str(item).strip()]
    descriptive_aliases = []
    if isinstance(payload, dict) and isinstance(payload.get("descriptive_aliases"), list):
        descriptive_aliases = [str(item).strip() for item in payload.get("descriptive_aliases", []) if str(item).strip()]
    state = _life_state(row_value(row, "to_state") or row_value(row, "life_state") or "unknown")
    custody = _custody_state(row_value(row, "to_state") or row_value(row, "custody_state") or "unknown")
    return CharacterReviewAsk(
        name=name,
        aliases=aliases,
        descriptive_aliases=descriptive_aliases,
        prior_life_state=state,
        prior_custody_state=custody,
        last_seen_chapter=int(row_value(row, "chapter_number", 0) or 0),
        must_track=bool(payload.get("must_track") if isinstance(payload, dict) else False),
    )


def _countdown_ask(row: Any) -> CountdownReviewAsk:
    key = str(row_value(row, "countdown_key") or row_value(row, "key") or "main").strip() or "main"
    label = str(row_value(row, "label") or key).strip() or key
    return CountdownReviewAsk(
        key=key,
        label=label,
        prior_value_minutes=_optional_int(row_value(row, "normalized_remaining_minutes", None)),
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


def _fit_budget(form: ChapterReviewForm, *, max_chars: int) -> ChapterReviewForm:
    while len(form.model_dump_json()) > max_chars and form.open_signals:
        form.open_signals.pop()
    while len(form.model_dump_json()) > max_chars and form.obligations:
        form.obligations.pop()
    while len(form.model_dump_json()) > max_chars and form.countdowns:
        form.countdowns.pop()
    while len(form.model_dump_json()) > max_chars and form.characters:
        removable = next((index for index, item in enumerate(form.characters) if not item.must_track), len(form.characters) - 1)
        form.characters.pop(removable)
    return form


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
