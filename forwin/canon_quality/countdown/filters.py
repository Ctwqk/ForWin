from __future__ import annotations

import re
from typing import Any


def _looks_like_time_of_day_minute(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 3) : start]
    after = text[end : min(len(text), end + 2)]
    return bool(before) and not before.endswith("计时") and before[-1:] in {"点", "时"} and after[:1] not in {"钟"}


def _looks_like_effect_window(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 18) : start]
    after = text[end : min(len(text), end + 10)]
    if not after.startswith("内"):
        return False
    after_clause = re.split(r"[。！？!?；;]", after, maxsplit=1)[0]
    local = before + after_clause
    if any(keyword in local for keyword in ("倒计时", "剩余", "重置前", "调度窗口", "核心窗口")):
        return False
    return any(keyword in local for keyword in ("崩塌", "扩散", "传播", "传遍", "发酵", "瘫痪", "恢复"))


def _looks_like_local_tactical_window(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if any(
        keyword in clause
        for keyword in (
            "倒计时",
            "剩余",
            "重置",
            "归零",
            "计时",
            "核心窗口",
            "调度窗口",
            "审计窗口",
            "终端审计",
            "授权窗口",
            "档案清理",
            "档案抹除",
        )
    ):
        return False
    if "窗口" in clause and any(
        keyword in clause
        for keyword in (
            "最多还有",
            "至多还有",
            "还能维持",
            "可以维持",
            "搜索范围",
            "换防间隙",
            "封锁",
            "警报",
        )
    ):
        return True
    return any(
        keyword in clause
        for keyword in (
            "争取",
            "拖住",
            "拖延",
            "掩护",
            "排查",
            "抵达",
            "赶到",
            "到达",
            "接触",
            "派遣",
            "巡检员",
            "巡检部队",
            "追兵",
            "封锁圈",
            "搜索范围",
        )
    )


def _looks_like_wall_clock_reading(text: str, start: int, end: int) -> bool:
    raw = str(text[start:end] or "")
    if not re.fullmatch(r"[0-9]{1,2}:[0-9]{2}(?::[0-9]{2})?", raw):
        return False
    parts = [int(part) for part in raw.split(":")]
    if len(parts) >= 2 and parts[1] >= 60:
        return False
    if len(parts) == 3 and parts[2] >= 60:
        return False
    immediate_before = str(text[max(0, start - 18) : start])
    immediate_after = str(text[end : min(len(text), end + 8)])
    for marker in (
        "时间跳到",
        "时间跳至",
        "时间显示",
        "屏幕时间",
        "当前时间",
        "时间是",
        "时间为",
        "时刻是",
        "时刻为",
        "凌晨",
        "上午",
        "下午",
        "晚上",
        "夜里",
    ):
        marker_index = immediate_before.rfind(marker)
        if marker_index < 0:
            continue
        tail = immediate_before[marker_index + len(marker) :]
        if not any(countdown_marker in tail for countdown_marker in ("倒计时", "剩余", "窗口", "计时器")):
            return True
    return bool(
        immediate_before.rstrip().endswith(("时间", "时刻"))
        and immediate_after.startswith(("。", "，", ",", "；", ";"))
    )


def _is_ignored_duration_reference(text: str, start: int, end: int) -> bool:
    return (
        _is_negated_duration_reference(text, start, end)
        or _is_negated_baseline_duration_reference(text, start, end)
        or _is_negated_cycle_extension_reference(text, start, end)
        or _is_retrospective_duration_reference(text, start, end)
        or _is_static_duration_reference(text, start, end)
        or _is_policy_threshold_duration_reference(text, start, end)
        or _is_observation_duration_reference(text, start, end)
        or _is_wait_duration_reference(text, start, end)
        or _is_frequency_duration_reference(text, start, end)
        or _is_countdown_cost_duration_reference(text, start, end)
        or _is_internal_audit_delay_reference(text, start, end)
        or _is_local_memory_erosion_threshold_duration_reference(text, start, end)
        or _is_local_operation_window_duration_reference(text, start, end)
        or _is_tracker_unlock_window_duration_reference(text, start, end)
        or _is_detention_review_window_duration_reference(text, start, end)
        or _is_travel_duration_reference(text, start, end)
        or _is_delta_duration_reference(text, start, end)
        or _is_elapsed_duration_reference(text, start, end)
        or _is_elapsed_to_baseline_duration_reference(text, start, end)
        or _is_hypothetical_completion_duration_reference(text, start, end)
        or _is_hypothetical_protocol_compression_reference(text, start, end)
        or _is_protocol_result_duration_reference(text, start, end)
        or _is_interception_eta_duration_reference(text, start, end)
        or _is_approximate_window_duration_reference(text, start, end)
        or _is_scheduled_remaining_threshold_reference(text, start, end)
        or _is_activation_window_threshold_reference(text, start, end)
        or _is_future_offset_duration_reference(text, start, end)
        or _is_local_operation_eta_duration_reference(text, start, end)
        or _is_action_deadline_duration_reference(text, start, end)
        or _is_decision_deadline_duration_reference(text, start, end)
        or _is_access_token_validity_duration_reference(text, start, end)
        or _is_hypothetical_letter_day_reference(text, start, end)
        or _is_generic_remaining_time_restatement(text, start, end)
    )


def _is_negated_duration_reference(text: str, start: int, end: int) -> bool:
    before = text[max(0, start - 8) : start]
    local = text[max(0, start - 8) : min(len(text), end + 4)]
    if "不是还有" in local or "不是还剩" in local:
        return False
    clause = _current_clause(text, start, end)
    clause_offset = start - max(text.rfind(marker, 0, start) for marker in ("。", "！", "？", "；", "\n")) - 1
    prefix = clause[: max(0, clause_offset)]
    return (
        before.endswith(("不是", "并非", "绝非", "不再是", "而不是", "也不是"))
        or "不是之前说的" in prefix
        or "不是原本说的" in prefix
        or prefix.rstrip().endswith(("也不是", "并非", "而不是"))
    )


def _is_negated_baseline_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before_in_clause = clause[: max(0, start - _clause_start_index(text, start))]
    return (
        "不是" in clause
        and any(marker in clause for marker in ("这一次", "本次", "而是", "是"))
        and before_in_clause.rstrip().endswith(("是", "而是", "而是："))
    )


def _is_negated_cycle_extension_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before_in_clause = clause[: max(0, start - _clause_start_index(text, start))]
    if any(keyword in before_in_clause for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return any(marker in before_in_clause for marker in ("不会", "不再", "不得", "不能")) and any(
        marker in before_in_clause
        for marker in (
            "重启成",
            "重置成",
            "跳回",
            "扩展到",
            "延长到",
            "归零重启成",
            "触发新的",
        )
    )


def _is_retrospective_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    after = text[end : min(len(text), end + 4)]
    if before.endswith(("现在只剩", "现在还剩", "此刻只剩", "此刻还剩")):
        return False
    if any(marker in clause for marker in ("一直以为", "以为还有")) and "时间" in clause:
        return True
    if "昏迷前" in clause and any(marker in clause for marker in ("倒计时", "计时器", "还有", "还剩", "显示")):
        return True
    if before.endswith(("昨天的", "标准")):
        return True
    if any(marker in clause for marker in ("记得", "回忆")) and any(
        marker in clause
        for marker in (
            "看到的是",
            "显示的是",
            "看到的时间",
            "看到的倒计时",
            "看到的终端显示的是",
        )
    ):
        return True
    if any(marker in clause for marker in ("说过", "曾说", "曾经说", "告诉过")) and any(
        marker in clause for marker in ("会在", "将在", "以内", "内触发", "触发", "启动")
    ):
        return True
    if any(marker in clause for marker in ("上一次", "上一回", "上一轮", "上回")) and "还是" in clause:
        return True
    if "上次" in clause and any(marker in clause for marker in ("看到", "还有", "还剩", "显示")):
        return True
    if any(
        marker in clause
        for marker in ("上周", "上一次", "上一回", "上一轮", "上回", "之前", "此前", "原本", "过去", "当时", "昨天")
    ):
        if any(
            marker in clause
            for marker in ("怎么会", "怎么可能", "那时", "那会", "内", "前", "后", "说的", "写的是", "还有", "还是", "压缩")
        ):
            return True
    return after.startswith(("前", "后", "以前", "之后")) and not any(
        keyword in clause for keyword in ("倒计时", "剩余", "还剩", "只剩", "距离", "窗口", "关闭")
    )


def _is_static_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时显示", "显示", "跳到", "跳至")):
        return False
    return (
        any(marker in before for marker in ("固定", "默认", "标准", "周期长度", "窗口长度"))
        and any(marker in clause for marker in ("不是固定", "并非固定", "并不是固定", "会根据", "取决于", "缩短", "延长"))
    )


def _is_policy_threshold_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return (
        (
            any(marker in clause for marker in ("从未", "不会", "通常", "正常状态", "公开的", "校准频率"))
            and any(marker in clause for marker in ("少于", "多于", "超过", "小于", "大于", "低于", "高于"))
            and any(marker in clause for marker in ("启动", "触发", "校准程序", "频率", "规则"))
        )
        or (
            any(marker in clause for marker in ("低于", "高于", "少于", "超过"))
            and any(marker in clause for marker in ("规则", "意味着", "阶段", "阈值"))
            and any(marker in clause for marker in ("重置周期", "系统", "校准", "主控", "扫描"))
        )
        or (
            any(marker in clause for marker in ("低于", "高于", "少于", "超过"))
            and any(marker in clause for marker in ("启动", "触发", "进入"))
            and any(marker in clause for marker in ("重置周期", "系统", "校准", "主控", "扫描"))
        )
        or (
            any(marker in clause for marker in ("不会", "从来不会", "通常", "正常"))
            and any(marker in clause for marker in ("短于", "低于", "少于", "小于"))
            and any(marker in clause for marker in ("维护周期", "维护窗口", "普通维护"))
        )
        or (
            any(marker in clause for marker in ("应急加速协议", "标准", "逐级缩减", "最终压缩"))
            and any(marker in clause for marker in ("压缩", "缩减", "以内", "协议"))
            and not any(marker in before for marker in ("现在的", "当前", "此刻", "实际"))
        )
        or (
            any(marker in clause for marker in ("必须按", "需要按", "只能按", "应按"))
            and any(marker in clause for marker in ("以下处理", "以下", "以内处理"))
        )
    )


def _is_observation_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return any(marker in clause for marker in ("观察", "留观", "监测")) and any(
        marker in clause for marker in ("生命体征", "伤口", "医疗", "医用", "建议继续", "继续观察")
    )


def _is_wait_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return any(marker in clause for marker in ("要等", "需要等", "至少要等", "等待", "再等")) and any(
        marker in clause for marker in ("下一次", "下次", "打开", "开启", "恢复")
    )


def _is_frequency_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if "间隔" in clause and any(marker in clause for marker in ("巡逻", "换岗", "轮班", "刷新", "切换")):
        return True
    return "频率" in clause and ("每" in clause or "一次" in clause)


def _is_hypothetical_letter_day_reference(text: str, start: int, end: int) -> bool:
    raw = text[start:end]
    if raw not in {"一天", "一日"}:
        return False
    clause = _current_clause(text, start, end)
    clause_start = _clause_start_index(text, start)
    before_in_clause = clause[: max(0, start - clause_start)].rstrip()
    after_in_clause = clause[max(0, end - clause_start) : max(0, end - clause_start) + 32]
    if not before_in_clause.endswith(("如果有", "若有", "倘若有", "假如有")):
        return False
    return any(
        marker in after_in_clause
        for marker in (
            "看到这段话",
            "读到这段话",
            "看到这封信",
            "读到这封信",
            "收到这封信",
            "看到这些字",
            "读到这些字",
        )
    )


def _is_delta_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    after = text[end : min(len(text), end + 12)]
    if "从" in before and any(marker in after for marker in ("降到", "降至", "直接降", "缩短到", "压到")):
        return True
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return any(marker in clause for marker in ("少了", "减少了", "缩短了", "又少了", "又减少"))


def _is_countdown_cost_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 18) : start].rstrip()
    local = text[max(0, start - 48) : min(len(text), end + 48)]
    if not any(
        marker in local
        for marker in (
            "倒计时",
            "剩余时间",
            "重置窗口",
            "记忆重置",
            "重置周期",
            "记忆片段",
            "记忆样本",
            "失去",
            "剥离",
        )
    ):
        return False
    if not any(
        marker in clause
        for marker in (
            "扣除",
            "扣掉",
            "扣减",
            "消耗",
            "耗费",
            "代价",
            "付出",
            "失去",
            "损失",
            "牺牲",
        )
    ):
        return False
    return not before.endswith(
        (
            "显示",
            "显示：",
            "显示:",
            "剩余：",
            "剩余:",
            "还剩",
            "只剩",
            "跳到",
            "跳至",
            "降到",
            "降至",
        )
    )


