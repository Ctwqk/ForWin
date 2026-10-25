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
                    status="active",
                    payload={
                    "invariant": {
                        "kind": "active_rule",
                        "subject_key": "city_renovation",
                        "current_value": {"deadline_chapter": 8},
                        "constraints": {"cannot_extend_without_bridge": True},
                    }
                },
            ),
            trigger_quote=TriggerQuote(chapter_number=5, quote="城市改造期限锁定。"),
        )

    with Session() as session:
        query = SqlBookStateQueryInterface(session)
        invariants = query.get_current_invariants(project_id="p1", as_of_chapter=7)
        snapshot = query.get_current_invariant_state(project_id="p1", as_of_chapter=7)

    assert list(invariants) == ["city_renovation_deadline"]
    assert snapshot.invariants == invariants
    assert "countdowns" not in snapshot.model_dump()
    assert not hasattr(query, "get_current_countdown_values")
    invariant = invariants["city_renovation_deadline"]
    assert invariant.kind == "active_rule"
    assert invariant.subject_key == "city_renovation"
    assert invariant.current_value == {"deadline_chapter": 8}
    assert invariant.constraints["cannot_extend_without_bridge"] is True
