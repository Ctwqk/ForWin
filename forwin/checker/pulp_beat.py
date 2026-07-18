from __future__ import annotations

import re

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
        pressure_words=(
            "嘲笑",
            "看不起",
            "羞辱",
            "威胁",
            "逼迫",
            "驱赶",
            "扣钱",
            "没资格",
        ),
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
PULP_GENRE_TRACKS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("treasure_medicine", ("神医", "鉴宝", "古玩", "古董", "中医", "医术")),
    ("rural", ("乡村", "种田", "农村")),
    ("rebirth_period", ("年代", "重生", "知青")),
    ("xuanhuan", ("玄幻", "仙侠", "修仙", "武道")),
    ("urban", ("都市", "职场", "商战", "现代")),
)
REWARD_PAYOFF_MARKERS: dict[str, tuple[str, ...]] = {
    "power": (
        "能力提升",
        "资源到账",
        "权限打开",
        "权限解锁",
        "权限已发放",
        "权限变更：开放",
        "权限变更:开放",
        "权限变更为",
        "权限已开放",
        "功能已开放",
        "获得权限",
        "取得权限",
        "突破",
        "晋升",
        "到手",
        "入袋",
        "战局逆转",
    ),
    "social": (
        "改口",
        "当场签约",
        "合同到手",
        "资格到手",
        "获得资格",
        "公开背书",
        "态度转变",
        "站队",
    ),
    "justice": (
        "押走",
        "罚没",
        "除名",
        "赔偿到账",
        "公开道歉",
        "被开除",
        "落网",
        "认罪",
        "收回利益",
    ),
    "mystery": (
        "线索到手",
        "锁定",
        "识破",
        "查明",
        "证实",
        "记录显示",
        "证据表明",
        "真相浮出",
        "工号在线",
        "刻痕指向",
        "编号对应",
    ),
    "emotion": (
        "主动靠近",
        "承担风险",
        "挡在",
        "护住",
        "关系缓和",
        "建立信任",
        "拥抱",
        "牵手",
    ),
}
MYSTERY_PAYOFF_EVIDENCE_WORDS = (
    "线索",
    "名字",
    "地点",
    "记录",
    "符号",
    "矛盾",
    "证据",
    "工号",
    "刻痕",
    "编号",
    "回执",
    "插孔",
    "时间戳",
    "名单",
)
_REWARD_SENTENCE_RE = re.compile(r"[。！？!?；;\n]+")
_REWARD_SUBCLAUSE_RE = re.compile(r"[，,]+")
_MARKER_CONTEXT_BEFORE = 18
_MARKER_CONTEXT_AFTER = 12
_MARKER_NEGATION_PREFIXES = (
    "没有",
    "没能",
    "不具备",
    "不符合",
    "不满足",
    "尚无",
    "并无",
    "未能",
    "不能",
    "无法",
    "不可",
    "不曾",
    "未曾",
    "并未",
    "并非",
    "不予",
    "未予",
    "不再",
    "未再",
    "不得",
    "不准",
    "不许",
    "缺乏",
    "没",
    "无",
    "不",
    "未",
)
_MARKER_NEGATION_RE = re.compile(
    r"(?:不|未|没|无|尚无|并无|尚未|仍未|从未|并未|并非)"
    r"(?:被(?:认定|视为|判定|确认|证明))?"
    r"(?:获|得|有|能|予|具备|符合|满足|拥有|获得|取得|得到|拿到|达到|达成)?$"
)
_MARKER_POST_NEGATION_RE = re.compile(
    r"^(?:状态|身份|结果)?"
    r"(?:尚未|仍未|并未|并非|不予|未予|未|不|没|无)"
    r"(?:被|获|获得|得到|取得|拿到)?"
    r"(?:认定|视为|判定|确认|证明|认可|批准|通过)"
)
_POST_MARKER_STATE_LABELS = ("状态", "身份", "结果")
_MARKER_FAILURE_SUFFIXES = (
    "没有成功",
    "没成功",
    "未成功",
    "失败",
    "未果",
    "无果",
    "落空",
    "作废",
    "失效",
    "无效",
    "泡汤",
    "被拒",
    "遭拒",
)
_DELIVERED_GAIN_ACTIONS = (
    "开放",
    "解锁",
    "授予",
    "发放",
    "获得",
    "取得",
    "拥有",
    "有权限",
    "获准",
    "批准",
    "晋升",
    "升级",
    "提升",
    "到手",
    "生效",
    "可以执行",
)
_POWER_GAIN_TARGETS = (
    "权限",
    "功能",
    "资格",
    "职位",
    "封存操作",
    "复核操作",
    "查验操作",
    "处置权",
)
_SOCIAL_GAIN_ACTIONS = (
    "更新",
    "变更",
    "改为",
    "转为",
    "改口",
    "承认",
    "背书",
    "晋升",
    "提升",
)
_SOCIAL_GAIN_TARGETS = (
    "身份标签",
    "角色标签",
    "职位",
    "称呼",
    "评价",
    "地位",
    "权力排序",
)
_SOCIAL_POSITIVE_RESULTS = (
    "正式",
    "晋升",
    "升任",
    "提升",
    "认可",
    "清白",
    "合格",
    "优先",
    "核心",
    "负责人",
    "管理者",
    "承运者",
    "合作方",
    "成员",
    "代表",
)
_SOCIAL_NEGATIVE_RESULTS = (
    "失信",
    "待审查",
    "待复核",
    "嫌疑",
    "违规",
    "处罚",
    "黑名单",
    "降级",
    "撤职",
    "剥夺",
)
_SOCIAL_NEGATED_RESULT_PREFIXES = (
    "不",
    "未",
    "无",
    "非",
    "不是",
    "并非",
    "不再",
    "不被",
    "未被",
    "不予",
    "未予",
)
_SOCIAL_NEGATED_POSITIVE_RESULTS = tuple(
    f"{prefix}{result}"
    for prefix in _SOCIAL_NEGATED_RESULT_PREFIXES
    for result in _SOCIAL_POSITIVE_RESULTS
)
_SOCIAL_BLOCKING_RESULTS = (
    *_SOCIAL_NEGATIVE_RESULTS,
    *_SOCIAL_NEGATED_POSITIVE_RESULTS,
)
_DENIED_OUTCOME_ACTIONS = (
    "驳回",
    "否决",
    "拒批",
    "退回",
    "不通过",
    "未通过",
)
_LOSS_DIRECTION_ACTIONS = (
    "撤销",
    "取消",
    "剥夺",
    "失去",
    "收回",
    "没收",
    "降低",
    "降级",
    "撤职",
    "丧失",
    "吊销",
    "废除",
    "废止",
    "作废",
    "失效",
    *_DENIED_OUTCOME_ACTIONS,
)
_DIRECTION_RESET_MARKERS = (
    "随后",
    "然后",
    "继而",
    "转而",
    "重新",
    "现已",
    "现在",
    "最终",
    "反而",
    "并",
    "又",
    "却",
    "但",
    "后",
)
_NEGATION_RESET_MARKERS = (
    "但",
    "却",
    "随后",
    "然后",
    "继而",
    "转而",
    "反而",
    "后",
)
_DIRECTION_BLOCKED_PREFIXES = (
    "续",
    "非",
    "不",
    "未",
    "没有",
    "可能",
    "计划",
    "准备",
    "等待",
    "申请",
    "待",
    "拒绝",
    "禁止",
)
_RELIEF_ACTIONS = ("撤销", "解除", "取消", "洗脱", "移除")
_NEGATED_DELIVERY_PREFIXES = (
    "不",
    "未",
    "尚未",
    "仍未",
    "从未",
    "没有",
    "并未",
    "并非",
    "不予",
    "未予",
    "不再",
    "未曾",
    "不得",
    "不可",
    "不准",
    "不许",
    "不允许",
    "未允许",
    "没有允许",
    "未获批准",
    "未获准",
    "未经批准",
    "未经允许",
    "未得到批准",
    "未取得批准",
    "不同意",
    "未同意",
    "没有同意",
    "不会",
    "不能",
    "无法",
)
_ALL_DELIVERY_ACTIONS = tuple(
    dict.fromkeys((*_DELIVERED_GAIN_ACTIONS, *_SOCIAL_GAIN_ACTIONS, *_RELIEF_ACTIONS))
)
_NEGATED_DELIVERY_ACTION_MARKERS = tuple(
    f"{prefix}{passive}{action}"
    for prefix in _NEGATED_DELIVERY_PREFIXES
    for passive in ("", "被")
    for action in _ALL_DELIVERY_ACTIONS
)
_SHORT_NEGATED_DELIVERY_PREFIXES = ("不", "未", "没", "无")
_BOUNDED_NEGATED_DELIVERY_PREFIXES = tuple(
    sorted(
        {
            *_NEGATED_DELIVERY_PREFIXES,
            *(f"{prefix}被" for prefix in _NEGATED_DELIVERY_PREFIXES),
            *_SHORT_NEGATED_DELIVERY_PREFIXES,
        },
        key=len,
        reverse=True,
    )
)
_NEGATED_DELIVERY_BETWEEN_LEADS = (
    "向",
    "为",
    "给",
    "对",
    "由",
    "替",
    "被",
    "获准",
    "获批",
    "获得",
    "得到",
    "取得",
    "拿到",
)
_POWER_RELIEF_TARGETS = ("限制", "封禁", "禁令", "处罚")
_SOCIAL_RELIEF_TARGETS = (
    "责任标记",
    "处罚",
    "追责",
    "指控",
    "嫌疑",
)
_NON_DELIVERY_MARKERS = (
    "未开放",
    "未解锁",
    "未授予",
    "未发放",
    "未获得",
    "未取得",
    "未拥有",
    "未获准",
    "未批准",
    "未晋升",
    "未升级",
    "未提升",
    "未生效",
    "没有权限",
    "没有资格",
    "没有获得",
    "没有取得",
    "没有开放",
    "没有解锁",
    "没有获准",
    "没有批准",
    "不曾获得",
    "不曾取得",
    "不曾开放",
    "尚未",
    "仍未",
    "从未",
    "未能",
    "并非",
    "不是",
    "并没有",
    "未撤销",
    "未解除",
    "未取消",
    "未洗脱",
    "未移除",
    "申请撤销",
    "申请解除",
    "申请取消",
    "申请洗脱",
    "申请移除",
    "不可执行",
    "不能执行",
    "无法执行",
    "无权",
    "拒绝",
    "禁止",
    "询问",
    "是否",
    "能否",
    "如果",
    "若是",
    "可能",
    "计划",
    "准备",
    "等待",
    "申请已提交",
    "提交申请",
    "申请中",
    "待审批",
    "待批准",
    "即将",
    "将会",
    "尚待",
    "待定",
)
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


