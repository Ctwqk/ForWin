from __future__ import annotations

from pydantic import BaseModel, Field


class PulpBeatResult(BaseModel):
    pressure_present: bool = False
    protagonist_action_present: bool = False
    visible_payoff_present: bool = False
    audience_reaction_present: bool = False
    enemy_or_obstacle_damage_present: bool = False
    new_gain_or_status_shift_present: bool = False
    next_hook_present: bool = False
    boring_setup_ratio: float = 0.0
    payoff_delay_chapters: int | None = None
    missing_fields: list[str] = Field(default_factory=list)


PRESSURE_WORDS = ("嘲笑", "看不起", "羞辱", "威胁", "逼迫", "驱赶", "扣钱", "没资格")
ACTION_WORDS = ("当场", "出手", "拿出", "开口", "反击", "证明", "亮出")
PAYOFF_WORDS = ("到账", "赔偿", "合同", "资格", "名额", "升职", "奖励")
AUDIENCE_WORDS = ("众人", "全场", "同事", "邻居", "直播间", "村里", "当众")
DAMAGE_WORDS = ("道歉", "跪下", "开除", "赔钱", "封杀", "脸色大变", "失去资格")
GAIN_WORDS = ("赔偿", "到账", "资格", "名额", "合同", "职位", "资源")
HOOK_WORDS = ("忽然", "没想到", "就在这时", "门外", "电话响起", "新的威胁")
SETUP_WORDS = ("想起", "回忆", "前情", "沉默", "走在路上", "夜色")
CORE_FIELDS = (
    "pressure_present",
    "protagonist_action_present",
    "visible_payoff_present",
    "audience_reaction_present",
    "enemy_or_obstacle_damage_present",
    "new_gain_or_status_shift_present",
    "next_hook_present",
)


def _has_any(body: str, words: tuple[str, ...]) -> bool:
    return any(word in body for word in words)


def _boring_setup_ratio(body: str) -> float:
    text = str(body or "")
    if not text:
        return 0.0
    setup_hits = sum(text.count(word) for word in SETUP_WORDS)
    sentence_count = max(1, sum(text.count(mark) for mark in "。！？!?"))
    return round(min(1.0, setup_hits / sentence_count), 3)


def verify_pulp_beats(body: str) -> PulpBeatResult:
    text = str(body or "")
    result = PulpBeatResult(
        pressure_present=_has_any(text, PRESSURE_WORDS),
        protagonist_action_present=_has_any(text, ACTION_WORDS),
        visible_payoff_present=_has_any(text, PAYOFF_WORDS),
        audience_reaction_present=_has_any(text, AUDIENCE_WORDS),
        enemy_or_obstacle_damage_present=_has_any(text, DAMAGE_WORDS),
        new_gain_or_status_shift_present=_has_any(text, GAIN_WORDS),
        next_hook_present=_has_any(text[-240:], HOOK_WORDS),
        boring_setup_ratio=_boring_setup_ratio(text),
    )
    missing = [field for field in CORE_FIELDS if not getattr(result, field)]
    return result.model_copy(update={"missing_fields": missing})