def _is_internal_audit_delay_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "计时器", "窗口")):
        return False
    return (
        "审计" in clause
        and any(marker in clause for marker in ("触发", "启动", "开始", "激活"))
        and any(marker in clause for marker in ("后", "将在", "会在"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "全域记忆", "熔铸倒计时"))
    )


def _is_local_memory_erosion_threshold_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "计时器", "显示", "跳到", "跳至")):
        return False
    return (
        any(marker in clause for marker in ("超过", "多于", "停留", "每在", "每停留"))
        and any(marker in clause for marker in ("记忆剥离", "短期记忆", "自己是谁", "第三层", "局部记忆"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "全域记忆", "熔铸倒计时"))
    )


def _is_detention_review_window_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("倒计时", "记忆重置", "重置周期", "距离")):
        return False
    return (
        any(marker in clause for marker in ("审查窗口", "审查时限", "审查时间"))
        and any(marker in clause for marker in ("预计", "关押", "捕获", "押送", "监室", "牢房", "隔离"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "全域记忆", "熔铸倒计时"))
    )


def _is_travel_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("倒计时", "剩余", "还剩", "只剩", "重置")):
        return False
    return (
        "距离" in clause
        and any(marker in clause for marker in ("需要", "至少需要", "耗时", "路程", "赶到", "抵达", "到达"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "倒计时", "归零"))
    )


