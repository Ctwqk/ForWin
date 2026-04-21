from __future__ import annotations

from forwin.governance_checks import (
    chapter_combined_text,
    evaluate_constraint_issues,
    evaluate_task_contract,
)
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from .context_builder import build_review_context_pack
from .lint import LintSignalCollector
from .webnovel import WebNovelExperienceReviewer


class HistoricalReviewHub:
    def __init__(
        self,
        *,
        experience_review_enabled: bool = True,
        lint_review_enabled: bool = True,
        llm_client=None,
        llm_enabled: bool | None = None,
    ) -> None:
        self.experience_reviewer = WebNovelExperienceReviewer(
            enabled=experience_review_enabled,
            llm_client=llm_client,
            llm_enabled=llm_enabled,
        )
        self.lint_collector = LintSignalCollector(enabled=lint_review_enabled)

    def review(
        self,
        *,
        project_id: str,
        repo=None,
        context: ChapterContextPack,
        writer_output: WriterOutput,
        continuity_checker,
    ) -> ReviewVerdict:
        continuity = continuity_checker.check(project_id, writer_output)
        lint_signals = self.lint_collector.collect(writer_output)
        review_context = build_review_context_pack(
            repo=repo,
            context=context,
            lint_signals=lint_signals,
        )
        webnovel = self.experience_reviewer.review(review_context, writer_output)
        governance_issues = self._governance_issues(
            context=review_context,
            writer_output=writer_output,
        )
        issues = [
            *self._normalize_issues(continuity.issues, reviewer="continuity"),
            *self._normalize_issues(governance_issues, reviewer="governance"),
            *self._normalize_issues(webnovel.issues, reviewer="webnovel_experience"),
        ]
        governance_verdict = self._issues_verdict(governance_issues)
        verdict = self._merge_verdicts(continuity.verdict, governance_verdict, webnovel.verdict)
        repair_instruction = None
        if verdict == "fail":
            repair_instruction = self._merge_repair_instructions(
                continuity_instruction=(
                    self._continuity_repair_instruction(
                        continuity_issues=[issue for issue in issues if issue.reviewer == "continuity" and issue.severity == "error"],
                        context=context,
                    )
                    if continuity.verdict == "fail"
                    else None
                ),
                governance_instruction=(
                    self._governance_repair_instruction(
                        governance_issues=[issue for issue in issues if issue.reviewer == "governance" and issue.severity == "error"],
                        context=context,
                    )
                    if governance_verdict == "fail"
                    else None
                ),
                webnovel_instruction=webnovel.repair_instruction,
            )
        return ReviewVerdict(
            verdict=verdict,
            issues=issues,
            recommended_action=(
                "rewrite"
                if verdict == "fail"
                else "pause_for_review" if verdict == "warn" else "continue"
            ),
            review_summary=" | ".join(
                item for item in [continuity.review_summary, webnovel.review_summary] if item
            ),
            planned_reward_tags=list(webnovel.planned_reward_tags),
            delivered_reward_tags=list(webnovel.delivered_reward_tags),
            experience_scores=dict(webnovel.experience_scores),
            review_notes=list(webnovel.review_notes),
            lint_signals=list(lint_signals),
            evidence_refs=list(webnovel.evidence_refs),
            confirmed_signal_refs=list(webnovel.confirmed_signal_refs),
            reviewer_mode=webnovel.reviewer_mode,
            repair_instruction=repair_instruction,
            forced_accept_applied=False,
        )

    @staticmethod
    def _issues_verdict(issues: list[ContinuityIssue]) -> str:
        if any(issue.severity == "error" for issue in issues):
            return "fail"
        if any(issue.severity == "warning" for issue in issues):
            return "warn"
        return "pass"

    @staticmethod
    def _governance_issues(
        *,
        context,
        writer_output: WriterOutput,
    ) -> list[ContinuityIssue]:
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

    @staticmethod
    def _normalize_issues(
        issues: list[ContinuityIssue],
        *,
        reviewer: str,
    ) -> list[ContinuityIssue]:
        normalized: list[ContinuityIssue] = []
        for issue in issues:
            normalized.append(
                issue.model_copy(
                    update={
                        "reviewer": issue.reviewer or reviewer,
                        "issue_type": issue.issue_type or ("continuity" if reviewer == "continuity" else reviewer),
                        "target_scope": issue.target_scope or "chapter",
                    }
                )
            )
        return normalized

    @staticmethod
    def _merge_verdicts(*verdicts: str) -> str:
        if "fail" in verdicts:
            return "fail"
        if "warn" in verdicts:
            return "warn"
        return "pass"

    @staticmethod
    def _continuity_repair_instruction(
        *,
        continuity_issues: list[ContinuityIssue],
        context: ChapterContextPack,
    ) -> RepairInstruction:
        return RepairInstruction(
            repair_scope="draft",
            failure_type="continuity",
            must_fix=[issue.description for issue in continuity_issues if issue.severity == "error"],
            must_preserve=[
                context.chapter_plan_title,
                context.chapter_plan_one_line,
                *(context.chapter_goals[:2]),
            ],
            design_patch={
                "continuity_focus": [issue.rule_name for issue in continuity_issues if issue.severity == "error"],
            },
            evidence_refs=[ref for issue in continuity_issues for ref in issue.evidence_refs],
        )

    @staticmethod
    def _governance_repair_instruction(
        *,
        governance_issues: list[ContinuityIssue],
        context: ChapterContextPack,
    ) -> RepairInstruction:
        return RepairInstruction(
            repair_scope="draft",
            failure_type="mixed",
            must_fix=[issue.description for issue in governance_issues if issue.severity == "error"],
            must_preserve=[
                context.chapter_plan_title,
                context.chapter_plan_one_line,
                *(context.chapter_goals[:2]),
            ],
            design_patch={
                "governance_focus": [issue.rule_name for issue in governance_issues],
            },
            evidence_refs=[ref for issue in governance_issues for ref in issue.evidence_refs],
        )

    @staticmethod
    def _merge_repair_instructions(
        *,
        continuity_instruction: RepairInstruction | None,
        governance_instruction: RepairInstruction | None,
        webnovel_instruction: RepairInstruction | None,
    ) -> RepairInstruction | None:
        if continuity_instruction is None and governance_instruction is None:
            return webnovel_instruction
        base_instruction = continuity_instruction or governance_instruction
        if webnovel_instruction is None:
            return base_instruction
        if base_instruction is None:
            return webnovel_instruction

        scope_rank = {"draft": 1, "chapter_plan": 2, "band_plan": 3}
        merged_scope = max(
            [base_instruction.repair_scope, webnovel_instruction.repair_scope],
            key=lambda item: scope_rank.get(item, 1),
        )
        merged_failure_type = (
            base_instruction.failure_type
            if base_instruction.failure_type == webnovel_instruction.failure_type
            else "mixed"
        )
        merged_design_patch = dict(base_instruction.design_patch)
        for key, value in webnovel_instruction.design_patch.items():
            if key in merged_design_patch and isinstance(merged_design_patch[key], list) and isinstance(value, list):
                merged_design_patch[key] = list(dict.fromkeys([*merged_design_patch[key], *value]))
            elif key in merged_design_patch and isinstance(merged_design_patch[key], dict) and isinstance(value, dict):
                merged_design_patch[key] = {**merged_design_patch[key], **value}
            else:
                merged_design_patch[key] = value
        return RepairInstruction(
            repair_scope=merged_scope,
            failure_type=merged_failure_type,
            must_fix=list(dict.fromkeys([*base_instruction.must_fix, *webnovel_instruction.must_fix])),
            must_preserve=list(dict.fromkeys([*base_instruction.must_preserve, *webnovel_instruction.must_preserve])),
            design_patch=merged_design_patch,
            evidence_refs=list(dict.fromkeys([*base_instruction.evidence_refs, *webnovel_instruction.evidence_refs])),
        )
