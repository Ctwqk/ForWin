from __future__ import annotations

import json
from dataclasses import dataclass, field

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.phase import BandExperiencePlan
from forwin.planning.world_contracts import WorldContractRepository


def _json_object(raw: str) -> dict:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


@dataclass(slots=True)
class ScenarioTriggerContext:
    should_run: bool
    reasons: list[str] = field(default_factory=list)
    boundary_kind: str = ""
    consecutive_review_failures: int = 0
    repair_escalated: bool = False
    future_dependency_refs: list[str] = field(default_factory=list)

    @property
    def high_complexity(self) -> bool:
        return any(
            reason in set(self.reasons)
            for reason in (
                "major_reveal",
                "false_belief",
                "reader_cognition_transition",
                "long_payoff_release",
                "future_dependency",
                "repair_escalation",
                "consecutive_review_fail",
            )
        )

    def model_dump(self) -> dict:
        return {
            "should_run": self.should_run,
            "reasons": list(self.reasons),
            "boundary_kind": self.boundary_kind,
            "consecutive_review_failures": self.consecutive_review_failures,
            "repair_escalated": self.repair_escalated,
            "future_dependency_refs": list(self.future_dependency_refs),
            "high_complexity": self.high_complexity,
        }


class ScenarioTriggerEvaluator:
    def __init__(self, session: Session) -> None:
        self.session = session
        self.contracts = WorldContractRepository(session)

    def evaluate_for_band(
        self,
        *,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_numbers: list[int],
        boundary_kind: str = "band_plan",
        consecutive_review_failures: int = 0,
        repair_escalated: bool = False,
        future_dependency_refs: list[str] | None = None,
    ) -> ScenarioTriggerContext:
        reasons: list[str] = []
        if boundary_kind in {"book_genesis", "new_arc", "arc_start"}:
            reasons.append("new_arc")

        arc_contract = self.contracts.get_arc_contract(project_id, arc_id)
        band_contract = self.contracts.get_band_contract(project_id, band_id)
        if arc_contract is not None:
            if arc_contract.hidden_world_line_ids or arc_contract.major_gap_ids:
                reasons.append("future_dependency")
            if arc_contract.reveal_ladder:
                reasons.append("major_reveal")
            if arc_contract.reader_cognition_trajectory:
                reasons.append("reader_cognition_transition")
            if arc_contract.long_term_payoff_promises:
                reasons.append("long_payoff_release")
            if arc_contract.false_belief_ids:
                reasons.append("false_belief")
        if band_contract is not None:
            if band_contract.false_belief_adjustments:
                reasons.append("false_belief")
            if band_contract.payoff_commitments:
                reasons.append("reward_planning")

        band_experience = self.session.execute(
            select(BandExperiencePlan)
            .where(BandExperiencePlan.project_id == project_id, BandExperiencePlan.band_id == band_id)
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if band_experience is not None:
            payload = _json_object(band_experience.schedule_json)
            if payload.get("active_subworld_ids"):
                reasons.append("subworld_activation")

        for chapter_number in chapter_numbers:
            intent = self.contracts.get_chapter_intent(project_id, chapter_number)
            if intent is None:
                continue
            if intent.must_not_reveal:
                reasons.append("must_not_reveal_guard")
            if intent.reveal_delta_intents:
                reasons.append("major_reveal")
            if intent.expected_observer_state_changes.get("reader"):
                reasons.append("reader_cognition_transition")
            if intent.reader_experience_intents:
                reasons.append("reward_planning")

        if consecutive_review_failures >= 2:
            reasons.append("consecutive_review_fail")
        if repair_escalated:
            reasons.append("repair_escalation")
        normalized_future_refs = [
            str(item).strip()
            for item in (future_dependency_refs or [])
            if str(item).strip()
        ]
        if normalized_future_refs:
            reasons.append("future_dependency")

        unique_reasons = list(dict.fromkeys(reasons))
        if not unique_reasons:
            unique_reasons = ["low_risk_skip"]
        return ScenarioTriggerContext(
            should_run=unique_reasons != ["low_risk_skip"],
            reasons=unique_reasons,
            boundary_kind=boundary_kind,
            consecutive_review_failures=max(0, int(consecutive_review_failures or 0)),
            repair_escalated=bool(repair_escalated),
            future_dependency_refs=normalized_future_refs,
        )
