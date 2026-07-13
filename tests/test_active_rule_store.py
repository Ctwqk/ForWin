from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.canon_quality.active_rule_store import (
    ActiveRule,
    CanonQualityActiveRuleStore,
    TriggerQuote,
)
from forwin.models.base import Base


def test_active_rule_store_register_query_revoke_cycle() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        store = CanonQualityActiveRuleStore(session)
        result = store.register_rule(
            project_id="p1",
            rule=ActiveRule(rule_key="hidden_timer", summary="局部倒计时甲活跃", valid_from_chapter=17),
            trigger_quote=TriggerQuote(chapter_number=17, quote="局部倒计时甲开始跳动。"),
        )
        assert result.applied is True
        assert store.query_active_as_of(project_id="p1", chapter_number=17) == []
        activated = store.transition_status(
            project_id="p1",
            rule_key="hidden_timer",
            chapter_number=18,
            status="active",
            reason="owner approved",
        )
        assert activated.applied is True
        assert [rule.rule_key for rule in store.query_active_as_of(project_id="p1", chapter_number=18)] == ["hidden_timer"]

        conflict = store.register_rule(
            project_id="p1",
            rule=ActiveRule(rule_key="hidden_timer", summary="重复注册", valid_from_chapter=18),
            trigger_quote=TriggerQuote(chapter_number=18, quote="重复。"),
        )
        assert conflict.applied is False
        assert conflict.reason == "active_rule_conflict"

        suspended = store.transition_status(
            project_id="p1",
            rule_key="hidden_timer",
            chapter_number=19,
            status="suspended",
            reason="high override rate",
        )
        assert suspended.applied is True
        assert store.query_active_as_of(project_id="p1", chapter_number=20) == []


def test_active_rule_store_preserves_as_of_history_after_revoke() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        store = CanonQualityActiveRuleStore(session)
        store.register_rule(
            project_id="p1",
            rule=ActiveRule(
                rule_key="window",
                summary="窗口开启",
                valid_from_chapter=5,
                status="active",
            ),
            trigger_quote=TriggerQuote(chapter_number=5, quote="窗口开启。"),
        )
        store.transition_status(
            project_id="p1",
            rule_key="window",
            chapter_number=9,
            status="suspended",
            reason="closed",
        )

        assert [rule.rule_key for rule in store.query_active_as_of(project_id="p1", chapter_number=8)] == ["window"]
        assert store.query_active_as_of(project_id="p1", chapter_number=9) == []


def test_active_rule_store_honors_valid_until_chapter() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        store = CanonQualityActiveRuleStore(session)
        store.register_rule(
            project_id="p1",
            rule=ActiveRule(
                rule_key="window",
                summary="窗口开启",
                valid_from_chapter=5,
                valid_until_chapter=7,
                status="active",
            ),
            trigger_quote=TriggerQuote(chapter_number=5, quote="窗口开启。"),
        )

        assert [rule.rule_key for rule in store.query_active_as_of(project_id="p1", chapter_number=7)] == ["window"]
        assert store.query_active_as_of(project_id="p1", chapter_number=8) == []


def test_active_rule_store_allows_non_overlapping_registration_after_revoke() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        store = CanonQualityActiveRuleStore(session)
        store.register_rule(
            project_id="p1",
            rule=ActiveRule(
                rule_key="window",
                summary="旧窗口",
                valid_from_chapter=5,
                status="active",
            ),
            trigger_quote=TriggerQuote(chapter_number=5, quote="旧窗口开启。"),
        )
        store.transition_status(
            project_id="p1",
            rule_key="window",
            chapter_number=8,
            status="suspended",
            reason="closed",
        )
        store.transition_status(
            project_id="p1",
            rule_key="window",
            chapter_number=9,
            status="retired",
            reason="retired after suspension",
        )

        result = store.register_rule(
            project_id="p1",
            rule=ActiveRule(
                rule_key="window",
                summary="新窗口",
                valid_from_chapter=10,
                status="active",
            ),
            trigger_quote=TriggerQuote(chapter_number=10, quote="新窗口开启。"),
        )

        assert result.applied is True
        assert [rule.summary for rule in store.query_active_as_of(project_id="p1", chapter_number=7)] == ["旧窗口"]
        assert store.query_active_as_of(project_id="p1", chapter_number=8) == []
        assert [rule.summary for rule in store.query_active_as_of(project_id="p1", chapter_number=10)] == ["新窗口"]


def test_active_rule_store_rejects_out_of_order_history_writes() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        store = CanonQualityActiveRuleStore(session)
        assert store.register_rule(
            project_id="p1",
            rule=ActiveRule(rule_key="future-window", valid_from_chapter=20),
            trigger_quote=TriggerQuote(chapter_number=20, quote="未来窗口。"),
        ).applied

        stale_registration = store.register_rule(
            project_id="p1",
            rule=ActiveRule(rule_key="future-window", valid_from_chapter=15),
            trigger_quote=TriggerQuote(chapter_number=15, quote="倒插窗口。"),
        )
        stale_transition = store.transition_status(
            project_id="p1",
            rule_key="future-window",
            chapter_number=19,
            status="active",
            reason="backdated transition",
        )

        assert stale_registration.applied is False
        assert stale_registration.reason == "out_of_order_rule_event"
        assert stale_transition.applied is False
        assert stale_transition.reason == "out_of_order_rule_event"
        assert store.list_rules(project_id="p1")[0].status == "observing"
