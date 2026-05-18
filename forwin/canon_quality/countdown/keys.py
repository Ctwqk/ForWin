from __future__ import annotations

import re
from typing import Any

from .filters import _clause_start_index, _current_clause


def _prefer_active_memory_reset_key(countdown_key: str, minutes: int, previous_by_key: dict[str, int]) -> str:
    if countdown_key != "main":
        return countdown_key
    memory_previous = previous_by_key.get("memory_reset")
    main_previous = previous_by_key.get("main")
    if memory_previous is None:
        return countdown_key
    if main_previous is None:
        return "memory_reset"
    if main_previous < memory_previous and minutes > main_previous:
        return "memory_reset"
    return countdown_key


def _prefer_short_clock_continuation_key(
    countdown_key: str, raw: str, previous_by_key: dict[str, int], context: str
) -> str:
    match = re.fullmatch(r"([0-9]{2,3}):([0-9]{2})", str(raw or ""))
    if not match:
        return countdown_key
    if _short_clock_has_explicit_key_label(countdown_key, context):
        return countdown_key
    first = int(match.group(1))
    second = int(match.group(2))
    if first >= 60 or second >= 60:
        return countdown_key
    compatible_keys: list[tuple[int, int, str]] = []
    for order, candidate_key in enumerate(
        ("terminal_audit_window", "memory_reset", "core_access_window", "archive_cleanup", "main")
    ):
        candidate_previous = previous_by_key.get(candidate_key)
        if candidate_previous is not None and candidate_previous <= 180 and first <= candidate_previous:
            compatible_keys.append((candidate_previous - first, order, candidate_key))
    if compatible_keys:
        compatible_keys.sort()
        return compatible_keys[0][2]
    return countdown_key


def _short_clock_has_explicit_key_label(countdown_key: str, context: str) -> bool:
    labels = {
        "memory_reset": (
            "记忆重置窗口",
            "记忆重置倒计时",
            "主线清理倒计时",
            "主线清理",
            "重置窗口",
            "重置倒计时",
            "记忆熔铸倒计时",
        ),
        "terminal_audit_window": ("终端审计窗口", "终端审计倒计时"),
        "archive_cleanup": (
            "档案抹除倒计时",
            "档案清理倒计时",
            "抹除倒计时",
            "授权窗口",
            "档案审计窗口",
        ),
        "core_access_window": ("核心层入口", "核心层授权窗口", "核心层的授权窗口", "入口关闭倒计时"),
    }
    return any(label in str(context or "") for label in labels.get(countdown_key, ()))


def _normalize_ambiguous_clock_minutes(
    *,
    raw: str,
    minutes: int,
    previous_minutes: int | None,
    countdown_key: str,
    context: str,
) -> int:
    two_part_match = re.fullmatch(r"([0-9]{2,3}):([0-9]{2})", str(raw or ""))
    if two_part_match:
        first = int(two_part_match.group(1))
        second = int(two_part_match.group(2))
        if (
            second < 60
            and first < 60
            and previous_minutes is not None
            and previous_minutes <= 180
            and first <= previous_minutes
        ):
            return first
        if (
            second < 60
            and first < 60
            and previous_minutes is None
            and _explicit_minute_second_countdown_context(countdown_key, context)
        ):
            return first
        if first >= 60 and previous_minutes is not None and previous_minutes <= 180 and first <= previous_minutes:
            return first
        return minutes
    match = re.fullmatch(r"([0-9]{2,3}):([0-9]{2}):([0-9]{2})", str(raw or ""))
    if not match:
        return minutes
    first = int(match.group(1))
    if first < 60:
        return minutes
    if (
        previous_minutes is None
        and first <= 180
        and _explicit_minute_second_countdown_context(countdown_key, context)
        and ("分钟" in str(context or "") or "分" in str(context or ""))
    ):
        return first
    if previous_minutes is not None and previous_minutes <= 180 and first <= previous_minutes:
        return first
    if countdown_key == "memory_reset" and first <= 180 and "分钟" in str(context or "") and "小时" not in str(context or ""):
        return first
    return minutes