def _non_delivery_present(body: str) -> bool:
    return (
        _has_any(body, _NON_DELIVERY_MARKERS)
        or _has_any(body, _NEGATED_DELIVERY_ACTION_MARKERS)
        or _bounded_negated_delivery_action_present(body)
    )


def _bounded_negated_delivery_action_present(body: str) -> bool:
    text = str(body or "")
    for action in _ALL_DELIVERY_ACTIONS:
        action_at = text.find(action)
        while action_at >= 0:
            before = text[max(0, action_at - 18) : action_at]
            for prefix in _BOUNDED_NEGATED_DELIVERY_PREFIXES:
                prefix_at = before.rfind(prefix)
                if prefix_at < 0:
                    continue
                between = before[prefix_at + len(prefix) :]
                if len(between) > 8:
                    continue
                approval_carrier = action == "批准" and between in {
                    "获",
                    "获得",
                    "得到",
                    "取得",
                    "拿到",
                }
                if (
                    prefix in _SHORT_NEGATED_DELIVERY_PREFIXES
                    and between
                    and not between.startswith(_NEGATED_DELIVERY_BETWEEN_LEADS)
                    and not approval_carrier
                ):
                    continue
                if _valid_direction_reset_suffix_present(
                    between,
                    reset_markers=_NEGATION_RESET_MARKERS,
                ):
                    continue
                return True
            action_at = text.find(action, action_at + len(action))
    return False


