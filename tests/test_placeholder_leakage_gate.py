from __future__ import annotations

from forwin.canon_quality.placeholder import analyze_placeholder_leakage
from forwin.checker.rules import ContinuityChecker


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


def test_placeholder_only_in_summary_is_warning() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="林澈收起档案，离开旧楼。",
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
        expected_character_names={"林澈"},
    )

    assert [signal.signal_type for signal in signals] == ["protagonist_placeholder_leakage"]
    assert signals[0].severity == "error"
    assert signals[0].payload["expected_character_names"] == ["林澈"]


def test_generic_role_is_allowed_when_named_protagonist_appears() -> None:
    body = "林澈推开档案柜。工作人员正在整理白塔旧档。"
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body=body,
        summary="林澈发现线索。",
        expected_character_names={"林澈"},
    )

    assert not signals


def test_bare_role_label_for_key_actor_blocks_canon() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="林澈看见对方腰间的徽章。\n\n工作人员。\n\n脚步声逼近旧轨。",
        summary="林澈遭遇工作人员追踪。",
        expected_character_names={"林澈"},
    )

    assert [signal.signal_type for signal in signals] == ["bare_role_placeholder_leakage"]
    assert signals[0].severity == "error"


def test_generic_staff_role_used_as_actor_name_blocks_canon() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=2,
        draft_id="d1",
        body="白塔巡检部第七小队队长，工作人员。工作人员停下脚步，抬头看向林澈。",
        summary="白塔巡检员工作人员拦截林澈。",
        expected_character_names={"林澈"},
    )

    assert [signal.signal_type for signal in signals] == ["bare_role_placeholder_leakage"]
    assert signals[0].severity == "error"


def test_related_personnel_is_not_safe_generic_character_reference() -> None:
    assert ContinuityChecker._looks_like_generic_character_reference("相关人员") is False
