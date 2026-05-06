from __future__ import annotations

import json

from forwin.protocol.review import ContinuityIssue, RepairInstruction, ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reviser.verification import RepairVerifier


class ContradictingLLMClient:
    def chat(self, messages, **_kwargs) -> str:
        return json.dumps(
            {
                "fixed_all_must_fix": False,
                "preserved_all_must_preserve": False,
                "unfixed": ["母亲的 canon 姓名是「林若」，本章写成了「林若林若」。"],
                "broken_preserve_constraints": ["第六日·协议真相"],
                "new_risks": ["LLM verifier disagreed without deterministic evidence."],
            },
            ensure_ascii=False,
        )


def test_repair_verifier_does_not_turn_rule_clean_repair_into_hard_failure() -> None:
    instruction = RepairInstruction(
        repair_scope="draft",
        failure_type="continuity",
        must_fix=["母亲的 canon 姓名是「林若」，本章写成了「林若林若」。"],
        must_preserve=["第六日·协议真相"],
    )
    before_review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="canon_name_drift",
                severity="error",
                description="母亲的 canon 姓名是「林若」，本章写成了「林若林若」。",
                issue_type="continuity",
                target_scope="chapter",
            )
        ],
    )
    after_review = ReviewVerdict(verdict="warn", issues=[])

    result = RepairVerifier(
        llm_client=ContradictingLLMClient(),
        llm_enabled=True,
    ).verify(
        original_output=WriterOutput(
            chapter_number=7,
            title="第六日·协议真相",
            body="旧稿误写母亲林若林若。",
            end_of_chapter_summary="第六日·协议真相",
        ),
        repaired_output=WriterOutput(
            chapter_number=7,
            title="第六日·协议真相",
            body="新稿只保留母亲林若。",
            end_of_chapter_summary="第六日·协议真相",
        ),
        before_review=before_review,
        after_review=after_review,
        repair_instruction=instruction,
    )

    assert result.fixed_all_must_fix is True
    assert result.preserved_all_must_preserve is True
    assert result.unfixed == []
    assert result.broken_preserve_constraints == []
    assert result.verifier_mode == "rule_preferred_llm_disagreed"
