from __future__ import annotations

from forwin.experience.trope_cooldown import (
    TropeCooldownPolicy,
    recent_trope_usage,
    save_trope_usage,
    select_available_templates,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.project import Project
from forwin.protocol.trope_library import TropeTemplate
from tests.postgres import postgres_test_url


def _template(template_id: str, category: str, cost: int = 1) -> TropeTemplate:
    return TropeTemplate(
        template_id=template_id,
        display_name=template_id,
        category=category,
        cost_weight=cost,
    )


def test_select_available_templates_filters_recent_template_and_category() -> None:
    templates = [
        _template("power-a", "power"),
        _template("power-b", "power"),
        _template("justice-a", "justice"),
    ]
    selected = select_available_templates(
        templates,
        recent_template_ids=["power-a"],
        recent_categories=["justice"],
        policy=TropeCooldownPolicy(template_band_gap=3, category_band_gap=1),
    )

    assert [item.template_id for item in selected] == ["power-b"]


def test_trope_usage_records_roundtrip_recent_usage() -> None:
    engine = get_engine(postgres_test_url("trope-usage-records"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            session.flush()
            save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="band-1",
                chapter_number=1,
                template_id="power-a",
                category="power",
            )

        with Session.begin() as session:
            template_ids, categories = recent_trope_usage(session, project_id="project-1")

        assert template_ids == ["power-a"]
        assert categories == ["power"]
    finally:
        engine.dispose()