def _valid_direction_reset_suffix_present(
    text: str,
    *,
    reset_markers: tuple[str, ...] = _DIRECTION_RESET_MARKERS,
) -> bool:
    for reset_marker in reset_markers:
        reset_at = text.find(reset_marker)
        while reset_at >= 0:
            suffix = text[reset_at + len(reset_marker) :]
            if len(suffix) <= 8 and not _has_any(
                suffix,
                _DIRECTION_BLOCKED_PREFIXES,
            ):
                return True
            reset_at = text.find(reset_marker, reset_at + len(reset_marker))
    return False


def _boring_setup_ratio(body: str) -> float:
    text = str(body or "")
    if not text:
        return 0.0
    setup_hits = sum(text.count(word) for word in SETUP_WORDS)
    sentence_count = max(1, sum(text.count(mark) for mark in "。！？!?"))
    return round(min(1.0, setup_hits / sentence_count), 3)


def verify_pulp_beats(
    body: str,
    *,
    track: str | None = None,
    reward_tags: tuple[str, ...] = (),
) -> PulpBeatResult:
    text = str(body or "")
    profile = _profile_for(text, track=track)
    result = PulpBeatResult(
        pressure_present=_has_any(text, profile.pressure_words),
        protagonist_action_present=_has_any(text, profile.action_words),
        visible_payoff_present=(
            _profile_payoff_present(text, profile.payoff_words, reward_tags)
            or _planned_reward_payoff_present(text, reward_tags)
        ),
        audience_reaction_present=_has_any(text, profile.audience_words),
        enemy_or_obstacle_damage_present=_has_any(text, profile.damage_words),
        new_gain_or_status_shift_present=_has_any(text, profile.gain_words),
        next_hook_present=_has_any(text[-240:], profile.hook_words),
        boring_setup_ratio=_boring_setup_ratio(text),
    )
    missing = [field for field in CORE_FIELDS if not getattr(result, field)]
    return result.model_copy(update={"missing_fields": missing})


