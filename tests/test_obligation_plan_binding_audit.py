from __future__ import annotations

import json

from forwin.models import ChapterPlan
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.planning.future_plan_auditor import FuturePlanAuditor


def test_future_plan_auditor_binds_deadline_plan_to_missing_obligation() -> None:
    plan = ChapterPlan(
        id="plan-11",
        project_id="project-1",
        arc_plan_id="arc-1",
        chapter_number=11,
        title="第十一章",
        one_line="韩砚继续协助陆明。",
        goals_json=json.dumps(["推进核心系统线索"], ensure_ascii=False),
        task_contract_json="[]",
        experience_plan_json="{}",
        status="planned",
    )
    obligation = NarrativeObligation(
        id="obl-shenyan",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="motivation_gap",
        priority="P1",
        status="active",
        summary="韩砚第10章协助陆明的动机尚未解释。",
        hardness="design_debt",
        deadline_chapter=11,
        payoff_test="第11章必须给出韩砚协助陆明的明确动机证据。",
        linked_plan_patch_ids=["patch-old"],
    )

    result = FuturePlanAuditor().audit_plans(
        project_id="project-1",
        current_chapter=10,
        trigger_stage="post_acceptance",
        plans=[plan],
        canon_quality_context={},
        obligations=[obligation],
        target_total_chapters=12,
        include_current=False,
    )

    assert result.status == "fail"
    assert result.issues[0].issue_type == "obligation_missing_from_future_plan"
    patch = result.plan_patches[0]
    assert patch.patch_type == "obligation_plan_binding"
    assert patch.source_obligation_ids == ["obl-shenyan"]
    assert patch.affected_chapters == [11]
    assert patch.writer_context_injections[0]["obligation_id"] == "obl-shenyan"
    assert patch.reviewer_context_injections[0]["payoff_test"] == obligation.payoff_test

    FuturePlanAuditor().apply_plan_patch(plan, patch)

    assert "obl-shenyan" in plan.experience_plan_json
    assert obligation.payoff_test in plan.goals_json
