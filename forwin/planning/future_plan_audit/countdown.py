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

from .helpers import *
from .models import AuditStatus, FuturePlanAuditIssue, FuturePlanAuditRun


class FuturePlanCountdownMixin:
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


__all__ = ["FuturePlanCountdownMixin"]
