from __future__ import annotations

import json
import warnings
from collections import OrderedDict
from typing import Iterable

warnings.warn(
    "forwin.planning.scenario_rehearsal is deprecated; use "
    "forwin.planning.scenario_rehearsal_service for new orchestration. "
    "See Design-docs/DESIGN_STATUS.md.",
    DeprecationWarning,
    stacklevel=2,
)

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.models.subworld import SubWorld, SubWorldRosterItem
from forwin.models.world_v4 import ScenarioPlanPatchRow, ScenarioRehearsalRunRow
from forwin.planning.scenario_triggers import ScenarioTriggerContext, ScenarioTriggerEvaluator
from forwin.planning.world_contracts import (
    ArcWorldContract,
    BandWorldContract,
    ChapterWorldDeltaIntent,
    WorldContractRepository,
)
from forwin.protocol.scenario_rehearsal import (
    ScenarioPlanPatch,
    ScenarioRehearsalRecommendation,
    ScenarioRehearsalReport,
    ScenarioRiskFinding,
)


def _ordered_unique(values: Iterable[str]) -> list[str]:
    return list(OrderedDict.fromkeys(str(value) for value in values if str(value or "").strip()))


def _dump_report(report: ScenarioRehearsalReport) -> str:
    return json.dumps(report.model_dump(mode="json"), ensure_ascii=False)


def _load_report(raw: str) -> ScenarioRehearsalReport | None:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return None
    if not isinstance(payload, dict) or not payload:
        return None
    return ScenarioRehearsalReport.model_validate(payload)


def _json_object(raw: str) -> dict:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, ValueError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


class ScenarioRehearsalRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save(self, report: ScenarioRehearsalReport) -> ScenarioRehearsalRunRow:
        blocker_count = sum(1 for issue in report.risk_findings if issue.severity == "fail")
        row = ScenarioRehearsalRunRow(
            project_id=report.project_id,
            arc_id=report.arc_id,
            band_id=report.band_id,
            rehearsal_scope=report.rehearsal_scope,
            chapter_numbers_json=json.dumps(report.chapter_numbers, ensure_ascii=False),
            trigger_reasons_json=json.dumps(report.trigger_reasons, ensure_ascii=False),
            recommendation=report.recommendation.value,
            risk_count=len(report.risk_findings),
            blocker_count=blocker_count,
            required_patch_count=len(report.required_plan_patches),
            report_json=_dump_report(report),
        )
        self.session.add(row)
        self.session.flush()
        applied_by_key = {
            (item.patch_type, item.target, item.message): item.status
            for item in report.applied_patches
        }
        saved_patch_keys: set[tuple[str, str, str]] = set()
        for patch in report.required_plan_patches:
            key = (patch.patch_type, patch.target, patch.message)
            saved_patch_keys.add(key)
            status = applied_by_key.get((patch.patch_type, patch.target, patch.message), "proposed")
            self.session.add(
                ScenarioPlanPatchRow(
                    project_id=report.project_id,
                    run_id=row.id,
                    arc_id=report.arc_id,
                    band_id=report.band_id,
                    patch_type=patch.patch_type,
                    target=patch.target,
                    message=patch.message,
                    evidence_refs_json=json.dumps(patch.evidence_refs, ensure_ascii=False),
                    patch_json=json.dumps(patch.model_dump(mode="json"), ensure_ascii=False),
                    status=status if status in {"applied", "failed", "unsupported"} else "proposed",
                )
            )
        for patch in report.applied_patches:
            key = (patch.patch_type, patch.target, patch.message)
            if key in saved_patch_keys:
                continue
            self.session.add(
                ScenarioPlanPatchRow(
                    project_id=report.project_id,
                    run_id=row.id,
                    arc_id=report.arc_id,
                    band_id=report.band_id,
                    patch_type=patch.patch_type,
                    target=patch.target,
                    message=patch.message,
                    evidence_refs_json=json.dumps(patch.evidence_refs, ensure_ascii=False),
                    patch_json=json.dumps(patch.model_dump(mode="json"), ensure_ascii=False),
                    status=patch.status,
                )
            )
        self.session.flush()
        return row

    def latest_for_project(self, project_id: str) -> ScenarioRehearsalReport | None:
        row = self.session.execute(
            select(ScenarioRehearsalRunRow)
            .where(ScenarioRehearsalRunRow.project_id == project_id)
            .order_by(ScenarioRehearsalRunRow.created_at.desc(), ScenarioRehearsalRunRow.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        return _load_report(row.report_json) if row is not None else None


class ScenarioRehearsalRunner:
    """Deterministic planning preflight for world/cognition/reward risk."""

    def __init__(self, session: Session, *, director=None) -> None:
        self.session = session
        self.contracts = WorldContractRepository(session)
        self.repository = ScenarioRehearsalRepository(session)
        self.director = director

    def run_for_band(
        self,
        *,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_numbers: list[int],
        trigger_context: ScenarioTriggerContext | None = None,
    ) -> ScenarioRehearsalReport:
        report = self.evaluate_for_band(
            project_id=project_id,
            arc_id=arc_id,
            band_id=band_id,
            chapter_numbers=chapter_numbers,
            trigger_context=trigger_context,
        )
        self.repository.save(report)
        return report

    def evaluate_for_band(
        self,
        *,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_numbers: list[int],
        trigger_context: ScenarioTriggerContext | None = None,
    ) -> ScenarioRehearsalReport:
        if trigger_context is None:
            trigger_context = ScenarioTriggerEvaluator(self.session).evaluate_for_band(
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                chapter_numbers=chapter_numbers,
            )
        arc_contract = self.contracts.get_arc_contract(project_id, arc_id)
        band_contract = self.contracts.get_band_contract(project_id, band_id)
        intents = [
            intent
            for chapter_number in chapter_numbers
            if (intent := self.contracts.get_chapter_intent(project_id, chapter_number)) is not None
        ]
        band_experience = self.session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == project_id,
                BandExperiencePlan.band_id == band_id,
            )
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        chapter_experience_payloads: dict[int, dict] = {}
        for plan in self.session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number.in_(chapter_numbers),
            )
        ).scalars().all():
            chapter_experience_payloads[int(plan.chapter_number or 0)] = _json_object(
                getattr(plan, "experience_plan_json", "{}")
            )
        return self.build_report(
            project_id=project_id,
            arc_id=arc_id,
            band_id=band_id,
            chapter_numbers=chapter_numbers,
            arc_contract=arc_contract,
            band_contract=band_contract,
            chapter_intents=intents,
            band_experience_payload=(
                _json_object(band_experience.schedule_json) if band_experience is not None else {}
            ),
            chapter_experience_payloads=chapter_experience_payloads,
            trigger_context=trigger_context,
        )

    def build_report(
        self,
        *,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_numbers: list[int],
        arc_contract: ArcWorldContract | None,
        band_contract: BandWorldContract | None,
        chapter_intents: list[ChapterWorldDeltaIntent],
        band_experience_payload: dict | None = None,
        chapter_experience_payloads: dict[int, dict] | None = None,
        trigger_context: ScenarioTriggerContext | None = None,
    ) -> ScenarioRehearsalReport:
        trigger_reasons: list[str] = []
        band_experience_payload = band_experience_payload or {}
        chapter_experience_payloads = chapter_experience_payloads or {}
        trigger_context = trigger_context or ScenarioTriggerContext(
            should_run=True,
            reasons=[],
        )
        hidden_lines = _ordered_unique(
            [
                *(arc_contract.hidden_world_line_ids if arc_contract else []),
                *(band_contract.hidden_world_line_ids if band_contract else []),
            ]
        )
        if hidden_lines:
            trigger_reasons.append("hidden_world_line")
        if arc_contract is not None and arc_contract.reveal_ladder:
            trigger_reasons.append("reveal_ladder")

        must_not_reveal = _ordered_unique(
            item
            for intent in chapter_intents
            for item in intent.must_not_reveal
        )
        if must_not_reveal:
            trigger_reasons.append("must_not_reveal_guard")

        observer_changes = [
            change
            for intent in chapter_intents
            for change in intent.expected_observer_state_changes.values()
            if str(change or "").strip()
        ]
        if observer_changes or (arc_contract is not None and arc_contract.reader_cognition_trajectory):
            trigger_reasons.append("reader_cognition_transition")

        payoff_items = _ordered_unique(
            [
                *(arc_contract.short_term_payoff_promises if arc_contract else []),
                *(arc_contract.medium_term_payoff_promises if arc_contract else []),
                *(arc_contract.long_term_payoff_promises if arc_contract else []),
                *(band_contract.payoff_commitments if band_contract else []),
                *[
                    item
                    for intent in chapter_intents
                    for item in intent.reader_experience_intents
                ],
            ]
        )
        if payoff_items:
            trigger_reasons.append("reward_planning")

        active_subworld_ids = _ordered_unique(
            str(item)
            for item in (band_experience_payload.get("active_subworld_ids") or [])
        )
        chapter_entry_targets = [
            item
            for item in (band_experience_payload.get("chapter_entry_targets") or [])
            if isinstance(item, dict)
        ]
        if active_subworld_ids:
            trigger_reasons.append("subworld_activation")

        trigger_reasons = _ordered_unique([*trigger_context.reasons, *trigger_reasons])
        if trigger_reasons == ["low_risk_skip"] or not trigger_reasons:
            return ScenarioRehearsalReport(
                project_id=project_id,
                arc_id=arc_id,
                band_id=band_id,
                chapter_numbers=chapter_numbers,
                trigger_reasons=["low_risk_skip"],
                recommendation=ScenarioRehearsalRecommendation.PASS,
                metadata={
                    "skipped": True,
                    "simulation_mode": "deterministic",
                    "trigger_context": trigger_context.model_dump(),
                    "rule_results": [],
                },
            )

        world_state_deltas = _ordered_unique(
            [
                *[
                    item
                    for intent in chapter_intents
                    for item in [*intent.visible_delta_intents, *intent.offscreen_delta_intents]
                ],
                *[
                    f"{gap}:{transition}"
                    for gap, transition in (band_contract.gap_transitions.items() if band_contract else [])
                ],
            ]
        )
        character_knowledge_deltas = _ordered_unique(
            f"{observer}:{change}"
            for intent in chapter_intents
            for observer, change in intent.expected_observer_state_changes.items()
            if observer != "reader"
        )
        reader_cognition_deltas = _ordered_unique(
            [
                *[
                    change
                    for intent in chapter_intents
                    for observer, change in intent.expected_observer_state_changes.items()
                    if observer == "reader"
                ],
                *[
                    f"{step.chapter_hint}:{step.from_state}->{step.to_state}"
                    for step in (arc_contract.reader_cognition_trajectory if arc_contract else [])
                ],
            ]
        )
        visibility_deltas = _ordered_unique(
            [
                *[
                    f"{step.chapter_hint}:{step.gap_id}:{step.from_state}->{step.to_state}"
                    for step in (arc_contract.reveal_ladder if arc_contract else [])
                ],
                *[
                    f"must_not_reveal:{item}"
                    for item in must_not_reveal
                ],
            ]
        )

        risk_findings: list[ScenarioRiskFinding] = []
        future_conflicts: list[str] = []
        patches: list[ScenarioPlanPatch] = []
        reveal_guard_by_gap = {
            str(step.gap_id or ""): int(step.must_not_reveal_before or 0)
            for step in (arc_contract.reveal_ladder if arc_contract else [])
            if int(step.must_not_reveal_before or 0) > 0
        }
        reveal_items = _ordered_unique(
            item
            for intent in chapter_intents
            for item in intent.reveal_delta_intents
        )
        for reveal_id in reveal_items:
            guard_chapter = reveal_guard_by_gap.get(reveal_id, 0)
            if guard_chapter and min(chapter_numbers or [0]) < guard_chapter:
                risk_findings.append(
                    ScenarioRiskFinding(
                        risk_type="early_reveal_blocker",
                        severity="fail",
                        message="Reveal intent occurs before its must_not_reveal guard expires.",
                        evidence_refs=[f"reveal:{reveal_id}", f"must_not_reveal_before:{guard_chapter}"],
                        affected_chapters=list(chapter_numbers),
                    )
                )
                future_conflicts.append(f"early reveal before chapter {guard_chapter}: {reveal_id}")
        if must_not_reveal and (arc_contract is None or not arc_contract.reveal_ladder):
            risk_findings.append(
                ScenarioRiskFinding(
                    risk_type="missing_reveal_ladder",
                    severity="warn",
                    message="must_not_reveal exists but the arc has no reveal ladder.",
                    evidence_refs=["chapter_intent:must_not_reveal"],
                    affected_chapters=list(chapter_numbers),
                )
            )
            future_conflicts.append("hidden information has no planned reveal path")
            patches.append(
                ScenarioPlanPatch(
                    patch_type="add_reveal_ladder",
                    target="arc_world_contract.reveal_ladder",
                    message="Add planned hint/reveal/payoff steps before writing the band.",
                    evidence_refs=["chapter_intent:must_not_reveal"],
                )
            )
        if hidden_lines and band_contract is not None and not band_contract.required_hints:
            risk_findings.append(
                ScenarioRiskFinding(
                    risk_type="hidden_line_without_hint",
                    severity="warn",
                    message="Hidden world lines are active without required hint commitments.",
                    evidence_refs=["band_world_contract.hidden_world_line_ids"],
                    affected_chapters=list(chapter_numbers),
                )
            )
            patches.append(
                ScenarioPlanPatch(
                    patch_type="add_required_hints",
                    target="band_world_contract.required_hints",
                    message="Specify fair hints for the hidden line before writer execution.",
                    evidence_refs=["band_world_contract.hidden_world_line_ids"],
                )
            )
        if (
            band_contract is not None
            and band_contract.false_belief_adjustments
            and (arc_contract is None or not arc_contract.reader_cognition_trajectory)
        ):
            risk_findings.append(
                ScenarioRiskFinding(
                    risk_type="false_belief_without_exit",
                    severity="warn",
                    message="False belief adjustments have no reader cognition exit path.",
                    evidence_refs=["band_world_contract.false_belief_adjustments"],
                    affected_chapters=list(chapter_numbers),
                )
            )
            future_conflicts.append("false belief has no planned correction path")
            patches.append(
                ScenarioPlanPatch(
                    patch_type="add_false_belief_exit",
                    target="arc_world_contract.reader_cognition_trajectory",
                    message="Add an explicit reader cognition correction path.",
                    evidence_refs=["band_world_contract.false_belief_adjustments"],
                )
            )
        if active_subworld_ids:
            entry_subworld_ids = {
                str(item.get("subworld_id") or "")
                for item in chapter_entry_targets
                if str(item.get("subworld_id") or "").strip()
            }
            critical_role_slots = [
                item
                for item in (band_experience_payload.get("critical_role_slots") or [])
                if isinstance(item, dict)
            ]
            for subworld_id in active_subworld_ids:
                subworld = self.session.get(SubWorld, subworld_id)
                subworld_meta = _json_object(getattr(subworld, "metadata_json", "{}") if subworld is not None else "{}")
                roster_items = self.session.execute(
                    select(SubWorldRosterItem)
                    .where(SubWorldRosterItem.subworld_id == subworld_id)
                ).scalars().all()
                if not roster_items:
                    risk_findings.append(
                        ScenarioRiskFinding(
                            risk_type="subworld_roster_empty",
                            severity="warn",
                            message="Active subworld has no roster resources planned.",
                            evidence_refs=[f"subworld:{subworld_id}"],
                            affected_chapters=list(chapter_numbers),
                        )
                    )
                    patches.append(
                        ScenarioPlanPatch(
                            patch_type="add_subworld_roster_slot",
                            target=f"sub_world_roster_items:{subworld_id}",
                            message="Create at least one planned character slot for the active subworld.",
                            evidence_refs=[f"subworld:{subworld_id}"],
                            metadata={"subworld_id": subworld_id},
                        )
                    )
                elif subworld_id not in entry_subworld_ids:
                    risk_findings.append(
                        ScenarioRiskFinding(
                            risk_type="missing_entry_target",
                            severity="warn",
                            message="Active subworld has roster resources but no current band entry target.",
                            evidence_refs=[f"subworld:{subworld_id}"],
                            affected_chapters=list(chapter_numbers),
                        )
                    )
                if not (
                    str(subworld_meta.get("region_anchor") or "").strip()
                    and str(subworld_meta.get("node_anchor") or "").strip()
                ):
                    risk_findings.append(
                        ScenarioRiskFinding(
                            risk_type="missing_subworld_region_node",
                            severity="warn",
                            message="Active subworld is missing region/node anchors.",
                            evidence_refs=[f"subworld:{subworld_id}"],
                            affected_chapters=list(chapter_numbers),
                        )
                    )
                    patches.append(
                        ScenarioPlanPatch(
                            patch_type="add_subworld_anchor",
                            target=f"sub_worlds.metadata:{subworld_id}",
                            message="Add region/node anchors for the active subworld.",
                            evidence_refs=[f"subworld:{subworld_id}"],
                            metadata={"subworld_id": subworld_id},
                        )
                    )
                subworld_culture = str(subworld_meta.get("culture_profile_id") or "").strip()
                for roster in roster_items:
                    roster_meta = _json_object(getattr(roster, "metadata_json", "{}"))
                    roster_culture = str(roster_meta.get("culture_profile_id") or "").strip()
                    if subworld_culture and roster_culture and roster_culture != subworld_culture:
                        risk_findings.append(
                            ScenarioRiskFinding(
                                risk_type="subworld_culture_mismatch",
                                severity="warn",
                                message="Subworld roster culture profile does not match the active subworld.",
                                evidence_refs=[f"subworld:{subworld_id}", f"roster:{roster.id}"],
                                affected_chapters=list(chapter_numbers),
                            )
                        )
                        patches.append(
                            ScenarioPlanPatch(
                                patch_type="align_subworld_culture_profile",
                                target=f"sub_world_roster_items.metadata:{roster.id}",
                                message="Align roster culture_profile_id with the active subworld.",
                                evidence_refs=[f"roster:{roster.id}"],
                                metadata={"subworld_id": subworld_id, "roster_id": roster.id},
                            )
                        )
                for role_slot in critical_role_slots:
                    if str(role_slot.get("subworld_id") or "") != subworld_id:
                        continue
                    role = str(role_slot.get("role") or "").strip()
                    if not role:
                        continue
                    if not any(role in str(item.role_hint or "") for item in roster_items):
                        risk_findings.append(
                            ScenarioRiskFinding(
                                risk_type="missing_critical_role_slot",
                                severity="warn",
                                message=f"Active subworld is missing required {role} slot.",
                                evidence_refs=[f"subworld:{subworld_id}", f"role:{role}"],
                                affected_chapters=list(chapter_numbers),
                            )
                        )
                        patches.append(
                            ScenarioPlanPatch(
                                patch_type="add_subworld_roster_slot",
                                target=f"sub_world_roster_items:{subworld_id}:{role}",
                                message=f"Create planned {role} slot for the active subworld.",
                                evidence_refs=[f"subworld:{subworld_id}", f"role:{role}"],
                                metadata={"subworld_id": subworld_id, "role": role},
                            )
                        )
                    patches.append(
                        ScenarioPlanPatch(
                            patch_type="add_entry_target",
                            target=f"band_experience_plan.chapter_entry_targets:{subworld_id}",
                            message="Add an entry target for the active subworld.",
                            evidence_refs=[f"subworld:{subworld_id}"],
                            metadata={"subworld_id": subworld_id},
                        )
                    )
            if any(
                str(payload.get("entity_admission_rule") or "").strip() != "strict_named_character"
                for payload in chapter_experience_payloads.values()
            ):
                risk_findings.append(
                    ScenarioRiskFinding(
                        risk_type="entity_admission_rule_missing",
                        severity="warn",
                        message="Subworld-active chapter plan is missing strict named character admission.",
                        evidence_refs=["chapter_experience_plan.entity_admission_rule"],
                        affected_chapters=list(chapter_numbers),
                    )
                )
                patches.append(
                    ScenarioPlanPatch(
                        patch_type="set_entity_admission_rule",
                        target="chapter_experience_plan.entity_admission_rule",
                        message="Set strict_named_character on subworld-active chapter plans.",
                        evidence_refs=["chapter_experience_plan.entity_admission_rule"],
                    )
                )

        recommendation = ScenarioRehearsalRecommendation.PASS
        if any(issue.severity == "fail" for issue in risk_findings):
            recommendation = ScenarioRehearsalRecommendation.BLOCK
        elif any(patch.patch_type == "add_false_belief_exit" for patch in patches):
            recommendation = ScenarioRehearsalRecommendation.REPLAN
        elif patches:
            recommendation = ScenarioRehearsalRecommendation.PATCH

        report = ScenarioRehearsalReport(
            project_id=project_id,
            arc_id=arc_id,
            band_id=band_id,
            chapter_numbers=chapter_numbers,
            trigger_reasons=trigger_reasons,
            world_state_deltas=world_state_deltas,
            character_knowledge_deltas=character_knowledge_deltas,
            reader_cognition_deltas=reader_cognition_deltas,
            visibility_deltas=visibility_deltas,
            planned_rewards=payoff_items,
            risk_findings=risk_findings,
            future_conflicts=future_conflicts,
            required_plan_patches=patches,
            recommendation=recommendation,
            metadata={
                "simulation_mode": "hybrid" if self.director is not None else "deterministic",
                "trigger_context": trigger_context.model_dump(),
                "rule_results": [
                    {"risk_type": finding.risk_type, "severity": finding.severity}
                    for finding in risk_findings
                ],
                "director_used": False,
                "director_error": "",
            },
        )
        return self._apply_director_simulation(report)

    def _apply_director_simulation(
        self,
        report: ScenarioRehearsalReport,
    ) -> ScenarioRehearsalReport:
        if self.director is None or not hasattr(self.director, "rehearse_scenario"):
            return report
        metadata = dict(report.metadata)
        try:
            payload = self.director.rehearse_scenario(report=report.model_dump(mode="json"))
        except Exception as exc:  # noqa: BLE001
            metadata["director_used"] = False
            metadata["director_error"] = str(exc)
            metadata["simulation_mode"] = "hybrid"
            return report.model_copy(update={"metadata": metadata})
        if not isinstance(payload, dict):
            metadata["director_used"] = False
            metadata["director_error"] = "director returned non-object payload"
            metadata["simulation_mode"] = "hybrid"
            return report.model_copy(update={"metadata": metadata})

        director_findings = [
            ScenarioRiskFinding.model_validate(item)
            for item in (payload.get("risk_findings") or [])
            if isinstance(item, dict)
        ]
        director_patches = [
            ScenarioPlanPatch.model_validate(item)
            for item in (payload.get("required_plan_patches") or [])
            if isinstance(item, dict)
        ]
        future_conflicts = list(report.future_conflicts)
        future_conflicts.extend(
            str(item)
            for item in (payload.get("future_conflicts") or [])
            if str(item or "").strip()
        )
        recommendation = _merge_recommendations(
            report.recommendation,
            str(payload.get("recommendation") or "pass"),
            [*report.risk_findings, *director_findings],
            [*report.required_plan_patches, *director_patches],
        )
        metadata["director_used"] = True
        metadata["director_error"] = ""
        metadata["simulation_mode"] = "hybrid"
        metadata["director_recommendation"] = str(payload.get("recommendation") or "pass")
        return report.model_copy(
            update={
                "risk_findings": [*report.risk_findings, *director_findings],
                "future_conflicts": _ordered_unique(future_conflicts),
                "required_plan_patches": [*report.required_plan_patches, *director_patches],
                "recommendation": recommendation,
                "metadata": metadata,
            }
        )


def _merge_recommendations(
    deterministic: ScenarioRehearsalRecommendation,
    director_recommendation: str,
    risk_findings: list[ScenarioRiskFinding],
    patches: list[ScenarioPlanPatch],
) -> ScenarioRehearsalRecommendation:
    if any(item.severity == "fail" for item in risk_findings):
        return ScenarioRehearsalRecommendation.BLOCK
    normalized_director = str(director_recommendation or "pass").strip().lower()
    rank = {
        "pass": 0,
        "patch": 1,
        "replan": 2,
        "block": 3,
    }
    deterministic_value = deterministic.value if hasattr(deterministic, "value") else str(deterministic)
    best = max(rank.get(deterministic_value, 0), rank.get(normalized_director, 0))
    if best >= 3:
        return ScenarioRehearsalRecommendation.BLOCK
    if best >= 2:
        return ScenarioRehearsalRecommendation.REPLAN
    if best >= 1 or patches:
        return ScenarioRehearsalRecommendation.PATCH
    return ScenarioRehearsalRecommendation.PASS
