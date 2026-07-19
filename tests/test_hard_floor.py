from __future__ import annotations

import pytest

from forwin.checker.hard_floor import run_hard_floor
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.state_change import DeliveredPayoffCandidate, EventCandidate
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import ChapterLengthPolicy, RuntimePolicy


def _policy(profile: str = "standard") -> RuntimePolicy:
    policy = RuntimePolicy.for_profile(profile)  # type: ignore[arg-type]
    return policy.model_copy(
        update={
            "chapter_length": ChapterLengthPolicy.model_construct(
                min_chars=20,
                target_chars=40,
                max_chars=80,
            )
        }
    )


def _writer(body: str, **updates) -> WriterOutput:
    payload = {
        "chapter_number": 1,
        "title": "第一章",
        "body": body,
        "char_count": len(body),
        "end_of_chapter_summary": "本章发生了一件事。",
        "new_events": [EventCandidate(summary="角色A完成行动")],
    }
    payload.update(updates)
    return WriterOutput(**payload)


def _context(**updates) -> ChapterContextPack:
    payload = {
        "project_id": "project-1",
        "project_title": "测试项目",
        "premise": "测试前提",
        "genre": "玄幻",
        "setting_summary": "测试设定",
        "chapter_number": 1,
        "chapter_plan_title": "第一章",
        "chapter_plan_one_line": "角色A开始调查。",
        "chapter_goals": ["找到线索"],
        "must_not_reveal": [],
    }
    payload.update(updates)
    return ChapterContextPack(**payload)


def _run(body: str, *, policy: RuntimePolicy | None = None, **writer_updates):
    return run_hard_floor(
        writer_output=_writer(body, **writer_updates),
        context_pack=_context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        policy=policy or _policy(),
    )


def test_short_chapter_fails_policy_length_floor() -> None:
    result = _run("太短")

    assert result.passed is False
    assert "chapter_length" in result.fail_reasons
    assert result.metadata["min_chapter_chars"] == 20


def test_inconsistent_character_count_fails() -> None:
    body = "角色A推开门，看见证据。他当场拿出证据，反派失去资格。"
    result = _run(body, char_count=0)

    assert result.passed is False
    assert "char_count_consistent" in result.fail_reasons


def test_model_artifact_fails() -> None:
    result = _run("角色A推开门，看见证据。assistant: 模型分析。章末问题出现。")

    assert result.passed is False
    assert "no_garbage" in result.fail_reasons


def test_masked_identifier_is_not_treated_as_garbage() -> None:
    result = _run(
        "系统显示姓名张伟，身份证号************0012。角色A核验记录后继续行动。"
    )

    assert result.passed is True
    assert result.checks["no_garbage"] is True


def test_fully_masked_identifier_is_not_treated_as_garbage() -> None:
    result = _run(
        "系统显示实际住户刘国栋，身份证号：************。角色A核验记录后继续行动。"
    )

    assert result.passed is True
    assert result.checks["no_garbage"] is True


def test_unsupported_symbol_run_still_fails_as_garbage() -> None:
    result = _run("角色A核验记录后看到@#$%^&*+=<>|/，随后继续行动。")

    assert result.passed is False
    assert "no_garbage" in result.fail_reasons


def test_must_not_reveal_and_missing_event_fail() -> None:
    body = "角色A终于发现父亲被围的真相，众人沉默片刻后继续行动。"
    result = run_hard_floor(
        writer_output=_writer(
            body,
            new_events=[],
            state_changes=[],
            thread_beats=[],
        ),
        context_pack=_context(must_not_reveal=["父亲被围"]),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        policy=_policy(),
    )

    assert {"must_not_reveal", "at_least_one_event"}.issubset(result.fail_reasons)


def test_clean_chapter_passes() -> None:
    result = _run(
        "角色A推开门，看见证据。他当场拿出合同，反派赔偿三十万。门外忽然传来密令？"
    )

    assert result.passed is True
    assert result.fail_reasons == []


def test_pulp_missing_visible_payoff_is_warning_only() -> None:
    result = _run(
        "角色A走在路上，想起很多前情。忽然门外传来第二封密令？",
        policy=_policy("pulp"),
    )

    assert result.passed is True
    assert "pulp_visible_payoff" in result.warning_reasons


