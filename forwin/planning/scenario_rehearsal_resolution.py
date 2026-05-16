from __future__ import annotations

import json
from dataclasses import dataclass

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from forwin.governance import (
    BandCheckpointDetail,
    BandCheckpointIssueInfo,
    DecisionEventType,
    ensure_decision_event_type,
    issue_group_for_issue,
)
from forwin.models.base import new_id
from forwin.models.governance import DecisionEvent
from forwin.models.phase import ArcStructureDraft, BandExperiencePlan, ProjectReplanEvent
from forwin.models.project import ArcPlanVersion, ChapterPlan
from forwin.models.subworld import SubWorld, SubWorldRosterItem
from forwin.models.world_v4 import ScenarioRehearsalRunRow
from forwin.planning.scenario_rehearsal import ScenarioRehearsalRepository, ScenarioRehearsalRunner
from forwin.planning.world_contracts import (
    ArcWorldContract,
    ReaderCognitionTransition,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.protocol.scenario_rehearsal import (
    ScenarioAppliedPatch,
    ScenarioPlanPatch,
    ScenarioRehearsalRecommendation,
    ScenarioRehearsalReport,
)


TERMINAL_BLOCKING_RESOLUTION_STATUSES = {
    "manual_patch_required",
    "replan_required",
    "blocked",
}
AUTO_PATCH_TYPES = {
    "add_reveal_ladder",
    "add_required_hints",
    "add_subworld_roster_slot",
    "add_entry_target",
    "set_entity_admission_rule",
    "add_subworld_anchor",
    "align_subworld_culture_profile",
}


@dataclass(slots=True)
class ScenarioRehearsalOutcome:
    status: str
    report: ScenarioRehearsalReport
    checkpoint_id: str = ""
    replan_event_id: str = ""
    applied_patches: list[ScenarioAppliedPatch] | None = None

    @property
    def blocks_writing(self) -> bool:
        return self.status in TERMINAL_BLOCKING_RESOLUTION_STATUSES


class ScenarioRehearsalCoordinator:
    def __init__(self, session: Session, *, director=None) -> None:
        self.session = session
        self.runner = ScenarioRehearsalRunner(session, director=director)
        self.repository = ScenarioRehearsalRepository(session)
        self.contracts = WorldContractRepository(session)

    def run_for_band(
        self,
        *,
        project_id: str,
        arc_id: str,
        band_id: str,
        chapter_numbers: list[int],
        max_patch_attempts: int = 1,
    ) -> ScenarioRehearsalOutcome:
        report = self.runner.evaluate_for_band(
            project_id=project_id,
            arc_id=arc_id,
            band_id=band_id,
            chapter_numbers=chapter_numbers,
        )
        if report.recommendation == ScenarioRehearsalRecommendation.PASS:
            status = "skipped" if report.metadata.get("skipped") else "passed"
            report = self._finalize_report(report, resolution_status=status)
            self._save_and_record_evaluation(report, summary=f"Scenario rehearsal {status}。")
            return ScenarioRehearsalOutcome(status=status, report=report, applied_patches=[])

        if report.recommendation == ScenarioRehearsalRecommendation.PATCH:
            applied = self._apply_known_patches(report) if max_patch_attempts > 0 else []
            if applied:
                self._record_scenario_event(
                    report,
                    event_type=DecisionEventType.SCENARIO_REHEARSAL_PATCH_APPLIED,
                    summary="Scenario rehearsal 已尝试计划补丁。",
                    payload={"applied_patches": [item.model_dump(mode="json") for item in applied]},
                )
            if applied and all(item.status == "applied" for item in applied) and max_patch_attempts > 0:
                rerun = self.runner.evaluate_for_band(
                    project_id=project_id,
                    arc_id=arc_id,
                    band_id=band_id,
                    chapter_numbers=chapter_numbers,
                )
                if rerun.recommendation == ScenarioRehearsalRecommendation.PASS:
                    rerun = self._finalize_report(
                        rerun,
                        resolution_status="patched_passed",
                        applied_patches=applied,
                        patch_attempt_count=1,
                    )
                    self._save_and_record_evaluation(rerun, summary="Scenario rehearsal 计划补丁后通过。")
                    return ScenarioRehearsalOutcome(
                        status="patched_passed",
                        report=rerun,
                        applied_patches=applied,
                    )
                checkpoint_id = self._create_checkpoint(
                    report=rerun,
                    status="pending",
                    summary="Scenario rehearsal patch 后仍需人工处理。",
                )
                rerun = self._finalize_report(
                    rerun,
                    resolution_status="manual_patch_required",
                    applied_patches=applied,
                    patch_attempt_count=1,
                    checkpoint_id=checkpoint_id,
                )
                self._save_and_record_evaluation(rerun, summary="Scenario rehearsal 计划补丁后仍需人工处理。")
                return ScenarioRehearsalOutcome(
                    status="manual_patch_required",
                    report=rerun,
                    checkpoint_id=checkpoint_id,
                    applied_patches=applied,
                )
            checkpoint_id = self._create_checkpoint(
                report=report,
                status="pending",
                summary="Scenario rehearsal 需要人工计划补丁。",
            )
            report = self._finalize_report(
                report,
                resolution_status="manual_patch_required",
                applied_patches=applied,
                checkpoint_id=checkpoint_id,
            )
            self._save_and_record_evaluation(report, summary="Scenario rehearsal 需要人工计划补丁。")
            return ScenarioRehearsalOutcome(
                status="manual_patch_required",
                report=report,
                checkpoint_id=checkpoint_id,
                applied_patches=applied,
            )

        if report.recommendation == ScenarioRehearsalRecommendation.REPLAN:
            replan_metadata = self._create_replan_plan_version(report)
            if replan_metadata and max_patch_attempts > 0:
                rerun = self.runner.evaluate_for_band(
                    project_id=project_id,
                    arc_id=str(replan_metadata.get("new_arc_id") or arc_id),
                    band_id=band_id,
                    chapter_numbers=chapter_numbers,
                )
                rerun = rerun.model_copy(
                    update={
                        "metadata": {
                            **dict(rerun.metadata),
                            "replan": json.dumps(replan_metadata, ensure_ascii=False),
                        }
                    }
                )
                event_id = self._create_replan_event(rerun, status="applied")
                self._record_scenario_event(
                    report,
                    event_type=DecisionEventType.SCENARIO_REHEARSAL_REPLAN_REQUIRED,
                    summary="Scenario rehearsal 已生成新计划版本。",
                    related_object_type="project_replan_event",
                    related_object_id=event_id,
                    payload={"replan": replan_metadata},
                )
                if rerun.recommendation == ScenarioRehearsalRecommendation.PASS:
                    rerun = self._finalize_report(
                        rerun,
                        resolution_status="replanned_passed",
                        replan_event_id=event_id,
                    )
                    self._save_and_record_evaluation(rerun, summary="Scenario rehearsal 重排计划后通过。")
                    return ScenarioRehearsalOutcome(
                        status="replanned_passed",
                        report=rerun,
                        replan_event_id=event_id,
                        applied_patches=[],
                    )
                checkpoint_id = self._create_checkpoint(
                    report=rerun,
                    status="pending",
                    summary="Scenario rehearsal 重排计划后仍需人工治理。",
                )
                rerun = self._finalize_report(
                    rerun,
                    resolution_status="replan_required",
                    checkpoint_id=checkpoint_id,
                    replan_event_id=event_id,
                )
                self._save_and_record_evaluation(rerun, summary="Scenario rehearsal 重排计划后仍未通过。")
                return ScenarioRehearsalOutcome(
                    status="replan_required",
                    report=rerun,
                    checkpoint_id=checkpoint_id,
                    replan_event_id=event_id,
                    applied_patches=[],
                )

            event_id = self._create_replan_event(report, status="pending")
            self._record_scenario_event(
                report,
                event_type=DecisionEventType.SCENARIO_REHEARSAL_REPLAN_REQUIRED,
                summary="Scenario rehearsal 要求回到 planner/director 重排计划。",
                related_object_type="project_replan_event",
                related_object_id=event_id,
            )
            checkpoint_id = self._create_checkpoint(
                report=report,
                status="pending",
                summary="Scenario rehearsal 要求回到 planner/director 重排计划。",
            )
            report = self._finalize_report(
                report,
                resolution_status="replan_required",
                checkpoint_id=checkpoint_id,
                replan_event_id=event_id,
            )
            self._save_and_record_evaluation(report, summary="Scenario rehearsal 等待重排计划。")
            return ScenarioRehearsalOutcome(
                status="replan_required",
                report=report,
                checkpoint_id=checkpoint_id,
                replan_event_id=event_id,
                applied_patches=[],
            )

        checkpoint_id = self._create_checkpoint(
            report=report,
            status="fail",
            summary="Scenario rehearsal 已阻断当前写作计划。",
        )
        self._record_scenario_event(
            report,
            event_type=DecisionEventType.SCENARIO_REHEARSAL_BLOCKED,
            summary="Scenario rehearsal 阻断当前写作计划。",
            related_object_type="band_checkpoint",
            related_object_id=checkpoint_id,
        )
        report = self._finalize_report(
            report,
            resolution_status="blocked",
            checkpoint_id=checkpoint_id,
        )
        self._save_and_record_evaluation(report, summary="Scenario rehearsal 已阻断。")
        return ScenarioRehearsalOutcome(
            status="blocked",
            report=report,
            checkpoint_id=checkpoint_id,
            applied_patches=[],
        )

    def _finalize_report(
        self,
        report: ScenarioRehearsalReport,
        *,
        resolution_status: str,
        applied_patches: list[ScenarioAppliedPatch] | None = None,
        patch_attempt_count: int = 0,
        checkpoint_id: str = "",
        replan_event_id: str = "",
    ) -> ScenarioRehearsalReport:
        return report.model_copy(
            update={
                "resolution_status": resolution_status,
                "applied_patches": list(applied_patches or []),
                "patch_attempt_count": patch_attempt_count,
                "checkpoint_id": checkpoint_id,
                "replan_event_id": replan_event_id,
            }
        )

    def _save_and_record_evaluation(
        self,
        report: ScenarioRehearsalReport,
        *,
        summary: str,
    ) -> ScenarioRehearsalRunRow:
        row = self.repository.save(report)
        self._record_scenario_event(
            report,
            event_type=DecisionEventType.SCENARIO_REHEARSAL_EVALUATED,
            summary=summary,
            related_object_type="scenario_rehearsal_run",
            related_object_id=row.id,
        )
        return row

    def _record_scenario_event(
        self,
        report: ScenarioRehearsalReport,
        *,
        event_type: str,
        summary: str,
        reason: str = "",
        related_object_type: str = "",
        related_object_id: str = "",
        payload: dict | None = None,
    ) -> DecisionEvent:
        chapter_number = min(report.chapter_numbers or [0])
        event_payload = {
            "arc_id": report.arc_id,
            "band_id": report.band_id,
            "chapter_numbers": list(report.chapter_numbers),
            "recommendation": str(report.recommendation.value if hasattr(report.recommendation, "value") else report.recommendation),
            "resolution_status": report.resolution_status,
            "trigger_reasons": list(report.trigger_reasons),
            **(payload or {}),
        }
        row = DecisionEvent(
            id=new_id(),
            project_id=report.project_id,
            band_id=report.band_id,
            chapter_number=chapter_number,
            scope="band",
            event_family="evaluation_verdict",
            event_type=ensure_decision_event_type(event_type),
            actor_type="system",
            summary=summary,
            reason=reason or "; ".join(report.trigger_reasons),
            payload_json=json.dumps(event_payload, ensure_ascii=False),
            related_object_type=related_object_type,
            related_object_id=related_object_id,
        )
        self.session.add(row)
        self.session.flush()
        if not str(row.causal_root_id or "").strip():
            row.causal_root_id = row.id
            self.session.add(row)
            self.session.flush()
        return row

    def _apply_known_patches(self, report: ScenarioRehearsalReport) -> list[ScenarioAppliedPatch]:
        applied: list[ScenarioAppliedPatch] = []
        for patch in report.required_plan_patches:
            if patch.patch_type not in AUTO_PATCH_TYPES:
                applied.append(self._patch_result(patch, status="unsupported", message="Patch type requires manual handling."))
                continue
            method = getattr(self, f"_apply_{patch.patch_type}", None)
            if method is None:
                applied.append(self._patch_result(patch, status="unsupported", message="Patch handler is not registered."))
                continue
            try:
                method(report, patch)
            except Exception as exc:  # noqa: BLE001
                applied.append(self._patch_result(patch, status="failed", message=str(exc)))
            else:
                applied.append(self._patch_result(patch, status="applied", message=patch.message))
        return applied

    @staticmethod
    def _patch_result(patch: ScenarioPlanPatch, *, status: str, message: str) -> ScenarioAppliedPatch:
        return ScenarioAppliedPatch(
            patch_type=patch.patch_type,
            target=patch.target,
            status=status,
            message=message,
            evidence_refs=list(patch.evidence_refs),
            metadata=dict(patch.metadata),
        )

    def _apply_add_reveal_ladder(
        self,
        report: ScenarioRehearsalReport,
        patch: ScenarioPlanPatch,
    ) -> None:
        contract = self.contracts.get_arc_contract(report.project_id, report.arc_id)
        if contract is None:
            arc = self.session.get(ArcPlanVersion, report.arc_id)
            contract = ArcWorldContract(
                contract_id=f"arc_contract:{report.arc_id}",
                project_id=report.project_id,
                arc_id=report.arc_id,
                arc_number=int(getattr(arc, "arc_number", 1) or 1),
            )
        gap_id = (
            contract.major_gap_ids[0]
            if contract.major_gap_ids
            else (contract.hidden_world_line_ids[0] if contract.hidden_world_line_ids else "scenario_gap")
        )
        existing = {str(step.gap_id or "") for step in contract.reveal_ladder}
        if gap_id not in existing:
            chapter_hint = min(report.chapter_numbers or [1])
            contract.reveal_ladder.append(
                RevealLadderStep(
                    gap_id=gap_id,
                    chapter_hint=chapter_hint,
                    from_state="hidden",
                    to_state="hinted",
                    method="scenario_auto_patch",
                    fairness_evidence=list(patch.evidence_refs or ["scenario_rehearsal"]),
                )
            )
        self.contracts.save_arc_contract(contract)

    def _apply_add_required_hints(
        self,
        report: ScenarioRehearsalReport,
        patch: ScenarioPlanPatch,
    ) -> None:
        contract = self.contracts.get_band_contract(report.project_id, report.band_id)
        if contract is None:
            raise ValueError("band contract is missing")
        hints = list(contract.required_hints)
        for chapter_number in report.chapter_numbers:
            intent = self.contracts.get_chapter_intent(report.project_id, chapter_number)
            if intent is None:
                continue
            hints.extend(intent.hint_delta_intents)
            hints.extend(f"hint:{item}" for item in intent.must_not_reveal)
        if not hints:
            hints.extend(f"hint:{item}" for item in contract.hidden_world_line_ids)
        contract.required_hints = list(dict.fromkeys(str(item) for item in hints if str(item or "").strip()))
        self.contracts.save_band_contract(contract)

    def _apply_add_subworld_roster_slot(
        self,
        report: ScenarioRehearsalReport,
        patch: ScenarioPlanPatch,
    ) -> None:
        subworld_id = str(patch.metadata.get("subworld_id") or "").strip()
        if not subworld_id:
            raise ValueError("subworld_id is required")
        existing = self.session.execute(
            select(SubWorldRosterItem)
            .where(SubWorldRosterItem.project_id == report.project_id, SubWorldRosterItem.subworld_id == subworld_id)
            .limit(1)
        ).scalar_one_or_none()
        if existing is not None:
            return
        self.session.add(
            SubWorldRosterItem(
                project_id=report.project_id,
                subworld_id=subworld_id,
                entity_kind="character",
                display_name="待定角色",
                slot_key=f"scenario_slot:{report.band_id}",
                role_hint=str(patch.metadata.get("role") or "scenario rehearsal 自动补位"),
                description="用于防止 writer 随机创造关键角色。",
                is_core=True,
                status="planned_slot",
                activation_chapter=min(report.chapter_numbers or [0]),
                metadata_json=json.dumps({"source": "scenario_rehearsal"}, ensure_ascii=False),
            )
        )
        self.session.flush()

    def _apply_add_subworld_anchor(
        self,
        report: ScenarioRehearsalReport,
        patch: ScenarioPlanPatch,
    ) -> None:
        subworld_id = str(patch.metadata.get("subworld_id") or "").strip()
        if not subworld_id:
            raise ValueError("subworld_id is required")
        row = self.session.get(SubWorld, subworld_id)
        if row is None:
            raise ValueError("subworld is missing")
        metadata = self._json_object(row.metadata_json)
        metadata.setdefault("region_anchor", f"scenario_region:{report.band_id}")
        metadata.setdefault("node_anchor", f"scenario_node:{report.band_id}")
        metadata.setdefault("anchor_source", "scenario_rehearsal")
        row.metadata_json = json.dumps(metadata, ensure_ascii=False)
        self.session.add(row)
        self.session.flush()

    def _apply_align_subworld_culture_profile(
        self,
        _report: ScenarioRehearsalReport,
        patch: ScenarioPlanPatch,
    ) -> None:
        subworld_id = str(patch.metadata.get("subworld_id") or "").strip()
        roster_id = str(patch.metadata.get("roster_id") or "").strip()
        subworld = self.session.get(SubWorld, subworld_id) if subworld_id else None
        roster = self.session.get(SubWorldRosterItem, roster_id) if roster_id else None
        if subworld is None or roster is None:
            raise ValueError("subworld or roster item is missing")
        subworld_meta = self._json_object(subworld.metadata_json)
        culture_profile_id = str(subworld_meta.get("culture_profile_id") or "").strip()
        if not culture_profile_id:
            return
        roster_meta = self._json_object(roster.metadata_json)
        roster_meta["culture_profile_id"] = culture_profile_id
        roster.metadata_json = json.dumps(roster_meta, ensure_ascii=False)
        self.session.add(roster)
        self.session.flush()

    def _apply_add_entry_target(
        self,
        report: ScenarioRehearsalReport,
        patch: ScenarioPlanPatch,
    ) -> None:
        subworld_id = str(patch.metadata.get("subworld_id") or "").strip()
        if not subworld_id:
            raise ValueError("subworld_id is required")
        row = self.session.execute(
            select(BandExperiencePlan)
            .where(BandExperiencePlan.project_id == report.project_id, BandExperiencePlan.band_id == report.band_id)
            .order_by(BandExperiencePlan.created_at.desc(), BandExperiencePlan.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if row is None:
            raise ValueError("band experience plan is missing")
        payload = self._json_object(row.schedule_json)
        targets = [item for item in (payload.get("chapter_entry_targets") or []) if isinstance(item, dict)]
        if not any(str(item.get("subworld_id") or "") == subworld_id for item in targets):
            roster = self.session.execute(
                select(SubWorldRosterItem)
                .where(SubWorldRosterItem.project_id == report.project_id, SubWorldRosterItem.subworld_id == subworld_id)
                .order_by(SubWorldRosterItem.is_core.desc(), SubWorldRosterItem.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            targets.append(
                {
                    "chapter_hint": min(report.chapter_numbers or [0]),
                    "entity_name": str(getattr(roster, "display_name", "") or "待定角色"),
                    "subworld_id": subworld_id,
                    "role_hint": str(getattr(roster, "role_hint", "") or "scenario rehearsal 补位"),
                }
            )
        payload["chapter_entry_targets"] = targets
        row.schedule_json = json.dumps(payload, ensure_ascii=False)
        self.session.add(row)
        self.session.flush()

    def _apply_set_entity_admission_rule(
        self,
        report: ScenarioRehearsalReport,
        _patch: ScenarioPlanPatch,
    ) -> None:
        plans = self.session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == report.project_id,
                ChapterPlan.chapter_number.in_(report.chapter_numbers),
            )
        ).scalars().all()
        for plan in plans:
            payload = self._json_object(plan.experience_plan_json)
            payload["entity_admission_rule"] = "strict_named_character"
            plan.experience_plan_json = json.dumps(payload, ensure_ascii=False)
            self.session.add(plan)
        self.session.flush()

    def _create_checkpoint(
        self,
        *,
        report: ScenarioRehearsalReport,
        status: str,
        summary: str,
    ) -> str:
        from forwin.state.updater import StateUpdater

        updater = StateUpdater(self.session)
        chapter_start = min(report.chapter_numbers or [0])
        chapter_end = max(report.chapter_numbers or [0])
        row = updater.save_band_checkpoint(
            BandCheckpointDetail(
                project_id=report.project_id,
                arc_id=report.arc_id,
                band_id=report.band_id,
                chapter_start=chapter_start,
                chapter_end=chapter_end,
                trigger_source="scenario_rehearsal",
                boundary_kind="chapter_start",
                boundary_chapter=chapter_start,
                status=status,
                summary=summary,
                reason="; ".join(report.trigger_reasons),
                issues=[
                    BandCheckpointIssueInfo(
                        code=finding.risk_type,
                        severity="error" if finding.severity == "fail" else "warning",
                        issue_group=issue_group_for_issue(code="future_constraint"),
                        description=finding.message,
                        detail="; ".join(finding.evidence_refs),
                    )
                    for finding in report.risk_findings
                ],
            )
        )
        return row.id

    def _create_replan_plan_version(self, report: ScenarioRehearsalReport) -> dict:
        active_arc = self.session.get(ArcPlanVersion, report.arc_id)
        if active_arc is None:
            return {}
        start_chapter = min(report.chapter_numbers or [int(active_arc.chapter_start or 1)])
        end_chapter = max(report.chapter_numbers or [int(active_arc.chapter_end or 0)])
        max_version = self.session.execute(
            select(func.max(ArcPlanVersion.version)).where(ArcPlanVersion.project_id == report.project_id)
        ).scalar_one() or int(active_arc.version or 0)
        next_version = int(max_version or 0) + 1
        risk_summary = "; ".join(
            f"{item.risk_type}: {item.message}" for item in report.risk_findings
        ).strip()
        for row in self.session.execute(
            select(ArcPlanVersion)
            .where(
                ArcPlanVersion.project_id == report.project_id,
                ArcPlanVersion.status == "active",
            )
        ).scalars().all():
            row.status = "superseded"
            self.session.add(row)
        new_arc = ArcPlanVersion(
            id=new_id(),
            project_id=report.project_id,
            version=next_version,
            arc_number=int(active_arc.arc_number or 1),
            chapter_start=int(active_arc.chapter_start or start_chapter),
            chapter_end=int(active_arc.chapter_end or end_chapter),
            arc_synopsis=(
                f"{active_arc.arc_synopsis}\n\n"
                f"[Scenario Rehearsal replan v{next_version}] {risk_summary or '补足写作前叙事推演约束。'}"
            ).strip(),
            planned_target_size=int(active_arc.planned_target_size or 0),
            planned_soft_min=int(active_arc.planned_soft_min or 0),
            planned_soft_max=int(active_arc.planned_soft_max or 0),
            status="active",
        )
        self.session.add(new_arc)
        self.session.flush()

        future_plans = self.session.execute(
            select(ChapterPlan)
            .where(
                ChapterPlan.project_id == report.project_id,
                ChapterPlan.chapter_number >= start_chapter,
            )
            .order_by(ChapterPlan.chapter_number.asc())
        ).scalars().all()
        goal = f"Scenario Rehearsal replan: {risk_summary or '补足认知/visibility 约束'}"
        for plan in future_plans:
            plan.arc_plan_id = new_arc.id
            goals = self._json_list(plan.goals_json)
            if goal not in goals:
                goals.insert(0, goal)
            plan.goals_json = json.dumps(goals[:5], ensure_ascii=False)
            prefix = "[scenario replan] "
            if plan.one_line and not plan.one_line.startswith(prefix):
                plan.one_line = f"{prefix}{plan.one_line}"
            self.session.add(plan)

        latest_structure = self.session.execute(
            select(ArcStructureDraft)
            .where(
                ArcStructureDraft.project_id == report.project_id,
                ArcStructureDraft.arc_id == active_arc.id,
            )
            .order_by(ArcStructureDraft.created_at.desc(), ArcStructureDraft.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        if latest_structure is not None:
            self.session.add(
                ArcStructureDraft(
                    id=new_id(),
                    project_id=report.project_id,
                    arc_id=new_arc.id,
                    phase_layout_json=latest_structure.phase_layout_json,
                    key_beats_json=latest_structure.key_beats_json,
                    thread_priorities_json=latest_structure.thread_priorities_json,
                    hotspot_candidates_json=latest_structure.hotspot_candidates_json,
                    compression_candidates_json=latest_structure.compression_candidates_json,
                    reader_promise_json=latest_structure.reader_promise_json,
                    arc_payoff_map_json=latest_structure.arc_payoff_map_json,
                )
            )

        self._clone_replan_contracts(
            report=report,
            old_arc_id=active_arc.id,
            new_arc=new_arc,
            start_chapter=start_chapter,
            risk_summary=risk_summary,
        )
        for band_plan in self.session.execute(
            select(BandExperiencePlan)
            .where(
                BandExperiencePlan.project_id == report.project_id,
                BandExperiencePlan.band_id == report.band_id,
            )
        ).scalars().all():
            band_plan.arc_id = new_arc.id
            self.session.add(band_plan)
        self.session.flush()
        return {
            "old_arc_id": active_arc.id,
            "new_arc_id": new_arc.id,
            "new_version": next_version,
            "start_chapter": start_chapter,
            "chapter_count": len(future_plans),
            "risk_summary": risk_summary,
        }

    def _clone_replan_contracts(
        self,
        *,
        report: ScenarioRehearsalReport,
        old_arc_id: str,
        new_arc: ArcPlanVersion,
        start_chapter: int,
        risk_summary: str,
    ) -> None:
        old_arc_contract = self.contracts.get_arc_contract(report.project_id, old_arc_id)
        if old_arc_contract is None:
            new_arc_contract = ArcWorldContract(
                contract_id=f"arc_contract:{new_arc.id}",
                project_id=report.project_id,
                arc_id=new_arc.id,
                arc_number=int(new_arc.arc_number or 1),
            )
        else:
            new_arc_contract = old_arc_contract.model_copy(
                deep=True,
                update={
                    "contract_id": f"arc_contract:{new_arc.id}",
                    "arc_id": new_arc.id,
                    "arc_number": int(new_arc.arc_number or 1),
                    "metadata": {
                        **dict(old_arc_contract.metadata),
                        "scenario_replan": {
                            "source_arc_id": old_arc_id,
                            "risk_summary": risk_summary,
                        },
                    },
                },
            )
        if not new_arc_contract.reader_cognition_trajectory:
            new_arc_contract.reader_cognition_trajectory.append(
                ReaderCognitionTransition(
                    chapter_hint=start_chapter,
                    observer_id="reader",
                    from_state="false_belief",
                    to_state="managed_uncertainty",
                    intended_effect="为误会/欺骗线补足读者认知退出路径。",
                    payoff_type="clarification",
                )
            )
        self.contracts.save_arc_contract(new_arc_contract)

        old_band_contract = self.contracts.get_band_contract(report.project_id, report.band_id)
        if old_band_contract is not None:
            self.contracts.save_band_contract(
                old_band_contract.model_copy(
                    deep=True,
                    update={
                        "contract_id": f"band_contract:{new_arc.id}:{report.band_id}",
                        "arc_id": new_arc.id,
                        "metadata": {
                            **dict(old_band_contract.metadata),
                            "scenario_replan": {
                                "source_arc_id": old_arc_id,
                                "risk_summary": risk_summary,
                            },
                        },
                    },
                )
            )

    def _create_replan_event(self, report: ScenarioRehearsalReport, *, status: str) -> str:
        max_version = self.session.execute(
            select(func.max(ArcPlanVersion.version)).where(ArcPlanVersion.project_id == report.project_id)
        ).scalar_one() or 0
        row = ProjectReplanEvent(
            project_id=report.project_id,
            trigger_chapter=min(report.chapter_numbers or [0]),
            risk_level="high",
            reason="; ".join(f"{item.risk_type}: {item.message}" for item in report.risk_findings),
            focus_threads_json=json.dumps(
                list(dict.fromkeys(item.risk_type for item in report.risk_findings)),
                ensure_ascii=False,
            ),
            strategy="scenario_rehearsal_replan",
            status=status,
            cooldown_until_chapter=max(report.chapter_numbers or [0]),
        )
        self.session.add(row)
        _ = max_version
        self.session.flush()
        return row.id

    @staticmethod
    def _json_list(raw: str) -> list[str]:
        try:
            payload = json.loads(raw or "[]")
        except (TypeError, ValueError, json.JSONDecodeError):
            return []
        if not isinstance(payload, list):
            return []
        return [str(item) for item in payload if str(item or "").strip()]

    @staticmethod
    def _json_object(raw: str) -> dict:
        try:
            payload = json.loads(raw or "{}")
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}
        return payload if isinstance(payload, dict) else {}


def latest_blocking_scenario_rehearsal(session: Session, project_id: str) -> ScenarioRehearsalRunRow | None:
    row = session.execute(
        select(ScenarioRehearsalRunRow)
        .where(ScenarioRehearsalRunRow.project_id == project_id)
        .order_by(ScenarioRehearsalRunRow.created_at.desc(), ScenarioRehearsalRunRow.id.desc())
        .limit(1)
    ).scalar_one_or_none()
    if row is None:
        return None
    try:
        payload = json.loads(row.report_json or "{}") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        payload = {}
    if str(payload.get("resolution_status") or "") in TERMINAL_BLOCKING_RESOLUTION_STATUSES:
        return row
    return None
