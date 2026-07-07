from __future__ import annotations

import re


_GENERIC_NUMERIC_TITLE_RE = re.compile(r"^\s*第\s*(\d+)\s*章(?P<suffix>.*)$")


def rebase_generic_numeric_chapter_title(title: str, chapter_number: int) -> str:
    normalized = str(title or "").strip()
    absolute_number = int(chapter_number or 0)
    if not normalized or absolute_number <= 0:
        return normalized
    match = _GENERIC_NUMERIC_TITLE_RE.match(normalized)
    if match is None:
        return normalized
    observed_number = int(match.group(1) or 0)
    if observed_number == absolute_number:
        return normalized
    suffix = str(match.group("suffix") or "").strip()
    return f"第{absolute_number}章{suffix}"


__all__ = ["rebase_generic_numeric_chapter_title"]