def _explicit_minute_second_countdown_context(countdown_key: str, context: str) -> bool:
    local = str(context or "")
    if countdown_key not in {"main", "memory_reset", "terminal_audit_window", "archive_cleanup", "core_access_window"}:
        return False
    if any(marker in local for marker in ("小时", "时后", "时内", "时启动", "时关闭")):
        return False
    return any(marker in local for marker in ("倒计时", "计时器", "剩余", "跳到", "跳至", "归零"))


def _non_monotonic_repair_hint(*, countdown_key: str, raw: str, previous_minutes: int, minutes: int) -> str:
    if countdown_key == "terminal_audit_window":
        return (
            f"不要把同一个终端审计窗口从 {previous_minutes} 分钟延长到 {raw}。"
            "删除或改写这个数字，使终端审计窗口继续小于等于前值；如果这是档案清理或核心授权窗口，"
            "必须明确命名为不同倒计时。"
        )
    if countdown_key == "archive_cleanup":
        return (
            f"不要把同一个终端审计/授权窗口从 {previous_minutes} 分钟延长到 {raw}。"
            "删除或改写这个数字，使审计窗口继续小于等于前值；如果要写七天记忆重置，"
            "必须明确它是 memory_reset 另一个倒计时，不能继续称为终端审计窗口。"
        )
    if countdown_key == "memory_reset":
        return (
            f"不要把同一个记忆重置周期从 {previous_minutes} 分钟延长到 {raw}。"
            "只能写剩余时间继续减少；若发生 reset，必须用明确重置事件解释。"
        )
    return (
        f"不要把同一个倒计时从 {previous_minutes} 分钟延长到 {raw}。"
        "删除或改写该数字，让剩余时间单调减少；若这是新时钟，必须明确命名为不同倒计时。"
    )


