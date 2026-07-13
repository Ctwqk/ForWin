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

def test_active_rules_handler_registers_valid_prior_trigger_patch() -> None:
    store = Store()

    report = apply_pre_write_active_rules(
        project_id="p1",
        chapter_number=18,
        patches=[
            ActiveRulePatch(
                rule=ActiveRule(rule_key="hidden_timer", summary="局部倒计时甲活跃", valid_from_chapter=17),
                trigger_quote=TriggerQuote(chapter_number=17, quote="局部倒计时甲开始跳动。"),
            )
        ],
        store=store,
    )

    assert report.applied == 1
    assert store.rules[0].rule_key == "hidden_timer"
    assert store.rules[0].origin_project_id == "p1"
    assert store.rules[0].status == "observing"


def test_active_rules_handler_rejects_ambiguous_or_future_trigger_patch() -> None:
    store = Store()

    report = apply_pre_write_active_rules(
        project_id="p1",
        chapter_number=18,
        patches=[
            ActiveRulePatch(
                rule=ActiveRule(rule_key="hidden_timer", summary="局部倒计时甲活跃", valid_from_chapter=18),
                trigger_quote=TriggerQuote(chapter_number=18, quote=""),
            )
        ],
        store=store,
    )

    assert report.applied == 0
    assert report.rejected == 1
    assert store.rules == []


def test_active_rules_handler_rejects_runtime_rule_that_skips_observing() -> None:
    store = Store()

    report = apply_pre_write_active_rules(
        project_id="p1",
        chapter_number=18,
        patches=[
            ActiveRulePatch(
                rule=ActiveRule(
                    rule_key="hidden_timer",
                    status="active",
                    valid_from_chapter=17,
                ),
                trigger_quote=TriggerQuote(
                    chapter_number=17,
                    quote="局部倒计时开始跳动。",
                ),
            )
        ],
        store=store,
    )

    assert report.applied == 0
    assert report.rejection_reasons == ["runtime_rule_must_start_observing"]
    assert store.rules == []
