from __future__ import annotations

from collections.abc import Iterable


GENERIC_CHARACTER_TOKENS = frozenset(
    {
        "路人",
        "守卫",
        "老板",
        "同学",
        "众人",
        "人群",
        "伙计",
        "旁人",
    }
)


def is_generic_character_name(name: str, *, extra_refs: Iterable[str] = ()) -> bool:
    text = str(name or "").strip()
    return text in GENERIC_CHARACTER_TOKENS or text in extra_refs
