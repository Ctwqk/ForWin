from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reviewer.hub import HistoricalReviewHub
from forwin.reviewer.personality import PersonalityConsistencyReviewer


def _context() -> ChapterContextPack:
    return ChapterContextPack.model_construct(
        project_id="proj",
        project_title="人格复核",
        chapter_number=1,
        chapter_plan_title="第一章",
        chapter_plan_one_line="测试",
        chapter_goals=[],
        active_personality_contexts=[
            {
                "character_id": "char_shen",
                "character_name": "沈临川",
                "active_skills": {
                    "dominant": ["trait-loyal-protector"],
                    "secondary": [],
                    "social_mask": [],
                    "stress_mode": [],
                    "relationship_pattern": [],
                },
                "current_behavior_bias": {
                    "decision": ["先保护承诺对象。"],
                    "stress_behavior": [],
                },
                "constraints": [],
            }
        ],
        personality_integrity_issues=[
            {
                "code": "personality_missing_loadout",
                "severity": "error",
                "message": "裸角色缺少 loadout",
                "character_id": "char_bare",
            }
        ],
    )


def _writer(body: str) -> WriterOutput:
    return WriterOutput(chapter_number=1, title="第一章", body=body, end_of_chapter_summary="测试")


def test_personality_reviewer_emits_evidence_refs_for_integrity_issues() -> None:
    signals = PersonalityConsistencyReviewer().collect(_context(), _writer("沈临川继续前进。"))

    missing = next(signal for signal in signals if signal.code == "personality_missing_loadout")
    assert missing.severity == "error"
    assert missing.evidence_refs == ["personality:char_bare"]


def test_personality_reviewer_flags_reference_model_and_untriggered_stress_behavior() -> None:
    signals = PersonalityConsistencyReviewer().collect(
        _context(),
        _writer("沈临川按 MBTI 类型直接判断众人，并在没有触发的情况下开始信息失控式控制。"),
    )

    codes = {signal.code for signal in signals}
    assert "reference_model_override" in codes
    assert "stress_mode_without_trigger" in codes
    for signal in signals:
        if signal.code in {"reference_model_override", "stress_mode_without_trigger"}:
            assert "personality:char_shen" in signal.evidence_refs


def test_historical_review_hub_merges_personality_lint_signals() -> None:
    class Continuity:
        def check(self, project_id, writer_output):  # noqa: ANN001
            return ReviewVerdict(verdict="pass", issues=[])

    verdict = HistoricalReviewHub(experience_review_enabled=False, lint_review_enabled=False).review(
        project_id="proj",
        context=_context(),
        writer_output=_writer("沈临川按九型标签直接解释行为。"),
        continuity_checker=Continuity(),
    )

    assert any(signal.tool == "personality" and signal.code == "reference_model_override" for signal in verdict.lint_signals)
