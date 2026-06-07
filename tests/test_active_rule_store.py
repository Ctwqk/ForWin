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
        assert [rule.rule_key for rule in store.query_active_as_of(project_id="p1", chapter_number=18)] == ["hidden_timer"]

        conflict = store.register_rule(
            project_id="p1",
            rule=ActiveRule(rule_key="hidden_timer", summary="重复注册", valid_from_chapter=18),
            trigger_quote=TriggerQuote(chapter_number=18, quote="重复。"),
        )
        assert conflict.applied is False
        assert conflict.reason == "active_rule_conflict"

        revoked = store.revoke_rule(project_id="p1", rule_key="hidden_timer", revoke_chapter=19, reason="resolved")
        assert revoked.applied is True
        assert store.query_active_as_of(project_id="p1", chapter_number=20) == []


def test_active_rule_store_preserves_as_of_history_after_revoke() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        store = CanonQualityActiveRuleStore(session)
        store.register_rule(
            project_id="p1",
            rule=ActiveRule(rule_key="window", summary="窗口开启", valid_from_chapter=5),
            trigger_quote=TriggerQuote(chapter_number=5, quote="窗口开启。"),
        )
        store.revoke_rule(project_id="p1", rule_key="window", revoke_chapter=9, reason="closed")

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
            rule=ActiveRule(rule_key="window", summary="旧窗口", valid_from_chapter=5),
            trigger_quote=TriggerQuote(chapter_number=5, quote="旧窗口开启。"),
        )
        store.revoke_rule(project_id="p1", rule_key="window", revoke_chapter=9, reason="closed")

        result = store.register_rule(
            project_id="p1",
            rule=ActiveRule(rule_key="window", summary="新窗口", valid_from_chapter=10),
            trigger_quote=TriggerQuote(chapter_number=10, quote="新窗口开启。"),
        )

        assert result.applied is True
        assert [rule.summary for rule in store.query_active_as_of(project_id="p1", chapter_number=8)] == ["旧窗口"]
        assert [rule.summary for rule in store.query_active_as_of(project_id="p1", chapter_number=10)] == ["新窗口"]
