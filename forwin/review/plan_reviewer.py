from __future__ import annotations

from forwin.book_state.extraction.chapter_input import plan_goal_targets
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.review.issue_groups import issue_group_for_issue
from forwin.review.plan_checks import evaluate_constraint_issues


class PlanContractReviewer:
    name = "plan_contract"

    def review(self, context, writer_output: WriterOutput, **_kwargs) -> ReviewVerdict:
        issues = self.issues(context=context, writer_output=writer_output)
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
    def issues(*, context, writer_output: WriterOutput) -> list[ContinuityIssue]:
        delta = writer_output.chapter_delta
        issue_type = "plan_task_fulfillment"
        issue_group = issue_group_for_issue(issue_type=issue_type)
        goal_targets = plan_goal_targets(context.chapter_goals)
        if delta is None:
            if not goal_targets:
                return []
            return [
                ContinuityIssue(
                    rule_name="plan_fulfillment_unavailable",
                    severity="error",
                    description="缺少章节抽取，无法核验计划目标。",
                    reviewer="plan_contract",
                    issue_type=issue_type,
                    target_scope="chapter",
                    issue_group=issue_group,
                    suggested_fix="完成章节 delta 抽取后重新核验计划目标。",
                )
            ]
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
                )
            )
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
