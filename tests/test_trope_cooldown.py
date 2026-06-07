from __future__ import annotations

import json

from sqlalchemy import select, text

from forwin.experience.trope_cooldown import (
    TropeCooldownPolicy,
    recent_trope_usage,
    save_accepted_trope_usage_for_chapter,
    save_trope_usage,
    select_available_templates,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.phase import TropeUsageRecord
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


def test_init_db_upgrades_legacy_trope_usage_records_with_usage_stage() -> None:
    engine = get_engine(postgres_test_url("trope-usage-stage-legacy-upgrade"))
    try:
        with engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS trope_usage_records"))
            conn.execute(
                text(
                    """
                    CREATE TABLE trope_usage_records (
                        id VARCHAR PRIMARY KEY,
                        project_id VARCHAR NOT NULL,
                        arc_id VARCHAR NOT NULL DEFAULT '',
                        band_id VARCHAR NOT NULL DEFAULT '',
                        chapter_number INTEGER NOT NULL DEFAULT 0,
                        template_id VARCHAR NOT NULL,
                        category VARCHAR NOT NULL DEFAULT '',
                        created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                    )
                    """
                )
            )

        init_db(engine)

        with engine.begin() as conn:
            has_usage_stage = conn.execute(
                text(
                    """
                    SELECT 1
                    FROM information_schema.columns
                    WHERE table_name = 'trope_usage_records'
                      AND column_name = 'usage_stage'
                    """
                )
            ).scalar_one_or_none()
            index_name = conn.execute(
                text(
                    """
                    SELECT indexname
                    FROM pg_indexes
                    WHERE tablename = 'trope_usage_records'
                      AND indexname = 'ix_trope_usage_project_stage_created'
                    """
                )
            ).scalar_one_or_none()

        assert has_usage_stage == 1
        assert index_name == "ix_trope_usage_project_stage_created"
    finally:
        engine.dispose()


def test_planned_trope_usage_does_not_count_as_default_recent_usage() -> None:
    engine = get_engine(postgres_test_url("trope-usage-stages"))
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
                usage_stage="planned",
            )
            save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="band-1",
                chapter_number=1,
                template_id="justice-a",
                category="justice",
                usage_stage="accepted",
            )

        with Session.begin() as session:
            template_ids, categories = recent_trope_usage(session, project_id="project-1")
            planned_template_ids, planned_categories = recent_trope_usage(
                session,
                project_id="project-1",
                usage_stage="planned",
            )

        assert template_ids == ["justice-a"]
        assert categories == ["justice"]
        assert planned_template_ids == ["power-a"]
        assert planned_categories == ["power"]
    finally:
        engine.dispose()


def test_save_trope_usage_is_idempotent_by_project_chapter_template_stage() -> None:
    engine = get_engine(postgres_test_url("trope-usage-idempotent"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            first = save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="band-1",
                chapter_number=1,
                template_id="power-a",
                category="power",
                usage_stage="accepted",
            )
            second = save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="band-1",
                chapter_number=1,
                template_id="power-a",
                category="power",
                usage_stage="accepted",
            )
            session.flush()

        assert first.id == second.id
    finally:
        engine.dispose()


def test_save_trope_usage_tolerates_legacy_duplicate_rows() -> None:
    engine = get_engine(postgres_test_url("trope-usage-legacy-duplicates"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            session.flush()
            session.add_all(
                [
                    TropeUsageRecord(
                        project_id="project-1",
                        arc_id="arc-old",
                        band_id="band-old",
                        chapter_number=5,
                        template_id="power-a",
                        category="power",
                        usage_stage="accepted",
                    ),
                    TropeUsageRecord(
                        project_id="project-1",
                        arc_id="arc-newer",
                        band_id="band-newer",
                        chapter_number=5,
                        template_id="power-a",
                        category="power",
                        usage_stage="accepted",
                    ),
                ]
            )
            session.flush()

            row = save_trope_usage(
                session,
                project_id="project-1",
                arc_id="arc-runtime",
                band_id="band-runtime",
                chapter_number=5,
                template_id="power-a",
                category="power",
                usage_stage="accepted",
            )
            rows = (
                session.execute(
                    select(TropeUsageRecord).where(
                        TropeUsageRecord.project_id == "project-1",
                        TropeUsageRecord.chapter_number == 5,
                        TropeUsageRecord.template_id == "power-a",
                        TropeUsageRecord.usage_stage == "accepted",
                    )
                )
                .scalars()
                .all()
            )

        assert row.id in {item.id for item in rows}
        assert len(rows) == 2
    finally:
        engine.dispose()


def test_save_accepted_trope_usage_for_chapter_extracts_plan_templates() -> None:
    engine = get_engine(postgres_test_url("trope-accepted-helper"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            rows = save_accepted_trope_usage_for_chapter(
                session,
                project_id="project-1",
                arc_id="arc-1",
                band_id="",
                chapter_number=3,
                experience_plan_json=json.dumps(
                    {
                        "selected_template_ids": ["power-a", "justice-a"],
                        "planned_reward_tags": ["power", "justice"],
                    },
                    ensure_ascii=False,
                ),
            )

        with Session.begin() as session:
            template_ids, categories = recent_trope_usage(session, project_id="project-1")

        assert len(rows) == 2
        assert set(template_ids) == {"power-a", "justice-a"}
        assert set(categories) == {"power", "justice"}
    finally:
        engine.dispose()
