from __future__ import annotations

from sqlalchemy import select

from forwin.models import (
    ArcPlanVersion,
    ChapterPlan,
    NarrativeObligationRow,
    NarrativePlanPatchRow,
    Project,
)
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.phase import BandExperiencePlan
from forwin.narrative_obligations.transaction import DeferAcceptanceTransaction
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.protocol.experience import BandDelightSchedule


def _obligation(project_id: str) -> NarrativeObligation:
    return NarrativeObligation(
        project_id=project_id,
        origin_chapter_number=10,
        origin_draft_id="draft-10",
        origin_review_id="review-10",
        origin_signal_ids=["sig-1"],
        obligation_type="motivation_gap",
        priority="P1",
        status="proposed",
        summary="韩砚动机尚未解释。",
        deferral_reason="下一章可以偿还。",
        hardness="design_debt",
        deadline_chapter=11,
        payoff_test="第11章必须给出韩砚动机证据。",
    )


def _patch(project_id: str, obligation_id: str = "") -> NarrativePlanPatch:
    return NarrativePlanPatch(
        project_id=project_id,
        target_scope="chapter",
        affected_chapters=[11],
        source_obligation_ids=[obligation_id] if obligation_id else [],
        new_contract={"payoff_test": "第11章必须给出韩砚动机证据。"},
        writer_context_injections=[{"instruction": "补足韩砚动机"}],
        reviewer_context_injections=[{"payoff_test": "必须看到动机证据"}],
        expected_resolution_tests=["第11章必须给出韩砚动机证据。"],
    )


def test_defer_acceptance_transaction_plans_patch_and_allows_commit() -> None:
    engine = get_engine(postgres_test_url("defer_acceptance_success"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="延后接受", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_synopsis="测试 arc",
                chapter_start=1,
                chapter_end=20,
            )
            session.add(arc)
            session.flush()
            deadline_plan = ChapterPlan(
                project_id=project.id,
                arc_plan_id=arc.id,
                chapter_number=11,
                title="动机回收",
                one_line="解释下一步选择。",
                goals_json="[]",
                experience_plan_json="{}",
                status="planned",
            )
            session.add(deadline_plan)
            session.flush()
            obligation = _obligation(project.id).model_copy(update={"id": "obl-tx"})
            patch = _patch(project.id, obligation_id="obl-tx").model_copy(
                update={"target_plan_id": deadline_plan.id}
            )

            result = DeferAcceptanceTransaction(session).run(
                obligation=obligation,
                plan_patch=patch,
                current_chapter=10,
                target_total_chapters=20,
            )
            session.commit()

        assert result.success is True
        assert result.gate_result is not None
        assert result.gate_result.commit_allowed is True
        assert result.gate_result.admission_mode == "with_obligation"

        with session_factory() as session:
            stored_obligation = session.get(NarrativeObligationRow, result.obligation.id)
            stored_patch = session.get(NarrativePlanPatchRow, result.plan_patch.id)
            updated_plan = session.get(ChapterPlan, deadline_plan.id)
            assert stored_obligation is not None
            assert stored_obligation.status == "planned"
            assert stored_patch is not None
            assert stored_patch.validation_status == "passed"
            assert stored_patch.applied is True
            assert updated_plan is not None
            assert "obl-tx" in str(updated_plan.experience_plan_json)
            assert "第11章必须给出韩砚动机证据" in str(updated_plan.goals_json)
    finally:
        engine.dispose()


def test_defer_acceptance_transaction_rolls_back_when_patch_invalid() -> None:
    engine = get_engine(postgres_test_url("defer_acceptance_failure"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="延后失败", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            obligation = _obligation(project.id).model_copy(update={"id": "obl-invalid"})
            patch = _patch(project.id, obligation_id="obl-invalid").model_copy(
                update={"affected_chapters": [10]}
            )

            result = DeferAcceptanceTransaction(session).run(
                obligation=obligation,
                plan_patch=patch,
                current_chapter=10,
                target_total_chapters=20,
            )
            session.commit()

        assert result.success is False
        assert "affected_chapter_not_future:10" in result.errors

        with session_factory() as session:
            obligations = session.execute(select(NarrativeObligationRow)).scalars().all()
            patches = session.execute(select(NarrativePlanPatchRow)).scalars().all()
            assert obligations == []
            assert patches == []
    finally:
        engine.dispose()


def test_defer_acceptance_transaction_applies_band_plan_patch_and_allows_commit() -> None:
    engine = get_engine(postgres_test_url("defer_acceptance_band_success"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="band 延后接受", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            arc = ArcPlanVersion(
                project_id=project.id,
                arc_synopsis="测试 arc",
                chapter_start=1,
                chapter_end=20,
            )
            session.add(arc)
            session.flush()
            schedule = BandDelightSchedule(
                band_id="arc-1:band:2",
                chapter_start=11,
                chapter_end=14,
                stall_guard_max_gap=1,
            )
            band_row = BandExperiencePlan(
                project_id=project.id,
                arc_id=arc.id,
                band_id="arc-1:band:2",
                chapter_start=11,
                chapter_end=14,
                stall_guard_max_gap=1,
                schedule_json=schedule.model_dump_json(),
                task_contract_json="[]",
            )
            session.add(band_row)
            session.flush()
            obligation = _obligation(project.id).model_copy(
                update={
                    "id": "obl-band-tx",
                    "obligation_type": "reader_promise_payoff",
                    "deadline_chapter": 14,
                    "payoff_test": "第14章必须兑现审计窗口真相。",
                    "metadata": {"minimum_scope": "band"},
                }
            )
            patch = NarrativePlanPatch(
                project_id=project.id,
                patch_type="band_defer_acceptance",
                target_scope="band",
                target_band_id=band_row.band_id,
                affected_chapters=[11, 12, 13, 14],
                source_obligation_ids=["obl-band-tx"],
                new_contract={
                    "band_obligation_contract": {
                        "open_obligations": ["obl-band-tx"],
                        "must_resolve_by_band_end": ["obl-band-tx"],
                        "allowed_carry_forward": [],
                        "payoff_tests": {"obl-band-tx": "第14章必须兑现审计窗口真相。"},
                    }
                },
                writer_context_injections=[{"obligation_id": "obl-band-tx", "instruction": "band 内兑现"}],
                reviewer_context_injections=[{"obligation_id": "obl-band-tx", "payoff_test": "第14章必须兑现审计窗口真相。"}],
                expected_resolution_tests=["第14章必须兑现审计窗口真相。"],
            )

            result = DeferAcceptanceTransaction(session).run(
                obligation=obligation,
                plan_patch=patch,
                current_chapter=10,
                target_total_chapters=20,
            )
            session.commit()

        assert result.success is True
        assert result.gate_result is not None
        assert result.gate_result.admission_mode == "with_obligation"

        with session_factory() as session:
            stored_obligation = session.get(NarrativeObligationRow, result.obligation.id)
            stored_patch = session.get(NarrativePlanPatchRow, result.plan_patch.id)
            updated_band = session.get(BandExperiencePlan, band_row.id)
            assert stored_obligation is not None
            assert stored_obligation.status == "planned"
            assert stored_patch is not None
            assert stored_patch.target_scope == "band"
            assert stored_patch.validation_status == "passed"
            assert stored_patch.applied is True
            assert updated_band is not None
            assert "obl-band-tx" in updated_band.schedule_json
            assert "narrative_obligation" in updated_band.task_contract_json
    finally:
        engine.dispose()
