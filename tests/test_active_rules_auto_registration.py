from __future__ import annotations

from forwin.canon_quality.active_rule_store import ActiveRule, ActiveRulePatch, TriggerQuote
from forwin.canon_quality.active_rules_handler import apply_pre_write_active_rules


class Store:
    def __init__(self) -> None:
        self.rules: list[ActiveRule] = []

    def register_rule(self, *, project_id: str, rule: ActiveRule, trigger_quote: TriggerQuote):
        self.rules.append(rule)
        return type("Result", (), {"applied": True, "reason": "", "rule_key": rule.rule_key})()

    def query_active_as_of(self, *, project_id: str, chapter_number: int):
        return list(self.rules)

    def revoke_rule(self, *, project_id: str, rule_key: str, revoke_chapter: int, reason: str):
        raise AssertionError("not used")


def test_active_rules_handler_registers_valid_prior_trigger_patch() -> None:
    store = Store()

    report = apply_pre_write_active_rules(
        project_id="p1",
        chapter_number=18,
        patches=[
            ActiveRulePatch(
                rule=ActiveRule(rule_key="hidden_timer", summary="隐藏子程序倒计时活跃", valid_from_chapter=17),
                trigger_quote=TriggerQuote(chapter_number=17, quote="隐藏子程序倒计时开始跳动。"),
            )
        ],
        store=store,
    )

    assert report.applied == 1
    assert store.rules[0].rule_key == "hidden_timer"


def test_active_rules_handler_rejects_ambiguous_or_future_trigger_patch() -> None:
    store = Store()

    report = apply_pre_write_active_rules(
        project_id="p1",
        chapter_number=18,
        patches=[
            ActiveRulePatch(
                rule=ActiveRule(rule_key="hidden_timer", summary="隐藏子程序倒计时活跃", valid_from_chapter=18),
                trigger_quote=TriggerQuote(chapter_number=18, quote=""),
            )
        ],
        store=store,
    )

    assert report.applied == 0
    assert report.rejected == 1
    assert store.rules == []