def _is_elapsed_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    if before.rstrip().endswith(("流逝了", "又流逝了", "过去了", "又过去了", "耗费了", "用了")):
        return True
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return any(
        marker in clause
        for marker in (
            "已经过去",
            "过去了",
            "已经持续",
            "已持续",
            "持续了",
            "持续",
            "耗时",
            "路上用了",
            "昏迷期间",
            "昏迷了",
            "昏过去",
            "失去意识",
        )
    )


def _is_elapsed_to_baseline_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    clause_start = _clause_start_index(text, start)
    relative_end = max(0, end - clause_start)
    after_mention = clause[relative_end:]
    return (
        any(marker in after_mention for marker in ("流逝到", "流逝至", "降到", "降至", "缩到", "缩至", "变成"))
        and any(marker in clause for marker in ("倒计时", "重置周期", "剩余时间"))
        and not str(clause[: max(0, start - clause_start)]).rstrip().endswith(("剩余", "还剩", "只剩", "显示", "跳到", "跳至"))
    )


def _is_hypothetical_completion_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    after = text[end : min(len(text), end + 8)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return (
        any(marker in clause for marker in ("如果", "可能", "预计", "预估"))
        and after.startswith("内")
        and any(marker in clause for marker in ("完成", "结束", "归零"))
    )


def _is_hypothetical_protocol_compression_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    local = clause
    return (
        any(marker in local for marker in ("如果", "异常", "否则", "强制启动"))
        and "协议" in local
        and any(marker in local for marker in ("压缩到", "压缩至", "压到", "缩短到", "缩短至"))
        and any(marker in local for marker in ("所有剩余时间", "剩余时间", "整个记忆重置周期"))
        and not any(marker in clause for marker in ("当前", "此刻", "现在"))
    )


def _is_protocol_result_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 12) : start]
    local = text[max(0, start - 80) : min(len(text), end + 80)]
    if any(keyword in before for keyword in ("当前", "剩余", "还剩", "只剩", "倒计时", "距离")):
        return False
    return (
        "协议" in local
        and any(marker in clause for marker in ("激活后", "执行后", "将", "会"))
        and any(marker in clause for marker in ("缩短至", "缩短到", "重启", "恢复基准"))
    )


