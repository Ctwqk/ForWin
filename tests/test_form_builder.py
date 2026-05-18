from __future__ import annotations

from forwin.canon_quality.chapter_review_form.form_builder import build_form


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
