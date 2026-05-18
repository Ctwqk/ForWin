from __future__ import annotations

import json
from pathlib import Path

import pytest

from forwin.canon_quality.chapter_review_form.canon_projector import project_validated_answers
from forwin.canon_quality.chapter_review_form.evidence_validator import validate_answers
from forwin.canon_quality.chapter_review_form.form_schema import ChapterReviewAnswers, ChapterReviewForm


FIXTURE_ROOT = Path(__file__).resolve().parent / "fixtures" / "chapter_review_form"

REQUIRED_CASES = [
    "subject_attribution_misdirection",
    "cross_chapter_countdown_regression",
    "already_dead_character_resurrected_with_bridge",
    "already_dead_character_mentioned_without_resurrection",
    "final_chapter_main_crisis_closed",
    "final_chapter_main_crisis_dangling",
    "obligation_silently_skipped",
    "obligation_partial_default_warning",
    "open_signal_resolved_with_evidence",
    "open_signal_persisting_high_severity",
    "pruning_drops_long_dead_minor_character",
    "pruning_protects_active_countdown_under_pressure",
    "quote_punctuation_form_difference",
    "subject_descriptive_reference",
    "budget_exceeded_emits_warning",
]

REQUIRED_FILES = [
    "form.json",
    "chapter.txt",
    "expected_answers.json",
    "expected_signals.json",
    "expected_transitions.json",
    "notes.md",
]


def test_required_fixture_cases_are_present() -> None:
    assert FIXTURE_ROOT.exists()
    actual_cases = sorted(path.name for path in FIXTURE_ROOT.iterdir() if path.is_dir())
    assert actual_cases == sorted(REQUIRED_CASES)


@pytest.mark.parametrize("case_name", REQUIRED_CASES)
def test_chapter_review_form_fixture_case(case_name: str) -> None:
    case_dir = FIXTURE_ROOT / case_name
    missing = [name for name in REQUIRED_FILES if not (case_dir / name).exists()]
    assert missing == []
    assert (case_dir / "notes.md").read_text(encoding="utf-8").strip()

    form = ChapterReviewForm.model_validate_json((case_dir / "form.json").read_text(encoding="utf-8"))
    answers = ChapterReviewAnswers.model_validate_json((case_dir / "expected_answers.json").read_text(encoding="utf-8"))
    chapter_text = (case_dir / "chapter.txt").read_text(encoding="utf-8")

    validation_report = validate_answers(form=form, answers=answers, chapter_text=chapter_text)
    projection = project_validated_answers(answers=answers, validation_report=validation_report, draft_id=case_name)

    assert _signal_summary(projection.signals) == _load_json(case_dir / "expected_signals.json")
    assert _transition_summary(projection.character_transitions) == _load_json(case_dir / "expected_transitions.json")


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def _signal_summary(signals: object) -> list[dict[str, str]]:
    return [
        {
            "signal_type": signal.signal_type,
            "severity": signal.severity,
            "subject_key": signal.subject_key,
        }
        for signal in signals
    ]


def _transition_summary(transitions: object) -> list[dict[str, str]]:
    return [
        {
            "character_name": transition.character_name,
            "transition_type": transition.transition_type,
            "to_state": transition.to_state,
            "terminality": transition.terminality,
        }
        for transition in transitions
    ]
