from __future__ import annotations

from sqlalchemy import select

from forwin.models import ArcPlanVersion, NarrativeObligationRow, NarrativePlanPatchRow, Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol.experience import BandDelightSchedule
from forwin.protocol.review import ContinuityIssue, ReviewVerdict


def test_orchestrator_prepares_band_deferred_acceptance_before_canon_gate() -> None:
    engine = get_engine(postgres_test_url("orchestrator-defer-band"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="band defer", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(project_id=project.id, arc_synopsis="arc", chapter_start=1, chapter_end=20)
            session.add(arc)
            session.flush()
            for chapter in range(11, 15):
                session.add(
                    ChapterPlan(
                        project_id=project.id,
                        arc_plan_id=arc.id,
                        chapter_number=chapter,
                        title=f"第{chapter}章",
                        one_line="推进 band payoff。",
                        goals_json="[]",
                        experience_plan_json="{}",
                        task_contract_json="[]",
                        status="planned",
                    )
                )
            schedule = BandDelightSchedule(
                band_id="arc-1:band:2",
                chapter_start=11,
                chapter_end=14,
                stall_guard_max_gap=1,
            )
            session.add(
                BandExperiencePlan(
                    project_id=project.id,
                    arc_id=arc.id,
                    band_id="arc-1:band:2",
                    chapter_start=11,
                    chapter_end=14,
                    stall_guard_max_gap=1,
                    schedule_json=schedule.model_dump_json(),
                    task_contract_json="[]",
                )
            )
            session.flush()
            verdict = ReviewVerdict(
                verdict="warn",
                issues=[
                    ContinuityIssue(
                        rule_name="reader_promise_payoff",
                        severity="warning",
                        description="前文读者承诺需要在本 band 内兑现。",
                        issue_type="reader_promise_payoff",
                        target_scope="band",
                        suggested_fix="第14章前必须兑现前文读者承诺。",
                    )
                ],
            )

            result = WritingOrchestrator.__new__(WritingOrchestrator)._prepare_deferred_acceptance_if_needed(
                session=session,
                project_id=project.id,
                chapter_number=10,
                draft_id="draft-10",
                review_id="review-10",
                verdict=verdict,
                signals=[],
                target_total_chapters=20,
            )
            session.commit()

        assert result == []

        with session_factory() as session:
            obligations = session.execute(select(NarrativeObligationRow)).scalars().all()
            patches = session.execute(select(NarrativePlanPatchRow)).scalars().all()
            band = session.execute(select(BandExperiencePlan)).scalar_one()
            assert len(obligations) == 1
            assert obligations[0].status == "planned"
            assert obligations[0].obligation_type == "reader_promise_payoff"
            assert len(patches) == 1
            assert patches[0].target_scope == "band"
            assert patches[0].patch_type == "band_defer_acceptance"
            assert "obl" in band.schedule_json
            assert "narrative_obligation" in band.task_contract_json
    finally:
        engine.dispose()
