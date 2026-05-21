from __future__ import annotations

import json
from types import SimpleNamespace

from forwin.review_engine.dashboard import build_waiting_review_breakdown


def _event(payload: dict[str, object], *, reason: str = "") -> SimpleNamespace:
    return SimpleNamespace(payload_json=json.dumps(payload, ensure_ascii=False), reason=reason)


def test_waiting_review_breakdown_groups_manual_review_by_rule_id() -> None:
    rows = [
        _event(
            {
                "rule_id": "auto_approve_policy_disabled",
                "outcome": "manual_review",
                "reason": "policy disabled: review_engine.auto_approve_enabled=false",
            }
        ),
        _event(
            {
                "rule_id": "auto_approve_policy_disabled",
                "outcome": "manual_review",
                "reason": "policy disabled: review_engine.auto_approve_enabled=false",
            }
        ),
        _event({"rule_id": "copilot_safe_warn", "outcome": "auto_approve"}),
        _event({"rule_id": "canon_gate_block", "outcome": "system_block"}),
    ]

    breakdown = build_waiting_review_breakdown(rows)

    assert breakdown == [
        {
            "rule_id": "auto_approve_policy_disabled",
            "outcome": "manual_review",
            "reason": "policy disabled: review_engine.auto_approve_enabled=false",
            "count": 2,
            "status_chip": "可自动处理但策略关闭",
        },
        {
            "rule_id": "canon_gate_block",
            "outcome": "system_block",
            "reason": "",
            "count": 1,
            "status_chip": "系统阻断",
        },
    ]


def test_waiting_review_breakdown_marks_plain_manual_reviews() -> None:
    breakdown = build_waiting_review_breakdown(
        [
            _event(
                {
                    "rule_id": "arc_patcher_disabled",
                    "outcome": "manual_review",
                    "reason": "arc patcher disabled",
                }
            )
        ]
    )

    assert breakdown[0]["status_chip"] == "需要人工判断"


def test_waiting_review_breakdown_marks_system_blocks() -> None:
    breakdown = build_waiting_review_breakdown(
        [
            _event(
                {
                    "rule_id": "commit_with_obligation_over_budget",
                    "outcome": "system_block",
                    "reason": "arc budget exceeded",
                }
            )
        ]
    )

    assert breakdown == [
        {
            "rule_id": "commit_with_obligation_over_budget",
            "outcome": "system_block",
            "reason": "arc budget exceeded",
            "count": 1,
            "status_chip": "系统阻断",
        }
    ]


def test_waiting_review_breakdown_keeps_same_rule_different_outcomes_separate() -> None:
    breakdown = build_waiting_review_breakdown(
        [
            _event({"rule_id": "budget_rule", "outcome": "manual_review", "reason": "needs review"}),
            _event({"rule_id": "budget_rule", "outcome": "system_block", "reason": "over budget"}),
        ]
    )

    assert [row["outcome"] for row in breakdown] == ["manual_review", "system_block"]
    assert [row["status_chip"] for row in breakdown] == ["需要人工判断", "系统阻断"]
