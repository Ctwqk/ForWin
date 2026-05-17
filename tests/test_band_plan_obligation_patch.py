from __future__ import annotations

import json

from forwin.governance import load_plan_task_contract
from forwin.models.phase import BandExperiencePlan
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.protocol.experience import BandDelightSchedule


def _band_row() -> BandExperiencePlan:
    schedule = BandDelightSchedule(
        band_id="arc-1:band:2",
        chapter_start=11,
        chapter_end=14,
        stall_guard_max_gap=1,
    )
    return BandExperiencePlan(
        id="band-row-1",
        project_id="project-1",
        arc_id="arc-1",
        band_id="arc-1:band:2",
        chapter_start=11,
        chapter_end=14,
        stall_guard_max_gap=1,
        schedule_json=json.dumps(schedule.model_dump(mode="json"), ensure_ascii=False),
        task_contract_json="[]",
    )


def _obligation() -> NarrativeObligation:
    return NarrativeObligation(
        id="obl-band",
        project_id="project-1",
        origin_chapter_number=10,
        obligation_type="reader_promise_payoff",
        priority="P1",
        status="planned",
        summary="本 band 必须兑现前文对审计窗口真相的读者承诺。",
        deferral_reason="需要多个章节铺垫与兑现。",
        hardness="design_debt",
        deadline_chapter=14,
        payoff_test="第14章前必须给出审计窗口真相的实质证据。",
        metadata={"minimum_scope": "band"},
    )


def test_band_plan_patcher_writes_obligation_contract_and_band_task_contract() -> None:
    row = _band_row()
    obligation = _obligation()
    patcher = BandPlanPatcher()
    patch = patcher.build_obligation_patch(
        project_id="project-1",
        band_row=row,
        obligations=[obligation],
        current_chapter=10,
        patch_type="band_defer_acceptance",
    )

    patcher.apply(row, patch, obligations=[obligation])

    schedule = BandDelightSchedule.model_validate(json.loads(row.schedule_json))
    contract = schedule.band_obligation_contract
    assert contract.open_obligations == ["obl-band"]
    assert contract.must_resolve_by_band_end == ["obl-band"]
    assert contract.allowed_carry_forward == []
    assert contract.payoff_tests["obl-band"] == obligation.payoff_test
    assert contract.affected_chapters["obl-band"] == [11, 12, 13, 14]
    assert contract.writer_context_injections[0]["obligation_id"] == "obl-band"
    assert contract.reviewer_context_injections[0]["payoff_test"] == obligation.payoff_test

    tasks = load_plan_task_contract(row.task_contract_json)
    assert [(task.source, task.description, task.required_keywords) for task in tasks] == [
        (
            "narrative_obligation",
            obligation.payoff_test,
            ["审计窗口真相", "实质证据"],
        )
    ]


def test_band_plan_patcher_is_idempotent_for_same_obligation() -> None:
    row = _band_row()
    obligation = _obligation()
    patcher = BandPlanPatcher()
    patch = patcher.build_obligation_patch(
        project_id="project-1",
        band_row=row,
        obligations=[obligation],
        current_chapter=10,
        patch_type="band_defer_acceptance",
    )

    patcher.apply(row, patch, obligations=[obligation])
    patcher.apply(row, patch, obligations=[obligation])

    schedule = BandDelightSchedule.model_validate(json.loads(row.schedule_json))
    assert schedule.band_obligation_contract.open_obligations == ["obl-band"]
    tasks = load_plan_task_contract(row.task_contract_json)
    assert [task.source for task in tasks].count("narrative_obligation") == 1
