from __future__ import annotations

from forwin.canon_quality.chapter_review_form import FORM_SCHEMA_VERSION
from forwin.canon_quality.chapter_review_form.canon_projector import project_validated_answers
from forwin.canon_quality.chapter_review_form.evidence_validator import ValidationReport
from forwin.canon_quality.chapter_review_form.form_schema import (
    ChapterReviewAnswers,
    CountdownReviewAnswer,
    FinalChapterAnswer,
    FormAnswer,
    NewObservations,
    ObligationReviewAnswer,
    OpenSignalReviewAnswer,
)
from forwin.models.project import ChapterPlan
from forwin.planning.future_plan_audit.auditor import FuturePlanAuditor


def test_form_signals_are_plan_patchable() -> None:
    answers = ChapterReviewAnswers(
        project_id="p1",
        chapter_number=12,
        form_schema_version=FORM_SCHEMA_VERSION,
        characters=[],
        countdowns=[
            CountdownReviewAnswer(
                key="倒计时甲",
                mentioned_in_chapter=True,
                status_in_this_chapter=_answer("advanced"),
                new_value_minutes=20,
                new_value_evidence=_answer("20"),
                consistent_with_prior=_answer("false"),
                inconsistency_kind="regression",
            )
        ],
        obligations=[
            ObligationReviewAnswer(
                id="义务-1",
                addressed=_answer("unaddressed"),
                payoff_evidence=None,
            )
        ],
        open_signals=[
            OpenSignalReviewAnswer(
                id="信号-1",
                status=_answer("persisting"),
                resolution_evidence=None,
            )
        ],
        final_chapter=FinalChapterAnswer(
            main_crisis_status=_answer("left_dangling"),
            closure_evidence=None,
            unresolved_promises=["事件-1"],
        ),
        new_observations=NewObservations(),
    )

    projection = project_validated_answers(
        answers=answers,
        validation_report=ValidationReport(
            validated=[
                "countdowns[0].status_in_this_chapter",
                "countdowns[0].new_value_evidence",
                "countdowns[0].consistent_with_prior",
                "obligations[0].addressed",
                "open_signals[0].status",
                "final_chapter.main_crisis_status",
            ],
            blocking_paths=[
                "countdowns[0].consistent_with_prior",
                "obligations[0].addressed",
                "open_signals[0].status",
                "final_chapter.main_crisis_status",
            ],
        ),
        draft_id="d1",
    )

    payload_by_type = {signal.signal_type: signal.payload for signal in projection.signals}

    assert payload_by_type["form_countdown_inconsistency"]["plan_patchable"] is True
    assert payload_by_type["form_countdown_inconsistency"]["patch_kind"] == "countdown_drift"
    assert payload_by_type["form_countdown_inconsistency"]["suppression_key"] == "countdown:倒计时甲"
    assert payload_by_type["form_obligation_unresolved"]["plan_patchable"] is True
    assert payload_by_type["form_obligation_unresolved"]["patch_kind"] == "obligation_unresolved"
    assert payload_by_type["form_obligation_unresolved"]["suppression_key"] == "obligation:义务-1"
    assert payload_by_type["form_open_signal_persisting"]["plan_patchable"] is True
    assert payload_by_type["form_open_signal_persisting"]["patch_kind"] == "signal_persisting"
    assert payload_by_type["form_open_signal_persisting"]["suppression_key"] == "signal:信号-1"
    assert payload_by_type["form_final_chapter_unresolved"]["plan_patchable"] is True
    assert payload_by_type["form_final_chapter_unresolved"]["patch_kind"] == "final_dangling"
    assert payload_by_type["form_final_chapter_unresolved"]["suppression_key"] == "final:main_crisis"


def test_countdown_drift_signal_creates_next_chapter_plan_patch() -> None:
    from forwin.planning.countdown_drift_pre_audit import select_countdown_drift_targets

    targets = select_countdown_drift_targets(
        [
            {
                "signal_id": "sig-countdown",
                "signal_type": "form_countdown_inconsistency",
                "severity": "error",
                "chapter_number": 12,
                "subject_key": "倒计时甲",
                "description": "倒计时甲发生跨章回退。",
                "payload": {
                    "plan_patchable": True,
                    "patch_kind": "countdown_drift",
                    "suppression_key": "countdown:倒计时甲",
                },
            }
        ]
    )

    assert targets == [
        {
            "patch_kind": "countdown_drift",
            "suppression_key": "countdown:倒计时甲",
            "task": (
                "本章必须明确处理 倒计时甲 的当前状态。"
                "如继续，必须不大于既有值；如已 closed，不得再次出现正数剩余时间；"
                "如确实重新开启，必须显式写出 reopen 事件并命名为新的局部窗口。"
            ),
            "source_signal_id": "sig-countdown",
            "source_mode": "chapter_review_form",
        }
    ]


