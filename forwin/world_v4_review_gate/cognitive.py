from __future__ import annotations

from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import ExtractedWorldChangeSet
from forwin.world_v4_review_gate.types import V4ReviewIssue


class CognitiveConsistencyReviewer:
    name = "CognitiveConsistencyReviewer"

    def review(
        self,
        extracted: ExtractedWorldChangeSet,
        *,
        chapter_intent: ChapterWorldDeltaIntent | None,
        chapter_body: str,
    ) -> list[V4ReviewIssue]:
        issues: list[V4ReviewIssue] = []
        for belief in extracted.belief_updates:
            truth_relation = str(getattr(belief.truth_relation, "value", belief.truth_relation))
            if truth_relation == "false" and not belief.evidence_sources:
                issues.append(
                    V4ReviewIssue(
                        reviewer=self.name,
                        severity="warn",
                        failure_type="unsupported_false_belief",
                        message="false belief 缺少 evidence source，误导基础不足。",
                        evidence_refs=[f"belief:{belief.belief_id}:evidence_sources"],
                        repair_patch={
                            "required_belief_patch": "为 false belief 补充通讯延迟、旧记录或误导话术等证据来源。",
                        },
                    )
                )
        if chapter_intent is None:
            return issues
        protagonist_transition = chapter_intent.expected_observer_state_changes.get(
            "protagonist",
            "",
        )
        protagonist_not_known = any(
            token in protagonist_transition
            for token in ("unknown", "suspected", "partial")
        )
        hidden_guarded = "father_sieged" in chapter_intent.must_not_reveal
        body = chapter_body or ""
        summaries = "；".join(delta.summary for delta in extracted.world_deltas)
        acts_on_rescue_truth = any(token in body + summaries for token in ("返航救父", "救父"))
        hidden_truth_named = any(token in body + summaries for token in ("父亲被围", "母星被围"))
        if hidden_guarded and protagonist_not_known and acts_on_rescue_truth and hidden_truth_named:
            issues.append(
                V4ReviewIssue(
                    reviewer=self.name,
                    severity="fail",
                    failure_type="character_omniscience",
                    message="主角尚未知道父亲被围，却直接基于该真相行动。",
                    evidence_refs=["chapter_body:救父", "chapter_intent:must_not_reveal"],
                    repair_patch={
                        "must_fix": "把行动动机改为通讯异常引发的怀疑，而不是已知父亲被围。",
                        "required_belief_patch": "protagonist: unknown -> suspected",
                    },
                )
            )
        return issues
