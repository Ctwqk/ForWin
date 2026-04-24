from __future__ import annotations

from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.review import RepairInstruction
from forwin.protocol.world_v4 import ApprovedWorldChangeSet, ExtractedWorldChangeSet
from forwin.reviewer_v4.cognitive import CognitiveConsistencyReviewer
from forwin.reviewer_v4.reader_cognition import ReaderCognitionReviewer
from forwin.reviewer_v4.reveal import RevealReviewer
from forwin.reviewer_v4.types import V4ReviewGateVerdict, V4ReviewIssue
from forwin.reviewer_v4.world_delta import WorldDeltaReviewer


class V4ReviewGate:
    """Aggregate deterministic v4 reviewers before compiler commit."""

    def __init__(self) -> None:
        self.cognitive = CognitiveConsistencyReviewer()
        self.world_delta = WorldDeltaReviewer()
        self.reveal = RevealReviewer()
        self.reader_cognition = ReaderCognitionReviewer()

    def review(
        self,
        extracted: ExtractedWorldChangeSet,
        *,
        chapter_intent: ChapterWorldDeltaIntent | None = None,
        chapter_body: str = "",
        promise_debt_count: int = 0,
    ) -> V4ReviewGateVerdict:
        issues: list[V4ReviewIssue] = []
        issues.extend(self.world_delta.review(extracted, chapter_intent=chapter_intent))
        issues.extend(
            self.cognitive.review(
                extracted,
                chapter_intent=chapter_intent,
                chapter_body=chapter_body,
            )
        )
        issues.extend(
            self.reveal.review(
                extracted,
                chapter_intent=chapter_intent,
                chapter_body=chapter_body,
            )
        )
        issues.extend(
            self.reader_cognition.review(
                extracted,
                chapter_intent=chapter_intent,
                promise_debt_count=promise_debt_count,
            )
        )
        passed = not any(issue.severity == "fail" for issue in issues)
        approved_changes = (
            ApprovedWorldChangeSet.from_extracted(
                extracted,
                approved_by=[
                    self.world_delta.name,
                    self.cognitive.name,
                    self.reveal.name,
                    self.reader_cognition.name,
                ],
            )
            if passed
            else None
        )
        repair_instruction = (
            None if passed else self._build_repair_instruction(issues, chapter_intent)
        )
        return V4ReviewGateVerdict(
            passed=passed,
            issues=issues,
            approved_changes=approved_changes,
            repair_instruction=repair_instruction,
        )

    @staticmethod
    def _build_repair_instruction(
        issues: list[V4ReviewIssue],
        chapter_intent: ChapterWorldDeltaIntent | None,
    ) -> RepairInstruction:
        fail_issues = [issue for issue in issues if issue.severity == "fail"] or issues
        failure_type = V4ReviewGate._repair_failure_type(
            fail_issues[0].failure_type if fail_issues else "world_model_conflict"
        )
        required_delta_patch: dict[str, object] = {}
        required_belief_patch: dict[str, object] = {}
        required_hint_patch: dict[str, object] = {}
        required_payoff_patch: dict[str, object] = {}

        for issue in fail_issues:
            patch = dict(issue.repair_patch)
            if issue.failure_type in {"missing_delta_source", "missing_world_line"}:
                required_delta_patch.update(patch)
            elif issue.failure_type in {
                "character_omniscience",
                "unsupported_false_belief",
            }:
                required_belief_patch.update(patch)
            elif issue.failure_type in {
                "early_reveal",
                "reveal_before_planned_chapter",
            }:
                required_hint_patch.update(patch)
            elif issue.failure_type in {
                "missing_chapter_increment",
                "unpaid_promise_debt",
            }:
                required_payoff_patch.update(patch)
            else:
                required_delta_patch.update(patch)

        if chapter_intent is not None and chapter_intent.hint_delta_intents:
            required_hint_patch.setdefault(
                "required_hints",
                list(chapter_intent.hint_delta_intents),
            )
        if chapter_intent is not None and chapter_intent.expected_observer_state_changes:
            required_belief_patch.setdefault(
                "expected_observer_state_changes",
                dict(chapter_intent.expected_observer_state_changes),
            )

        return RepairInstruction(
            repair_scope="world_model",
            failure_type=failure_type,
            must_fix=[issue.message for issue in fail_issues],
            must_preserve=list(chapter_intent.hint_delta_intents)
            if chapter_intent is not None
            else [],
            must_not_reveal=list(chapter_intent.must_not_reveal)
            if chapter_intent is not None
            else [],
            required_delta_patch=required_delta_patch,
            required_belief_patch=required_belief_patch,
            required_hint_patch=required_hint_patch,
            required_payoff_patch=required_payoff_patch,
            design_patch={
                "chapter_number": chapter_intent.chapter_number
                if chapter_intent is not None
                else 0,
                "must_not_reveal": list(chapter_intent.must_not_reveal)
                if chapter_intent is not None
                else [],
            },
            evidence_refs=[
                ref
                for issue in fail_issues
                for ref in issue.evidence_refs
            ],
        )

    @staticmethod
    def _repair_failure_type(failure_type: str) -> str:
        if failure_type in {"early_reveal", "reveal_before_planned_chapter"}:
            return "early_reveal"
        if failure_type in {"missing_delta_source", "missing_world_line"}:
            return failure_type
        if failure_type in {"character_omniscience", "unsupported_false_belief"}:
            return "cognition_conflict"
        if failure_type in {"missing_chapter_increment"}:
            return "stall"
        if failure_type in {"unpaid_promise_debt"}:
            return "unpaid_promise_debt"
        return "world_model_conflict"
