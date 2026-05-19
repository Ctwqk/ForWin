from __future__ import annotations

from pydantic import BaseModel

from .band_role import BandRole


class BandContractTemplate(BaseModel):
    role: BandRole
    requirement_text: str
    requires_main_debt_closure: bool = False


OPENING_BAND_CONTRACT = BandContractTemplate(
    role=BandRole.opening,
    requirement_text=(
        "establish protagonist goals, introduce world stakes, and plant primary mystery hooks; "
        "do not require main-debt closure"
    ),
    requires_main_debt_closure=False,
)

MID_ARC_BAND_CONTRACT = BandContractTemplate(
    role=BandRole.mid_arc,
    requirement_text=(
        "deliver one staged payoff, advance main debts without closing them, "
        "and end with an explicit handoff hook to the next band"
    ),
    requires_main_debt_closure=False,
)

FINAL_BAND_CONTRACT = BandContractTemplate(
    role=BandRole.final,
    requirement_text=(
        "close all P0 main debts, close the main crisis, resolve terminal countdowns, "
        "and allow only clearly framed denouement carry-forward"
    ),
    requires_main_debt_closure=True,
)


def contract_for_role(role: BandRole | str) -> BandContractTemplate:
    resolved = BandRole(str(role or BandRole.mid_arc.value))
    if resolved == BandRole.opening:
        return OPENING_BAND_CONTRACT
    if resolved == BandRole.final:
        return FINAL_BAND_CONTRACT
    return MID_ARC_BAND_CONTRACT


__all__ = [
    "BandContractTemplate",
    "FINAL_BAND_CONTRACT",
    "MID_ARC_BAND_CONTRACT",
    "OPENING_BAND_CONTRACT",
    "contract_for_role",
]
