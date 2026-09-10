"""Interpret explicit BookState rule lifecycle values without rewriting history."""

from __future__ import annotations

from typing import Literal

RuleStatus = Literal["active", "inactive", "unknown"]

_ACTIVE = frozenset(
    {
        "active",
        "effective",
        "enabled",
        "in_force",
        "in force",
        "生效",
        "已生效",
        "有效",
        "启用",
        "已启用",
    }
)
_INACTIVE = frozenset(
    {
        "inactive",
        "retired",
        "deleted",
        "revoked",
        "withdrawn",
        "suspended",
        "expired",
        "paused",
        "disabled",
        "撤回",
        "已撤回",
        "撤销",
        "已撤销",
        "废止",
        "已废止",
        "暂停",
        "已暂停",
        "停用",
        "已停用",
        "失效",
        "已失效",
        "过期",
        "已过期",
    }
)


def normalize_rule_status(value: object) -> RuleStatus:
    """Only an explicit lifecycle value establishes an active rule.

    Unknown prose, absence and non-string values do not establish activation.
    Exact matching avoids interpreting a negation or tentative claim as active.
    """
    if not isinstance(value, str):
        return "unknown"
    normalized = value.strip().casefold()
    if normalized in _ACTIVE:
        return "active"
    if normalized in _INACTIVE:
        return "inactive"
    return "unknown"