def _is_interception_eta_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    if any(keyword in before for keyword in ("倒计时", "记忆重置", "重置周期", "计时器")):
        return False
    return (
        any(marker in clause for marker in ("拦截时间", "拦截抵达", "抵达时间", "赶到时间", "巡检员抵达时间"))
        and any(marker in clause for marker in ("预计", "剩余", "将在", "还需"))
        and not any(marker in clause for marker in ("记忆重置", "重置周期", "全域记忆", "熔铸倒计时"))
    )


def _is_approximate_window_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 24) : start]
    if any(keyword in before for keyword in ("显示", "跳到", "跳至", "还剩", "只剩")) and "窗口" not in before:
        return False
    return (
        any(marker in before for marker in ("约", "大约", "大概", "左右"))
        and any(marker in clause for marker in ("有效期", "权限", "密钥", "救援", "恢复路径"))
        and not any(marker in clause for marker in ("手腕", "腕表", "计时器显示", "倒计时显示"))
    )


def _is_scheduled_remaining_threshold_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 24) : start]
    after = text[end : min(len(text), end + 12)]
    if not before.rstrip().endswith(("倒计时剩余", "重置周期剩余", "记忆重置剩余")):
        return False
    return after.lstrip().startswith("时") and any(marker in clause for marker in ("将在", "将", "会")) and any(
        marker in clause for marker in ("留下", "接头", "出现", "发送", "启动", "触发", "转移", "关闭")
    )


