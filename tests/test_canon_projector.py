from __future__ import annotations

import pytest

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.config import FormBlockingPolicy
from forwin.canon_quality.chapter_review_form.canon_projector import project_validated_answers
from forwin.canon_quality.chapter_review_form.evidence_validator import RejectedAnswer, ValidationReport
from forwin.canon_quality.chapter_review_form.form_builder import _clock_row, build_form
from forwin.canon_quality.chapter_review_form.form_schema import (
    CharacterReviewAnswer,
    ChapterReviewAnswers,
    ChapterReviewForm,
    CountdownReviewAnswer,
    FormAnswer,
    NewObservations,
    ObligationReviewAnswer,
    OpenSignalReviewAnswer,
)
from forwin.canon_quality.chapter_review_form.service import review_chapter_with_form
from forwin.protocol.writer import WriterOutput
from forwin.protocol.book_state import WorldNode


def test_projector_validated_character_answer_emits_nothing() -> None:
    answer = CharacterReviewAnswer(
        name="林青",
        appears_in_chapter=True,
        life_state=FormAnswer(
            value="dead",
            evidence_quote="林青倒下，再无呼吸。",
            subject_of_quote="林青",
            confidence=0.95,
        ),
        custody_state=FormAnswer(value="unknown"),
        participation=FormAnswer(value="present_acting"),
    )
    answers = _answers(characters=[answer])

    projection = project_validated_answers(
        answers=answers,
        validation_report=ValidationReport(validated=["characters[0].life_state"], rejected=[]),
        draft_id="d1",
        min_blocking_confidence=0.8,
    )

    assert projection.signals == []
    assert projection.review_issues == []
    assert set(projection.model_dump()) == {"signals", "review_issues"}


def test_projector_rejected_answer_emits_rejection_signal() -> None:
    answer = CharacterReviewAnswer(
        name="林青",
        appears_in_chapter=True,
        life_state=FormAnswer(
            value="dead",
            evidence_quote="家族成员死亡。",
            subject_of_quote="家族成员",
            confidence=0.95,
        ),
        custody_state=FormAnswer(value="unknown"),
        participation=FormAnswer(value="mentioned_only"),
    )

    projection = project_validated_answers(
        answers=_answers(characters=[answer]),
        validation_report=ValidationReport(
            rejected=[RejectedAnswer(path="characters[0].life_state", reason="subject_mismatch", blocking=True)]
        ),
        draft_id="d1",
        min_blocking_confidence=0.8,
    )

    assert projection.signals[0].signal_type == "form_answer_rejected"
    assert projection.signals[0].severity == "error"


def test_projector_blocks_closed_countdown_regression_without_bridge() -> None:
    countdown = CountdownReviewAnswer(
        key="main",
        mentioned_in_chapter=True,
        status_in_this_chapter=FormAnswer(
            value="reopened",
            evidence_quote="倒计时剩余五十分钟。",
            subject_of_quote="主倒计时",
            confidence=0.92,
        ),
        new_value_minutes=50,
        new_value_evidence=FormAnswer(
            value="50",
            evidence_quote="倒计时剩余五十分钟。",
            subject_of_quote="主倒计时",
            confidence=0.92,
        ),
        consistent_with_prior=FormAnswer(
            value="false",
            evidence_quote="倒计时剩余五十分钟。",
            subject_of_quote="主倒计时",
            confidence=0.92,
        ),
        inconsistency_kind="reopened_after_close",
    )

    projection = project_validated_answers(
        answers=_answers(countdowns=[countdown]),
        validation_report=ValidationReport(
            validated=[
                "countdowns[0].status_in_this_chapter",
                "countdowns[0].new_value_evidence",
                "countdowns[0].consistent_with_prior",
            ],
            rejected=[],
            blocking_paths=["countdowns[0].consistent_with_prior"],
        ),
        draft_id="d1",
        min_blocking_confidence=0.8,
    )

    assert [signal.signal_type for signal in projection.signals] == ["form_countdown_inconsistency"]
    assert projection.signals[0].severity == "error"
    payload = projection.signals[0].payload
    assert projection.signals[0].description == "A countdown is inconsistent with prior canon."
    assert (payload["plan_patchable"], payload["patch_kind"], payload["suppression_key"]) == (False, "", "")
    assert not {"generic_patch_kind", "generic_suppression_key", "invariant_key", "countdown_label"} & set(payload)


def test_projector_uses_form_blocking_policy_for_warning_category() -> None:
    obligation = ObligationReviewAnswer(
        id="义务-1",
        addressed=FormAnswer(
            value="partial",
            evidence_quote="角色A只完成了第一步。",
            subject_of_quote="义务-1",
            confidence=0.93,
        ),
    )
    answers = ChapterReviewAnswers(
        project_id="p1",
        chapter_number=2,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[],
        obligations=[obligation],
        open_signals=[],
        new_observations=NewObservations(),
    )

    projection = project_validated_answers(
        answers=answers,
        validation_report=ValidationReport(
            validated=["obligations[0].addressed"],
            blocking_paths=["obligations[0].addressed"],
        ),
        blocking_policy=FormBlockingPolicy(obligation_partial="warning"),
    )

    assert projection.signals[0].signal_type == "form_obligation_unresolved"
    assert projection.signals[0].severity == "warning"
    assert projection.review_issues[0]["severity"] == "warning"


def _answers(
    *,
    characters: list[CharacterReviewAnswer] | None = None,
    countdowns: list[CountdownReviewAnswer] | None = None,
) -> ChapterReviewAnswers:
    return ChapterReviewAnswers(
        project_id="p1",
        chapter_number=2,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=characters or [],
        countdowns=countdowns or [],
        obligations=[],
        open_signals=[],
        new_observations=NewObservations(),
    )


