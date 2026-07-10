from __future__ import annotations

from forwin.checker.hard_floor import run_hard_floor
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.state_change import EventCandidate
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
