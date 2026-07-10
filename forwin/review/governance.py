from __future__ import annotations

from forwin.governance_checks import (
    chapter_combined_text,
    evaluate_constraint_issues,
    evaluate_task_contract,
)
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput


class GovernanceReviewer:
    name = "governance"

    def review(self, context, writer_output: WriterOutput, **_kwargs) -> ReviewVerdict:
        issues = self.issues(context=context, writer_output=writer_output)
        verdict = "fail" if any(issue.severity == "error" for issue in issues) else (
            "warn" if any(issue.severity == "warning" for issue in issues) else "pass"
        )
        return ReviewVerdict(verdict=verdict, issues=issues)

    @staticmethod
    def issues(*, context, writer_output: WriterOutput):
        combined_text = chapter_combined_text(writer_output)
        task_issues = evaluate_task_contract(
            context.chapter_task_contract,
            combined_text=combined_text,
            reviewer="governance",
            issue_type="plan_task_fulfillment",
            target_scope="chapter",
        )
        constraint_issues = evaluate_constraint_issues(
            [
                constraint
                for constraint in context.active_future_constraints
                if str(constraint.level or "") in {"hard", "soft", "hint"}
            ],
            combined_text=combined_text,
            state_changes=writer_output.state_changes,
            events=writer_output.new_events,
            thread_beats=writer_output.thread_beats,
            reviewer="governance",
            issue_type="future_constraint",
            target_scope="chapter",
        )
        return [*task_issues, *constraint_issues]
