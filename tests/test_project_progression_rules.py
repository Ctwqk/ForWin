from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.models import Base, ProjectProgressionRule
from forwin.planning.progression_rules import (
    ProgressionRuleRepository,
    active_progression_rules_for_chapter,
)


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_repository_lists_active_rules_for_chapter_range() -> None:
    session = _session()
    session.add_all(
        [
            ProjectProgressionRule(
                project_id="p1",
                rule_type="repetition_ban",
                chapter_start=1,
                chapter_end=50,
                severity="blocking",
                payload_json='{"blocked_categories":["cheap_insult"]}',
                active=True,
            ),
            ProjectProgressionRule(
                project_id="p1",
                rule_type="wealth_ceiling",
                chapter_start=60,
                chapter_end=80,
                severity="warning",
                payload_json='{"max_tier":3}',
                active=True,
            ),
            ProjectProgressionRule(
                project_id="p1",
                rule_type="trope_filter",
                chapter_start=1,
                chapter_end=50,
                severity="blocking",
                payload_json='{"blocked_template_ids":["face_slap_001"]}',
                active=False,
            ),
        ]
    )
    session.commit()

    rules = active_progression_rules_for_chapter(session, project_id="p1", chapter_number=25)

    assert [rule.rule_type for rule in rules] == ["repetition_ban"]
    assert rules[0].payload["blocked_categories"] == ["cheap_insult"]


def test_repository_create_rule_normalizes_payload() -> None:
    session = _session()
    repo = ProgressionRuleRepository(session)

    rule = repo.create_rule(
        project_id="p1",
        rule_type="trope_filter",
        chapter_start=10,
        chapter_end=20,
        severity="blocking",
        payload={"blocked_template_ids": ["t1"]},
    )
    session.commit()

    loaded = active_progression_rules_for_chapter(session, project_id="p1", chapter_number=15)
    assert loaded[0].id == rule.id
    assert loaded[0].payload == {"blocked_template_ids": ["t1"]}
