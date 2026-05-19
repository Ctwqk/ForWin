from __future__ import annotations

from forwin.planning.band_plan.band_role import BandRole
from forwin.planning.band_plan.contract_templates import contract_for_role
from forwin.protocol.experience import BandDelightSchedule


def test_mid_arc_contract_does_not_require_p0_p1_closure() -> None:
    contract = contract_for_role(BandRole.mid_arc)

    assert "close all P0" not in contract.requirement_text
    assert "handoff hook" in contract.requirement_text
    assert contract.requires_main_debt_closure is False


def test_schedule_can_store_band_role_contract_metadata() -> None:
    contract = contract_for_role(BandRole.final)
    schedule = BandDelightSchedule(
        band_id="band:25:30",
        chapter_start=25,
        chapter_end=30,
        band_role=BandRole.final.value,
        band_contract_template=contract.model_dump(mode="json"),
    )

    assert schedule.band_role == "final"
    assert schedule.band_contract_template["requires_main_debt_closure"] is True
