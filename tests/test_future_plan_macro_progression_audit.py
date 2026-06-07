import json

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from forwin.book_state.macro_status import ProtagonistMacroStatus
from forwin.models import Base, ChapterPlan, Project, ArcPlanVersion
from forwin.models.narrative_obligation import FuturePlanAuditRunRow
from forwin.planning.future_plan_audit import FuturePlanAuditor
from forwin.planning.future_plan_audit.macro_progression import audit_arc_macro_boundary
from forwin.planning.macro_progression import ArcMacroProgression, dump_arc_macro_progression


def _session():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def test_arc_macro_boundary_passes_when_status_reaches_target() -> None:
    arc = ArcPlanVersion(
        id="a1",
        project_id="p1",
        arc_synopsis="a",
        chapter_start=1,
        chapter_end=10,
        macro_progression_json=dump_arc_macro_progression(
            ArcMacroProgression(status_tier_to=2, wealth_tier_to=1, market_space_to="县城")
        ),
    )

    issues = audit_arc_macro_boundary(
        arc=arc,
        current_chapter=10,
        status=ProtagonistMacroStatus(
            project_id="p1",
            as_of_chapter=10,
            status_tier=2,
            wealth_tier=1,
            market_space="县城",
        ),
    )

    assert issues == []


def test_arc_macro_boundary_blocks_unmet_status_target() -> None:
    arc = ArcPlanVersion(
        id="a1",
        project_id="p1",
        arc_synopsis="a",
        chapter_start=1,
        chapter_end=10,
        macro_progression_json=dump_arc_macro_progression(
            ArcMacroProgression(status_tier_to=3, wealth_tier_to=2, market_space_to="县城")
        ),
    )

    issues = audit_arc_macro_boundary(
        arc=arc,
        current_chapter=10,
        status=ProtagonistMacroStatus(
            project_id="p1",
            as_of_chapter=10,
            status_tier=1,
            wealth_tier=2,
            market_space="县城",
        ),
    )

    assert len(issues) == 1
    assert issues[0].issue_type == "arc_macro_progression_not_met"
    assert issues[0].blocking is True
    assert "status_tier" in issues[0].metadata["missing_targets"]


def test_audit_and_apply_blocks_finished_arc_with_unmet_macro_target() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(
        ArcPlanVersion(
            id="a1",
            project_id="p1",
            arc_number=1,
            arc_synopsis="县城跃迁",
            chapter_start=1,
            chapter_end=10,
            macro_progression_json=dump_arc_macro_progression(
                ArcMacroProgression(status_tier_to=3, wealth_tier_to=2, market_space_to="县城")
            ),
        )
    )
    session.add(
        ChapterPlan(
            id="c10",
            project_id="p1",
            arc_plan_id="a1",
            chapter_number=10,
            status="accepted",
            experience_plan_json=json.dumps(
                {
                    "macro_status": {
                        "status_tier": 1,
                        "wealth_tier": 2,
                        "market_space": "县城",
                    }
                },
                ensure_ascii=False,
            ),
        )
    )
    session.commit()

    result = FuturePlanAuditor().audit_and_apply(
        session=session,
        project_id="p1",
        current_chapter=10,
        trigger_stage="post_acceptance",
        plans=[],
        canon_quality_context={},
        obligations=[],
        target_total_chapters=1000,
        include_current=False,
    )

    assert result.status == "fail"
    assert result.issues[0].issue_type == "arc_macro_progression_not_met"
    assert result.blocking_reasons == ["arc_macro_progression_not_met:a1"]


def test_audit_and_apply_blocks_unmet_macro_target_after_boundary_was_passed() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(
        ArcPlanVersion(
            id="a1",
            project_id="p1",
            arc_number=1,
            arc_synopsis="县城跃迁",
            chapter_start=1,
            chapter_end=10,
            macro_progression_json=dump_arc_macro_progression(
                ArcMacroProgression(status_tier_to=3, wealth_tier_to=2, market_space_to="县城")
            ),
        )
    )
    session.add(
        ChapterPlan(
            id="c12",
            project_id="p1",
            arc_plan_id="a1",
            chapter_number=12,
            status="accepted",
            experience_plan_json=json.dumps(
                {
                    "macro_status": {
                        "status_tier": 1,
                        "wealth_tier": 2,
                        "market_space": "县城",
                    }
                },
                ensure_ascii=False,
            ),
        )
    )
    session.commit()

    result = FuturePlanAuditor().audit_and_apply(
        session=session,
        project_id="p1",
        current_chapter=12,
        trigger_stage="post_acceptance",
        plans=[],
        canon_quality_context={},
        obligations=[],
        target_total_chapters=1000,
        include_current=False,
    )

    assert result.status == "fail"
    assert result.issues[0].metadata["arc_id"] == "a1"
    assert result.metadata["macro_boundary_audited_arc_ids"] == ["a1"]


def test_audit_and_apply_skips_successfully_audited_macro_boundary() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(
        ArcPlanVersion(
            id="a1",
            project_id="p1",
            arc_number=1,
            arc_synopsis="县城跃迁",
            chapter_start=1,
            chapter_end=10,
            macro_progression_json=dump_arc_macro_progression(
                ArcMacroProgression(status_tier_to=3, wealth_tier_to=2, market_space_to="县城")
            ),
        )
    )
    session.add(
        ChapterPlan(
            id="c12",
            project_id="p1",
            arc_plan_id="a1",
            chapter_number=12,
            status="accepted",
            experience_plan_json=json.dumps(
                {
                    "macro_status": {
                        "status_tier": 1,
                        "wealth_tier": 2,
                        "market_space": "县城",
                    }
                },
                ensure_ascii=False,
            ),
        )
    )
    session.add(
        FuturePlanAuditRunRow(
            id="audit-1",
            project_id="p1",
            current_chapter_number=10,
            trigger_stage="post_acceptance",
            status="pass",
            metadata_json=json.dumps(
                {"macro_boundary_audited_arc_ids": ["a1"]},
                ensure_ascii=False,
            ),
        )
    )
    session.commit()

    result = FuturePlanAuditor().audit_and_apply(
        session=session,
        project_id="p1",
        current_chapter=12,
        trigger_stage="post_acceptance",
        plans=[],
        canon_quality_context={},
        obligations=[],
        target_total_chapters=1000,
        include_current=False,
    )

    assert result.status == "pass"
    assert result.issues == []
    assert result.metadata["macro_boundary_audited_arc_ids"] == ["a1"]
