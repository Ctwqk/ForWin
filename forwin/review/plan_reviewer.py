from __future__ import annotations

import json
import logging

from sqlalchemy import select

from forwin.book_state.extraction.chapter_input import plan_binding_targets, plan_goal_targets
from forwin.models.project import ChapterPlan
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.issue_groups import issue_group_for_issue
from forwin.review.plan_checks import evaluate_constraint_issues

from forwin.writer.prompt_core.world_repair import PLAN_BINDING_REPAIR_TEXT


logger = logging.getLogger(__name__)


class PlanContractReviewer:
    name = "plan_contract"

    def review(self, context, writer_output: WriterOutput, *, chapter_plan=None, session=None,
               project_id: str = "", **_kwargs) -> ReviewVerdict:
        if chapter_plan is None and session is not None:
            if not project_id:
                logger.warning("Plan binding lookup missing project_id: chapter=%s", context.chapter_number)
            chapter_plan = session.scalar(select(ChapterPlan).where(
                ChapterPlan.project_id == project_id,
                ChapterPlan.chapter_number == context.chapter_number,
            ))
        issues = self.issues(context=context, writer_output=writer_output, chapter_plan=chapter_plan)
        verdict = (
            "fail"
            if any(issue.severity == "error" for issue in issues)
            else (
                "warn"
                if any(issue.severity == "warning" for issue in issues)
                else "pass"
            )
        )
        return ReviewVerdict(verdict=verdict, issues=issues)

    @staticmethod
    def issues(*, context, writer_output: WriterOutput, chapter_plan=None) -> list[ContinuityIssue]:
        delta = writer_output.chapter_delta
        if delta is None:
            # The world consistency signal reports the missing chapter delta.
            return []
        issue_type = "plan_task_fulfillment"
        issue_group = issue_group_for_issue(issue_type=issue_type)
        goal_targets = plan_goal_targets(context.chapter_goals)
        status_rank = {"missing": 0, "partial": 1, "done": 2}
        fulfillment = {}
        for item in delta.plan_fulfillment:
            current = fulfillment.get(item.target_id)
            if current is None or status_rank[item.status] >= status_rank[current.status]:
                fulfillment[item.target_id] = item
        issues: list[ContinuityIssue] = []
        for target_id, goal in goal_targets:
            item = fulfillment.get(target_id)
            status = item.status if item is not None else "missing"
            if status == "done":
                continue
            partial = status == "partial"
            evidence_refs = [f"target={target_id}"]
            if item is not None and item.evidence:
                evidence_refs.append(item.evidence)
            issues.append(
                ContinuityIssue(
                    rule_name="plan_goal_partial" if partial else "plan_goal_missing",
                    severity="warning" if partial else "error",
                    description=(
                        f"计划目标仅部分交付：{goal}" if partial else f"计划目标未交付：{goal}"
                    ),
                    reviewer="plan_contract",
                    issue_type=issue_type,
                    target_scope="chapter",
                    issue_group=issue_group,
                    evidence_refs=evidence_refs,
                    suggested_fix=f"补足计划目标：{goal}",
                    # Blocking buys a missed goal the profile's blocking rewrites (pulp has
                    # no others) before manual review; standard's own budget is larger.
                    blocking=not partial,
                )
            )
        for target_id, binding in plan_binding_targets(chapter_plan):
            item = fulfillment.get(target_id)
            if item is not None and item.status == "done":
                continue
            partial = item is not None and item.status == "partial"
            details = json.loads(binding)
            index = int(target_id.removeprefix("bind:"))
            kind = details.get("kind", "unknown")
            issues.append(ContinuityIssue(
                rule_name="plan_binding_partial" if partial else "plan_binding_missing",
                severity="warning" if partial else "error",
                description=PLAN_BINDING_REPAIR_TEXT["partial" if partial else "missing"].format(n=index + 1, kind=kind),
                reviewer="plan_contract", issue_type=issue_type, target_scope="chapter",
                issue_group=issue_group,
                evidence_refs=[f"target={target_id}", *([item.evidence] if item and item.evidence else [])],
                suggested_fix=PLAN_BINDING_REPAIR_TEXT["fix"].format(n=index + 1, kind=kind),
                original_result={**details, "index": index}, blocking=not partial,
            ))
        issues.extend(
            evaluate_constraint_issues(
                context.active_future_constraints,
                chapter_delta=delta,
                reviewer="plan_contract",
                issue_type="future_constraint",
                target_scope="chapter",
            )
        )
        return issues


__all__ = ["PlanContractReviewer"]