def test_pulp_track_prefers_project_genre_over_incidental_body_terms() -> None:
    body = (
        "沈川检查包裹里的一枚灵石后，当场拿出赔偿单，确认赔偿金已经到账。"
        "门外忽然传来新的催单声？"
    )

    result = run_hard_floor(
        writer_output=_writer(body),
        context_pack=_context(genre="都市爽文"),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        policy=_policy("pulp"),
    )

    assert result.metadata["pulp_beat_track"] == "urban"
    assert result.metadata["pulp_beat"]["visible_payoff_present"] is True
    assert "pulp_visible_payoff" not in result.warning_reasons


def test_pulp_hard_floor_uses_planned_mystery_reward_contract() -> None:
    body = (
        "三份记录叠好，边缘刚好卡进环形刻痕的缺口，缺口共同指向检测台底部的插孔。"
        "终端随后弹出提示：未登记工号，当前在线。"
    )

    result = run_hard_floor(
        writer_output=_writer(body),
        context_pack=_context(
            genre="都市爽文",
            chapter_experience_plan=ChapterExperiencePlan(
                planned_reward_tags=["mystery"]
            ),
        ),
        repo=None,
        project_id="project-1",
        chapter_number=4,
        policy=_policy("pulp"),
    )

    assert result.metadata["pulp_beat_track"] == "urban"
    assert result.metadata["planned_reward_tags"] == ["mystery"]
    assert result.metadata["pulp_beat"]["visible_payoff_present"] is True
    assert "pulp_visible_payoff" not in result.warning_reasons


def test_pulp_hard_floor_uses_explicit_planned_power_delivery() -> None:
    body = (
        "系统结算完成，权限变更：开放【异常收件人复核】功能。"
        "沈川立刻调出完整面单，门外忽然响起新的催单声？"
    )

    result = run_hard_floor(
        writer_output=_writer(body),
        context_pack=_context(
            genre="都市爽文",
            chapter_experience_plan=ChapterExperiencePlan(
                planned_reward_tags=["power", "social"]
            ),
        ),
        repo=None,
        project_id="project-1",
        chapter_number=3,
        policy=_policy("pulp"),
    )

    assert result.metadata["planned_reward_tags"] == ["power", "social"]
    assert result.metadata["pulp_beat"]["visible_payoff_present"] is True
    assert "pulp_visible_payoff" not in result.warning_reasons


def test_pulp_hard_floor_uses_declared_delivered_payoff() -> None:
    body = "系统回执写明：陈默的临时追件视野已解锁，他立刻看见了目标的因果线。"
    result = run_hard_floor(
        writer_output=_writer(
            body,
            delivered_payoffs=[
                {
                    "entity_name": "陈默",
                    "category": "power",
                    "direction": "gain",
                    "before_state": "只能感知货物余效",
                    "after_state": "临时追件视野已解锁",
                    "evidence_quote": "陈默的临时追件视野已解锁",
                }
            ],
            new_events=[
                EventCandidate(
                    summary="陈默解锁追件视野。",
                    significance="major",
                    involved_entity_names=["陈默"],
                    roles=["protagonist"],
                )
            ],
        ),
        context_pack=_context(genre="都市玄幻"),
        repo=None,
        project_id="project-1",
        chapter_number=2,
        policy=_policy("pulp"),
    )

    assert result.metadata["planned_reward_tags"] == []
    assert result.metadata["pulp_beat"]["visible_payoff_present"] is True
    assert result.metadata["pulp_beat"]["visible_payoff_evidence"] == [
        "delivered_payoff:陈默:power:临时追件视野已解锁"
    ]