def _profile_payoff_present(
    body: str,
    markers: tuple[str, ...],
    reward_tags: tuple[str, ...],
) -> bool:
    guarded_tags = tuple(
        str(tag) for tag in reward_tags if str(tag) in {"power", "social"}
    )
    if guarded_tags:
        return any(
            _delivered_reward_marker_present(body, markers, reward_tag=tag)
            for tag in guarded_tags
        )
    return _has_any(body, markers)


def _planned_reward_payoff_present(body: str, reward_tags: tuple[str, ...]) -> bool:
    for tag in reward_tags:
        normalized = str(tag)
        if normalized in {"power", "social"}:
            if _delivered_reward_transition_present(body, reward_tag=normalized):
                return True
        markers = REWARD_PAYOFF_MARKERS.get(normalized, ())
        marker_present = (
            _delivered_reward_marker_present(
                body,
                markers,
                reward_tag=normalized,
            )
            if normalized in {"power", "social"}
            else _has_any(body, markers)
        )
        if marker_present:
            return True
        if normalized == "mystery":
            evidence_count = sum(word in body for word in MYSTERY_PAYOFF_EVIDENCE_WORDS)
            if evidence_count >= 2:
                return True
    return False


def _delivered_reward_transition_present(body: str, *, reward_tag: str) -> bool:
    gain_targets = (
        _POWER_GAIN_TARGETS if reward_tag == "power" else _SOCIAL_GAIN_TARGETS
    )
    relief_targets = (
        _POWER_RELIEF_TARGETS if reward_tag == "power" else _SOCIAL_RELIEF_TARGETS
    )
    for span in _reward_transition_spans(
        body,
        include_adjacent=reward_tag == "social",
    ):
        if _non_delivery_present(span):
            continue
        if reward_tag == "power":
            delivered_gain = _delivered_transition_after_loss_present(
                span,
                _DELIVERED_GAIN_ACTIONS,
                gain_targets,
            ) and not _last_reward_marker_failed(
                span,
                (*_DELIVERED_GAIN_ACTIONS, *gain_targets),
            )
        else:
            delivered_gain = (
                _delivered_transition_after_loss_present(
                    span,
                    _SOCIAL_GAIN_ACTIONS,
                    gain_targets,
                )
                and _has_any(span, _SOCIAL_POSITIVE_RESULTS)
                and not _has_any(span, _SOCIAL_BLOCKING_RESULTS)
                and not _last_reward_marker_failed(
                    span,
                    (
                        *_SOCIAL_GAIN_ACTIONS,
                        *gain_targets,
                        *_SOCIAL_POSITIVE_RESULTS,
                    ),
                )
            )
        delivered_relief = _delivered_transition_after_loss_present(
            span,
            _RELIEF_ACTIONS,
            relief_targets,
            blocking_loss_actions=_DENIED_OUTCOME_ACTIONS,
        ) and not _last_reward_marker_failed(
            span,
            (*_RELIEF_ACTIONS, *relief_targets),
        )
        if delivered_gain or delivered_relief:
            return True
    return False


def _delivered_transition_after_loss_present(
    span: str,
    gain_actions: tuple[str, ...],
    gain_targets: tuple[str, ...],
    *,
    blocking_loss_actions: tuple[str, ...] = _LOSS_DIRECTION_ACTIONS,
) -> bool:
    if not (_has_any(span, gain_actions) and _has_any(span, gain_targets)):
        return False
    loss_ends = [
        index + len(action)
        for action in blocking_loss_actions
        if (index := span.rfind(action)) >= 0
    ]
    if not loss_ends:
        return True
    tail = span[max(loss_ends) :]
    for reset_marker in _DIRECTION_RESET_MARKERS:
        reset_at = tail.find(reset_marker)
        while reset_at >= 0:
            reset_tail = tail[reset_at + len(reset_marker) :]
            action_matches = [
                (index, action)
                for action in gain_actions
                if (index := reset_tail.find(action)) >= 0
            ]
            if action_matches:
                action_at, action = min(action_matches)
                prefix = reset_tail[:action_at]
                blocked_prefix = _has_any(prefix, _DIRECTION_BLOCKED_PREFIXES)
                if action_at <= 8 and not blocked_prefix:
                    after_action = reset_tail[action_at + len(action) :]
                    if _has_any(after_action, gain_targets):
                        return True
            reset_at = tail.find(reset_marker, reset_at + len(reset_marker))
    return False


