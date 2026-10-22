from __future__ import annotations

import logging

import pytest

from forwin.canon_quality.chapter_review_form.form_builder import build_form
from forwin.canon_quality.chapter_review_form.pruning import FormBudgetExceeded, fit_form_budget
from forwin.canon_quality.chapter_review_form.service import review_chapter_with_form
from forwin.protocol.writer import WriterOutput


def test_builder_includes_active_countdowns_and_mentioned_characters() -> None:
    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="林青再次提到主倒计时。",
        character_rows=[
            {
                "character_name": "林青",
                "aliases": ["阿青"],
                "life_state": "dead",
                "custody_state": "captured",
                "location_id": "site-1",
                "chapter_number": 3,
            },
            {"character_name": "未出现", "life_state": "alive", "chapter_number": 4},
        ],
        countdown_rows=[
            {
                "countdown_key": "main",
                "label": "主倒计时",
                "remaining_minutes": 50,
                "status": "consistent",
                "chapter_number": 6,
            }
        ],
        open_signal_rows=[],
        obligations=[],
        target_total_chapters=12,
        token_budget_chars=4000,
    )

    assert [
        (item.name, item.aliases, item.prior_life_state, item.prior_custody_state, item.prior_location_id)
        for item in form.characters
    ] == [("林青", ["阿青"], "dead", "captured", "site-1")]
    assert [(item.key, item.label, item.prior_value_minutes) for item in form.countdowns] == [("main", "主倒计时", 50)]


def test_builder_does_not_infer_dead_state_from_chapter_text() -> None:
    form = build_form(
        project_id="p1",
        chapter_number=7,
        chapter_text="林青和委员会高层的合谋导致家族成员死亡。",
        character_rows=[{"character_name": "林青", "life_state": "alive", "chapter_number": 3}],
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
                "remaining_minutes": 90,
                "status": "consistent",
                "chapter_number": 6,
            },
            {
                "countdown_key": "z-active",
                "label": "主倒计时" + "x" * 700,
                "remaining_minutes": 12,
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


@pytest.mark.parametrize("remaining", [-10, 0])
def test_overdue_running_clock_is_not_protected(remaining: int) -> None:
    row = {"countdown_key": "clock-overdue", "label": "Overdue", "remaining_minutes": remaining, "status": "active"}
    form = build_form(project_id="p1", chapter_number=2, chapter_text="No clocks here.", countdown_rows=[row])
    assert form.countdowns == []

    mentioned = build_form(project_id="p1", chapter_number=2, chapter_text="Overdue", countdown_rows=[row])
    assert mentioned.countdowns[0].prior_status == "closed"
    assert mentioned.countdowns[0].prior_value_minutes == remaining

    row["label"] = "Overdue" + "x" * 1800
    pruned = build_form(project_id="p1", chapter_number=2, chapter_text="clock-overdue", countdown_rows=[row], token_budget_chars=1000)
    assert pruned.countdowns == []


def test_protected_countdowns_are_capped_by_remaining() -> None:
    rows = [
        {"countdown_key": f"clock-{index}", "label": f"Clock {index}", "remaining_minutes": remaining, "status": "active"}
        for index, remaining in enumerate([80, 10, 70, 20, 60, 30, 50, 40])
    ]
    form = build_form(project_id="p1", chapter_number=2, chapter_text="", countdown_rows=rows)
    assert [item.prior_value_minutes for item in form.countdowns] == [10, 20, 30, 40, 50, 60]
    assert [item.key for item in form.countdowns] == ["clock-1", "clock-3", "clock-5", "clock-7", "clock-6", "clock-4"]


@pytest.mark.parametrize("mention", ["c7", "Clock seven"])
def test_mentioned_clock_beyond_protected_cap_is_asked_and_pruned_first(mention: str) -> None:
    rows = [
        {"countdown_key": f"c{index}", "label": "Clock seven" if index == 7 else f"Clock {index}",
         "remaining_minutes": index + 1, "status": "active"}
        for index in range(8)
    ]
    form = build_form(project_id="p1", chapter_number=2, chapter_text=mention, countdown_rows=rows)
    assert [item.key for item in form.countdowns] == ["c0", "c1", "c2", "c3", "c4", "c5", "c7"]
    assert form.countdowns[-1].prior_status == "active"
    assert form.countdowns[-1].prior_value_minutes == 8

    fit_form_budget(form, max_chars=len(form.model_dump_json()) - 1)
    assert [item.key for item in form.countdowns] == ["c0", "c1", "c2", "c3", "c4", "c5"]


def test_equal_remaining_countdowns_are_capped_in_key_order() -> None:
    rows = [
        {"countdown_key": f"c{index}", "label": f"Clock {index}", "remaining_minutes": 5, "status": "active"}
        for index in [7, 2, 5, 0, 6, 3, 1, 4]
    ]
    form = build_form(project_id="p1", chapter_number=2, chapter_text="", countdown_rows=rows)
    assert [item.key for item in form.countdowns] == ["c0", "c1", "c2", "c3", "c4", "c5"]


def test_six_protected_countdowns_over_budget_raise() -> None:
    rows = [
        {"countdown_key": f"c{index}", "label": "x" * 1800, "remaining_minutes": index + 1, "status": "active"}
        for index in range(6)
    ]
    with pytest.raises(FormBudgetExceeded) as exc_info:
        build_form(project_id="p1", chapter_number=2, chapter_text="", countdown_rows=rows, token_budget_chars=1000)
    assert exc_info.value.protected_counts["countdowns"] == 6
    assert [item.key for item in exc_info.value.form.countdowns] == ["c0", "c1", "c2", "c3", "c4", "c5"]


@pytest.mark.parametrize("status,remaining", [("active", None), ("paused", 5), ("reopened", 5)])
def test_non_running_or_unknown_remaining_clock_can_be_pruned(status: str, remaining: int | None) -> None:
    form = build_form(
        project_id="p1", chapter_number=2, chapter_text="",
        countdown_rows=[{"countdown_key": "clock", "label": "x" * 1800, "remaining_minutes": remaining, "status": status}],
        token_budget_chars=1000,
    )
    assert form.countdowns == []


def test_review_form_emits_budget_warning_and_blocks_when_required_answer_missing() -> None:
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
                "remaining_minutes": 12,
                "status": "active",
                "chapter_number": 6,
            }
        ],
        token_budget_chars=1000,
    )

    assert result.blocking is True
    assert result.form is not None
    assert [item.key for item in result.form.countdowns] == ["main"]
    assert [signal.signal_type for signal in result.signals] == [
        "form_budget_exceeded",
        "form_schema_invalid",
    ]
    assert result.signals[0].severity == "warning"
    assert result.signals[1].severity == "error"
    assert result.summary == "Missing countdowns answers: main"
    assert result.raw_analyzer_results[0]["verdict"] == "fail"


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
