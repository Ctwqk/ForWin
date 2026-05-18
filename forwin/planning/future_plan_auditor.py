from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.canon_quality.prompt_json.normalization import prompt_issue_evidence_refs
from forwin.canon_quality.prompt_json.schemas import PromptJsonMode, normalize_prompt_json_mode
from forwin.canon_quality.prompt_json.validation import issue_can_block
from forwin.models.base import new_id
from forwin.models.narrative_obligation import FuturePlanAuditRunRow
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.planning.obligation_pre_audit import select_urgent_obligation_targets
from forwin.planning.plan_patch_validator import PlanPatchValidator
from forwin.planning.prompt_json.future_plan_auditor_prompt import FuturePlanPromptAuditor
from forwin.planning.signal_pre_audit import select_stale_signal_targets
from forwin.protocol.experience import BandDelightSchedule


AuditStatus = Literal["pass", "warn", "fail"]


class FuturePlanAuditIssue(BaseModel):
    issue_type: str
    severity: Literal["warning", "error"] = "error"
    target_chapter: int
    target_plan_id: str = ""
    description: str
    evidence_refs: list[str] = Field(default_factory=list)
    patch_type: str = "future_plan_audit"
    blocking: bool = True
    metadata: dict[str, Any] = Field(default_factory=dict)


class FuturePlanAuditRun(BaseModel):
    id: str = ""
    project_id: str
    current_chapter: int
    trigger_stage: str
    inspected_chapters: list[int] = Field(default_factory=list)
    status: AuditStatus = "pass"
    issues: list[FuturePlanAuditIssue] = Field(default_factory=list)
    plan_patches: list[NarrativePlanPatch] = Field(default_factory=list)
    applied_plan_patch_ids: list[str] = Field(default_factory=list)
    blocking_reasons: list[str] = Field(default_factory=list)
    metadata: dict[str, Any] = Field(default_factory=dict)


class FuturePlanAuditRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def save_run(self, run: FuturePlanAuditRun) -> FuturePlanAuditRun:
        item = run.model_copy(update={"id": run.id or new_id()})
        row = FuturePlanAuditRunRow(
            id=item.id,
            project_id=item.project_id,
            current_chapter_number=item.current_chapter,
            trigger_stage=item.trigger_stage,
            inspected_chapters_json=_json(item.inspected_chapters),
            status=item.status,
            issues_json=_json([issue.model_dump(mode="json") for issue in item.issues]),
            applied_plan_patch_ids_json=_json(item.applied_plan_patch_ids),
            blocking_reasons_json=_json(item.blocking_reasons),
            metadata_json=_json(item.metadata),
            created_at=datetime.now(UTC),
        )
        self.session.add(row)
        self.session.flush()
        return self._from_row(row)

    def list_recent(self, project_id: str, *, limit: int = 5) -> list[FuturePlanAuditRun]:
        rows = self.session.execute(
            select(FuturePlanAuditRunRow)
            .where(FuturePlanAuditRunRow.project_id == project_id)
            .order_by(FuturePlanAuditRunRow.created_at.desc())
            .limit(max(1, int(limit or 1)))
        ).scalars().all()
        return [self._from_row(row) for row in rows]

    @staticmethod
    def _from_row(row: FuturePlanAuditRunRow) -> FuturePlanAuditRun:
        return FuturePlanAuditRun(
            id=row.id,
            project_id=row.project_id,
            current_chapter=int(row.current_chapter_number or 0),
            trigger_stage=row.trigger_stage,
            inspected_chapters=[int(item) for item in _loads(row.inspected_chapters_json, [])],
            status=row.status,  # type: ignore[arg-type]
            issues=[
                FuturePlanAuditIssue.model_validate(item)
                for item in _loads(row.issues_json, [])
                if isinstance(item, dict)
            ],
            applied_plan_patch_ids=_loads(row.applied_plan_patch_ids_json, []),
            blocking_reasons=_loads(row.blocking_reasons_json, []),
            metadata=_loads(row.metadata_json, {}),
        )