def _reward_transition_spans(
    body: str,
    *,
    include_adjacent: bool = False,
) -> list[str]:
    spans: list[str] = []
    for sentence in _REWARD_SENTENCE_RE.split(str(body or "")):
        clauses = [
            clause.strip()
            for clause in _REWARD_SUBCLAUSE_RE.split(sentence)
            if clause.strip()
        ]
        spans.extend(clauses)
        if include_adjacent:
            spans.extend(
                f"{left}，{right}" for left, right in zip(clauses, clauses[1:])
            )
    return spans


def _marker_has_negative_direction(clause: str, start: int, end: int) -> bool:
    before = clause[max(0, start - _MARKER_CONTEXT_BEFORE) : start]
    after = clause[end : end + _MARKER_CONTEXT_AFTER]
    direct_negation = any(
        before.rstrip().endswith(prefix) for prefix in _MARKER_NEGATION_PREFIXES
    ) or bool(_MARKER_NEGATION_RE.search(before.rstrip()))
    post_negation = bool(
        _MARKER_POST_NEGATION_RE.search(_normalize_post_marker_context(after))
    )
    direct_failure = _failure_suffix_present(after)
    unresolved_loss_before = _has_any(
        before,
        _LOSS_DIRECTION_ACTIONS,
    ) and not _direction_reset_before_outcome_present(before)
    return (
        direct_negation
        or post_negation
        or direct_failure
        or unresolved_loss_before
        or _has_any(after, _LOSS_DIRECTION_ACTIONS)
    )


def _direction_reset_before_outcome_present(before: str) -> bool:
    loss_ends = [
        index + len(action)
        for action in _LOSS_DIRECTION_ACTIONS
        if (index := before.rfind(action)) >= 0
    ]
    if not loss_ends:
        return False
    tail = before[max(loss_ends) :]
    return _valid_direction_reset_suffix_present(tail)


def _failure_suffix_present(after: str) -> bool:
    normalized = _normalize_post_marker_context(after)
    return any(normalized.startswith(suffix) for suffix in _MARKER_FAILURE_SUFFIXES)


def _normalize_post_marker_context(after: str) -> str:
    normalized = after.lstrip("的了仍然最终却但 ")
    for label in _POST_MARKER_STATE_LABELS:
        if normalized.startswith(label):
            return normalized[len(label) :].lstrip("的了仍然最终却但 ")
    return normalized


def _last_reward_marker_failed(span: str, markers: tuple[str, ...]) -> bool:
    latest_end = max(
        (
            index + len(marker)
            for marker in markers
            if (index := span.rfind(marker)) >= 0
        ),
        default=-1,
    )
    if latest_end < 0:
        return False
    return _failure_suffix_present(
        span[latest_end : latest_end + _MARKER_CONTEXT_AFTER]
    )


def _delivered_reward_marker_present(
    body: str,
    markers: tuple[str, ...],
    *,
    reward_tag: str,
) -> bool:
    for clause in _reward_transition_spans(body):
        for marker in markers:
            start = clause.find(marker)
            while marker and start >= 0:
                end = start + len(marker)
                context = clause[
                    max(0, start - _MARKER_CONTEXT_BEFORE) : end + _MARKER_CONTEXT_AFTER
                ]
                blocked_social_result = reward_tag == "social" and _has_any(
                    context,
                    _SOCIAL_BLOCKING_RESULTS,
                )
                if (
                    not _non_delivery_present(context)
                    and not blocked_social_result
                    and not _marker_has_negative_direction(clause, start, end)
                ):
                    return True
                start = clause.find(marker, start + len(marker))
    return False


def pulp_track_for_genre(genre: str) -> str | None:
    normalized = str(genre or "").strip().lower()
    for track, markers in PULP_GENRE_TRACKS:
        if any(marker.lower() in normalized for marker in markers):
            return track
    return None


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
