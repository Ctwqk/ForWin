from __future__ import annotations

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state.query_interface import SqlBookStateQueryInterface
from forwin.canon_quality.active_rule_store import (
    ActiveRule,
    CanonQualityActiveRuleStore,
    TriggerQuote,
)
from forwin.models.base import Base
from forwin.models.canon_quality import CountdownLedgerRow


def test_bookstate_query_interface_returns_latest_countdown_as_of_chapter() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        session.add_all(
            [
                CountdownLedgerRow(project_id="p1", countdown_key="main", chapter_number=2, normalized_remaining_minutes=79),
                CountdownLedgerRow(project_id="p1", countdown_key="main", chapter_number=17, normalized_remaining_minutes=57),
                CountdownLedgerRow(project_id="p1", countdown_key="hidden", chapter_number=17, normalized_remaining_minutes=16),
            ]
        )

    with Session() as session:
        values = SqlBookStateQueryInterface(session).get_current_countdown_values(
            project_id="p1",
            as_of_chapter=17,
        )

    assert values["main"].remaining_minutes == 57
    assert values["hidden"].remaining_minutes == 16


def test_bookstate_query_interface_projects_countdowns_to_invariants() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        session.add(
            CountdownLedgerRow(
                project_id="p1",
                countdown_key="main",
                label="主倒计时",
                chapter_number=7,
                normalized_remaining_minutes=42,
                raw_mention="还剩42分钟",
                status="active",
            )
        )

    with Session() as session:
        snapshot = SqlBookStateQueryInterface(session).get_current_invariant_state(
            project_id="p1",
            as_of_chapter=8,
        )

    invariant = snapshot.invariants["countdown:main"]
    assert invariant.invariant_key == "countdown:main"
    assert invariant.kind == "monotonic_numeric"
    assert invariant.value_unit == "minutes"
    assert invariant.current_value == 42
    assert invariant.status == "active"
    assert snapshot.countdowns["main"].remaining_minutes == 42


def test_bookstate_query_interface_projects_active_rules_to_invariants() -> None:
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine)
    with Session.begin() as session:
        CanonQualityActiveRuleStore(session).register_rule(
            project_id="p1",
            rule=ActiveRule(
                rule_key="city_renovation_deadline",
                summary="城市改造期限强状态",
                valid_from_chapter=5,
                payload={
                    "invariant": {
                        "kind": "deadline",
                        "subject_key": "city_renovation",
                        "current_value": {"deadline_chapter": 8},
                        "constraints": {"cannot_extend_without_bridge": True},
                    }
                },
            ),
            trigger_quote=TriggerQuote(chapter_number=5, quote="城市改造期限锁定。"),
        )

    with Session() as session:
        invariants = SqlBookStateQueryInterface(session).get_current_invariants(
            project_id="p1",
            as_of_chapter=7,
        )

    invariant = invariants["city_renovation_deadline"]
    assert invariant.kind == "deadline"
    assert invariant.subject_key == "city_renovation"
    assert invariant.current_value == {"deadline_chapter": 8}
    assert invariant.constraints["cannot_extend_without_bridge"] is True
