from __future__ import annotations


GENERIC_CHARACTER_TOKENS = {
    "路人",
    "守卫",
    "老板",
    "店小二",
    "师兄",
    "师姐",
    "弟子",
    "同学",
    "众人",
    "人群",
    "伙计",
    "旁人",
}


def is_generic_character_name(name: str) -> bool:
    return str(name or "").strip() in GENERIC_CHARACTER_TOKENS
