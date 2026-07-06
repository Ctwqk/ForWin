from __future__ import annotations


def repair_word_budget_patch(context) -> dict[str, object]:
    patch: dict[str, object] = {
        "repair_max_growth_ratio": 1.08,
        "must_replace_not_append": True,
    }
    for key in ("target_chapter_chars", "min_chapter_chars", "max_chapter_chars"):
        value = getattr(context, key, 0)
        try:
            normalized = int(value or 0)
        except (TypeError, ValueError):
            normalized = 0
        if normalized > 0:
            patch[key] = normalized
    return patch


__all__ = ["repair_word_budget_patch"]
