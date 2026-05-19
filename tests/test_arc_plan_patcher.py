from __future__ import annotations

from forwin.planning.arc_plan_patcher import ArcPlanPatcher


def test_arc_plan_patcher_creates_narrative_plan_patch() -> None:
    patch = ArcPlanPatcher().build_patch(
        project_id="project-1",
        origin_chapter_number=10,
        target_arc_id="arc-1",
        issue_kind="identity_ambiguity",
        summary="身份线索需要在本 arc 内澄清。",
        source_signal_ids=["sig-1"],
        source_obligation_ids=["obl-1"],
        payoff_test="本 arc 结束前必须解释身份线索。",
        affected_chapters=[11, 12],
    )

    assert patch.project_id == "project-1"
    assert patch.target_scope == "arc"
    assert patch.target_arc_id == "arc-1"
    assert patch.affected_chapters == [11, 12]
    assert patch.source_signal_ids == ["sig-1"]
    assert patch.source_obligation_ids == ["obl-1"]
    assert "payoff_test" in patch.new_contract
