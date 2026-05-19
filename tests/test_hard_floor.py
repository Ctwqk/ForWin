from __future__ import annotations

from types import SimpleNamespace

from forwin.checker.hard_floor import run_hard_floor
from forwin.config import Config
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.state_change import EventCandidate
from forwin.protocol.writer import WriterOutput


def writer(body: str, **updates) -> WriterOutput:
    data = {
        "chapter_number": 1,
        "title": "第一章",
        "body": body,
        "char_count": len(body),
        "end_of_chapter_summary": "本章发生了一件事。",
        "new_events": [EventCandidate(summary="角色A完成行动")],
    }
    data.update(updates)
    return WriterOutput(**data)


def context(**updates) -> ChapterContextPack:
    data = {
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
    data.update(updates)
    return ChapterContextPack(**data)


def config() -> Config:
    return Config(min_chapter_chars=20, hard_floor_gate_enabled=True)


def test_short_chapter_fails() -> None:
    result = run_hard_floor(
        writer_output=writer("太短"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "chapter_length" in result.fail_reasons
    assert result.checks["chapter_length"] is False


def test_model_artifact_fails() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见证据。assistant: 模型分析。章末问题出现。"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "no_garbage" in result.fail_reasons
    assert result.checks["no_garbage"] is False


def test_must_not_reveal_fails_on_direct_match() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A终于发现父亲被围的真相，众人沉默片刻后继续行动。"),
        context_pack=context(must_not_reveal=["父亲被围"]),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "must_not_reveal" in result.fail_reasons
    assert result.checks["must_not_reveal"] is False
    assert result.metadata["must_not_reveal_hits"] == ["父亲被围"]


def test_missing_event_fails() -> None:
    result = run_hard_floor(
        writer_output=writer(
            "角色A推开门，看见证据。章末新的脚步声靠近。",
            new_events=[],
            state_changes=[],
            thread_beats=[],
        ),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "at_least_one_event" in result.fail_reasons
    assert result.checks["at_least_one_event"] is False


def test_ending_hook_is_warning_only() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见证据。他把证据交给同伴，众人决定继续调查。"),
        context_pack=context(),
        repo=SimpleNamespace(get_chapter_experience_plan=lambda *args: None),
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is True
    assert "ending_hook" in result.warning_reasons
    assert "ending_hook" not in result.fail_reasons
    assert result.checks["ending_hook"] is False


def test_clean_chapter_passes() -> None:
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见证据。他当场拿出证据，反派失去资格。门外忽然传来第二封密令？"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is True
    assert result.fail_reasons == []
    assert result.warning_reasons == []
    assert result.metadata["project_id"] == "project-1"
    assert result.metadata["chapter_number"] == 1