def test_pulp_hard_floor_uses_frozen_chapter_one_payoff_excerpt() -> None:
    body = "承运人陈默，当前体力与轻度伤势已按签收单余效比例修复。"
    result = run_hard_floor(
        writer_output=_writer(
            body,
            delivered_payoffs=[
                {
                    "entity_name": "陈默",
                    "category": "power",
                    "direction": "gain",
                    "before_state": "肩膝酸胀且虎口受伤",
                    "after_state": "当前体力与轻度伤势已按签收单余效比例修复",
                    "evidence_quote": "承运人陈默，当前体力与轻度伤势已按签收单余效比例修复",
                }
            ],
            new_events=[
                EventCandidate(
                    summary="陈默获得货物余效修复。",
                    significance="major",
                    involved_entity_names=["陈默"],
                    roles=["protagonist"],
                )
            ],
        ),
        context_pack=_context(genre="都市玄幻"),
        repo=None,
        project_id="project-1",
        chapter_number=2,
        policy=_policy("pulp"),
    )

    assert result.metadata["pulp_beat"]["visible_payoff_present"] is True


@pytest.mark.parametrize(
    ("body", "payoff"),
    [
        (
            "反派的临时追件视野已解锁，陈默只能后退。",
            {
                "entity_name": "反派",
                "category": "power",
                "direction": "gain",
                "before_state": "无追件视野",
                "after_state": "临时追件视野已解锁",
                "evidence_quote": "反派的临时追件视野已解锁",
            },
        ),
        (
            "陈默的临时追件视野已解锁。",
            {
                "entity_name": "陈默",
                "category": "power",
                "direction": "gain",
                "before_state": "临时追件视野已解锁",
                "after_state": "临时追件视野已解锁",
                "evidence_quote": "陈默的临时追件视野已解锁",
            },
        ),
        (
            "陈默的临时追件视野仍未解锁。",
            {
                "entity_name": "陈默",
                "category": "power",
                "direction": "gain",
                "before_state": "无追件视野",
                "after_state": "临时追件视野已解锁",
                "evidence_quote": "陈默的临时追件视野已解锁",
            },
        ),
        (
            "陈默看到系统提示：临时追件视野已解锁。",
            {
                "entity_name": "陈默",
                "category": "power",
                "direction": "gain",
                "before_state": "无追件视野",
                "after_state": "临时追件视野已解锁",
                "evidence_quote": "临时追件视野已解锁",
            },
        ),
        (
            "陈默获得新的追件能力。",
            {
                "entity_name": "陈默",
                "category": "power",
                "direction": "gain",
                "before_state": "无追件能力",
                "after_state": "临时追件视野已解锁",
                "evidence_quote": "陈默获得新的追件能力",
            },
        ),
    ],
)
def test_pulp_hard_floor_rejects_unverifiable_delivered_payoff(
    body: str,
    payoff: dict[str, str],
) -> None:
    result = run_hard_floor(
        writer_output=_writer(
            body,
            delivered_payoffs=[payoff],
            new_events=[
                EventCandidate(
                    summary="陈默继续追查。",
                    significance="major",
                    involved_entity_names=["陈默"],
                    roles=["protagonist"],
                )
            ],
        ),
        context_pack=_context(genre="都市玄幻"),
        repo=None,
        project_id="project-1",
        chapter_number=2,
        policy=_policy("pulp"),
    )

    assert result.metadata["pulp_beat"]["visible_payoff_present"] is False
    assert result.metadata["pulp_beat"]["visible_payoff_evidence"] == []


def test_pulp_hard_floor_rejects_blank_normalized_after_state() -> None:
    body = "陈默获得临时追件视野。"
    malformed = DeliveredPayoffCandidate.model_construct(
        entity_name="陈默",
        category="power",
        direction="gain",
        before_state="无追件视野",
        after_state=" ",
        evidence_quote="陈默获得临时追件视野",
    )
    result = run_hard_floor(
        writer_output=_writer(
            body,
            delivered_payoffs=[malformed],
            new_events=[
                EventCandidate(
                    summary="陈默获得追件视野。",
                    significance="major",
                    involved_entity_names=["陈默"],
                    roles=["protagonist"],
                )
            ],
        ),
        context_pack=_context(genre="都市玄幻"),
        repo=None,
        project_id="project-1",
        chapter_number=2,
        policy=_policy("pulp"),
    )

    assert result.metadata["pulp_beat"]["visible_payoff_present"] is False
    assert result.metadata["pulp_beat"]["visible_payoff_evidence"] == []
