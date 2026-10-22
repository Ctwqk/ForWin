from __future__ import annotations

import json

import pytest

from forwin.checker.hard_floor import HardFloorResult
from forwin.checker.pulp_policy import evaluate_pulp_beat_policy
from forwin.checker.pulp_beat import (
    DeliveredPayoffCandidate,
    pulp_track_for_genre,
    verify_pulp_beats,
)
from forwin.audit.events import DecisionEventType
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.audit import DecisionEvent
from forwin.models.project import Project
from forwin.runtime.policy import RuntimePolicy
from tests.postgres import postgres_test_url


_URBAN_BODY = "众人嘲笑他没资格。林远当场拿出合同，老板脸色大变，当众道歉，还赔偿现金。门外忽然传来新的威胁。"


def test_verify_pulp_beats_detects_core_payoff() -> None:
    result = verify_pulp_beats(_URBAN_BODY, track="urban")

    assert result.pressure_present is True
    assert result.protagonist_action_present is True
    assert result.visible_payoff_present is True
    assert result.audience_reaction_present is True
    assert result.enemy_or_obstacle_damage_present is True
    assert result.new_gain_or_status_shift_present is True
    assert result.next_hook_present is True


def test_a_disciple_mention_is_not_an_audience_reaction() -> None:
    result = verify_pulp_beats("弟子推门进来，递上一封信。", track="xuanhuan")

    assert result.audience_reaction_present is False


def test_unmatched_genre_uses_neutral_pulp_profile() -> None:
    assert pulp_track_for_genre("星际") is None
    for track in (pulp_track_for_genre("星际"), "", "no-such-track"):
        result = verify_pulp_beats(_URBAN_BODY, track=track)

        assert result.pressure_present is False
        assert result.protagonist_action_present is False
        assert result.visible_payoff_present is False
        assert result.audience_reaction_present is False
        assert result.enemy_or_obstacle_damage_present is False
        assert result.new_gain_or_status_shift_present is False
        assert result.next_hook_present is False
        assert result.visible_payoff_evidence == []
        assert "visible_payoff_present" in result.missing_fields


def test_unknown_track_still_counts_planned_reward_tags_and_structured_payoffs() -> None:
    tagged = verify_pulp_beats(
        "系统开放了角色A的复核功能。", track=None, reward_tags=("power",)
    )
    declared = verify_pulp_beats(
        "陈默收到提示：临时视野已解锁。",
        track=None,
        delivered_payoffs=[
            DeliveredPayoffCandidate(
                target_id="reward:power",
                category="power",
                evidence_quote="陈默收到提示：临时视野已解锁",
            )
        ],
    )

    assert tagged.visible_payoff_present is True
    assert declared.visible_payoff_present is True
    assert declared.visible_payoff_evidence == ["delivered_payoff:reward:power"]
    assert declared.pressure_present is False


def test_verify_pulp_beats_flags_missing_payoff() -> None:
    result = verify_pulp_beats("他走在路上，想了很多前情，夜色越来越深。", track="urban")

    assert result.visible_payoff_present is False
    assert "visible_payoff_present" in result.missing_fields


def test_unplanned_profile_payoff_respects_negation() -> None:
    result = verify_pulp_beats("角色A没有资格，只能继续等待。", track="urban")

    assert result.visible_payoff_present is False


def test_unplanned_profile_payoff_accepts_delivery_after_contrast() -> None:
    result = verify_pulp_beats("他虽没有资格但合同已经生效。", track="urban")

    assert result.visible_payoff_present is True


def test_verify_pulp_beats_uses_planned_mystery_reward_contract() -> None:
    body = "三份记录叠好，名单上的名字与地点逐一对上。"

    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=("mystery",),
    )

    assert result.visible_payoff_present is True
    assert "visible_payoff_present" not in result.missing_fields


def test_verify_pulp_beats_uses_delivered_power_permission_change() -> None:
    result = verify_pulp_beats(
        "系统结算完成，已向角色A开放复核功能。",
        track="urban",
        reward_tags=("power", "social"),
    )

    assert result.visible_payoff_present is True


def test_verify_pulp_beats_uses_delivered_role_and_permission_transition() -> None:
    body = (
        "系统刚才更新了他的职位。"
        "撤销了单一错投指控，改为潜在原收件人兼争议负责人。"
        "工作人员重新看向角色A，语气变了：你有权限了，可以执行。"
    )

    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=("power", "social"),
    )

    assert result.visible_payoff_present is True
    assert "visible_payoff_present" not in result.missing_fields