def _is_activation_window_threshold_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 18) : start]
    after = text[end : min(len(text), end + 12)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return (
        "最后" in before
        and any(marker in (after + clause) for marker in ("窗口", "内激活", "内启动", "内完成", "内触发"))
        and (
            any(marker in clause for marker in ("激活", "启动", "触发", "锚点", "验证", "校准"))
            or ("窗口" in clause and any(marker in clause for marker in ("也就是", "某个时刻")))
        )
    )


def _is_future_offset_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 16) : start]
    after = text[end : min(len(text), end + 16)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return after.lstrip().startswith("后") and any(
        marker in clause for marker in ("也就是", "某个时刻", "执行前", "激活前", "触发前")
    )


def _is_action_deadline_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 18) : start]
    after = text[end : min(len(text), end + 8)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return (
        after.lstrip().startswith("内")
        and any(marker in clause for marker in ("必须", "需要", "要在", "只能在", "来得及"))
        and any(
            marker in clause
            for marker in (
                "离开",
                "赶到",
                "抵达",
                "到达",
                "完成",
                "进入",
                "撤离",
                "汇合",
                "验证",
                "救出",
                "拿到",
                "取得",
            )
        )
    )


def _is_decision_deadline_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    clause_start = _clause_start_index(text, start)
    before = clause[: max(0, start - clause_start)]
    after = text[end : min(len(text), end + 12)]
    if any(keyword in before for keyword in ("剩余", "还剩", "只剩", "显示", "跳到", "跳至")):
        return False
    return (
        any(marker in before for marker in ("你有", "给你", "给了你", "还有"))
        and any(marker in after + clause for marker in ("考虑", "答复", "回答", "答案", "选择"))
    )


def _is_access_token_validity_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if any(marker in clause for marker in ("记忆重置", "重置周期", "终端审计", "审计窗口")):
        return False
    return any(marker in clause for marker in ("临时访问令牌", "访问令牌", "令牌有效", "权限有效", "有效时间"))


def _is_local_operation_window_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if any(marker in clause for marker in ("记忆重置", "记忆熔铸", "全域记忆", "熔铸倒计时")):
        return False
    return any(
        marker in clause
        for marker in (
            "操作窗口",
            "局部倒计时",
            "后门关闭",
            "后门已启用",
            "数据板上的倒计时",
            "救援窗口",
            "隔离舱",
            "刷新间隙",
            "远程开锁",
            "救援条件",
        )
    )


def _is_tracker_unlock_window_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    if any(marker in clause for marker in ("记忆重置", "记忆熔铸", "全域记忆", "熔铸倒计时")):
        return False
    nearby = text[max(0, start - 96) : min(len(text), end + 96)]
    tracker_markers = (
        "解除操作",
        "解除程序",
        "解除协议",
        "解除点",
        "追踪器解除",
        "追踪器管理",
        "追踪器的屏蔽窗口",
        "追踪器屏蔽窗口",
        "信号屏蔽",
        "屏蔽窗口",
        "本地网络只能维持这个窗口",
        "被植入者进入指定解除点",
        "目标追踪器",
    )
    return any(
        marker in nearby
        for marker in tracker_markers
    ) and any(marker in nearby for marker in ("窗口", "剩余", "不到", "还剩", "维持", "恢复定位", "操作时间"))


def _is_local_operation_eta_duration_reference(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before = text[max(0, start - 24) : start]
    nearby = text[max(0, start - 96) : min(len(text), end + 32)]
    if any(marker in clause for marker in ("记忆重置", "重置周期", "记忆熔铸", "熔铸协议", "熔铸倒计时", "全域记忆")):
        return False
    if before.rstrip().endswith(("倒计时", "倒计时还剩", "倒计时剩余", "剩余", "还剩", "只剩")):
        return False
    operation_markers = (
        "注入",
        "写入",
        "读取",
        "上传",
        "下载",
        "传输",
        "解码",
        "认证",
        "验证",
        "扫描",
        "同步",
        "加载",
        "执行",
        "生成",
        "破解",
        "门禁",
    )
    if any(marker in nearby for marker in ("预计时间", "预计用时", "预计剩余时间")) and any(
        marker in nearby for marker in operation_markers
    ):
        return True
    return (
        any(marker in clause for marker in ("预计剩余时间", "预计还需", "预计需要", "进度条", "进度", "剩余操作时间"))
        and any(marker in clause for marker in operation_markers)
    )


def _bridge_mentions_competing_countdown(text: str) -> bool:
    bridge = str(text or "")
    if any(
        keyword in bridge
        for keyword in (
            "终端审计",
            "审计窗口",
            "授权窗口",
            "档案清理",
            "公开数据",
            "公开窗口",
            "对外数据",
            "救援窗口",
            "隔离舱",
            "刷新间隙",
            "追踪器解除",
            "解除窗口",
            "屏蔽窗口",
            "信号屏蔽",
            "剩余操作时间",
        )
    ):
        return True
    return False


def _is_generic_remaining_time_restatement(text: str, start: int, end: int) -> bool:
    clause = _current_clause(text, start, end)
    before_in_clause = clause[: max(0, start - _clause_start_index(text, start))]
    prefix = before_in_clause.rstrip()
    subject_match = re.search(
        r"(他|她|它|他们|她们|两人|二人|[\u4e00-\u9fff]{2,4})(?:还)?(?:有|剩|只剩)不(?:到|足)$",
        prefix,
    )
    if not subject_match:
        return False
    subject = subject_match.group(1)
    if subject not in {"他", "她", "它", "他们", "她们", "两人", "二人"} and any(
        marker in subject for marker in ("重置", "倒计时", "距离", "剩余")
    ):
        return False
    if any(marker in clause for marker in ("窗口", "审计", "档案清理", "档案抹除", "授权")):
        return False
    nearby_before = text[max(0, start - 140) : start]
    return any(
        marker in nearby_before
        for marker in ("记忆重置倒计时", "记忆重置剩余", "重置倒计时", "重置周期剩余")
    )


def _current_clause(text: str, start: int, end: int) -> str:
    left = _clause_start_index(text, start) - 1
    right_candidates = [idx for marker in ("。", "！", "？", "；", "\n") if (idx := text.find(marker, end)) >= 0]
    right = min(right_candidates) if right_candidates else len(text)
    return text[left + 1 : right]


def _clause_start_index(text: str, start: int) -> int:
    return max(text.rfind(marker, 0, start) for marker in ("。", "！", "？", "；", "\n")) + 1


def _is_reset_context(text: str) -> bool:
    lowered = str(text or "").lower()
    if any(
        keyword in lowered
        for keyword in (
            "倒计时重置",
            "倒计时被重置",
            "重新计时",
            "新的倒计时",
            "新倒计时",
            "clock reset",
            "timer reset",
        )
    ):
        return True
    return False


def _is_countdown_context(text: str) -> bool:
    context = str(text or "")
    return any(keyword in context for keyword in ("倒计时", "剩余", "距离", "归零", "计时", "重置", "窗口", "调度窗口"))


def _is_resolution_context(text: str) -> bool:
    return _text_has_countdown_resolution(str(text or ""))


def _text_has_countdown_resolution(text: str) -> bool:
    context = str(text or "")
    context = context.replace("倒计时结束前", "").replace("重置周期结束前", "")
    if any(
        keyword in context
        for keyword in (
            "归零",
            "倒计时解除",
            "倒计时结束",
            "倒计时归零",
            "危机解除",
            "记忆重置系统失效",
            "记忆重置系统已经失效",
            "系统关闭",
            "系统已关闭",
            "记忆重置停止",
            "记忆重置被阻止",
            "记忆重置已取消",
            "重置程序终止",
            "重置程序已终止",
            "重置周期被阻止",
            "重置周期终止",
            "重置周期已终止",
            "重置周期被永久终止",
            "重置周期将被永久终止",
            "记忆重置周期被永久终止",
            "记忆重置周期将被永久终止",
            "记忆重置周期永久终止",
            "不再有记忆重置",
            "无法再进行下一次记忆重置",
        )
    ):
        return True
    if any(keyword in context for keyword in ("重置周期结束", "重置周期结束了")) and any(
        keyword in context
        for keyword in (
            "没有人会忘记",
            "记忆没有被抹去",
            "无人遗忘",
            "旧城终于自由",
            "旧城，终于自由",
            "被抹除的记忆",
        )
    ):
        return True
    return False


def _has_upper_bound_prefix(text: str, start: int) -> bool:
    before = text[max(0, start - 4) : start]
    return before.endswith(("不到", "不超过", "至多"))


def _is_rounding_equivalent(
    raw: str,
    previous_minutes: int,
    minutes: int,
    *,
    is_upper_bound: bool = False,
    context: str = "",
) -> bool:
    if is_upper_bound and minutes > previous_minutes:
        return minutes - previous_minutes <= max(10, int(previous_minutes * 0.2))
    if minutes - previous_minutes > 1:
        return _is_coarse_threshold_reference(raw, previous_minutes, minutes, context=context)
    if minutes > previous_minutes and any(marker in str(context or "") for marker in ("约", "大约", "左右", "不到", "不超过")):
        return True
    if 0 < minutes - previous_minutes <= 1 and any(marker in str(context or "") for marker in ("只有", "约", "大约", "左右")):
        return True
    mention = str(raw or "")
    return "小时" in mention and not any(unit in mention for unit in ("分钟", "分", ":"))


def _is_coarse_threshold_reference(raw: str, previous_minutes: int, minutes: int, *, context: str = "") -> bool:
    mention = str(raw or "")
    local = str(context or "")
    if minutes <= previous_minutes or minutes - previous_minutes > 5:
        return False
    if minutes % 10 != 0:
        return False
    if "分钟" not in mention and "分" not in mention:
        return False
    return any(
        marker in local
        for marker in (
            "不到",
            "不足",
            "以内",
            "最多",
            "缩短到",
            "压缩到",
            "缩减到",
            "逼近",
        )
    )


__all__ = [
    '_looks_like_time_of_day_minute',
    '_looks_like_effect_window',
    '_looks_like_local_tactical_window',
    '_looks_like_wall_clock_reading',
    '_is_ignored_duration_reference',
    '_is_negated_duration_reference',
    '_is_negated_baseline_duration_reference',
    '_is_negated_cycle_extension_reference',
    '_is_retrospective_duration_reference',
    '_is_static_duration_reference',
    '_is_policy_threshold_duration_reference',
    '_is_observation_duration_reference',
    '_is_wait_duration_reference',
    '_is_frequency_duration_reference',
    '_is_hypothetical_letter_day_reference',
    '_is_delta_duration_reference',
    '_is_countdown_cost_duration_reference',
    '_is_internal_audit_delay_reference',
    '_is_local_memory_erosion_threshold_duration_reference',
    '_is_detention_review_window_duration_reference',
    '_is_travel_duration_reference',
    '_is_elapsed_duration_reference',
    '_is_elapsed_to_baseline_duration_reference',
    '_is_hypothetical_completion_duration_reference',
    '_is_hypothetical_protocol_compression_reference',
    '_is_protocol_result_duration_reference',
    '_is_interception_eta_duration_reference',
    '_is_approximate_window_duration_reference',
    '_is_scheduled_remaining_threshold_reference',
    '_is_activation_window_threshold_reference',
    '_is_future_offset_duration_reference',
    '_is_action_deadline_duration_reference',
    '_is_decision_deadline_duration_reference',
    '_is_access_token_validity_duration_reference',
    '_is_local_operation_window_duration_reference',
    '_is_tracker_unlock_window_duration_reference',
    '_is_local_operation_eta_duration_reference',
    '_bridge_mentions_competing_countdown',
    '_is_generic_remaining_time_restatement',
    '_current_clause',
    '_clause_start_index',
    '_is_reset_context',
    '_is_countdown_context',
    '_is_resolution_context',
    '_text_has_countdown_resolution',
    '_has_upper_bound_prefix',
    '_is_rounding_equivalent',
    '_is_coarse_threshold_reference',
]
