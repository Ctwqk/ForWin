import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state.query_interface import SqlBookStateQueryInterface
from forwin.models import Base, ChapterPlan, FactNodeRow, Project, ArcPlanVersion


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_macro_status_projection_derives_from_accepted_chapter_experience() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(
        ArcPlanVersion(
            id="a1",
            project_id="p1",
            arc_synopsis="a",
            chapter_start=1,
            chapter_end=10,
        )
    )
    session.add(
        ChapterPlan(
            project_id="p1",
            arc_plan_id="a1",
            chapter_number=8,
            status="accepted",
            experience_plan_json=json.dumps(
                {
                    "macro_status": {
                        "status_tier": 2,
                        "wealth_tier": 3,
                        "enemy_tier": 2,
                        "market_space": "县城",
                    }
                },
                ensure_ascii=False,
            ),
        )
    )
    session.commit()

    status = SqlBookStateQueryInterface(session).get_protagonist_macro_status(
        project_id="p1",
        as_of_chapter=8,
    )

    assert status.status_tier == 2
    assert status.wealth_tier == 3
    assert status.enemy_tier == 2
    assert status.market_space == "县城"
    assert status.evidence_refs == ["chapter_plan:8"]
    assert status.source == "accepted_chapter_macro_legacy_projection"


def test_macro_status_prefers_book_state_fact_evidence() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(
        ChapterPlan(
            project_id="p1",
            arc_plan_id="",
            chapter_number=8,
            status="accepted",
            experience_plan_json=json.dumps(
                {
                    "macro_status": {
                        "status_tier": 1,
                        "wealth_tier": 1,
                        "market_space": "镇上",
                    }
                },
                ensure_ascii=False,
            ),
        )
    )
    session.add(
        FactNodeRow(
            id="fact-macro-1",
            project_id="p1",
            proposition="主角完成县城阶梯跃迁",
            fact_type="macro_status",
            truth_value="true",
            created_at_chapter=9,
            state_json=json.dumps(
                {
                    "status_tier": 3,
                    "wealth_tier": 2,
                    "enemy_tier": 1,
                    "market_space": "县城",
                },
                ensure_ascii=False,
            ),
            source_refs_json=json.dumps(["book_state:fact:9"], ensure_ascii=False),
        )
    )
    session.commit()

    status = SqlBookStateQueryInterface(session).get_protagonist_macro_status(
        project_id="p1",
        as_of_chapter=10,
    )

    assert status.status_tier == 3
    assert status.wealth_tier == 2
    assert status.enemy_tier == 1
    assert status.market_space == "县城"
    assert status.evidence_refs == ["book_state:fact:9"]
    assert status.source == "book_state_macro_fact"


def test_macro_status_uses_explicit_accepted_chapter_evidence() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(
        ChapterPlan(
            project_id="p1",
            arc_plan_id="",
            chapter_number=9,
            status="accepted",
            experience_plan_json=json.dumps(
                {
                    "macro_status": {
                        "status_tier": 2,
                        "wealth_tier": 2,
                        "market_space": "县城",
                        "evidence_refs": ["chapter:9:payoff"],
                    }
                },
                ensure_ascii=False,
            ),
        )
    )
    session.commit()

    status = SqlBookStateQueryInterface(session).get_protagonist_macro_status(
        project_id="p1",
        as_of_chapter=9,
    )

    assert status.status_tier == 2
    assert status.wealth_tier == 2
    assert status.market_space == "县城"
    assert status.evidence_refs == ["chapter:9:payoff"]
    assert status.source == "accepted_chapter_macro_evidence"
