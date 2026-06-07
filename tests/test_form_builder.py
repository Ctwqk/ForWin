from __future__ import annotations

import logging

import pytest

from forwin.canon_quality.chapter_review_form.form_builder import build_form
from forwin.canon_quality.chapter_review_form.pruning import FormBudgetExceeded
from forwin.canon_quality.chapter_review_form.service import review_chapter_with_form
from forwin.protocol.writer import WriterOutput


def test_builder_includes_active_countdowns_and_mentioned_characters() -> None:
    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="林青再次提到主倒计时。",
        character_rows=[
            {"character_name": "林青", "to_state": "alive", "chapter_number": 3},
            {"character_name": "未出现", "to_state": "alive", "chapter_number": 4},
        ],
        countdown_rows=[
            {
                "countdown_key": "main",
                "label": "主倒计时",
                "normalized_remaining_minutes": 50,
                "status": "consistent",
                "chapter_number": 6,
            }
        ],
        open_signal_rows=[],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=4000,
    )

    assert [item.name for item in form.characters] == ["林青"]
    assert [item.key for item in form.countdowns] == ["main"]


def test_builder_does_not_infer_dead_state_from_chapter_text() -> None:
    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="林青和委员会高层的合谋导致家族成员死亡。",
        character_rows=[{"character_name": "林青", "to_state": "alive", "chapter_number": 3}],
        countdown_rows=[],
        open_signal_rows=[],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=4000,
    )

    assert form.characters[0].prior_life_state == "alive"


def test_builder_marks_final_chapter_ask() -> None:
    form = build_form(
        project_id="p1",
        chapter_number=12,
        chapter_text="终章",
        character_rows=[],
        countdown_rows=[],
        open_signal_rows=[],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=4000,
    )

    assert form.final_chapter is not None


def test_builder_treats_superseded_character_prior_as_unknown() -> None:
    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="林青回到档案室。",
        character_rows=[
            {
                "character_name": "林青",
                "to_state": "dead",
                "chapter_number": 3,
                "payload": {"must_track": True, "superseded_by": "chapter_review_form_migration"},
            }
        ],
        countdown_rows=[
            {
                "countdown_key": "main",
                "label": "主倒计时",
                "normalized_remaining_minutes": 50,
                "status": "consistent",
                "chapter_number": 6,
                "payload": {"superseded_by": "chapter_review_form_migration"},
            }
        ],
        open_signal_rows=[],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=4000,
    )

    assert len(form.characters) == 1
    assert form.characters[0].prior_life_state == "unknown"
    assert form.characters[0].prior_custody_state == "unknown"
    assert form.countdowns == []


def test_builder_includes_open_low_severity_signals_when_budget_allows() -> None:
    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="正文",
        character_rows=[],
        countdown_rows=[],
        open_signal_rows=[
            {
                "signal_id": "error-signal",
                "signal_type": "hard continuity break",
                "description": "高风险连续性问题",
                "severity": "error",
                "status": "open",
                "chapter_number": 3,
            },
            {
                "signal_id": "warning-signal",
                "signal_type": "soft continuity drift",
                "description": "低风险连续性漂移",
                "severity": "warning",
                "status": "open",
                "chapter_number": 3,
            },
        ],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=4000,
    )

    assert [item.id for item in form.open_signals] == ["error-signal", "warning-signal"]


def test_builder_prunes_low_severity_signal_before_error_signal(caplog: pytest.LogCaptureFixture) -> None:
    logging.disable(logging.NOTSET)
    logger = logging.getLogger("forwin.canon_quality.chapter_review_form.pruning")
    logger.disabled = False
    logger.propagate = True
    caplog.set_level(logging.INFO, logger="forwin.canon_quality.chapter_review_form.pruning")

    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="正文",
        character_rows=[],
        countdown_rows=[],
        open_signal_rows=[
            {
                "signal_id": "error-signal",
                "signal_type": "hard continuity break",
                "description": "高风险连续性问题",
                "severity": "error",
                "status": "open",
                "chapter_number": 3,
            },
            {
                "signal_id": "warning-signal",
                "signal_type": "soft continuity drift",
                "description": "低风险连续性漂移" + "x" * 1200,
                "severity": "warning",
                "status": "open",
                "chapter_number": 3,
            },
        ],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=1200,
    )

    assert [item.id for item in form.open_signals] == ["error-signal"]
    assert "open_signals=1" in caplog.text


def test_builder_does_not_silently_drop_must_resolve_obligation_when_over_budget() -> None:
    with pytest.raises(FormBudgetExceeded) as exc_info:
        build_form(
            project_id="p1",
            chapter_number=7,
            chapter_text="正文",
            character_rows=[],
            countdown_rows=[],
            open_signal_rows=[],
            obligations=[
                {
                    "id": "obligation-1",
                    "summary": "必须本章兑现" + "x" * 1800,
                    "deadline_chapter": 7,
                    "must_resolve_now": True,
                    "payoff_test": "需要明确兑现",
                }
            ],
            target_total_chapters=12,
            token_budget_chars=1000,
        )

    assert exc_info.value.protected_counts["obligations"] == 1
    assert exc_info.value.form is not None
    assert [item.id for item in exc_info.value.form.obligations] == ["obligation-1"]


def test_builder_keeps_active_countdown_before_consistent_under_pressure() -> None:
    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="主倒计时仍在推进。",
        character_rows=[],
        countdown_rows=[
            {
                "countdown_key": "a-consistent",
                "label": "低优先级倒计时" + "x" * 700,
                "normalized_remaining_minutes": 90,
                "status": "consistent",
                "chapter_number": 6,
            },
            {
                "countdown_key": "z-active",
                "label": "主倒计时" + "x" * 700,
                "normalized_remaining_minutes": 12,
                "status": "active",
                "chapter_number": 6,
            },
        ],
        open_signal_rows=[],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=1500,
    )

    assert [item.key for item in form.countdowns] == ["z-active"]


def test_review_form_emits_budget_warning_and_proceeds_when_protected_items_exceed_budget() -> None:
    writer_output = WriterOutput(
        project_id="p1",
        chapter_number=7,
        title="第7章",
        body="主倒计时仍在推进。",
        char_count=9,
        end_of_chapter_summary="倒计时继续。",
    )
    client = _EmptyAnswersClient()

    result = review_chapter_with_form(
        session=None,
        project_id="p1",
        chapter_number=7,
        writer_output=writer_output,
        llm_client=client,
        countdown_rows=[
            {
                "countdown_key": "main",
                "label": "主倒计时" + "x" * 1800,
                "normalized_remaining_minutes": 12,
                "status": "active",
                "chapter_number": 6,
            }
        ],
        token_budget_chars=1000,
    )

    assert result.blocking is False
    assert result.form is not None
    assert [item.key for item in result.form.countdowns] == ["main"]
    assert [signal.signal_type for signal in result.signals] == ["form_budget_exceeded"]
    assert result.signals[0].severity == "warning"
    assert result.raw_analyzer_results[0]["verdict"] == "warn"


class _EmptyAnswersClient:
    def complete_json(self, **_: object) -> dict[str, object]:
        return {
            "characters": [],
            "countdowns": [],
            "obligations": [],
            "open_signals": [],
            "new_observations": {},
            "chapter_summary": "ok",
        }