def _countdown_key_for_mention(text: str, start: int, end: int) -> str:
    before_context = str(text[max(0, start - 160) : end])
    after_context = _current_clause(text, start, end)
    immediate_after = re.split(r"[。！？!?；;\n]", str(text[end : min(len(text), end + 24)]), maxsplit=1)[0]
    near_after = str(text[end : min(len(text), end + 80)])
    next_clause_after = re.split(
        r"[。！？!?；;\n]",
        str(text[end : min(len(text), end + 80)]).lstrip("。！？!?；;\n "),
        maxsplit=1,
    )[0]
    public_markers = (
        "公开数据",
        "公开窗口",
        "对外数据",
        "对外公布",
        "公布数据",
        "公共时间",
        "心理缓冲",
        "普通市民",
    )
    if any(marker in after_context for marker in ("不是还有", "不是还剩")) and any(
        keyword in near_after for keyword in public_markers
    ):
        return "public_countdown"
    if any(keyword in immediate_after for keyword in public_markers):
        return "public_countdown"
    if any(keyword in near_after for keyword in public_markers) and any(
        marker in after_context for marker in ("上一次", "上一回", "上一轮", "上回", "之前", "此前", "预告")
    ):
        return "public_countdown"
    if any(keyword in after_context for keyword in public_markers):
        return "public_countdown"
    local_before = str(text[max(0, start - 48) : start])
    local_after = str(text[end : min(len(text), end + 56)])
    local_after_label = re.split(r"[。！？!?；;\n]", local_after, maxsplit=1)[0]
    local_memory_markers = (
        "主线清理倒计时",
        "主线清理",
        "记忆重置",
        "重置记忆",
        "重置一次记忆",
        "重置周期",
        "记忆熔铸",
        "熔铸倒计时",
        "记忆剥离",
        "剥离倒计时",
        "全域记忆归零",
        "记忆覆盖",
        "覆盖进程",
        "覆盖协议",
    )
    local_core_access_markers = ("核心层入口", "核心层的授权窗口", "核心层授权窗口", "入口关闭")
    local_terminal_audit_markers = ("终端审计窗口", "终端审计")
    local_archive_markers = (
        "核心层入口",
        "入口关闭",
        "授权窗口",
        "审计窗口",
        "档案清理",
        "档案抹除",
        "抹除令",
        "抹除倒计时",
        "档案记录",
    )
    local_after_key = _nearest_countdown_key(local_after_label)
    if (
        local_after_key in {"memory_reset", "core_access_window", "terminal_audit_window", "archive_cleanup"}
        and _after_label_binds_to_mention(local_after)
    ):
        return local_after_key
    current_clause = _current_clause(text, start, end)
    clause_start = _clause_start_index(text, start)
    current_clause_before = str(text[clause_start:start])
    explicit_current_clause_key = _explicit_countdown_label_key(current_clause_before)
    if explicit_current_clause_key:
        return explicit_current_clause_key
    if (
        any(marker in current_clause for marker in ("手腕", "左腕", "右腕", "腕表"))
        and any(marker in current_clause for marker in ("倒计时", "计时器", "数字", "显示"))
        and ":" in str(text[start:end])
        and any(marker in next_clause_after for marker in ("终端审计窗口", "终端审计倒计时"))
    ):
        return "terminal_audit_window"
    if (
        any(marker in current_clause for marker in ("手腕", "左腕", "右腕", "腕表"))
        and any(marker in current_clause for marker in ("倒计时", "计时器", "数字", "显示"))
    ):
        return "memory_reset"
    explicit_before_key = _explicit_countdown_label_key(local_before)
    if explicit_before_key:
        return explicit_before_key
    local_before_memory_index = max(local_before.rfind(keyword) for keyword in local_memory_markers)
    local_before_core_index = max(local_before.rfind(keyword) for keyword in local_core_access_markers)
    local_before_auth_index = local_before.rfind("授权窗口")
    if local_before_core_index >= 0 and local_before_core_index >= local_before_memory_index:
        return "core_access_window"
    if local_before_auth_index >= 0 and local_before_auth_index >= local_before_memory_index:
        return "archive_cleanup"
    local_key_context = local_before + after_context
    if "倒计时" in local_key_context and not any(
        marker in local_key_context
        for marker in (
            "终端审计",
            "授权窗口",
            "授权码",
            "档案清理",
            "档案抹除",
            "抹除令",
            "抹除倒计时",
            "记录群组",
            "审计日志",
            "限制级信息",
            "核心层入口",
            "公开数据",
            "公开窗口",
            "对外数据",
            "记忆重置",
            "重置记忆",
            "重置一次记忆",
            "重置周期",
            "记忆熔铸",
            "熔铸倒计时",
            "记忆剥离",
            "剥离倒计时",
            "全域记忆归零",
            "记忆覆盖",
            "覆盖进程",
            "覆盖协议",
            "记忆清除",
            "系统级威胁",
            "威胁评级",
            "追踪协议",
        )
    ):
        return "main"
    local_before_key = _nearest_countdown_key(local_before)
    if local_before_key in {"memory_reset", "core_access_window", "terminal_audit_window", "archive_cleanup"}:
        return local_before_key
    if local_after_key in {"memory_reset", "core_access_window", "terminal_audit_window", "archive_cleanup"}:
        return local_after_key
    if any(keyword in near_after for keyword in local_core_access_markers) and not any(
        keyword in (local_before + after_context) for keyword in local_memory_markers
    ):
        return "core_access_window"
    if any(keyword in near_after for keyword in local_archive_markers) and not any(
        keyword in (local_before + after_context) for keyword in local_memory_markers
    ):
        return "archive_cleanup"
    if _looks_like_forced_memory_calibration_context(before_context + after_context + immediate_after):
        return "memory_reset"
    key = _nearest_countdown_key(before_context)
    if key:
        return key
    key = _nearest_countdown_key(after_context)
    if key:
        return key
    if any(keyword in after_context for keyword in ("档案清理", "导出", "授权码", "授权窗口", "访问", "记录群组")):
        return "archive_cleanup"
    if any(keyword in after_context for keyword in ("记忆重置", "重置周期", "历史记录")):
        return "memory_reset"
    return "main"


def _looks_like_forced_memory_calibration_context(context: str) -> bool:
    local = str(context or "")
    return any(
        marker in local
        for marker in (
            "强制记忆校准",
            "强制校准",
            "校准倒计时",
            "记忆校准周期",
            "记忆重置加速程序",
            "加速程序授权",
            "全域记忆",
            "所有区域",
            "记忆覆盖",
        )
    ) and any(marker in local for marker in ("系统", "记忆", "重置", "覆盖", "校准"))


