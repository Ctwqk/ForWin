from __future__ import annotations

from forwin.canon_quality.obligation_verifier import expire_unresolved_obligations_after_acceptance
from forwin.canon_quality.gate import evaluate_canon_admission
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch


def test_final_gate_blocks_p1_obligation_even_with_valid_plan_patch() -> None:
    obligation = NarrativeObligation(
        id="obl-final",
        project_id="p1",
        origin_chapter_number=58,
        obligation_type="final_hook_closure",
        priority="P1",
        status="planned",
        summary="终章前仍需关闭主线 hook。",
        hardness="design_debt",
        deadline_chapter=60,
        payoff_test="终章必须关闭主线 hook。",
        linked_plan_patch_ids=["patch-final"],
    )
    patch = NarrativePlanPatch(
        id="patch-final",
        project_id="p1",
        target_scope="book",
        affected_chapters=[60],
        source_obligation_ids=["obl-final"],
        validation_status="passed",
        applied=True,
    )

    result = evaluate_canon_admission(
        project_id="p1",
        chapter_number=60,
        review_verdict="warn",
        obligations=[obligation],
        plan_patches=[patch],
        mode="strict",
        is_final_chapter=True,
    )

    assert result.commit_allowed is False
    assert result.admission_mode == "blocked"
    assert "final_obligation_not_cleared:obl-final" in result.blocking_reasons


def test_expired_unresolved_obligation_blocks_after_acceptance() -> None:
    engine = get_engine(postgres_test_url("expired_unresolved_obligation"))
    init_db(engine)
    session_factory = get_session_factory(engine)
    try:
        with session_factory() as session:
            project = Project(title="过期义务", premise="测试", genre="悬疑", target_total_chapters=20)
            session.add(project)
            session.flush()
            repo = NarrativeObligationRepository(session)
            created = repo.create_obligation(
                NarrativeObligation(
                    project_id=project.id,
                    origin_chapter_number=10,
                    obligation_type="motivation_gap",
                    priority="P1",
                    status="active",
                    summary="需要解释动机。",
                    hardness="design_debt",
                    deadline_chapter=12,
                    payoff_test="第12章必须解释动机。",
                    blocking_policy="block_at_deadline",
                )
            )

            result = expire_unresolved_obligations_after_acceptance(
                session=session,
                project_id=project.id,
                chapter_number=12,
            )
            session.commit()

        assert result["expired_obligation_ids"] == [created.id]
        assert result["blocked_obligation_ids"] == [created.id]
    finally:
        engine.dispose()