class FuturePlanAuditor:
    def __init__(
        self,
        *,
        mode: PromptJsonMode | str = "deterministic",
        plan_patch_validation_mode: PromptJsonMode | str = "deterministic",
        llm_client: object | None = None,
        min_blocking_confidence: float = 0.8,
    ) -> None:
        self.mode = normalize_prompt_json_mode(str(mode), default="deterministic")
        self.plan_patch_validation_mode = normalize_prompt_json_mode(
            str(plan_patch_validation_mode),
            default="deterministic",
        )
        self.llm_client = llm_client
        self.min_blocking_confidence = float(min_blocking_confidence)

    def audit_plans(
        self,
        *,
        project_id: str,
        current_chapter: int,
        trigger_stage: str,
        plans: list[ChapterPlan],
        canon_quality_context: dict[str, Any],
        obligations: list[NarrativeObligation],
        target_total_chapters: int,
        include_current: bool,
        band_rows: list[BandExperiencePlan] | None = None,
    ) -> FuturePlanAuditRun:
        if self.mode in {"hybrid", "prompt_json", "shadow"}:
            return self._audit_plans_prompt_json(
                project_id=project_id,
                current_chapter=current_chapter,
                trigger_stage=trigger_stage,
                plans=plans,
                canon_quality_context=canon_quality_context,
                obligations=obligations,
                target_total_chapters=target_total_chapters,
                include_current=include_current,
                band_rows=band_rows,
            )

        inspected = [
            int(plan.chapter_number or 0)
            for plan in plans
            if include_current or int(plan.chapter_number or 0) > int(current_chapter or 0)
        ]
        for row in band_rows or []:
            for chapter in range(int(row.chapter_start or 0), int(row.chapter_end or 0) + 1):
                if (include_current or chapter > int(current_chapter or 0)) and chapter not in inspected:
                    inspected.append(chapter)
        inspected.sort()
        issues: list[FuturePlanAuditIssue] = []
        patches: list[NarrativePlanPatch] = []
        countdowns = _countdown_constraints(canon_quality_context)
        character_state_constraints = _character_state_constraints(canon_quality_context)
        suppressed_prompt_constraint_keys: set[str] = set()
        for plan in plans:
            chapter_number = int(plan.chapter_number or 0)
            if not include_current and chapter_number <= int(current_chapter or 0):
                continue
            if str(plan.status or "") == "accepted":
                continue
            for issue, patch in self._audit_countdown_plan(
                project_id=project_id,
                current_chapter=int(current_chapter or 0),
                plan=plan,
                countdowns=countdowns,
            ):
                issues.append(issue)
                patches.append(patch)
            for issue, patch in self._audit_character_state_plan(
                project_id=project_id,
                current_chapter=int(current_chapter or 0),
                plan=plan,
                character_state_constraints=character_state_constraints,
            ):
                issues.append(issue)
                patches.append(patch)
        band_covered_obligation_ids: set[str] = set()
        for obligation in obligations:
            if obligation.status not in {"active", "planned"}:
                continue
            if _minimum_scope_for_obligation(obligation) != "band":
                continue
            band_row = _band_row_for_obligation(
                obligation=obligation,
                band_rows=list(band_rows or []),
                current_chapter=int(current_chapter or 0),
            )
            if band_row is None:
                continue
            if _band_contract_covers_obligation(band_row, obligation):
                band_covered_obligation_ids.add(obligation.id)
                continue
            issue, patch = self._audit_band_obligation_binding(
                project_id=project_id,
                current_chapter=int(current_chapter or 0),
                band_row=band_row,
                obligation=obligation,
            )
            issues.append(issue)
            patches.append(patch)

        for issue, patch in self._audit_pre_write_obligations_and_signals(
            project_id=project_id,
            current_chapter=int(current_chapter or 0),
            plans=plans,
            canon_quality_context=canon_quality_context,
            obligations=obligations,
            include_current=include_current,
        ):
            issues.append(issue)
            patches.append(patch)
            suppression_key = str(issue.metadata.get("suppression_key") or "").strip()
            if suppression_key:
                suppressed_prompt_constraint_keys.add(suppression_key)

        plans_by_chapter = {int(plan.chapter_number or 0): plan for plan in plans}
        for obligation in obligations:
            if obligation.id in band_covered_obligation_ids or _minimum_scope_for_obligation(obligation) == "band":
                continue
            if obligation.must_resolve_now:
                continue
            plan = plans_by_chapter.get(int(obligation.deadline_chapter or 0))
            if plan is None or str(plan.status or "") == "accepted":
                continue
            issue, patch = self._audit_obligation_binding(
                project_id=project_id,
                current_chapter=int(current_chapter or 0),
                plan=plan,
                obligation=obligation,
                target_total_chapters=int(target_total_chapters or 0),
            )
            if issue is not None and patch is not None:
                issues.append(issue)
                patches.append(patch)

        status: AuditStatus = "pass"
        if any(issue.severity == "error" for issue in issues):
            status = "fail"
        elif issues:
            status = "warn"
        return FuturePlanAuditRun(
            project_id=project_id,
            current_chapter=int(current_chapter or 0),
            trigger_stage=trigger_stage,
            inspected_chapters=inspected,
            status=status,
            issues=issues,
            plan_patches=patches,
            blocking_reasons=[
                f"{issue.issue_type}:{issue.target_chapter}"
                for issue in issues
                if issue.blocking
            ],
            metadata={
                "suppressed_prompt_constraint_keys": sorted(suppressed_prompt_constraint_keys),
                "pre_write_patch_count": len(suppressed_prompt_constraint_keys),
            },
        )

    def _audit_plans_prompt_json(
        self,
        *,
        project_id: str,
        current_chapter: int,
        trigger_stage: str,
        plans: list[ChapterPlan],
        canon_quality_context: dict[str, Any],
        obligations: list[NarrativeObligation],
        target_total_chapters: int,
        include_current: bool,
        band_rows: list[BandExperiencePlan] | None,
    ) -> FuturePlanAuditRun:
        inspected = _inspected_chapters(
            plans=plans,
            band_rows=band_rows,
            current_chapter=current_chapter,
            include_current=include_current,
        )
        result = FuturePlanPromptAuditor(
            llm_client=self.llm_client,
            min_blocking_confidence=self.min_blocking_confidence,
        ).analyze(
            _future_plan_prompt_payload(
                plans=plans,
                canon_quality_context=canon_quality_context,
                obligations=obligations,
                target_total_chapters=target_total_chapters,
                current_chapter=current_chapter,
                include_current=include_current,
                band_rows=band_rows,
            )
        )
        plans_by_id = {str(plan.id or ""): plan for plan in plans if str(plan.id or "").strip()}
        impacts_by_id = {
            str(item.get("plan_item_id") or ""): item
            for item in result.get("plan_impacts", [])
            if isinstance(item, dict)
        }
        fallback_plan = _first_prompt_target_plan(
            plans=plans,
            current_chapter=current_chapter,
            include_current=include_current,
        )
        issues: list[FuturePlanAuditIssue] = []
        patches: list[NarrativePlanPatch] = []
        for index, raw_issue in enumerate(result.get("issues", []) or [], start=1):
            if not isinstance(raw_issue, dict):
                continue
            plan_id = _prompt_issue_plan_id(raw_issue=raw_issue, impacts_by_id=impacts_by_id)
            plan = plans_by_id.get(plan_id) or fallback_plan
            impact = impacts_by_id.get(plan_id) if plan_id else None
            audit_issue = _prompt_issue_to_future_plan_issue(
                issue=raw_issue,
                result=result,
                current_chapter=current_chapter,
                plan=plan,
                plan_id=plan_id,
                impact=impact,
                min_blocking_confidence=self.min_blocking_confidence,
            )
            issues.append(audit_issue)
            patch = _prompt_issue_to_plan_patch(
                project_id=project_id,
                issue=raw_issue,
                result=result,
                plan=plan,
                plan_id=audit_issue.target_plan_id,
                target_chapter=audit_issue.target_chapter,
                index=index,
                impact=impact,
            )
            if patch is not None:
                patches.append(patch)

        suppressed_prompt_constraint_keys: set[str] = set()
        for issue, patch in self._audit_pre_write_obligations_and_signals(
            project_id=project_id,
            current_chapter=int(current_chapter or 0),
            plans=plans,
            canon_quality_context=canon_quality_context,
            obligations=obligations,
            include_current=include_current,
        ):
            issues.append(issue)
            patches.append(patch)
            suppression_key = str(issue.metadata.get("suppression_key") or "").strip()
            if suppression_key:
                suppressed_prompt_constraint_keys.add(suppression_key)

        status: AuditStatus = "pass"
        if any(issue.severity == "error" and issue.blocking for issue in issues):
            status = "fail"
        elif issues or patches or str(result.get("verdict") or "") in {"warn", "fail", "uncertain"}:
            status = "warn"
        return FuturePlanAuditRun(
            project_id=project_id,
            current_chapter=int(current_chapter or 0),
            trigger_stage=trigger_stage,
            inspected_chapters=inspected,
            status=status,
            issues=issues,
            plan_patches=patches,
            blocking_reasons=[
                f"{issue.issue_type}:{issue.target_chapter}"
                for issue in issues
                if issue.blocking
            ],
            metadata={
                "source_analyzer": str(result.get("analyzer") or "FuturePlanPromptAuditor"),
                "source_mode": "prompt_json",
                "original_verdict": str(result.get("verdict") or ""),
                "original_confidence": float(result.get("confidence") or 0.0),
                "prompt_json_summary": str(result.get("summary") or ""),
                "suppressed_prompt_constraint_keys": sorted(suppressed_prompt_constraint_keys),
                "pre_write_patch_count": len(suppressed_prompt_constraint_keys),
            },
        )

    def audit_and_apply(
        self,
        *,
        session: Session,
        project_id: str,
        current_chapter: int,
        trigger_stage: str,
        plans: list[ChapterPlan],
        canon_quality_context: dict[str, Any],
        obligations: list[NarrativeObligation],
        target_total_chapters: int,
        include_current: bool,
        band_rows: list[BandExperiencePlan] | None = None,
    ) -> FuturePlanAuditRun:
        result = self.audit_plans(
            project_id=project_id,
            current_chapter=current_chapter,
            trigger_stage=trigger_stage,
            plans=plans,
            canon_quality_context=canon_quality_context,
            obligations=obligations,
            target_total_chapters=target_total_chapters,
            include_current=include_current,
            band_rows=band_rows,
        )
        plans_by_id = {str(plan.id or ""): plan for plan in plans}
        bands_by_id = {str(row.band_id or ""): row for row in band_rows or []}
        obligation_repo = NarrativeObligationRepository(session)
        applied_patch_ids: list[str] = []
        persisted_patches: list[NarrativePlanPatch] = []
        blocking_reasons: list[str] = []
        accepted_chapters = [
            int(plan.chapter_number or 0)
            for plan in plans
            if str(plan.status or "") == "accepted"
        ]
        unresolved_obligation_ids = [
            obligation.id
            for obligation in obligations
            if obligation.status not in {"resolved", "waived"}
        ]
        validator = PlanPatchValidator(
            mode=self.plan_patch_validation_mode,
            llm_client=self.llm_client,
            min_blocking_confidence=self.min_blocking_confidence,
        )
        for patch in result.plan_patches:
            plan = None
            band_row = None
            band_plan_bounds: dict[str, tuple[int, int]] = {}
            if patch.target_scope == "band":
                band_row = bands_by_id.get(str(patch.target_band_id or ""))
                if band_row is None:
                    blocking_reasons.append(f"missing_band_for_patch:{patch.id or patch.target_band_id}")
                    continue
                band_plan_bounds[str(band_row.band_id or "")] = (
                    int(band_row.chapter_start or 0),
                    int(band_row.chapter_end or 0),
                )
            else:
                plan = plans_by_id.get(str(patch.target_plan_id or ""))
                if plan is None:
                    blocking_reasons.append(f"missing_plan_for_patch:{patch.id or patch.target_plan_id}")
                    continue
            patch_obligations = [
                obligation
                for obligation in obligations
                if obligation.id in set(patch.source_obligation_ids)
            ]
            validation = validator.validate(
                patch=patch,
                obligations=patch_obligations,
                current_chapter=current_chapter,
                target_total_chapters=target_total_chapters,
                accepted_chapters=accepted_chapters,
                unresolved_obligation_ids=unresolved_obligation_ids,
                band_plan_bounds=band_plan_bounds,
                minimum_scope_by_obligation={
                    obligation.id: _minimum_scope_for_obligation(obligation)
                    for obligation in patch_obligations
                    if obligation.id
                },
            )
            if not validation.passed:
                blocking_reasons.extend(f"plan_patch_validation_failed:{error}" for error in validation.errors)
                continue
            applied_patch = patch.model_copy(
                update={
                    "id": patch.id or new_id(),
                    "validation_status": "passed",
                    "validation_errors": [],
                    "applied": True,
                }
            )
            if band_row is not None:
                BandPlanPatcher().apply(band_row, applied_patch, obligations=patch_obligations)
                session.add(band_row)
            elif plan is not None:
                self.apply_plan_patch(plan, applied_patch)
                session.add(plan)
            stored_patch = obligation_repo.create_plan_patch(applied_patch)
            applied_patch_ids.append(stored_patch.id)
            persisted_patches.append(stored_patch)
        result = result.model_copy(
            update={
                "plan_patches": persisted_patches or result.plan_patches,
                "applied_plan_patch_ids": applied_patch_ids,
                "blocking_reasons": blocking_reasons,
            }
        )
        stored_run = FuturePlanAuditRepository(session).save_run(result)
        return stored_run.model_copy(update={"plan_patches": result.plan_patches})

    def apply_plan_patch(self, plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        if patch.patch_type in {"canon_plan_staleness", "future_plan_audit"}:
            if _is_custody_state_patch(patch):
                self._apply_custody_state_patch(plan, patch)
            else:
                self._apply_countdown_patch(plan, patch)
            return
        if patch.patch_type in {"obligation_plan_binding", "obligation_pre_write", "defer_acceptance"}:
            self._apply_obligation_patch(plan, patch)
            return
        if patch.patch_type in {"future_plan_prompt_update", "signal_pre_write"}:
            self._apply_prompt_plan_patch(plan, patch)

    def _audit_character_state_plan(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        character_state_constraints: list[dict[str, Any]],
    ) -> list[tuple[FuturePlanAuditIssue, NarrativePlanPatch]]:
        text = _plan_text(plan)
        result: list[tuple[FuturePlanAuditIssue, NarrativePlanPatch]] = []
        for constraint in character_state_constraints:
            transition_type = str(constraint.get("transition_type") or "").strip()
            latest_state = str(constraint.get("latest_state") or constraint.get("to_state") or "").strip()
            character_name = str(constraint.get("character_name") or "").strip()
            latest_chapter = int(constraint.get("latest_chapter") or constraint.get("chapter_number") or 0)
            if transition_type != "custody_state" or latest_state not in _CUSTODY_FREE_STATES:
                continue
            if not character_name:
                continue
            if not _plan_mentions_stale_custody(text, character_name=character_name):
                continue
            if _plan_declares_recapture_bridge(text, character_name=character_name):
                continue
            chapter_number = int(plan.chapter_number or 0)
            patch_type = "canon_plan_staleness" if chapter_number <= current_chapter else "future_plan_audit"
            description = (
                f"第{chapter_number}章计划把 {character_name} 写回被捕/羁押/救援对象，"
                f"但最新 accepted canon 第{latest_chapter}章已是脱困状态。"
            )
            issue = FuturePlanAuditIssue(
                issue_type="custody_future_plan_conflict",
                severity="error",
                target_chapter=chapter_number,
                target_plan_id=str(plan.id or ""),
                description=description,
                evidence_refs=[
                    f"chapter_plan:{chapter_number}",
                    *[str(item) for item in constraint.get("evidence_refs", []) or []],
                ],
                patch_type=patch_type,
                metadata={
                    "conflict_type": "custody_state",
                    "character_name": character_name,
                    "latest_state": latest_state,
                    "latest_chapter": latest_chapter,
                    "transition_type": transition_type,
                },
            )
            patch = self._custody_state_patch(
                project_id=project_id,
                plan=plan,
                character_name=character_name,
                latest_state=latest_state,
                latest_chapter=latest_chapter,
                description=description,
                patch_type=patch_type,
                metadata=issue.metadata,
            )
            result.append((issue, patch))
        return result

    @staticmethod
    def _custody_state_patch(
        *,
        project_id: str,
        plan: ChapterPlan,
        character_name: str,
        latest_state: str,
        latest_chapter: int,
        description: str,
        patch_type: str,
        metadata: dict[str, Any],
    ) -> NarrativePlanPatch:
        chapter_number = int(plan.chapter_number or 0)
        hard_instruction = _hard_custody_instruction(
            character_name=character_name,
            latest_chapter=latest_chapter,
        )
        return NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type=patch_type,
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "character_name": character_name,
                "transition_type": "custody_state",
                "latest_state": latest_state,
                "latest_chapter": latest_chapter,
                "rule": hard_instruction,
            },
            diff_summary=description,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=[
                f"accepted canon custody state {character_name}=free at chapter {latest_chapter}",
            ],
            writer_context_injections=[
                {
                    "type": "character_state_constraint",
                    "transition_type": "custody_state",
                    "character_name": character_name,
                    "latest_state": latest_state,
                    "latest_chapter": latest_chapter,
                    "instruction": hard_instruction,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "character_state_constraint",
                    "transition_type": "custody_state",
                    "character_name": character_name,
                    "latest_state": latest_state,
                    "latest_chapter": latest_chapter,
                    "payoff_test": hard_instruction,
                }
            ],
            expected_resolution_tests=[
                f"第{chapter_number}章必须承接 {character_name} 已脱困状态；不得把{character_name}写回被捕/羁押/固定状态，除非先给出再次被捕桥接。",
            ],
            validation_status="pending",
            metadata=metadata,
        )

    def _audit_countdown_plan(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        countdowns: list[dict[str, Any]],
    ) -> list[tuple[FuturePlanAuditIssue, NarrativePlanPatch]]:
        text = _plan_text(plan)
        result: list[tuple[FuturePlanAuditIssue, NarrativePlanPatch]] = []
        for countdown in countdowns:
            key = str(countdown.get("countdown_key") or "").strip()
            label = str(countdown.get("label") or key or "倒计时").strip()
            latest = int(countdown.get("latest_remaining_minutes") or 0)
            if not key:
                continue
            mentions = _duration_mentions_for_countdown(text, key=key, label=label)
            if latest <= 0:
                reopened_mentions = [item for item in mentions if int(item.get("minutes") or 0) > 0]
                if not reopened_mentions:
                    continue
                chapter_number = int(plan.chapter_number or 0)
                patch_type = "canon_plan_staleness" if chapter_number <= current_chapter else "future_plan_audit"
                largest = max(reopened_mentions, key=lambda item: int(item["minutes"]))
                description = (
                    f"第{chapter_number}章计划把已关闭的 {label}/{key} 写成 {largest['raw']}，"
                    "但最新 canon ledger 已归零。"
                )
                issue = FuturePlanAuditIssue(
                    issue_type="countdown_closed_future_plan_conflict",
                    severity="error",
                    target_chapter=chapter_number,
                    target_plan_id=str(plan.id or ""),
                    description=description,
                    evidence_refs=[f"chapter_plan:{chapter_number}"],
                    patch_type=patch_type,
                    metadata={
                        "countdown_key": key,
                        "label": label,
                        "latest_remaining_minutes": latest,
                        "latest_chapter": int(countdown.get("latest_chapter") or 0),
                        "stale_mentions": reopened_mentions,
                        "closed_countdown": True,
                    },
                )
                patch = self._countdown_patch(
                    project_id=project_id,
                    plan=plan,
                    countdown_key=key,
                    label=label,
                    latest=latest,
                    description=description,
                    patch_type=patch_type,
                    metadata=issue.metadata,
                )
                result.append((issue, patch))
                continue
            stale_mentions = [item for item in mentions if item["minutes"] > latest]
            false_prior_mentions = [
                item
                for item in mentions
                if int(item.get("minutes") or 0) < latest
                and _is_false_prior_countdown_clause(
                    str(item.get("context") or item.get("clause") or "")
                )
            ]
            if false_prior_mentions:
                chapter_number = int(plan.chapter_number or 0)
                patch_type = "canon_plan_staleness" if chapter_number <= current_chapter else "future_plan_audit"
                smallest = min(false_prior_mentions, key=lambda item: int(item["minutes"]))
                description = (
                    f"第{chapter_number}章计划把 {label}/{key} 的已 accepted canon 合同写成 {smallest['raw']}，"
                    f"但最新 canon ledger 为第{int(countdown.get('latest_chapter') or 0)}章 {latest} 分钟。"
                )
                issue = FuturePlanAuditIssue(
                    issue_type="countdown_plan_false_prior_conflict",
                    severity="error",
                    target_chapter=chapter_number,
                    target_plan_id=str(plan.id or ""),
                    description=description,
                    evidence_refs=[f"chapter_plan:{chapter_number}"],
                    patch_type=patch_type,
                    metadata={
                        "countdown_key": key,
                        "label": label,
                        "latest_remaining_minutes": latest,
                        "latest_chapter": int(countdown.get("latest_chapter") or 0),
                        "false_prior_mentions": false_prior_mentions,
                        "false_prior_conflict": True,
                    },
                )
                patch = self._countdown_patch(
                    project_id=project_id,
                    plan=plan,
                    countdown_key=key,
                    label=label,
                    latest=latest,
                    description=description,
                    patch_type=patch_type,
                    metadata=issue.metadata,
                )
                result.append((issue, patch))
                continue
            if not stale_mentions:
                hard_instruction = _hard_countdown_instruction(label=label, key=key, latest=latest)
                is_current_plan = int(plan.chapter_number or 0) <= int(current_chapter or 0)
                has_instruction_pollution = is_current_plan and _plan_has_countdown_instruction_pollution(plan)
                if has_instruction_pollution or (is_current_plan and latest <= 180 and hard_instruction not in text):
                    chapter_number = int(plan.chapter_number or 0)
                    patch_type = "canon_plan_staleness"
                    issue_type = "countdown_plan_instruction_pollution" if has_instruction_pollution else "countdown_plan_hard_constraint_missing"
                    description = (
                        f"第{chapter_number}章计划包含污染的倒计时合成约束，需要清理后重新写入 rule anchors。"
                        if has_instruction_pollution
                        else (
                            f"第{chapter_number}章计划缺少 {label}/{key} 的分钟级硬约束，"
                            f"最新 canon ledger 为 {latest} 分钟。"
                        )
                    )
                    issue = FuturePlanAuditIssue(
                        issue_type=issue_type,
                        severity="warning",
                        target_chapter=chapter_number,
                        target_plan_id=str(plan.id or ""),
                        description=description,
                        evidence_refs=[f"chapter_plan:{chapter_number}"],
                        patch_type=patch_type,
                        blocking=False,
                        metadata={
                            "countdown_key": key,
                            "label": label,
                            "latest_remaining_minutes": latest,
                        },
                    )
                    patch = self._countdown_patch(
                        project_id=project_id,
                        plan=plan,
                        countdown_key=key,
                        label=label,
                        latest=latest,
                        description=description,
                        patch_type=patch_type,
                        metadata=issue.metadata,
                    )
                    result.append((issue, patch))
                continue
            chapter_number = int(plan.chapter_number or 0)
            patch_type = "canon_plan_staleness" if chapter_number <= current_chapter else "future_plan_audit"
            largest = max(stale_mentions, key=lambda item: int(item["minutes"]))
            description = (
                f"第{chapter_number}章计划把 {label}/{key} 写成 {largest['raw']}，"
                f"超过最新 canon ledger 的 {latest} 分钟。"
            )
            issue = FuturePlanAuditIssue(
                issue_type="countdown_future_plan_conflict",
                severity="error",
                target_chapter=chapter_number,
                target_plan_id=str(plan.id or ""),
                description=description,
                evidence_refs=[f"chapter_plan:{chapter_number}"],
                patch_type=patch_type,
                metadata={
                    "countdown_key": key,
                    "label": label,
                    "latest_remaining_minutes": latest,
                    "stale_mentions": stale_mentions,
                },
            )
            patch = self._countdown_patch(
                project_id=project_id,
                plan=plan,
                countdown_key=key,
                label=label,
                latest=latest,
                description=description,
                patch_type=patch_type,
                metadata=issue.metadata,
            )
            result.append((issue, patch))
        return result

    @staticmethod
    def _countdown_patch(
        *,
        project_id: str,
        plan: ChapterPlan,
        countdown_key: str,
        label: str,
        latest: int,
        description: str,
        patch_type: str,
        metadata: dict[str, Any],
    ) -> NarrativePlanPatch:
        chapter_number = int(plan.chapter_number or 0)
        hard_instruction = _hard_countdown_instruction(label=label, key=countdown_key, latest=latest)
        return NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type=patch_type,
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "countdown_key": countdown_key,
                "label": label,
                "latest_remaining_minutes": latest,
                "rule": (
                    f"{label} 已关闭，不得写成仍有剩余时间，除非正文明确 reset 或 branch clock。"
                    if latest <= 0
                    else f"{label} 不得写成大于 {latest} 分钟，除非正文明确 reset 或 branch clock。"
                ),
                "hard_rule": hard_instruction,
            },
            diff_summary=description,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=[f"accepted canon countdown {countdown_key} <= {latest} minutes"],
            writer_context_injections=[
                {
                    "type": "countdown_constraint",
                    "countdown_key": countdown_key,
                    "label": label,
                    "latest_remaining_minutes": latest,
                    "instruction": hard_instruction,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "countdown_constraint",
                    "countdown_key": countdown_key,
                    "latest_remaining_minutes": latest,
                    "payoff_test": (
                        f"正文不得把已关闭的 {label} 写成仍有剩余时间，除非明确 reset 或 branch clock。"
                        if latest <= 0
                        else f"正文不得把 {label} 写成大于 {latest} 分钟，且不得把旧计划/旧摘要时间写成前文事实。"
                    ),
                }
            ],
            expected_resolution_tests=[
                (
                    f"第{chapter_number}章计划和正文必须承接 {label}已关闭，不得写成仍有剩余时间。"
                    if latest <= 0
                    else f"第{chapter_number}章计划和正文不得把 {label} 写成大于 {latest} 分钟。"
                )
            ],
            validation_status="pending",
            metadata=metadata,
        )

    def _audit_pre_write_obligations_and_signals(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plans: list[ChapterPlan],
        canon_quality_context: dict[str, Any],
        obligations: list[NarrativeObligation],
        include_current: bool,
    ) -> list[tuple[FuturePlanAuditIssue, NarrativePlanPatch]]:
        result: list[tuple[FuturePlanAuditIssue, NarrativePlanPatch]] = []
        for candidate in select_urgent_obligation_targets(
            obligations=obligations,
            plans=plans,
            current_chapter=current_chapter,
            include_current=include_current,
        ):
            obligation = candidate["obligation"]
            plan = candidate["plan"]
            suppression_key = str(candidate.get("suppression_key") or "").strip()
            issue, patch = self._must_resolve_obligation_patch(
                project_id=project_id,
                current_chapter=current_chapter,
                plan=plan,
                obligation=obligation,
                suppression_key=suppression_key,
            )
            result.append((issue, patch))
        for candidate in select_stale_signal_targets(
            open_signals=_open_signal_constraints(canon_quality_context),
            plans=plans,
            current_chapter=current_chapter,
            include_current=include_current,
        ):
            signal = candidate["signal"]
            plan = candidate["plan"]
            suppression_key = str(candidate.get("suppression_key") or "").strip()
            issue, patch = self._stale_signal_patch(
                project_id=project_id,
                current_chapter=current_chapter,
                plan=plan,
                signal=signal,
                suppression_key=suppression_key,
            )
            result.append((issue, patch))
        return result

    def _must_resolve_obligation_patch(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        obligation: NarrativeObligation,
        suppression_key: str,
    ) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch]:
        chapter_number = int(plan.chapter_number or 0)
        description = (
            f"第{chapter_number}章写作前必须偿还叙事义务 {obligation.id}: "
            f"{obligation.payoff_test}"
        )
        metadata = {
            "obligation_id": obligation.id,
            "priority": obligation.priority,
            "payoff_test": obligation.payoff_test,
            "must_resolve_now": True,
            "suppression_key": suppression_key,
        }
        issue = FuturePlanAuditIssue(
            issue_type="obligation_pre_write_required",
            severity="error",
            target_chapter=chapter_number,
            target_plan_id=str(plan.id or ""),
            description=description,
            evidence_refs=[f"obligation:{obligation.id}", f"chapter_plan:{chapter_number}"],
            patch_type="obligation_pre_write",
            metadata=metadata,
        )
        patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="obligation_pre_write",
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            source_obligation_ids=[obligation.id],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "obligations_to_resolve": [obligation.id],
                "payoff_test": obligation.payoff_test,
                "summary": obligation.summary,
                "must_resolve_now": True,
            },
            diff_summary=description,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=[f"remove must_resolve_now obligation {obligation.id}"],
            writer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation.id,
                    "priority": obligation.priority,
                    "summary": obligation.summary,
                    "payoff_test": obligation.payoff_test,
                    "deadline_chapter": obligation.deadline_chapter,
                    "must_resolve_now": True,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation.id,
                    "payoff_test": obligation.payoff_test,
                    "must_resolve_now": True,
                }
            ],
            expected_resolution_tests=[obligation.payoff_test],
            validation_status="pending",
            metadata=metadata,
        )
        return issue, patch

    def _stale_signal_patch(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        signal: dict[str, Any],
        suppression_key: str,
    ) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch]:
        chapter_number = int(plan.chapter_number or 0)
        signal_id = str(signal.get("signal_id") or signal.get("subject_key") or "").strip()
        signal_chapter = int(signal.get("chapter_number") or 0)
        signal_type = str(signal.get("signal_type") or "quality_signal").strip()
        description_text = str(signal.get("description") or "").strip()
        instruction = (
            f"修复第{signal_chapter}章遗留质量信号 {signal_type}"
            f"{f'({signal_id})' if signal_id else ''}：{description_text}"
        ).strip()
        metadata = {
            "signal_id": signal_id,
            "signal_type": signal_type,
            "origin_chapter": signal_chapter,
            "current_chapter": current_chapter,
            "suppression_key": suppression_key,
        }
        issue = FuturePlanAuditIssue(
            issue_type="stale_open_signal_pre_write_required",
            severity="error",
            target_chapter=chapter_number,
            target_plan_id=str(plan.id or ""),
            description=instruction,
            evidence_refs=[f"signal:{signal_id}", f"chapter_plan:{chapter_number}"],
            patch_type="signal_pre_write",
            metadata=metadata,
        )
        patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="signal_pre_write",
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            source_signal_ids=[signal_id] if signal_id else [],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "prompt_json_instruction": instruction,
                "signal_id": signal_id,
                "signal_type": signal_type,
                "origin_chapter": signal_chapter,
            },
            diff_summary=instruction,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=["drop stale open quality signal without resolution evidence"],
            writer_context_injections=[
                {
                    "type": "open_quality_signal_resolution",
                    "signal_id": signal_id,
                    "signal_type": signal_type,
                    "description": description_text,
                    "origin_chapter": signal_chapter,
                    "instruction": instruction,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "open_quality_signal_resolution",
                    "signal_id": signal_id,
                    "payoff_test": instruction,
                }
            ],
            expected_resolution_tests=[instruction],
            validation_status="pending",
            metadata=metadata,
        )
        return issue, patch

    def _audit_obligation_binding(
        self,
        *,
        project_id: str,
        current_chapter: int,
        plan: ChapterPlan,
        obligation: NarrativeObligation,
        target_total_chapters: int,
    ) -> tuple[FuturePlanAuditIssue | None, NarrativePlanPatch | None]:
        if obligation.status not in {"active", "planned"}:
            return None, None
        text = _plan_text(plan)
        if obligation.id and obligation.id in text and obligation.payoff_test and obligation.payoff_test in text:
            return None, None
        chapter_number = int(plan.chapter_number or 0)
        issue_type = "obligation_missing_from_future_plan"
        if (
            target_total_chapters
            and chapter_number >= target_total_chapters
            and obligation.priority in {"P0", "P1"}
        ):
            issue_type = "final_plan_carries_mainline_debt"
        description = f"第{chapter_number}章计划没有承接叙事义务 {obligation.id}: {obligation.payoff_test}"
        issue = FuturePlanAuditIssue(
            issue_type=issue_type,
            severity="error",
            target_chapter=chapter_number,
            target_plan_id=str(plan.id or ""),
            description=description,
            evidence_refs=[f"obligation:{obligation.id}", f"chapter_plan:{chapter_number}"],
            patch_type="obligation_plan_binding",
            metadata={
                "obligation_id": obligation.id,
                "priority": obligation.priority,
                "payoff_test": obligation.payoff_test,
            },
        )
        patch = NarrativePlanPatch(
            id=new_id(),
            project_id=project_id,
            patch_type="obligation_plan_binding",
            target_scope="chapter",
            target_plan_id=str(plan.id or ""),
            target_arc_id=str(plan.arc_plan_id or ""),
            affected_chapters=[chapter_number],
            source_obligation_ids=[obligation.id],
            old_contract=_chapter_plan_contract(plan),
            new_contract={
                "obligations_to_resolve": [obligation.id],
                "payoff_test": obligation.payoff_test,
                "summary": obligation.summary,
            },
            diff_summary=description,
            must_preserve=[str(plan.title or ""), str(plan.one_line or "")],
            must_not_change=[f"remove unresolved obligation {obligation.id}"],
            writer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation.id,
                    "priority": obligation.priority,
                    "summary": obligation.summary,
                    "payoff_test": obligation.payoff_test,
                    "deadline_chapter": obligation.deadline_chapter,
                }
            ],
            reviewer_context_injections=[
                {
                    "type": "narrative_obligation",
                    "obligation_id": obligation.id,
                    "payoff_test": obligation.payoff_test,
                    "must_resolve_now": True,
                }
            ],
            expected_resolution_tests=[obligation.payoff_test],
            validation_status="pending",
            metadata=issue.metadata,
        )
        return issue, patch

    def _audit_band_obligation_binding(
        self,
        *,
        project_id: str,
        current_chapter: int,
        band_row: BandExperiencePlan,
        obligation: NarrativeObligation,
    ) -> tuple[FuturePlanAuditIssue, NarrativePlanPatch]:
        description = f"band plan {band_row.band_id} 没有承接叙事义务 {obligation.id}: {obligation.payoff_test}"
        issue = FuturePlanAuditIssue(
            issue_type="obligation_missing_from_band_plan",
            severity="error",
            target_chapter=int(band_row.chapter_end or obligation.deadline_chapter or 0),
            target_plan_id=str(band_row.id or band_row.band_id or ""),
            description=description,
            evidence_refs=[f"obligation:{obligation.id}", f"band_plan:{band_row.band_id}"],
            patch_type="obligation_band_plan_binding",
            metadata={
                "obligation_id": obligation.id,
                "priority": obligation.priority,
                "payoff_test": obligation.payoff_test,
                "band_id": band_row.band_id,
            },
        )
        patch = BandPlanPatcher().build_obligation_patch(
            project_id=project_id,
            band_row=band_row,
            obligations=[obligation],
            current_chapter=current_chapter,
            patch_type="obligation_band_plan_binding",
        )
        return issue, patch

    @staticmethod
    def _apply_countdown_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        metadata = patch.metadata or patch.new_contract
        key = str(metadata.get("countdown_key") or patch.new_contract.get("countdown_key") or "").strip()
        label = str(metadata.get("label") or patch.new_contract.get("label") or key or "倒计时").strip()
        latest = int(metadata.get("latest_remaining_minutes") or patch.new_contract.get("latest_remaining_minutes") or 0)
        rewrite_false_prior = bool(metadata.get("false_prior_conflict"))
        instruction = (
            f"{label}已关闭；不得写成仍有剩余时间。"
            if latest <= 0
            else f"{label}必须延续最新 canon ledger：剩余时间不得超过 {latest} 分钟。"
        )
        hard_instruction = str(patch.new_contract.get("hard_rule") or _hard_countdown_instruction(label=label, key=key, latest=latest))
        plan.title = _rewrite_stale_countdown_text(
            str(plan.title or ""),
            key=key,
            label=label,
            latest=latest,
            rewrite_false_prior=rewrite_false_prior,
        )
        plan.one_line = _rewrite_stale_countdown_text(
            _strip_countdown_instruction_text(str(plan.one_line or "")),
            key=key,
            label=label,
            latest=latest,
            rewrite_false_prior=rewrite_false_prior,
        )
        goals = _loads(plan.goals_json, [])
        rewritten_goals = [
            _rewrite_stale_countdown_text(
                _strip_countdown_instruction_text(str(item)),
                key=key,
                label=label,
                latest=latest,
                rewrite_false_prior=rewrite_false_prior,
            )
            for item in goals
            if str(item).strip()
        ]
        plan.goals_json = _json([item for item in rewritten_goals if str(item).strip()][:5])
        plan.task_contract_json = _json(
            _rewrite_json_strings(
                _strip_countdown_instruction_noise(_loads(plan.task_contract_json, [])),
                key=key,
                label=label,
                latest=latest,
                rewrite_false_prior=rewrite_false_prior,
            )
        )
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        experience = _rewrite_json_strings(
            _strip_countdown_instruction_noise(experience),
            key=key,
            label=label,
            latest=latest,
            rewrite_false_prior=rewrite_false_prior,
        )
        rule_anchors = [str(item) for item in experience.get("rule_anchors", []) if str(item).strip()]
        if hard_instruction and hard_instruction not in rule_anchors:
            rule_anchors.insert(0, hard_instruction)
        if instruction not in rule_anchors:
            rule_anchors.insert(0, instruction)
        experience["rule_anchors"] = rule_anchors[:8]
        plan.experience_plan_json = _json(experience)

    @staticmethod
    def _apply_obligation_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        obligation_ids = list(patch.source_obligation_ids or [])
        payoff_tests = [str(test).strip() for test in patch.expected_resolution_tests if str(test).strip()]
        payoff_tests = payoff_tests or [
            str(item.get("payoff_test") or "").strip()
            for item in patch.reviewer_context_injections
            if isinstance(item, dict) and str(item.get("payoff_test") or "").strip()
        ]
        contract_payoff = str(patch.new_contract.get("payoff_test") or "").strip()
        if contract_payoff and contract_payoff not in payoff_tests:
            payoff_tests.append(contract_payoff)
        goals = [str(item).strip() for item in _loads(plan.goals_json, []) if str(item).strip()]
        for obligation_id, payoff_test in zip(obligation_ids, payoff_tests or [""]):
            goal = f"偿还叙事义务 {obligation_id}：{payoff_test}".strip("：")
            if goal not in goals:
                goals.insert(0, goal)
        plan.goals_json = _json(goals[:5])
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        progress_markers = [str(item) for item in experience.get("progress_markers", []) if str(item).strip()]
        rule_anchors = [str(item) for item in experience.get("rule_anchors", []) if str(item).strip()]
        obligation_contract = list(experience.get("obligations_to_resolve", []) or [])
        for obligation_id in obligation_ids:
            if obligation_id and obligation_id not in obligation_contract:
                obligation_contract.append(obligation_id)
        for payoff_test in payoff_tests:
            if payoff_test and payoff_test not in progress_markers:
                progress_markers.insert(0, payoff_test)
            if payoff_test and payoff_test not in rule_anchors:
                rule_anchors.insert(0, payoff_test)
        experience["obligations_to_resolve"] = obligation_contract
        experience["progress_markers"] = progress_markers[:5]
        experience["rule_anchors"] = rule_anchors[:5]
        plan.experience_plan_json = _json(experience)

    @staticmethod
    def _apply_custody_state_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        metadata = patch.metadata or patch.new_contract
        character_name = str(metadata.get("character_name") or patch.new_contract.get("character_name") or "").strip()
        latest_chapter = int(metadata.get("latest_chapter") or patch.new_contract.get("latest_chapter") or 0)
        hard_instruction = str(
            patch.new_contract.get("rule")
            or _hard_custody_instruction(character_name=character_name, latest_chapter=latest_chapter)
        )
        plan.title = _rewrite_stale_custody_text(str(plan.title or ""), character_name=character_name)
        plan.one_line = _rewrite_stale_custody_text(str(plan.one_line or ""), character_name=character_name)
        goals = [
            _rewrite_stale_custody_text(str(item), character_name=character_name)
            for item in _loads(plan.goals_json, [])
            if str(item).strip()
        ]
        plan.goals_json = _json([item for item in goals if str(item).strip()][:5])
        plan.task_contract_json = _json(
            _rewrite_custody_json_strings(_loads(plan.task_contract_json, []), character_name=character_name)
        )
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        experience = _rewrite_custody_json_strings(experience, character_name=character_name)
        if not isinstance(experience, dict):
            experience = {}
        progress_markers = [str(item) for item in experience.get("progress_markers", []) if str(item).strip()]
        rule_anchors = [str(item) for item in experience.get("rule_anchors", []) if str(item).strip()]
        if hard_instruction and hard_instruction not in rule_anchors:
            rule_anchors.insert(0, hard_instruction)
        progress = f"承接{character_name}已脱困但仍受追踪器或系统权限限制的状态。"
        if character_name and progress not in progress_markers:
            progress_markers.insert(0, progress)
        experience["progress_markers"] = progress_markers[:5]
        experience["rule_anchors"] = rule_anchors[:8]
        plan.experience_plan_json = _json(experience)

    @staticmethod
    def _apply_prompt_plan_patch(plan: ChapterPlan, patch: NarrativePlanPatch) -> None:
        instruction = str(patch.new_contract.get("prompt_json_instruction") or patch.diff_summary or "").strip()
        if not instruction:
            return
        goals = [str(item).strip() for item in _loads(plan.goals_json, []) if str(item).strip()]
        if instruction not in goals:
            goals.insert(0, instruction)
        plan.goals_json = _json(goals[:5])
        experience = _loads(plan.experience_plan_json, {})
        if not isinstance(experience, dict):
            experience = {}
        progress_markers = [str(item) for item in experience.get("progress_markers", []) if str(item).strip()]
        if instruction not in progress_markers:
            progress_markers.insert(0, instruction)
        experience["progress_markers"] = progress_markers[:5]
        plan.experience_plan_json = _json(experience)