def _clock_projection(scope: str):
    form = build_form(
        project_id="p1", chapter_number=2, chapter_text="",
        countdown_rows=[{"countdown_key": "clock-node-123", "label": "Departure deadline", "scope": scope, "remaining_minutes": 10, "status": "active"}],
    )
    answers = _answers(countdowns=[CountdownReviewAnswer(
        key="clock-node-123", mentioned_in_chapter=True,
        status_in_this_chapter=FormAnswer(value="active"),
        consistent_with_prior=FormAnswer(value="false", confidence=0.95),
    )])
    return project_validated_answers(
        answers=answers, form=form,
        validation_report=ValidationReport(blocking_paths=["countdowns[0].consistent_with_prior"]),
    )


def test_public_clock_signal_uses_label_not_node_id() -> None:
    projection = _clock_projection("public")
    signal = projection.signals[0]
    assert signal.subject_key == "clock-node-123"
    assert "Departure deadline" in signal.description
    assert "clock-node-123" not in signal.description
    assert projection.review_issues[0]["description"] == signal.description


def test_secret_clock_signal_reveals_neither_label_nor_id() -> None:
    projection = _clock_projection("secret")
    signal = projection.signals[0]
    assert signal.subject_key == "clock-node-123"
    assert signal.description
    assert "Departure deadline" not in signal.description
    assert "clock-node-123" not in signal.description
    assert projection.review_issues[0]["description"] == signal.description


@pytest.mark.parametrize("source", ["profile", "row", "legacy_form"])
def test_clock_without_scope_projects_generic_description(source: str) -> None:
    row = {"countdown_key": "clock-node-123", "label": "Departure deadline", "remaining_minutes": 10, "status": "active"}
    if source == "profile":
        row = _clock_row(
            WorldNode(id="clock-node-123", project_id="p1", node_type="clock", profile={"label": "Departure deadline"}),
            {"due_at": 110, "status": "running"}, story_minute=100, as_of_chapter=1,
        )
    form = build_form(project_id="p1", chapter_number=2, chapter_text="", countdown_rows=[row])
    if source == "legacy_form":
        raw_form = form.model_dump()
        raw_form["countdowns"][0].pop("scope")
        form = ChapterReviewForm.model_validate(raw_form)
    answers = _answers(countdowns=[CountdownReviewAnswer(
        key="clock-node-123", mentioned_in_chapter=True,
        status_in_this_chapter=FormAnswer(value="active"),
        consistent_with_prior=FormAnswer(value="false", confidence=0.95),
    )])
    projection = project_validated_answers(
        answers=answers, form=form,
        validation_report=ValidationReport(blocking_paths=["countdowns[0].consistent_with_prior"]),
    )
    assert form.countdowns[0].scope == "secret"
    assert projection.signals[0].description == "A countdown is inconsistent with prior canon."


@pytest.mark.parametrize("form_kind", ["matching", "unmatched", "missing"])
def test_persisting_signal_description_uses_matching_ask_or_generic_fallback(form_kind: str) -> None:
    signal_id = "p1:2:form_countdown_inconsistency:clock-node-123:1"
    form = build_form(
        project_id="p1", chapter_number=4, chapter_text="",
        open_signal_rows=[
            {"signal_id": "other", "description": "Another signal.", "chapter_number": 2},
            {"signal_id": signal_id, "description": "A countdown is inconsistent with prior canon.", "chapter_number": 2},
        ],
    )
    if form_kind == "unmatched":
        form.open_signals = [item for item in form.open_signals if item.id != signal_id]
    answers = ChapterReviewAnswers(
        project_id="p1", chapter_number=4, form_schema_version=FORM_SCHEMA_VERSION,
        open_signals=[OpenSignalReviewAnswer(id=signal_id, status=FormAnswer(value="persisting", confidence=0.95))],
    )
    projection = project_validated_answers(
        answers=answers, form=None if form_kind == "missing" else form,
        validation_report=ValidationReport(blocking_paths=["open_signals[0].status"]),
    )
    signal = projection.signals[0]
    expected = "A countdown is inconsistent with prior canon." if form_kind == "matching" else "An open signal is still unresolved."
    assert signal.description == expected
    assert projection.review_issues[0]["description"] == expected
    assert signal.subject_key == signal_id
    assert signal.payload["suppression_key"] == f"signal:{signal_id}"


@pytest.mark.parametrize("scope", ["public", "secret"])
def test_form_service_clock_signal_description_respects_scope(scope: str) -> None:
    quote = "The deadline moved backwards."
    answer = CountdownReviewAnswer(
        key="clock-node-123", mentioned_in_chapter=True,
        status_in_this_chapter=FormAnswer(value="active"),
        consistent_with_prior=FormAnswer(value="false", evidence_quote=quote, confidence=0.95),
    )

    class Client:
        def complete_json(self, **kwargs):
            return _answers(countdowns=[answer]).model_dump(mode="json")

    result = review_chapter_with_form(
        session=None, project_id="p1", chapter_number=2,
        writer_output=WriterOutput(project_id="p1", chapter_number=2, title="t", body=quote, end_of_chapter_summary=""),
        llm_client=Client(),
        countdown_rows=[{"countdown_key": answer.key, "label": "Departure deadline", "scope": scope, "remaining_minutes": 10, "status": "active"}],
    )

    assert [signal.signal_type for signal in result.signals] == ["form_countdown_inconsistency"]
    signal = result.signals[0]
    assert result.blocking is True
    assert signal.subject_key == answer.key
    assert answer.key not in signal.description
    assert ("Departure deadline" in signal.description) is (scope == "public")
    assert result.review_issues[0]["description"] == signal.description
