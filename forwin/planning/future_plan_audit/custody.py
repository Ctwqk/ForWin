from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.base import new_id
from forwin.models.narrative_obligation import FuturePlanAuditRunRow
from forwin.models.phase import BandExperiencePlan
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.planning.band_plan_patcher import BandPlanPatcher
from forwin.planning.obligation_pre_audit import select_urgent_obligation_targets
from forwin.planning.plan_patch_validator import PlanPatchValidator
from forwin.planning.signal_pre_audit import select_stale_signal_targets
from forwin.protocol.experience import BandDelightSchedule

from .helpers import *
from .models import AuditStatus, FuturePlanAuditIssue, FuturePlanAuditRun


class FuturePlanCustodyMixin:
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


__all__ = ["FuturePlanCustodyMixin"]