def _countdown_constraints(canon_quality_context: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(canon_quality_context, dict):
        return []
    return [
        item
        for item in canon_quality_context.get("countdown_constraints", []) or []
        if isinstance(item, dict)
    ]


def _character_state_constraints(canon_quality_context: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(canon_quality_context, dict):
        return []
    return [
        item
        for item in canon_quality_context.get("character_state_constraints", []) or []
        if isinstance(item, dict)
    ]


def _open_signal_constraints(canon_quality_context: dict[str, Any]) -> list[dict[str, Any]]:
    if not isinstance(canon_quality_context, dict):
        return []
    return [
        item
        for item in canon_quality_context.get("open_signals", []) or []
        if isinstance(item, dict)
    ]


def _chapter_plan_contract(plan: ChapterPlan) -> dict[str, Any]:
    return {
        "chapter_number": int(plan.chapter_number or 0),
        "title": str(plan.title or ""),
        "one_line": str(plan.one_line or ""),
        "goals": _loads(plan.goals_json, []),
        "task_contract": _loads(plan.task_contract_json, []),
        "experience_plan": _loads(plan.experience_plan_json, {}),
    }


def _plan_has_countdown_instruction_pollution(plan: ChapterPlan) -> bool:
    if "必须延续最新 canon ledger" in str(plan.one_line or ""):
        return True
    if any("必须延续最新 canon ledger" in str(item) for item in _loads(plan.goals_json, [])):
        return True
    if any("不超过不超过" in str(item) for item in _loads(plan.goals_json, [])):
        return True
    experience = _loads(plan.experience_plan_json, {})
    if not isinstance(experience, dict):
        return False
    for key, raw_items in experience.items():
        items = raw_items if isinstance(raw_items, list) else [raw_items]
        for item in items:
            text = str(item)
            if "不超过不超过" in text:
                return True
            if key != "rule_anchors" and "必须延续最新 canon ledger" in text:
                return True
    return False


def _plan_text(plan: ChapterPlan) -> str:
    payload = _chapter_plan_contract(plan)
    return json.dumps(payload, ensure_ascii=False, sort_keys=True)


_CUSTODY_FREE_STATES = {"free", "released", "rescued", "escaped"}
_CUSTODY_PLAN_STALE_MARKERS = (
    "被捕状态",
    "仍被捕",
    "仍被关押",
    "仍被羁押",
    "继续被关押",
    "继续被羁押",
    "被关押",
    "被关在",
    "被关进",
    "被扣押",
    "被捕",
    "被固定",
    "被束缚",
    "被锁在",
    "被磁扣锁",
    "被磁力铐",
    "临时羁押室",
    "羁押室",
    "救援窗口",
    "营救窗口",
    "救出",
    "营救",
)
_CUSTODY_RECAPTURE_MARKERS = (
    "再次被捕",
    "再度被捕",
    "重新被捕",
    "又被捕",
    "又被带走",
    "重新关押",
    "被重新关押",
    "再度关押",
    "重新关进",
    "再次关进",
    "又被关",
    "重新控制",
    "被押回",
    "被抓回",
    "被拖回",
)


def _is_custody_state_patch(patch: NarrativePlanPatch) -> bool:
    metadata = patch.metadata if isinstance(patch.metadata, dict) else {}
    if metadata.get("conflict_type") == "custody_state":
        return True
    return str(patch.new_contract.get("transition_type") or "") == "custody_state"


def _minimum_scope_for_obligation(obligation: NarrativeObligation) -> str:
    metadata = obligation.metadata if isinstance(obligation.metadata, dict) else {}
    explicit = str(metadata.get("minimum_scope") or "").strip()
    if explicit:
        return explicit
    if obligation.obligation_type in {
        "reader_promise_payoff",
        "reveal_escalation_needed",
        "style_repetition_pressure",
        "repeated_scene_pattern",
    }:
        return "band"
    return "chapter"


def _band_row_for_obligation(
    *,
    obligation: NarrativeObligation,
    band_rows: list[BandExperiencePlan],
    current_chapter: int,
) -> BandExperiencePlan | None:
    deadline = int(obligation.deadline_chapter or 0)
    for row in band_rows:
        if int(row.chapter_start or 0) <= deadline <= int(row.chapter_end or 0):
            return row
    for row in band_rows:
        if int(row.chapter_end or 0) > int(current_chapter or 0):
            return row
    return None


def _band_contract_covers_obligation(row: BandExperiencePlan, obligation: NarrativeObligation) -> bool:
    obligation_id = str(obligation.id or "").strip()
    if not obligation_id:
        return False
    try:
        payload = json.loads(row.schedule_json or "{}")
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    try:
        schedule = BandDelightSchedule.model_validate(payload)
    except Exception:
        return False
    contract = schedule.band_obligation_contract
    if obligation_id not in contract.open_obligations:
        return False
    if obligation_id not in contract.payoff_tests:
        return False
    return bool(str(contract.payoff_tests.get(obligation_id) or "").strip())


def _plan_mentions_stale_custody(text: str, *, character_name: str) -> bool:
    if not character_name or character_name not in text:
        return False
    clauses = re.split(r"[，,。；;！!？?\n]+", text)
    for clause in clauses:
        if not clause.strip():
            continue
        if not any(marker in clause for marker in _CUSTODY_PLAN_STALE_MARKERS):
            continue
        if character_name not in clause and "救援窗口" not in clause and "营救窗口" not in clause:
            continue
        if _clause_negates_custody_conflict(clause):
            continue
        return True
    return False


def _plan_declares_recapture_bridge(text: str, *, character_name: str) -> bool:
    if not character_name or character_name not in text:
        return False
    return any(marker in text for marker in _CUSTODY_RECAPTURE_MARKERS)


def _clause_negates_custody_conflict(clause: str) -> bool:
    return any(
        marker in clause
        for marker in (
            "不得把",
            "不要把",
            "不能把",
            "禁止把",
            "避免把",
            "不得将",
            "不要将",
            "不能将",
            "禁止将",
            "不得写",
            "不要写",
            "不能写",
            "禁止写",
            "不得让",
            "不要让",
            "不能让",
            "禁止让",
        )
    )


def _hard_custody_instruction(*, character_name: str, latest_chapter: int) -> str:
    prefix = f"{character_name}已在第{latest_chapter}章脱困" if latest_chapter else f"{character_name}已脱困"
    return (
        f"{prefix}；本章必须承接已脱困但仍受追踪器或系统权限限制的状态，"
        f"不得把{character_name}写回被捕/羁押/固定状态。"
        "如果剧情需要再次关押，必须先写出明确的再次被捕桥接事件。"
    )


def _rewrite_stale_custody_text(text: str, *, character_name: str) -> str:
    if not text or not character_name:
        return text
    replacements = {
        f"在不破坏{character_name}被捕状态的前提下": f"在承接{character_name}已脱困但仍受追踪器或系统权限限制的前提下",
        "在不破坏被捕状态的前提下": "在承接已脱困但仍受追踪器或系统权限限制的前提下",
        f"{character_name}仍被羁押": f"{character_name}已脱困但仍受追踪器或系统权限限制",
        f"{character_name}仍被关押": f"{character_name}已脱困但仍受追踪器或系统权限限制",
        f"{character_name}被羁押": f"{character_name}受追踪器或系统权限限制",
        f"{character_name}被关押": f"{character_name}受追踪器或系统权限限制",
        f"救出{character_name}": f"保护已脱困的{character_name}并解除追踪器",
        f"营救{character_name}": f"保护已脱困的{character_name}并解除追踪器",
        "救援窗口": "追踪器解除窗口",
        "营救窗口": "追踪器解除窗口",
        "被捕状态": "已脱困但仍受追踪器或系统权限限制的状态",
        "仍被羁押": "已脱困但仍受追踪器或系统权限限制",
        "仍被关押": "已脱困但仍受追踪器或系统权限限制",
        "被羁押": "受追踪器或系统权限限制",
        "被关押": "受追踪器或系统权限限制",
        "临时羁押室": "临时监听点",
        "羁押室": "监听点",
        "被固定": "受权限限制",
        "被束缚": "受权限限制",
        "被锁在": "受权限限制于",
        "被磁力铐": "受追踪器",
        "被磁扣锁": "受追踪器",
    }
    output = text
    for old, new in replacements.items():
        output = output.replace(old, new)
    return output


def _rewrite_custody_json_strings(value: Any, *, character_name: str) -> Any:
    if isinstance(value, str):
        return _rewrite_stale_custody_text(value, character_name=character_name)
    if isinstance(value, list):
        return [_rewrite_custody_json_strings(item, character_name=character_name) for item in value]
    if isinstance(value, dict):
        return {
            item_key: _rewrite_custody_json_strings(item, character_name=character_name)
            for item_key, item in value.items()
        }
    return value


def _duration_mentions_for_countdown(text: str, *, key: str, label: str) -> list[dict[str, Any]]:
    clauses = re.split(r"[，,。；;！!？?\n]+", text)
    mentions: list[dict[str, Any]] = []
    countdown_clause_indexes = {
        index
        for index, clause in enumerate(clauses)
        if _clause_mentions_countdown(clause, key=key, label=label)
    }
    for index, clause in enumerate(clauses):
        mentions_countdown = index in countdown_clause_indexes
        continues_previous_countdown = (
            index - 1 in countdown_clause_indexes
            and _clause_continues_countdown_duration(clause)
            and not _clause_mentions_other_countdown(clause, key=key, label=label)
        )
        if not mentions_countdown and not continues_previous_countdown:
            continue
        if _clause_declares_reset_or_branch(clause):
            continue
        for mention in _duration_mentions(clause):
            mention["clause"] = clause
            mention["context"] = "。".join(
                str(item)
                for item in (
                    clauses[index - 1] if index > 0 else "",
                    clause,
                    clauses[index + 1] if index + 1 < len(clauses) else "",
                )
                if str(item).strip()
            )
            mentions.append(mention)
    return mentions


def _is_false_prior_countdown_clause(clause: str) -> bool:
    text = str(clause or "")
    if _clause_declares_reset_or_branch(text):
        return False
    return any(
        marker in text
        for marker in (
            "accepted canon",
            "最新 canon",
            "最新ledger",
            "最新 ledger",
            "canon ledger",
            "已接受 canon",
            "已 accepted",
            "上一章 accepted",
            "承接上一章",
            "连续性护栏",
            "必须紧接此状态",
        )
    )


def _hard_countdown_instruction(*, label: str, key: str, latest: int) -> str:
    if latest <= 0:
        return (
            f"{label}已关闭；本章不得把同一倒计时写成仍有剩余时间，"
            "除非明确标记为 reset 或 branch clock。"
        )
    base = (
        f"{label}必须延续最新 canon ledger：剩余时间不得超过 {latest} 分钟。"
        "旧计划/旧摘要时间不得写成前文事实；"
        "不得写“系统日志原本还有三天/七天/几小时”、"
        "“主角以为还有几天”或任何大于最新 ledger 的旧尺度，"
        f"除非明确标记为公开伪数据、误导信息、reset 或 branch clock。"
    )
    if key == "memory_reset" or "记忆重置" in label or "重置周期" in label:
        return (
            base
            + f" 本章所有记忆重置/校准/熔铸窗口只能继续小于等于 {latest} 分钟，"
            "不要写回三天/七天/三小时/两小时等旧尺度。"
        )
    return base


def _clause_mentions_countdown(clause: str, *, key: str, label: str) -> bool:
    candidates = _countdown_markers_for(key=key, label=label)
    return any(candidate and candidate in clause for candidate in candidates)


def _clause_mentions_other_countdown(clause: str, *, key: str, label: str) -> bool:
    current = set(_countdown_markers_for(key=key, label=label))
    for other_key, other_label in (
        ("memory_reset", "记忆重置周期"),
        ("archive_cleanup", "档案清理窗口"),
        ("terminal_audit_window", "终端审计窗口"),
        ("core_access_window", "核心层授权窗口"),
        ("public_countdown", "公开数据倒计时"),
        ("main", "主倒计时"),
    ):
        if other_key == key:
            continue
        for candidate in _countdown_markers_for(key=other_key, label=other_label):
            if candidate and candidate not in current and candidate in clause:
                return True
    return False


def _countdown_markers_for(*, key: str, label: str) -> list[str]:
    candidates = [key, label]
    if key == "memory_reset" or "记忆" in label or "重置" in label:
        candidates.extend(["记忆重置", "重置周期", "记忆熔铸", "熔铸倒计时", "熔铸窗口", "memory_reset"])
    if key == "archive_cleanup" or "档案清理" in label:
        candidates.extend(["档案清理", "清理窗口", "archive_cleanup"])
    if key == "terminal_audit_window" or "终端审计" in label:
        candidates.extend(["终端审计", "终端审计窗口", "terminal_audit_window"])
    if key == "core_access_window" or "核心层" in label or "授权" in label:
        candidates.extend(["核心层入口", "核心层授权窗口", "核心层的授权窗口", "入口关闭", "core_access_window"])
    if key == "public_countdown" or "公开" in label:
        candidates.extend(["公开数据", "公开窗口", "对外数据", "public_countdown"])
    if key == "main":
        candidates.extend(["主倒计时", "倒计时"])
    return candidates


def _clause_continues_countdown_duration(clause: str) -> bool:
    if not _duration_mentions(clause):
        return False
    return any(marker in clause for marker in ("只剩", "还剩", "剩余", "还有", "距离", "窗口", "提前至", "缩短到"))


def _clause_declares_reset_or_branch(clause: str) -> bool:
    return any(marker in clause for marker in ("分支倒计时", "branch", "另一个倒计时", "新的倒计时", "重新开始", "重置为"))


_DURATION_RE = re.compile(r"(不到|不超过|约|大约|剩余|还有|还剩|第)?\s*([0-9]+|[零一二两三四五六七八九十百]+)\s*(分钟|分|小时|钟头|天|日)")
_COUNTDOWN_INSTRUCTION_RE = re.compile(
    r"(?:[A-Za-z_]+|[\u4e00-\u9fff]+)必须延续最新 canon ledger：剩余时间不得超过(?:不超过)?\s*[0-9零一二两三四五六七八九十百]+\s*分钟。?"
)


def _duration_mentions(text: str) -> list[dict[str, Any]]:
    mentions: list[dict[str, Any]] = []
    for match in _DURATION_RE.finditer(text):
        amount = _parse_amount(match.group(2))
        if amount <= 0:
            continue
        unit = match.group(3)
        multiplier = 1
        if unit in {"小时", "钟头"}:
            multiplier = 60
        elif unit in {"天", "日"}:
            multiplier = 24 * 60
        mentions.append(
            {
                "raw": match.group(0).strip(),
                "minutes": amount * multiplier,
                "span_start": match.start(),
                "span_end": match.end(),
            }
        )
    return mentions


def _parse_amount(raw: str) -> int:
    value = str(raw or "").strip()
    if not value:
        return 0
    if value.isdigit():
        return int(value)
    return _parse_chinese_number(value)


def _parse_chinese_number(value: str) -> int:
    digits = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5, "六": 6, "七": 7, "八": 8, "九": 9}
    if value == "十":
        return 10
    if "百" in value:
        left, _, right = value.partition("百")
        return (digits.get(left, 1) or 1) * 100 + _parse_chinese_number(right)
    if "十" in value:
        left, _, right = value.partition("十")
        tens = digits.get(left, 1) if left else 1
        ones = digits.get(right, 0) if right else 0
        return tens * 10 + ones
    total = 0
    for char in value:
        total = total * 10 + digits.get(char, 0)
    return total


def _rewrite_stale_countdown_text(
    text: str,
    *,
    key: str,
    label: str,
    latest: int,
    rewrite_false_prior: bool = False,
) -> str:
    if not text or latest < 0:
        return text
    parts = re.split(r"([，,。；;！!？?\n]+)", text)
    clause_indexes = [index for index in range(0, len(parts), 2)]
    countdown_clause_indexes = {
        index
        for index in clause_indexes
        if _clause_mentions_countdown(parts[index], key=key, label=label)
    }
    output: list[str] = []
    previous_clause_index: int | None = None
    for index, part in enumerate(parts):
        if index not in clause_indexes:
            output.append(part)
            continue
        mentions_countdown = index in countdown_clause_indexes
        continues_previous_countdown = (
            previous_clause_index in countdown_clause_indexes
            and _clause_continues_countdown_duration(part)
            and not _clause_mentions_other_countdown(part, key=key, label=label)
        )
        if (mentions_countdown or continues_previous_countdown) and not _clause_declares_reset_or_branch(part):
            surrounding = "。".join(
                str(parts[item])
                for item in (index - 2, index, index + 2)
                if item in clause_indexes and 0 <= item < len(parts) and str(parts[item]).strip()
            )
            part = _replace_stale_duration_mentions(
                part,
                latest=latest,
                rewrite_false_prior=(
                    rewrite_false_prior and _is_false_prior_countdown_clause(surrounding)
                ),
            )
        output.append(part)
        previous_clause_index = index
    return "".join(output)


def _replace_stale_duration_mentions(text: str, *, latest: int, rewrite_false_prior: bool = False) -> str:
    def replace(match: re.Match[str]) -> str:
        amount = _parse_amount(match.group(2))
        unit = match.group(3)
        multiplier = 1
        if unit in {"小时", "钟头"}:
            multiplier = 60
        elif unit in {"天", "日"}:
            multiplier = 24 * 60
        minutes = amount * multiplier
        if latest <= 0 and minutes > 0:
            return "已关闭"
        if minutes <= latest and not (rewrite_false_prior and minutes < latest):
            return match.group(0)
        return f"不超过{latest}分钟"

    return _DURATION_RE.sub(replace, text)


def _rewrite_json_strings(
    value: Any,
    *,
    key: str,
    label: str,
    latest: int,
    rewrite_false_prior: bool = False,
) -> Any:
    if isinstance(value, str):
        return _rewrite_stale_countdown_text(
            value,
            key=key,
            label=label,
            latest=latest,
            rewrite_false_prior=rewrite_false_prior,
        )
    if isinstance(value, list):
        return [
            _rewrite_json_strings(
                item,
                key=key,
                label=label,
                latest=latest,
                rewrite_false_prior=rewrite_false_prior,
            )
            for item in value
        ]
    if isinstance(value, dict):
        return {
            item_key: _rewrite_json_strings(
                item,
                key=key,
                label=label,
                latest=latest,
                rewrite_false_prior=rewrite_false_prior,
            )
            for item_key, item in value.items()
        }
    return value


def _strip_countdown_instruction_noise(value: Any, *, container_key: str = "") -> Any:
    if isinstance(value, str):
        return _strip_countdown_instruction_text(value)
    if isinstance(value, list):
        cleaned: list[Any] = []
        for item in value:
            if container_key == "rule_anchors" and isinstance(item, str):
                if "不超过不超过" in item:
                    continue
                cleaned.append(item)
                continue
            stripped = _strip_countdown_instruction_noise(item, container_key=container_key)
            if isinstance(stripped, str) and not stripped.strip():
                continue
            cleaned.append(stripped)
        return cleaned
    if isinstance(value, dict):
        return {
            item_key: _strip_countdown_instruction_noise(item, container_key=str(item_key))
            for item_key, item in value.items()
        }
    return value


def _strip_countdown_instruction_text(text: str) -> str:
    result = str(text or "")
    previous = None
    while previous != result:
        previous = result
        result = _COUNTDOWN_INSTRUCTION_RE.sub("", result)
    result = re.sub(r"\s{2,}", " ", result)
    result = re.sub(r"^\s*[。；;，,]\s*", "", result)
    return result.strip()


def _inspected_chapters(
    *,
    plans: list[ChapterPlan],
    band_rows: list[BandExperiencePlan] | None,
    current_chapter: int,
    include_current: bool,
) -> list[int]:
    inspected = [
        int(plan.chapter_number or 0)
        for plan in plans
        if include_current or int(plan.chapter_number or 0) > int(current_chapter or 0)
    ]
    for row in band_rows or []:
        for chapter in range(int(row.chapter_start or 0), int(row.chapter_end or 0) + 1):
            if (include_current or chapter > int(current_chapter or 0)) and chapter not in inspected:
                inspected.append(chapter)
    inspected.sort()
    return inspected


def _future_plan_prompt_payload(
    *,
    plans: list[ChapterPlan],
    canon_quality_context: dict[str, Any],
    obligations: list[NarrativeObligation],
    target_total_chapters: int,
    current_chapter: int,
    include_current: bool,
    band_rows: list[BandExperiencePlan] | None,
) -> dict[str, Any]:
    return {
        "writer_output": str(canon_quality_context.get("writer_output") or ""),
        "current_future_plan": [
            _chapter_plan_prompt_item(plan)
            for plan in plans
            if include_current or int(plan.chapter_number or 0) > int(current_chapter or 0)
        ],
        "canon_context": canon_quality_context.get("canon_context", []),
        "newly_extracted_facts": canon_quality_context.get("accepted_facts", []),
        "obligations": [obligation.model_dump(mode="json") for obligation in obligations],
        "target_total_chapters": int(target_total_chapters or 0),
        "band_context": [_band_prompt_item(row) for row in band_rows or []],
        "heuristic_hints": canon_quality_context.get("heuristic_hints", []),
    }


def _chapter_plan_prompt_item(plan: ChapterPlan) -> dict[str, Any]:
    return {
        "plan_item_id": str(plan.id or ""),
        "chapter_number": int(plan.chapter_number or 0),
        "title": str(plan.title or ""),
        "one_line": str(plan.one_line or ""),
        "goals": _loads(str(plan.goals_json or "[]"), []),
        "task_contract": _loads(str(plan.task_contract_json or "[]"), []),
        "experience_plan": _loads(str(plan.experience_plan_json or "{}"), {}),
        "status": str(plan.status or ""),
        "lock_level": "locked" if str(plan.status or "") == "accepted" else "soft",
    }


def _band_prompt_item(row: BandExperiencePlan) -> dict[str, Any]:
    return {
        "band_id": str(row.band_id or row.id or ""),
        "chapter_start": int(row.chapter_start or 0),
        "chapter_end": int(row.chapter_end or 0),
        "status": str(getattr(row, "status", "") or ""),
    }


def _first_prompt_target_plan(
    *,
    plans: list[ChapterPlan],
    current_chapter: int,
    include_current: bool,
) -> ChapterPlan | None:
    candidates = [
        plan
        for plan in plans
        if include_current or int(plan.chapter_number or 0) > int(current_chapter or 0)
    ]
    return sorted(candidates, key=lambda item: int(item.chapter_number or 0))[0] if candidates else None


def _prompt_issue_plan_id(
    *,
    raw_issue: dict[str, Any],
    impacts_by_id: dict[str, dict[str, Any]],
) -> str:
    for key in ("plan_item_id", "target_plan_id", "target_id"):
        value = str(raw_issue.get(key) or "").strip()
        if value:
            return value
    if len(impacts_by_id) == 1:
        return next(iter(impacts_by_id))
    for evidence in raw_issue.get("evidence", []) or []:
        if not isinstance(evidence, dict):
            continue
        location = str(evidence.get("location") or "")
        for plan_id in impacts_by_id:
            if plan_id and plan_id in location:
                return plan_id
    return ""


def _prompt_issue_to_future_plan_issue(
    *,
    issue: dict[str, Any],
    result: dict[str, Any],
    current_chapter: int,
    plan: ChapterPlan | None,
    plan_id: str,
    impact: dict[str, Any] | None,
    min_blocking_confidence: float,
) -> FuturePlanAuditIssue:
    blocking = issue_can_block(issue, min_confidence=min_blocking_confidence)
    target_chapter = int(getattr(plan, "chapter_number", 0) or current_chapter + 1)
    target_plan_id = str(plan_id or getattr(plan, "id", "") or "")
    evidence_refs = prompt_issue_evidence_refs(issue)
    if not evidence_refs and issue.get("issue_id"):
        evidence_refs = [f"prompt_json:{issue.get('issue_id')}"]
    return FuturePlanAuditIssue(
        issue_type=str(issue.get("type") or "future_plan_prompt_issue"),
        severity="error" if blocking else "warning",
        target_chapter=target_chapter,
        target_plan_id=target_plan_id,
        description=str(issue.get("claim") or result.get("summary") or "Future plan prompt issue."),
        evidence_refs=evidence_refs,
        patch_type="future_plan_prompt_update",
        blocking=blocking,
        metadata={
            "source_analyzer": str(result.get("analyzer") or "FuturePlanPromptAuditor"),
            "source_mode": "prompt_json",
            "original_verdict": str(result.get("verdict") or ""),
            "original_confidence": float(result.get("confidence") or 0.0),
            "blocking_origin": "prompt_json" if blocking else "non_blocking_prompt_json",
            "prompt_json_issue": issue,
            "plan_impact": impact or {},
        },
    )


def _prompt_issue_to_plan_patch(
    *,
    project_id: str,
    issue: dict[str, Any],
    result: dict[str, Any],
    plan: ChapterPlan | None,
    plan_id: str,
    target_chapter: int,
    index: int,
    impact: dict[str, Any] | None,
) -> NarrativePlanPatch | None:
    issue_type = str(issue.get("type") or "")
    if issue_type not in {
        "plan_needs_update",
        "soft_plan_mismatch",
        "future_beat_made_impossible",
        "locked_plan_contradiction",
    }:
        return None
    suggested_fix = str(issue.get("suggested_fix") or "").strip()
    recommended_patch = str((impact or {}).get("recommended_plan_patch") or "").strip()
    instruction = suggested_fix or recommended_patch or str(issue.get("claim") or result.get("summary") or "").strip()
    if not instruction:
        instruction = "Update the future plan to reflect the prompt-json audit result."
    return NarrativePlanPatch(
        id="",
        project_id=project_id,
        patch_type="future_plan_prompt_update",
        target_scope="chapter",
        target_plan_id=str(plan_id or getattr(plan, "id", "") or ""),
        target_arc_id=str(getattr(plan, "arc_plan_id", "") or ""),
        affected_chapters=[int(target_chapter or getattr(plan, "chapter_number", 0) or 0)],
        source_signal_ids=[str(issue.get("issue_id") or f"prompt_json_issue:{index}")],
        old_contract=_chapter_plan_contract(plan) if plan is not None else {},
        new_contract={
            "prompt_json_instruction": instruction,
            "source_analyzer": str(result.get("analyzer") or "FuturePlanPromptAuditor"),
            "issue_type": issue_type,
        },
        diff_summary=str(issue.get("claim") or result.get("summary") or issue_type),
        writer_context_injections=[
            {
                "type": "future_plan_prompt_update",
                "instruction": instruction,
                "source": str(result.get("analyzer") or "FuturePlanPromptAuditor"),
            }
        ],
        reviewer_context_injections=[
            {
                "type": "future_plan_prompt_update",
                "payoff_test": instruction,
                "source": str(result.get("analyzer") or "FuturePlanPromptAuditor"),
            }
        ],
        expected_resolution_tests=[instruction],
        validation_status="pending",
        metadata={
            "source_analyzer": str(result.get("analyzer") or "FuturePlanPromptAuditor"),
            "source_mode": "prompt_json",
            "original_verdict": str(result.get("verdict") or ""),
            "original_confidence": float(result.get("confidence") or 0.0),
            "prompt_json_issue_id": str(issue.get("issue_id") or ""),
            "plan_impact": impact or {},
        },
    )


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False)


def _loads(raw: str, default: Any) -> Any:
    try:
        value = json.loads(raw or "")
    except (TypeError, json.JSONDecodeError):
        return default
    return value if value is not None else default
