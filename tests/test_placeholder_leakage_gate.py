from __future__ import annotations

from forwin.canon_quality.placeholder import analyze_placeholder_leakage
from forwin.checker.rules import ContinuityChecker
from forwin.orchestrator.loop import WritingOrchestrator
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.protocol.writer import WriterOutput


def test_placeholder_in_signature_blocks_canon() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="档案最后一栏写着：签名人：相关人员。",
        summary="",
    )

    assert [signal.signal_type for signal in signals] == ["placeholder_leakage"]
    assert signals[0].severity == "error"
    assert signals[0].subject_key == "placeholder:相关人员"


def test_placeholder_in_body_dialogue_blocks_canon() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="相关人员说：“你不能进去。”",
        summary="",
    )

    assert signals
    assert signals[0].severity == "error"


def test_internal_countdown_state_key_leakage_blocks_canon() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=26,
        draft_id="d26",
        body="陆明低头看见手腕上写着：memory_reset剩余78分钟，archive_cleanup剩余10分钟。",
        summary="",
    )

    assert [signal.signal_type for signal in signals] == ["internal_state_key_leakage"]
    assert signals[0].severity == "error"
    assert signals[0].subject_key == "internal_state_key:memory_reset"


def test_placeholder_only_in_summary_is_warning() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="陆明收起档案，离开旧楼。",
        summary="相关人员待确认。",
    )

    assert len(signals) == 1
    assert signals[0].severity == "warning"


def test_generic_role_replacing_expected_protagonist_blocks_canon() -> None:
    body = "工作人员推开档案柜。" * 6 + "工作人员看见倒计时开始。"
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body=body,
        summary="工作人员发现家族档案被抹除。",
        expected_character_names={"陆明"},
    )

    assert [signal.signal_type for signal in signals] == ["protagonist_placeholder_leakage"]
    assert signals[0].severity == "error"
    assert signals[0].payload["expected_character_names"] == ["陆明"]


def test_generic_role_is_allowed_when_named_protagonist_appears() -> None:
    body = "陆明推开档案柜。工作人员正在整理核心系统旧档。"
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body=body,
        summary="陆明发现线索。",
        expected_character_names={"陆明"},
    )

    assert not signals


def test_bare_role_label_for_key_actor_blocks_canon() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="陆明看见对方腰间的徽章。\n\n工作人员。\n\n脚步声逼近旧轨。",
        summary="陆明遭遇工作人员追踪。",
        expected_character_names={"陆明"},
    )

    assert [signal.signal_type for signal in signals] == ["bare_role_placeholder_leakage"]
    assert signals[0].severity == "error"
    assert "不应再用「工作人员」" in signals[0].payload["repair_hint"]


def test_generic_staff_role_used_as_actor_name_blocks_canon() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=2,
        draft_id="d1",
        body="系统巡检部第七小队队长，工作人员。工作人员停下脚步，抬头看向陆明。",
        summary="系统巡检员工作人员拦截陆明。",
        expected_character_names={"陆明"},
    )

    assert [signal.signal_type for signal in signals] == ["bare_role_placeholder_leakage"]
    assert signals[0].severity == "error"


def test_related_personnel_is_not_safe_generic_character_reference() -> None:
    assert ContinuityChecker._looks_like_generic_character_reference("相关人员") is False


def test_placeholder_leakage_autofix_replaces_bare_staff_role_with_stable_alias() -> None:
    output = WriterOutput(
        project_id="p1",
        chapter_number=5,
        title="档案区的故人",
        body="陆明看见工作人员站在旧书摊后。工作人员说他在核心系统维护组。",
        end_of_chapter_summary="陆明遇到工作人员。",
    )
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(
                rule_name="bare_role_placeholder_leakage",
                severity="error",
                description="placeholder",
                reviewer="canon_quality",
            )
        ],
    )

    fixed = WritingOrchestrator._apply_placeholder_leakage_autofix(output, review)

    assert fixed is not None
    assert "工作人员" not in fixed.body
    assert "工作人员" not in fixed.end_of_chapter_summary
    assert fixed.generation_meta["placeholder_leakage_autofix"] == {"工作人员": "旧书摊主"}


def test_subworld_generic_autofix_does_not_introduce_blocked_staff_placeholder() -> None:
    assert WritingOrchestrator._generic_subworld_reference("普通现场", "陈总") == "馆员"
    assert ContinuityChecker._looks_like_generic_character_reference("馆员") is True
    assert ContinuityChecker._looks_like_generic_character_reference("档案区旧书摊主") is True