def test_future_plan_auditor_promotes_countdown_drift_signal_to_plan_patch() -> None:
    plan = _plan(13, one_line="角色A继续处理事件-1。")

    result = FuturePlanAuditor().audit_plans(
        project_id="p1",
        current_chapter=12,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={"open_signals": [_form_signal("form_countdown_inconsistency", "倒计时甲")]},
        obligations=[],
        target_total_chapters=20,
        include_current=False,
    )

    assert [issue.issue_type for issue in result.issues] == ["countdown_drift_pre_write_required"]
    assert result.plan_patches[0].patch_type == "countdown_drift_pre_write"
    assert result.metadata["suppressed_prompt_constraint_keys"] == ["countdown:倒计时甲"]
    assert result.metadata["form_plan_patch_signals_consumed"] == 1
    assert result.issues[0].metadata["source_signal_id"] == "sig-form_countdown_inconsistency"


def test_form_obligation_signal_behaves_like_urgent_obligation_patch() -> None:
    plan = _plan(13, one_line="角色A继续处理事件-1。")

    result = FuturePlanAuditor().audit_plans(
        project_id="p1",
        current_chapter=12,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={"open_signals": [_form_signal("form_obligation_unresolved", "义务-1")]},
        obligations=[],
        target_total_chapters=20,
        include_current=False,
    )

    assert [issue.issue_type for issue in result.issues] == ["obligation_pre_write_required"]
    assert result.plan_patches[0].patch_type == "obligation_pre_write"
    assert result.metadata["suppressed_prompt_constraint_keys"] == ["obligation:义务-1"]
    assert result.issues[0].metadata["source_signal_id"] == "sig-form_obligation_unresolved"
    assert result.issues[0].metadata["source_mode"] == "chapter_review_form"


def test_form_open_signal_persisting_behaves_like_stale_signal_without_age_delay() -> None:
    plan = _plan(13, one_line="角色A继续处理事件-1。")

    result = FuturePlanAuditor().audit_plans(
        project_id="p1",
        current_chapter=12,
        trigger_stage="pre_write",
        plans=[plan],
        canon_quality_context={"open_signals": [_form_signal("form_open_signal_persisting", "信号-1")]},
        obligations=[],
        target_total_chapters=20,
        include_current=False,
    )

    assert [issue.issue_type for issue in result.issues] == ["stale_open_signal_pre_write_required"]
    assert result.plan_patches[0].patch_type == "signal_pre_write"
    assert result.metadata["suppressed_prompt_constraint_keys"] == ["signal:信号-1"]
    assert result.issues[0].metadata["source_signal_id"] == "sig-form_open_signal_persisting"
    assert result.issues[0].metadata["source_mode"] == "chapter_review_form"


def _answer(value: str) -> FormAnswer:
    return FormAnswer(
        value=value,
        evidence_quote="事件-1仍未解决。",
        subject_of_quote="事件-1",
        confidence=0.93,
    )


def _plan(chapter_number: int, *, one_line: str) -> ChapterPlan:
    return ChapterPlan(
        id=f"plan-{chapter_number}",
        project_id="p1",
        arc_plan_id="arc-1",
        chapter_number=chapter_number,
        title=f"第{chapter_number}章",
        one_line=one_line,
        goals_json="[]",
        task_contract_json="[]",
        experience_plan_json="{}",
        status="planned",
    )


def _form_signal(signal_type: str, subject_key: str) -> dict[str, object]:
    patch_kind_by_type = {
        "form_countdown_inconsistency": "countdown_drift",
        "form_obligation_unresolved": "obligation_unresolved",
        "form_open_signal_persisting": "signal_persisting",
    }
    prefix_by_type = {
        "form_countdown_inconsistency": "countdown",
        "form_obligation_unresolved": "obligation",
        "form_open_signal_persisting": "signal",
    }
    return {
        "signal_id": f"sig-{signal_type}",
        "signal_type": signal_type,
        "severity": "error",
        "chapter_number": 12,
        "subject_key": subject_key,
        "description": f"{subject_key} 需要在下一章处理。",
        "payload": {
            "plan_patchable": True,
            "patch_kind": patch_kind_by_type[signal_type],
            "suppression_key": f"{prefix_by_type[signal_type]}:{subject_key}",
            "source_mode": "chapter_review_form",
        },
    }
