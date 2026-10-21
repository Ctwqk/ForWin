from __future__ import annotations

import pytest

from forwin.checker.hard_floor import run_hard_floor
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.writer import WriterOutput
from forwin.runtime.policy import ChapterLengthPolicy, RuntimePolicy
from tests.chapter_delta_helpers import chapter_delta


_EVENT = {
    "summary": "角色A完成行动",
    "significance": "minor",
    "visibility": "public",
    "evidence": "角色A",
}


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
        "chapter_delta": chapter_delta(body, events=[_EVENT]),
    }
    payload.update(updates)
    return WriterOutput(**payload)


def _fulfilled(body: str, *fulfillment: dict[str, str]):
    """A chapter delta with one event and the given plan fulfillment entries."""
    return chapter_delta(body, events=[_EVENT], plan_fulfillment=list(fulfillment))


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
        writer_output=_writer(body, chapter_delta=chapter_delta(body)),
        context_pack=_context(must_not_reveal=["父亲被围"]),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        policy=_policy(),
    )

    assert {"must_not_reveal", "at_least_one_event"}.issubset(result.fail_reasons)


def test_clean_chapter_passes() -> None:
    result = _run(
        "角色A推开门，看见证据。他当场拿出合同，反派赔偿现金。门外忽然传来密令？"
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
        "沈川检查包裹里的一枚灵石后，当场拿出赔偿单，确认赔偿款已经到账。"
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


def test_unmatched_genre_uses_neutral_pulp_profile() -> None:
    body = (
        "众人嘲笑他没资格。林远当场拿出合同，老板脸色大变，当众道歉，还赔偿现金。"
        "门外忽然传来新的威胁。"
    )

    result = run_hard_floor(
        writer_output=_writer(body),
        context_pack=_context(genre="星际"),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        policy=_policy("pulp"),
    )

    assert result.passed is True
    assert result.metadata["pulp_beat_track"] == "none"
    beat = result.metadata["pulp_beat"]
    assert beat["visible_payoff_present"] is False
    assert beat["visible_payoff_evidence"] == []
    assert not any(
        beat[field]
        for field in (
            "pressure_present",
            "protagonist_action_present",
            "audience_reaction_present",
            "enemy_or_obstacle_damage_present",
            "new_gain_or_status_shift_present",
            "next_hook_present",
        )
    )
    assert "pulp_visible_payoff" in result.warning_reasons


def test_unmatched_genre_still_counts_planned_reward_delivery() -> None:
    body = "三份记录叠好，名单上的名字与地点逐一对上。门外忽然传来脚步声？"

    result = run_hard_floor(
        writer_output=_writer(body),
        context_pack=_context(
            genre="星际",
            chapter_experience_plan=ChapterExperiencePlan(
                planned_reward_tags=["mystery"]
            ),
        ),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        policy=_policy("pulp"),
    )

    assert result.metadata["pulp_beat_track"] == "none"
    assert result.metadata["pulp_beat"]["visible_payoff_present"] is True
    assert "pulp_visible_payoff" not in result.warning_reasons


def test_pulp_hard_floor_uses_planned_mystery_reward_contract() -> None:
    body = "三份记录叠好，名单上的名字与地点逐一对上。门外忽然传来脚步声？"

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
        "系统结算完成，已向沈川开放复核功能。"
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
    body = "系统提示写明：陈默的临时追件视野已解锁，他立刻看见了目标的因果线。"
    result = run_hard_floor(
        writer_output=_writer(
            body,
            chapter_delta=_fulfilled(
                body,
                {"target_id": "reward:power", "status": "done", "evidence": "陈默的临时追件视野已解锁"},
            ),
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
        "delivered_payoff:reward:power"
    ]


def test_pulp_hard_floor_uses_frozen_chapter_one_payoff_excerpt() -> None:
    body = "承运人陈默，当前体力与轻度伤势已按签收单余效比例修复。"
    result = run_hard_floor(
        writer_output=_writer(
            body,
            chapter_delta=_fulfilled(
                body,
                {
                    "target_id": "reward:power",
                    "status": "done",
                    # Evidence matches the body up to whitespace.
                    "evidence": "承运人陈默，当前体力与轻度伤势 已按签收单余效比例修复",
                },
            ),
        ),
        context_pack=_context(genre="都市玄幻"),
        repo=None,
        project_id="project-1",
        chapter_number=2,
        policy=_policy("pulp"),
    )

    assert result.metadata["pulp_beat"]["visible_payoff_present"] is True


@pytest.mark.parametrize(
    "fulfillment",
    [
        # The quote is not in the body.
        {"target_id": "reward:power", "status": "done", "evidence": "陈默的临时追件视野已解锁"},
        # Only a done target is a delivered payoff.
        {"target_id": "reward:power", "status": "partial", "evidence": "陈默的临时追件视野仍未解锁"},
        # Goals are not rewards.
        {"target_id": "goal:1", "status": "done", "evidence": "陈默的临时追件视野仍未解锁"},
        # A reward needs evidence.
        {"target_id": "reward:power", "status": "done", "evidence": " "},
        # A reward target names a reward tag.
        {"target_id": "reward:fortune", "status": "done", "evidence": "陈默的临时追件视野仍未解锁"},
    ],
)
def test_pulp_hard_floor_rejects_unverifiable_delivered_payoff(
    fulfillment: dict[str, str],
) -> None:
    body = "陈默的临时追件视野仍未解锁。"
    result = run_hard_floor(
        writer_output=_writer(body, chapter_delta=_fulfilled(body, fulfillment)),
        context_pack=_context(genre="都市玄幻"),
        repo=None,
        project_id="project-1",
        chapter_number=2,
        policy=_policy("pulp"),
    )

    assert result.metadata["pulp_beat"]["visible_payoff_present"] is False
    assert result.metadata["pulp_beat"]["visible_payoff_evidence"] == []
