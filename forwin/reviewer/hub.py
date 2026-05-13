from __future__ import annotations

import inspect
from forwin.observability.context import OperationContext
from forwin.observability.ports import NullObservability
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import (
    ContinuityIssue,
    RepairInstruction,
    ReviewVerdict,
    normalize_repair_scope,
)
from forwin.protocol.writer import WriterOutput
from forwin.canon_quality.service import analyze_writer_output_quality
from forwin.skills import serialize_prompt_layers
from .context_builder import build_review_context_pack
from .experience import ExperienceReviewer
from .governance import GovernanceReviewer
from .lint import LintSignalCollector
from .map_movement import MapMovementReviewer
from .personality import PersonalityConsistencyReviewer


class HistoricalReviewHub:
    def __init__(
        self,
        *,
        experience_review_enabled: bool = True,
        lint_review_enabled: bool = True,
        llm_client=None,
        llm_enabled: bool | None = None,
        continuity_reviewer=None,
        governance_reviewer=None,
        experience_reviewer=None,
        map_movement_reviewer=None,
        personality_reviewer=None,
        lint_collector=None,
        llm_webnovel_reviewer=None,
        observability=None,
    ) -> None:
        self.continuity_reviewer = continuity_reviewer
        self.governance_reviewer = governance_reviewer or GovernanceReviewer()
        self.experience_reviewer = experience_reviewer or ExperienceReviewer(
            enabled=experience_review_enabled,
            llm_client=llm_client,
            llm_enabled=llm_enabled,
        )
        self.map_movement_reviewer = map_movement_reviewer or MapMovementReviewer()
        self.personality_reviewer = personality_reviewer or PersonalityConsistencyReviewer()
        self.lint_collector = lint_collector or LintSignalCollector(enabled=lint_review_enabled)
        self.llm_webnovel_reviewer = llm_webnovel_reviewer
        self.observability = observability or NullObservability()

    def review(
        self,
        *,
        project_id: str,
        repo=None,
        context: ChapterContextPack,
        writer_output: WriterOutput,
        continuity_checker,
        reviewer_skill_layers: list[object] | None = None,
    ) -> ReviewVerdict:
        obs_context = OperationContext(
            project_id=project_id,
            chapter_number=int(getattr(context, "chapter_number", 0) or 0),
            stage="chapter.review",
        )
        with self.observability.span(
            obs_context,
            "review.continuity",
            span_kind="reviewer",
            component="reviewer",
        ) as span:
            continuity = continuity_checker.check(project_id, writer_output)
            span.metric("issue_count", len(getattr(continuity, "issues", []) or []))
        with self.observability.span(
            obs_context,
            "review.lint",
            span_kind="reviewer",
            component="reviewer",
        ) as span:
            lint_signals = [*self.lint_collector.collect(writer_output)]
            span.metric("issue_count", len(lint_signals))
        personality_collect = getattr(self.personality_reviewer, "collect", None)
        if callable(personality_collect):
            with self.observability.span(
                obs_context,
                "review.personality.collect",
                span_kind="reviewer",
                component="reviewer",
            ) as span:
                personality_signals = personality_collect(context, writer_output)
                lint_signals.extend(personality_signals)
                span.metric("issue_count", len(personality_signals))
        deterministic_quality_report = {}
        session = getattr(repo, "session", None) if repo is not None else None
        if session is not None:
            with self.observability.span(
                obs_context,
                "review.canon_quality.collect",
                span_kind="reviewer",
                component="canon_quality",
            ) as span:
                quality = analyze_writer_output_quality(
                    session=session,
                    project_id=project_id,
                    chapter_number=int(getattr(context, "chapter_number", 0) or 0),
                    writer_output=writer_output,
                    persist=False,
                )
                deterministic_quality_report = quality.deterministic_quality_report
                span.metric("signal_count", len(quality.signals))
        review_context = build_review_context_pack(
            repo=repo,
            context=context,
            lint_signals=lint_signals,
            deterministic_quality_report=deterministic_quality_report,
        )
        with self.observability.span(
            obs_context,
            "review.webnovel_experience",
            span_kind="reviewer",
            component="reviewer",
        ) as span:
            webnovel = self._call_with_compatible_kwargs(
                self.experience_reviewer.review,
                review_context,
                writer_output,
                reviewer_skill_layers=reviewer_skill_layers,
            )
            span.metric("issue_count", len(getattr(webnovel, "issues", []) or []))
        with self.observability.span(
            obs_context,
            "review.governance",
            span_kind="reviewer",
            component="reviewer",
        ) as span:
            governance = self._call_with_compatible_kwargs(
                self.governance_reviewer.review,
                review_context,
                writer_output,
            )
            span.metric("issue_count", len(getattr(governance, "issues", []) or []))
        with self.observability.span(
            obs_context,
            "review.map_movement",
            span_kind="reviewer",
            component="reviewer",
        ) as span:
            map_movement = self._call_with_compatible_kwargs(
                self.map_movement_reviewer.review,
                review_context,
                writer_output,
            )
            span.metric("issue_count", len(getattr(map_movement, "issues", []) or []))
        personality_review = ReviewVerdict(verdict="pass", issues=[])
        personality_review_call = getattr(self.personality_reviewer, "review", None)
        if callable(personality_review_call) and not callable(personality_collect):
            with self.observability.span(
                obs_context,
                "review.personality",
                span_kind="reviewer",
                component="reviewer",
            ) as span:
                personality_review = self._call_with_compatible_kwargs(
                    personality_review_call,
                    review_context,
                    writer_output,
                )
                span.metric("issue_count", len(getattr(personality_review, "issues", []) or []))
        issues = [
            *self._normalize_issues(continuity.issues, reviewer="continuity"),
            *self._normalize_issues(governance.issues, reviewer="governance"),
            *self._normalize_issues(webnovel.issues, reviewer="webnovel_experience"),
            *self._normalize_issues(map_movement.issues, reviewer="map_movement"),
            *self._normalize_issues(personality_review.issues, reviewer="personality"),
        ]
        verdict = self._merge_verdicts(
            continuity.verdict,
            governance.verdict,
            webnovel.verdict,
            map_movement.verdict,
            personality_review.verdict,
        )
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
                    if governance.verdict == "fail"
                    else None
                ),
                webnovel_instruction=webnovel.repair_instruction,
            )
        verdict_payload = ReviewVerdict(
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
        return self._apply_reviewer_skill_layers(
            verdict=verdict_payload,
            skill_layers=reviewer_skill_layers,
            context=context,
            writer_output=writer_output,
        )

    @staticmethod
    def _apply_reviewer_skill_layers(
        *,
        verdict: ReviewVerdict,
        skill_layers: list[object] | None,
        context: ChapterContextPack,
        writer_output: WriterOutput,
    ) -> ReviewVerdict:
        selected_skills = HistoricalReviewHub._selected_skills_from_layers(skill_layers)
        if not selected_skills:
            return verdict
        review_notes = list(verdict.review_notes)
        review_notes.append(
            "启用 reviewer skills: " + "、".join(item["id"] for item in selected_skills)
        )
        repair_instruction = verdict.repair_instruction
        if repair_instruction is not None:
            repair_instruction = repair_instruction.model_copy(
                update={
                    "design_patch": {
                        **repair_instruction.design_patch,
                        "reviewer_skill_ids": [item["id"] for item in selected_skills],
                    }
                }
            )
        prompt_layers = serialize_prompt_layers(
            [
                {
                    "role": "system",
                    "content": "ForWin reviewer rubric aggregation. Reviewer skills may explain issues and repairs but must not override the final verdict.",
                }
            ],
            skill_layers or [],
        )
        prompt_trace = {
            "trace_scope": "reviewer",
            "stage_key": "chapter_review",
            "template_id": "reviewer:chapter_review",
            "template_version": "v1",
            "effective_system_prompt": "\n\n".join(
                str(item.get("content", "")).strip()
                for item in prompt_layers
                if str(item.get("role", "")).strip() == "system"
            ),
            "prompt_layers": prompt_layers,
            "input_snapshot": {
                "project_id": getattr(context, "project_id", ""),
                "chapter_number": context.chapter_number,
                "selected_skills": selected_skills,
                "body_char_count": int(getattr(writer_output, "char_count", 0) or 0),
            },
            "model_profile": {},
            "attempts": [],
            "output_summary": {
                "verdict": verdict.verdict,
                "issue_count": len(verdict.issues),
                "skill_summary": selected_skills,
            },
        }
        return verdict.model_copy(
            update={
                "review_notes": review_notes,
                "repair_instruction": repair_instruction,
                "prompt_trace": prompt_trace,
            }
        )

    @staticmethod
    def _call_with_compatible_kwargs(callable_obj, /, *args, **kwargs):  # noqa: ANN001, ANN002, ANN003
        try:
            signature = inspect.signature(callable_obj)
        except (TypeError, ValueError):
            return callable_obj(*args, **kwargs)
        parameters = signature.parameters
        if any(param.kind == inspect.Parameter.VAR_KEYWORD for param in parameters.values()):
            return callable_obj(*args, **kwargs)
        filtered_kwargs = {
            key: value
            for key, value in kwargs.items()
            if key in parameters
        }
        return callable_obj(*args, **filtered_kwargs)

    @staticmethod
    def _selected_skills_from_layers(skill_layers: list[object] | None) -> list[dict[str, str]]:
        payload: list[dict[str, str]] = []
        for item in skill_layers or []:
            payload.append(
                {
                    "id": str(getattr(item, "skill_id", getattr(item, "name", "")) or ""),
                    "version": str(getattr(item, "skill_version", getattr(item, "version", "")) or ""),
                    "hash": str(getattr(item, "skill_hash", "") or ""),
                    "path": str(getattr(item, "path", "") or ""),
                    "activation_reason": str(getattr(item, "activation_reason", "") or ""),
                    "mode": str(getattr(item, "mode", "") or ""),
                }
            )
        return [item for item in payload if item["id"]]

    def choose_repair_escalation(
        self,
        *,
        repo=None,
        context: ChapterContextPack,
        writer_output: WriterOutput,
        review: ReviewVerdict,
        repair_attempts: list[dict[str, object]] | None = None,
    ) -> RepairInstruction:
        review_context = build_review_context_pack(
            repo=repo,
            context=context,
            lint_signals=review.lint_signals,
        )
        return self.experience_reviewer.choose_repair_escalation(
            context=review_context,
            writer_output=writer_output,
            review=review,
            repair_attempts=repair_attempts or [],
        )

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
    def _issue_repair_text(issue: ContinuityIssue) -> str:
        description = str(issue.description or issue.rule_name or "").strip()
        suggested_fix = str(issue.suggested_fix or "").strip()
        if suggested_fix:
            return f"{description} 修复要求：{suggested_fix}"
        return description

    @staticmethod
    def _continuity_repair_instruction(
        *,
        continuity_issues: list[ContinuityIssue],
        context: ChapterContextPack,
    ) -> RepairInstruction:
        return RepairInstruction(
            repair_scope="draft",
            failure_type="continuity",
            must_fix=[
                HistoricalReviewHub._issue_repair_text(issue)
                for issue in continuity_issues
                if issue.severity == "error"
            ],
            must_preserve=[
                context.chapter_plan_title,
                context.chapter_plan_one_line,
                *(context.chapter_goals[:2]),
            ],
            scope_reason="continuity rule break needs local repair first",
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
            scope_reason="governance issues should start with local repair",
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

        scope_rank = {
            "draft": 1,
            "scene": 1,
            "chapter_plan": 2,
            "chapter": 2,
            "band": 2,
            "band_plan": 3,
            "arc": 3,
            "world_model": 4,
        }
        merged_scope = max(
            [base_instruction.repair_scope, webnovel_instruction.repair_scope],
            key=lambda item: scope_rank.get(item, 1),
        )
        merged_scope = normalize_repair_scope(
            merged_scope,
            preserve_v4=(merged_scope == "world_model"),
        )
        merged_failure_type = (
            base_instruction.failure_type
            if base_instruction.failure_type == webnovel_instruction.failure_type
            else "mixed"
        )
        merged_scope = "band" if merged_scope == "arc" else merged_scope
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
            scope_reason=(
                webnovel_instruction.scope_reason
                or base_instruction.scope_reason
            ),
            design_patch=merged_design_patch,
            evidence_refs=list(dict.fromkeys([*base_instruction.evidence_refs, *webnovel_instruction.evidence_refs])),
        )