def _explicit_countdown_label_key(context: str) -> str:
    labels = {
        "memory_reset": (
            "主线清理倒计时",
            "主线清理",
            "记忆重置",
            "记忆重置窗口",
            "记忆重置倒计时",
            "记忆重置周期",
            "记忆熔铸倒计时",
            "记忆剥离倒计时",
            "记忆覆盖",
            "重置一次记忆",
            "每十年重置",
        ),
        "terminal_audit_window": ("终端审计窗口", "终端审计倒计时"),
        "archive_cleanup": (
            "档案清理",
            "档案清理倒计时",
            "档案清理窗口",
            "档案清理窗口倒计时",
            "档案抹除",
            "档案抹除倒计时",
            "抹除倒计时",
            "授权窗口",
        ),
        "core_access_window": ("核心层入口", "核心层授权窗口", "核心层的授权窗口", "入口关闭倒计时"),
    }
    nearest_key = ""
    nearest_rank = (-1, -1)
    local = str(context or "")
    for key, key_labels in labels.items():
        for label in key_labels:
            index = local.rfind(label)
            rank = (index + len(label), len(label))
            if index >= 0 and rank > nearest_rank:
                nearest_key = key
                nearest_rank = rank
    return nearest_key


def _after_label_binds_to_mention(local_after: str) -> bool:
    return str(local_after or "").lstrip().startswith(("——", "—", "-", "：", ":"))


def _nearest_countdown_key(context: str) -> str:
    keyword_keys = {
        "threat_response": (
            "记忆清除",
            "系统级威胁",
            "威胁评级",
            "追踪协议",
            "锁定你的位置",
            "反向锁定",
        ),
        "memory_reset": (
            "主线清理倒计时",
            "主线清理",
            "记忆重置",
            "记忆校准",
            "记忆熔铸",
            "记忆剥离",
            "记忆覆盖",
            "覆盖进程",
            "覆盖协议",
            "熔铸倒计时",
            "剥离倒计时",
            "熔铸窗口",
            "重置周期",
            "重置窗口",
            "重置前",
            "重置结束",
            "重置倒计时",
            "真实窗口",
            "真正的核心",
            "核心调度窗口",
            "重置一次记忆",
            "每十年一次",
            "每十年重置",
            "全域记忆",
            "记忆归零",
            "全域重置",
            "历史记录",
        ),
        "terminal_audit_window": (
            "终端审计窗口",
            "终端审计",
        ),
        "archive_cleanup": (
            "档案清理",
            "档案抹除",
            "抹除令",
            "抹除倒计时",
            "档案记录",
            "导出",
            "访问",
            "记录群组",
            "授权码",
            "审计窗口",
            "身份核验",
            "审计日志",
            "查询",
            "限制级信息",
        ),
        "core_access_window": (
            "核心层入口",
            "核心层授权窗口",
            "核心层的授权窗口",
            "入口关闭",
        ),
        "public_countdown": (
            "公开数据",
            "公开窗口",
            "对外数据",
            "公布数据",
            "心理缓冲",
            "普通市民",
        ),
    }
    nearest_key = ""
    nearest_index = -1
    for key, keywords in keyword_keys.items():
        for keyword in keywords:
            index = context.rfind(keyword)
            if keyword == "审计窗口" and index >= 0 and context[max(0, index - 2) : index] == "终端":
                continue
            if index > nearest_index:
                nearest_key = key
                nearest_index = index
    return nearest_key


__all__ = [
    '_prefer_active_memory_reset_key',
    '_prefer_short_clock_continuation_key',
    '_short_clock_has_explicit_key_label',
    '_normalize_ambiguous_clock_minutes',
    '_explicit_minute_second_countdown_context',
    '_non_monotonic_repair_hint',
    '_countdown_key_for_mention',
    '_looks_like_forced_memory_calibration_context',
    '_explicit_countdown_label_key',
    '_after_label_binds_to_mention',
    '_nearest_countdown_key',
]
