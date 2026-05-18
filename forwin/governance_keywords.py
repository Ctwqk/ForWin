from __future__ import annotations

from dataclasses import dataclass


DEATH_KEYWORDS = ("死", "死亡", "身亡", "阵亡", "牺牲", "杀死", "写死")
REVEAL_KEYWORDS = ("真相", "秘密", "身份", "揭露", "公开", "坦白", "曝光")
RELATION_BREAK_KEYWORDS = ("决裂", "断绝", "反目", "分手", "离婚", "背叛")
LOCATION_DESTROY_KEYWORDS = ("毁灭", "坍塌", "封锁", "不可进入", "失守", "焚毁")
RULE_BREAK_KEYWORDS = ("失效", "崩坏", "破除", "废除", "不可逆", "解除")
RESOURCE_CLOSURE_KEYWORDS = ("彻底解决", "完全结束", "永远离开", "彻底公开", "永久失去", "不可逆")
THREAD_CLOSURE_KEYWORDS = ("结案", "了结", "落幕", "终结", "收束", "完结")
GROWTH_COMPLETION_KEYWORDS = ("完成成长", "彻底成熟", "终于成为", "再无成长空间", "终极形态", "圆满毕业")

NEGATION_MARKERS = ("避免", "不要", "不得", "不能", "防止", "禁止", "阻止误写")


@dataclass(frozen=True)
class ConstraintKeywordRegistry:
    death: tuple[str, ...] = DEATH_KEYWORDS
    reveal: tuple[str, ...] = REVEAL_KEYWORDS
    relation_break: tuple[str, ...] = RELATION_BREAK_KEYWORDS
    location_destroy: tuple[str, ...] = LOCATION_DESTROY_KEYWORDS
    rule_break: tuple[str, ...] = RULE_BREAK_KEYWORDS
    resource_closure: tuple[str, ...] = RESOURCE_CLOSURE_KEYWORDS
    thread_closure: tuple[str, ...] = THREAD_CLOSURE_KEYWORDS
    growth_completion: tuple[str, ...] = GROWTH_COMPLETION_KEYWORDS


def constraint_keywords() -> ConstraintKeywordRegistry:
    return ConstraintKeywordRegistry()


def keyword_is_prefix_negated(text: str, keyword: str, *, window: int = 12) -> bool:
    local = str(text or "")
    occurrences = _keyword_occurrences(local, keyword)
    return bool(occurrences) and all(
        _occurrence_is_prefix_negated(local, keyword, index, window=window)
        for index in occurrences
    )


def text_has_unnegated_keyword(text: str, keywords: tuple[str, ...]) -> bool:
    local = str(text or "")
    for keyword in keywords:
        for index in _keyword_occurrences(local, keyword):
            if not _occurrence_is_prefix_negated(local, keyword, index):
                return True
    return False


def first_unnegated_keyword(text: str, keywords: tuple[str, ...]) -> str:
    local = str(text or "")
    for keyword in keywords:
        for index in _keyword_occurrences(local, keyword):
            if not _occurrence_is_prefix_negated(local, keyword, index):
                return keyword
    return ""


def _keyword_occurrences(text: str, keyword: str) -> list[int]:
    if not keyword:
        return []
    local = str(text or "")
    indexes: list[int] = []
    index = local.find(keyword)
    while index >= 0:
        indexes.append(index)
        index = local.find(keyword, index + len(keyword))
    return indexes


def _occurrence_is_prefix_negated(text: str, keyword: str, index: int, *, window: int = 12) -> bool:
    if not keyword or index < 0:
        return False
    local = str(text or "")
    clause_start = max(
        local.rfind(marker, 0, index)
        for marker in ("。", "！", "？", "；", ";", "\n", "，", ",")
    )
    before = local[max(clause_start + 1, index - window) : index]
    return any(marker in before for marker in NEGATION_MARKERS)
