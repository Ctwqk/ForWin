import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state.query_interface import SqlBookStateQueryInterface
from forwin.models import Base, ChapterPlan, Project, ArcPlanVersion


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
    assert status.source == "book_state_macro_projection"