def test_delivered_permission_allows_non_negating_unregistered_label() -> None:
    result = verify_pulp_beats(
        "系统开放了未登记用户的查验权限。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is True


def test_planned_power_does_not_count_an_undelivered_permission() -> None:
    result = verify_pulp_beats(
        "他反复询问权限是否会变更，但系统确认功能仍未开放。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_exact_marker_respects_negation() -> None:
    result = verify_pulp_beats(
        "系统确认角色A尚未获得权限。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_does_not_count_a_pending_permission_request() -> None:
    result = verify_pulp_beats(
        "权限开放申请已提交，当前仍在等待批准。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_social_relief_respects_negation() -> None:
    result = verify_pulp_beats(
        "系统确认尚未撤销角色A的指控。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is False


def test_planned_social_relief_does_not_count_a_pending_request() -> None:
    result = verify_pulp_beats(
        "角色A申请撤销指控，审批结果尚未公布。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is False


def test_planned_social_does_not_count_permission_only_delivery() -> None:
    result = verify_pulp_beats(
        "系统向角色A开放查验权限。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is False


def test_planned_social_profile_marker_respects_negation() -> None:
    result = verify_pulp_beats(
        "系统确认角色A尚未获得资格。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is False


def test_planned_social_does_not_count_a_negative_role_reclassification() -> None:
    result = verify_pulp_beats(
        "系统更新职位为失信人员。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is False


def test_planned_social_counts_a_positive_role_reclassification() -> None:
    result = verify_pulp_beats(
        "系统将职位改为正式负责人。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is True


def test_planned_social_counts_role_reclassification_across_a_comma() -> None:
    result = verify_pulp_beats(
        "系统更新职位，改为正式负责人。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is True


def test_planned_social_does_not_count_a_revoked_qualification() -> None:
    result = verify_pulp_beats(
        "系统撤销了角色A的资格。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is False


def test_planned_social_does_not_count_a_revoked_acquired_qualification() -> None:
    result = verify_pulp_beats(
        "系统撤销了角色A已获得的资格。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_does_not_count_a_revoked_promotion_qualification() -> None:
    result = verify_pulp_beats(
        "系统撤销角色A的晋升资格。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_does_not_treat_future_as_a_direction_reset() -> None:
    result = verify_pulp_beats(
        "系统撤销角色A后续晋升资格。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_does_not_count_a_negated_post_loss_gain() -> None:
    result = verify_pulp_beats(
        "系统取消旧资格并非授予新资格。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_does_not_count_a_directly_negated_gain() -> None:
    result = verify_pulp_beats(
        "系统并非授予角色A新资格。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


@pytest.mark.parametrize(
    ("body", "reward_tag"),
    [
        ("系统不授予角色A资格。", "power"),
        ("系统不开放查验权限。", "power"),
        ("系统不更新职位为正式成员。", "social"),
        ("系统不予授予角色A资格。", "power"),
        ("系统未予开放查验权限。", "power"),
        ("系统不再开放查验权限。", "power"),
        ("系统不可开放查验权限。", "power"),
        ("角色A不被授予资格。", "power"),
        ("角色A未被授予资格。", "power"),
        ("角色A的指控未被撤销。", "social"),
        ("系统不允许开放查验权限。", "power"),
        ("系统未获批准开放查验权限。", "power"),
        ("系统不允许撤销指控。", "social"),
        ("审批组不同意向角色A授予资格。", "power"),
        ("审批组不允许为角色A撤销指控。", "social"),
        ("审批组不同意最终向角色A授予资格。", "power"),
        ("审批组不允许最终为角色A撤销指控。", "social"),
    ],
)
def test_planned_reward_does_not_count_a_negated_delivery_action(
    body: str,
    reward_tag: str,
) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=(reward_tag,),
    )

    assert result.visible_payoff_present is False


@pytest.mark.parametrize(
    "body",
    [
        "角色A没资格。",
        "角色A无资格。",
        "角色A尚无资格。",
        "角色A不具备资格。",
        "角色A未获资格。",
        "角色A未得到资格。",
        "角色A没拿到资格。",
        "角色A未被认定具备资格。",
        "角色A不被视为符合资格。",
    ],
)
def test_planned_power_does_not_count_a_negated_reward_state(body: str) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_social_counts_a_confirmed_positive_reward_state() -> None:
    result = verify_pulp_beats(
        "角色A被认定具备资格。",
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is True


@pytest.mark.parametrize(
    "body",
    [
        "角色A的资格未被确认。",
        "角色A的资格未被认定。",
        "角色A的资格尚未被确认，仍待审批。",
        "角色A的资格未获批准。",
        "角色A的资格未得到批准。",
        "角色A的资格未取得批准。",
    ],
)
def test_planned_power_does_not_count_a_post_marker_state_denial(body: str) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


@pytest.mark.parametrize(
    "body",
    [
        "角色A的资格结果无效。",
        "角色A的资格状态无效。",
        "角色A的资格结果落空。",
    ],
)
def test_planned_power_does_not_count_a_failed_post_marker_state(body: str) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_counts_a_post_marker_state_confirmation() -> None:
    result = verify_pulp_beats(
        "角色A的资格已被确认。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is True


@pytest.mark.parametrize(
    "body",
    [
        "角色A没有突破境界。",
        "角色A突破失败。",
        "角色A没能晋升。",
    ],
)
def test_planned_power_does_not_count_a_failed_track_payoff(body: str) -> None:
    result = verify_pulp_beats(
        body,
        track="xuanhuan",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


@pytest.mark.parametrize(
    ("body", "reward_tag"),
    [
        ("系统开放查验权限失败。", "power"),
        ("系统授予角色A资格未果。", "power"),
        ("系统更新职位为正式成员失败。", "social"),
        ("系统解除权限限制失败。", "power"),
        ("系统撤销指控失败。", "social"),
    ],
)
def test_planned_reward_does_not_count_a_failed_transition(
    body: str,
    reward_tag: str,
) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=(reward_tag,),
    )

    assert result.visible_payoff_present is False


def test_planned_power_does_not_let_an_adjacent_repeat_hide_failure() -> None:
    result = verify_pulp_beats(
        "系统开放查验权限失败，权限状态未变。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


@pytest.mark.parametrize(
    ("body", "reward_tag"),
    [
        ("角色A的资格授予申请被驳回。", "power"),
        ("角色A的资格授予申请被否决。", "power"),
        ("角色A的指控撤销申请未通过。", "social"),
    ],
)
def test_planned_reward_does_not_count_a_denied_outcome(
    body: str,
    reward_tag: str,
) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=(reward_tag,),
    )

    assert result.visible_payoff_present is False


@pytest.mark.parametrize(
    ("body", "reward_tag"),
    [
        ("系统撤销角色A的指控。", "social"),
        ("系统取消角色A的封禁。", "power"),
        ("系统撤销角色A的处罚。", "power"),
    ],
)
def test_planned_reward_counts_a_delivered_relief(
    body: str,
    reward_tag: str,
) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=(reward_tag,),
    )

    assert result.visible_payoff_present is True


def test_planned_power_counts_a_new_gain_after_a_denied_old_request() -> None:
    result = verify_pulp_beats(
        "系统驳回旧申请并授予角色A资格。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is True


@pytest.mark.parametrize(
    "body",
    [
        "系统更新职位为非正式成员。",
        "系统将职位改为不合格成员。",
    ],
)
def test_planned_social_does_not_count_a_negated_positive_result(body: str) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=("social",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_does_not_count_an_undelivered_breakthrough() -> None:
    result = verify_pulp_beats(
        "角色A尚未突破境界。",
        track="xuanhuan",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_power_counts_delivery_after_an_earlier_negative_clause() -> None:
    result = verify_pulp_beats(
        "角色A此前尚未获得资格，但系统现在开放了查验权限。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is True


def test_planned_power_counts_a_new_gain_after_an_unrelated_revocation() -> None:
    result = verify_pulp_beats(
        "系统撤销旧限制后授予角色A新的资格。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is True


@pytest.mark.parametrize(
    "body",
    [
        "系统撤销旧资格后资源到账。",
        "系统取消旧职位后战局逆转。",
    ],
)
def test_planned_power_counts_an_exact_payoff_after_a_direction_reset(
    body: str,
) -> None:
    result = verify_pulp_beats(
        body,
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is True


def test_planned_power_counts_delivery_after_a_negated_old_option() -> None:
    result = verify_pulp_beats(
        "审批组不同意旧方案但最终授予角色A资格。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is True


def test_planned_power_counts_delivery_after_unrelated_missing_information() -> None:
    result = verify_pulp_beats(
        "审批组未获悉详情仍授予角色A资格。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is True


def test_planned_power_does_not_count_social_relief_only() -> None:
    result = verify_pulp_beats(
        "系统撤销角色A的单一错投指控。",
        track="urban",
        reward_tags=("power",),
    )

    assert result.visible_payoff_present is False


def test_planned_mystery_reward_still_requires_concrete_evidence() -> None:
    result = verify_pulp_beats(
        "他翻看了一遍记录，觉得事情不太对，却暂时没有找到任何突破口。",
        track="urban",
        reward_tags=("mystery",),
    )

    assert result.visible_payoff_present is False


def test_verify_pulp_beats_detects_xuanhuan_payoff_without_urban_words() -> None:
    body = (
        "宗门长老当众威胁逐他出山。林远当场运转灵诀突破境界，擂台全场震动，"
        "敌人经脉受创退下，灵石奖励入袋。秘境入口忽然开启。"
    )

    result = verify_pulp_beats(body, track="xuanhuan")

    assert result.pressure_present is True
    assert result.protagonist_action_present is True
    assert result.visible_payoff_present is True
    assert result.audience_reaction_present is True
    assert result.enemy_or_obstacle_damage_present is True
    assert result.new_gain_or_status_shift_present is True
    assert result.next_hook_present is True


def test_verify_pulp_beats_detects_treasure_medicine_gain_separately() -> None:
    body = (
        "掌柜当众质疑他没眼力。沈青当场施针验出古玉暗纹，围观客人哗然，"
        "病人苏醒，假专家脸色大变。鉴定证书到手，后院忽然传来求救声。"
    )

    result = verify_pulp_beats(body, track="treasure_medicine")

    assert result.pressure_present is True
    assert result.visible_payoff_present is True
    assert result.new_gain_or_status_shift_present is True
    assert result.enemy_or_obstacle_damage_present is True
    assert "visible_payoff_present" not in result.missing_fields
    assert "new_gain_or_status_shift_present" not in result.missing_fields


def test_pulp_policy_blocks_consecutive_missing_payoff() -> None:
    engine = get_engine(postgres_test_url("pulp-policy-consecutive"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            session.flush()
            session.add(
                DecisionEvent(
                    project_id="project-1",
                    chapter_number=1,
                    event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                    payload_json=json.dumps(
                        {"pulp_beat": {"visible_payoff_present": False}},
                        ensure_ascii=False,
                    ),
                )
            )

        with Session.begin() as session:
            decision = evaluate_pulp_beat_policy(
                session=session,
                project_id="project-1",
                chapter_number=2,
                hard_floor_result=HardFloorResult(
                    passed=True,
                    warning_reasons=["pulp_visible_payoff"],
                    metadata={"pulp_beat": {"visible_payoff_present": False}},
                ),
                policy=RuntimePolicy.for_profile("pulp"),
            )

        assert decision.fatal is True
        assert decision.reason == "pulp_visible_payoff_consecutive_missing"
        assert decision.consecutive_missing_payoff == 2
    finally:
        engine.dispose()


def test_pulp_policy_does_not_block_a_chapter_without_a_usable_extraction() -> None:
    engine = get_engine(postgres_test_url("pulp-policy-no-extraction"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            session.flush()
            session.add(DecisionEvent(
                project_id="project-1", chapter_number=1, event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                payload_json=json.dumps({"pulp_beat": {"visible_payoff_present": False}}),
            ))

        with Session.begin() as session:
            decision = evaluate_pulp_beat_policy(
                session=session,
                project_id="project-1",
                chapter_number=2,
                hard_floor_result=HardFloorResult(
                    passed=True,
                    warning_reasons=["chapter_delta_unavailable", "pulp_visible_payoff"],
                    checks={"chapter_delta_available": False},
                    metadata={"pulp_beat": {"visible_payoff_present": False}},
                ),
                policy=RuntimePolicy.for_profile("pulp"),
            )

        # Without the extraction the delivered rewards are unknown, so the miss is not counted.
        assert decision.fatal is False
        assert decision.consecutive_missing_payoff == 0
    finally:
        engine.dispose()


def test_pulp_policy_does_not_count_a_prior_miss_recorded_without_an_extraction() -> None:
    engine = get_engine(postgres_test_url("pulp-policy-prior-no-extraction"))
    init_db(engine)
    Session = get_session_factory(engine)
    try:
        with Session.begin() as session:
            session.add(Project(id="project-1", title="P", premise="p", genre="都市"))
            session.flush()
            session.add(DecisionEvent(
                project_id="project-1", chapter_number=1, event_type=DecisionEventType.PULP_BEAT_EVALUATED,
                payload_json=json.dumps({
                    "warning_reasons": ["chapter_delta_unavailable", "pulp_visible_payoff"],
                    "pulp_beat": {"visible_payoff_present": False},
                }),
            ))

        with Session.begin() as session:
            decision = evaluate_pulp_beat_policy(
                session=session,
                project_id="project-1",
                chapter_number=2,
                hard_floor_result=HardFloorResult(
                    passed=True,
                    warning_reasons=["pulp_visible_payoff"],
                    checks={"chapter_delta_available": True},
                    metadata={"pulp_beat": {"visible_payoff_present": False}},
                ),
                policy=RuntimePolicy.for_profile("pulp"),
            )

        assert decision.fatal is False
        assert decision.consecutive_missing_payoff == 1
    finally:
        engine.dispose()


def test_pulp_policy_is_warning_only_for_standard_profile() -> None:
    decision = evaluate_pulp_beat_policy(
        session=object(),
        project_id="project-1",
        chapter_number=1,
        hard_floor_result=HardFloorResult(
            passed=True,
            warning_reasons=["pulp_visible_payoff"],
            metadata={"pulp_beat": {"visible_payoff_present": False}},
        ),
        policy=RuntimePolicy.for_profile("standard"),
    )

    assert decision.fatal is False
    assert decision.consecutive_missing_payoff == 1
