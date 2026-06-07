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


class PulpBeatProfile(BaseModel):
    pressure_words: tuple[str, ...]
    action_words: tuple[str, ...]
    payoff_words: tuple[str, ...]
    audience_words: tuple[str, ...]
    damage_words: tuple[str, ...]
    gain_words: tuple[str, ...]
    hook_words: tuple[str, ...]
    inference_words: tuple[str, ...] = ()


PULP_BEAT_PROFILES: dict[str, PulpBeatProfile] = {
    "urban": PulpBeatProfile(
        pressure_words=("嘲笑", "看不起", "羞辱", "威胁", "逼迫", "驱赶", "扣钱", "没资格"),
        action_words=("当场", "出手", "拿出", "开口", "反击", "证明", "亮出"),
        payoff_words=("到账", "赔偿", "合同", "资格", "名额", "升职", "奖励"),
        audience_words=("众人", "全场", "同事", "邻居", "直播间", "村里", "当众"),
        damage_words=("道歉", "跪下", "开除", "赔钱", "封杀", "脸色大变", "失去资格"),
        gain_words=("职位", "资源", "现金", "股份", "权限", "证据", "三十万", "赔偿金"),
        hook_words=("忽然", "没想到", "就在这时", "门外", "电话响起", "新的威胁"),
        inference_words=("合同", "升职", "同事", "直播间", "老板"),
    ),
    "xuanhuan": PulpBeatProfile(
        pressure_words=("威胁", "逐", "废物", "压迫", "挑衅", "夺令牌", "宗门责罚"),
        action_words=("当场", "运转", "祭出", "拔剑", "出手", "破阵", "突破"),
        payoff_words=("突破境界", "晋升", "夺魁", "灵石奖励", "传承认可", "试炼通过"),
        audience_words=("宗门", "长老", "弟子", "全场", "擂台", "众修"),
        damage_words=("受创", "吐血", "败退", "退下", "经脉", "跪地", "道心崩裂"),
        gain_words=("灵石", "功法", "境界", "令牌", "传承", "法器", "入袋"),
        hook_words=("忽然", "秘境", "天门", "雷劫", "古碑", "传送阵", "入口开启"),
        inference_words=("宗门", "灵石", "境界", "秘境", "长老", "擂台"),
    ),
    "rural": PulpBeatProfile(
        pressure_words=("村里", "乡亲", "逼债", "瞧不起", "抢地", "赶出", "亲戚奚落"),
        action_words=("当场", "掏出", "签下", "种出", "救下", "摆摊", "反问"),
        payoff_words=("订单", "分红", "承包", "赔钱", "收购", "销路打开"),
        audience_words=("村里", "邻居", "乡亲", "全村", "集市", "围观"),
        damage_words=("道歉", "赔钱", "灰溜溜", "被赶走", "脸色发白"),
        gain_words=("地契", "订单", "现金", "货款", "渠道", "分红"),
        hook_words=("忽然", "镇上", "电话", "来人", "新订单", "县里"),
        inference_words=("村里", "乡亲", "镇上", "地契", "承包"),
    ),
    "rebirth_period": PulpBeatProfile(
        pressure_words=("名声", "举报", "扣帽子", "粮票", "厂里", "排挤", "逼婚"),
        action_words=("当场", "拿出", "改口", "写下", "换票", "揭穿", "报名"),
        payoff_words=("录取", "表彰", "名额", "工分", "粮票", "证明开出"),
        audience_words=("大队", "厂里", "邻里", "众人", "全院", "当众"),
        damage_words=("处分", "道歉", "丢名额", "脸色发白", "被带走"),
        gain_words=("名额", "票证", "工分", "岗位", "证明", "口碑"),
        hook_words=("忽然", "广播", "通知", "门口", "信封", "新政策"),
        inference_words=("粮票", "工分", "大队", "厂里", "票证", "知青"),
    ),
    "treasure_medicine": PulpBeatProfile(
        pressure_words=("质疑", "没眼力", "庸医", "骗子", "掌柜", "假专家", "讥笑"),
        action_words=("当场", "施针", "验出", "鉴定", "开方", "揭开", "把脉"),
        payoff_words=("病人苏醒", "鉴定证书", "真品", "药效立现", "古玉暗纹"),
        audience_words=("围观", "客人", "掌柜", "病人家属", "当众", "满堂"),
        damage_words=("脸色大变", "认错", "退钱", "假专家", "当场噎住", "露馅"),
        gain_words=("证书到手", "古玉", "诊金", "药方", "人情", "名声"),
        hook_words=("忽然", "求救声", "后院", "急诊", "暗格", "新宝物"),
        inference_words=("古玉", "施针", "鉴定", "药方", "病人", "掌柜"),
    ),
}
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


def verify_pulp_beats(body: str, *, track: str | None = None) -> PulpBeatResult:
    text = str(body or "")
    profile = _profile_for(text, track=track)
    result = PulpBeatResult(
        pressure_present=_has_any(text, profile.pressure_words),
        protagonist_action_present=_has_any(text, profile.action_words),
        visible_payoff_present=_has_any(text, profile.payoff_words),
        audience_reaction_present=_has_any(text, profile.audience_words),
        enemy_or_obstacle_damage_present=_has_any(text, profile.damage_words),
        new_gain_or_status_shift_present=_has_any(text, profile.gain_words),
        next_hook_present=_has_any(text[-240:], profile.hook_words),
        boring_setup_ratio=_boring_setup_ratio(text),
    )
    missing = [field for field in CORE_FIELDS if not getattr(result, field)]
    return result.model_copy(update={"missing_fields": missing})


def _profile_for(body: str, *, track: str | None) -> PulpBeatProfile:
    requested = str(track or "").strip()
    if requested in PULP_BEAT_PROFILES:
        return PULP_BEAT_PROFILES[requested]
    inferred = _infer_track(body)
    return PULP_BEAT_PROFILES[inferred]


def _infer_track(body: str) -> str:
    text = str(body or "")
    for key, profile in PULP_BEAT_PROFILES.items():
        if key == "urban":
            continue
        if _has_any(text, profile.inference_words):
            return key
    return "urban"
