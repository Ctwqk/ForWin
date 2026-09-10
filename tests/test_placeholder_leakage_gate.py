from __future__ import annotations

import pytest

from forwin.canon_quality.placeholder import analyze_placeholder_leakage
from forwin.checker.reference_classifier import (
    looks_like_generic_character_reference,
)
from forwin.generation.pipeline import ChapterPipeline


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


def test_internal_state_key_leakage_blocks_trait_skill_ids() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=3,
        body="陆明的旁白里残留 trait-loyal-protector。",
    )

    assert [signal.signal_type for signal in signals] == ["internal_state_key_leakage"]
    assert signals[0].payload["internal_state_key"] == "trait-loyal-protector"


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


@pytest.mark.parametrize(
    "body",
    [
        "林川出示核查证。工作人员停下贴封条的手。",
        "林川核对目录。工作人员说：只能在阅卷区看。",
        "林川登记后，工作人员伸手接过申请。",
        "工作人员抬头看向林川，确认了预约时间。",
        "林川签名时，工作人员递来一支笔。",
        "林川核验权限后，工作人员打开窗口。",
        "林川看到徽章上写着工作人员，没有记载姓名。",
        "林川的签名是工作人员代签的。",
        "林川说，队长、工作人员、守卫都已经到场。",
        "林川清点到场人员：队长，工作人员，守卫。",
        "林川看见胸牌上只有岗位名称。\n工作人员。\n这位大厅服务员替他指了路。",
    ],
)
def test_ordinary_staff_actions_are_not_identity_placeholders(body: str) -> None:
    assert analyze_placeholder_leakage(
        project_id="p1", chapter_number=1, body=body,
        expected_character_names={"林川"},
    ) == []


@pytest.mark.parametrize(
    "label",
    [
        "姓名：工作人员",
        "签名栏写着“工作人员”",
        "签名栏上写着‘工作人员’",
        "署名处写着‘工作人员’",
        "签名人：工作人员",
        "巡检员名叫工作人员",
        "姓名栏里填的是‘工作人员’",
    ],
)
def test_explicit_staff_identity_placeholder_remains_blocking(label: str) -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1", chapter_number=1, body=f"林川核对登记表，{label}。",
        expected_character_names={"林川"},
    )
    assert [signal.signal_type for signal in signals] == ["bare_role_placeholder_leakage"]
    assert signals[0].severity == "error"


def test_standalone_role_does_not_prove_a_missing_identity() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=1,
        draft_id="d1",
        body="陆明看见对方腰间的徽章。\n\n工作人员。\n\n脚步声逼近旧轨。",
        summary="陆明遭遇工作人员追踪。",
        expected_character_names={"陆明"},
    )

    assert signals == []


def test_generic_staff_role_used_as_actor_name_blocks_canon() -> None:
    signals = analyze_placeholder_leakage(
        project_id="p1",
        chapter_number=2,
        draft_id="d1",
        body="系统巡检部第七小队队长，姓名：工作人员。工作人员停下脚步，抬头看向陆明。",
        summary="系统巡检员工作人员拦截陆明。",
        expected_character_names={"陆明"},
    )

    assert [signal.signal_type for signal in signals] == ["bare_role_placeholder_leakage"]
    assert signals[0].severity == "error"


def test_related_personnel_is_not_safe_generic_character_reference() -> None:
    assert looks_like_generic_character_reference("相关人员") is False


def test_subworld_generic_autofix_helper_is_removed_from_pipeline_boundary() -> None:
    assert not hasattr(ChapterPipeline, "_generic_subworld_reference")
    assert looks_like_generic_character_reference("路人") is True
    assert looks_like_generic_character_reference("馆员") is False
    assert looks_like_generic_character_reference("档案区旧书摊主") is False
