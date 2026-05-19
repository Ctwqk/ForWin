from __future__ import annotations

from types import SimpleNamespace

from forwin.context.assembler import ChapterContextAssembler
from forwin.context.gates import RecencyTruncateGate
from forwin.context.request import ContextDraft, ContextRequest


def _request(chapter_number: int = 100) -> ContextRequest:
    return ContextRequest(
        project_id="project-1",
        chapter_plan=SimpleNamespace(chapter_number=chapter_number),
        repo=SimpleNamespace(),
    )


def test_window_zero_is_strict_no_op_even_with_entity_cap() -> None:
    data = {
        "summaries": [{"chapter_number": 1, "text": "old"}],
        "entities": [{"name": "old", "last_seen_chapter": 1, "importance": 1}],
    }
    draft = ContextDraft(data=data, issues=[])

    issues = RecencyTruncateGate(window_chapters=0, max_entities=1).validate(_request(), draft)

    assert issues == []
    assert draft.data is data
    assert draft.data["summaries"] == [{"chapter_number": 1, "text": "old"}]
    assert draft.data["entities"] == [{"name": "old", "last_seen_chapter": 1, "importance": 1}]


def test_trims_old_items_by_chapter_number_or_last_seen_chapter() -> None:
    draft = ContextDraft(
        data={
            "summaries": [
                {"chapter_number": 49, "text": "too old"},
                {"chapter_number": 50, "text": "cutoff"},
                SimpleNamespace(chapter_number=99, text="recent"),
            ],
            "recent_state_changes": [
                {"last_seen_chapter": 12, "text": "too old"},
                {"last_seen_chapter": 75, "text": "recent"},
            ],
            "recent_thread_beats": [
                SimpleNamespace(chapter_number=49, text="too old"),
                SimpleNamespace(chapter_number=51, text="recent"),
            ],
            "recent_events": [
                {"chapter_number": 1, "text": "too old"},
                {"chapter_number": 100, "text": "current"},
            ],
        },
        issues=[],
    )

    issues = RecencyTruncateGate(window_chapters=50).validate(_request(), draft)

    assert issues == []
    assert draft.data["summaries"][0]["text"] == "cutoff"
    assert draft.data["summaries"][1].text == "recent"
    assert [item["text"] for item in draft.data["recent_state_changes"]] == ["recent"]
    assert [item.text for item in draft.data["recent_thread_beats"]] == ["recent"]
    assert [item["text"] for item in draft.data["recent_events"]] == ["current"]


def test_entity_ranking_keeps_recent_then_important_when_capped() -> None:
    draft = ContextDraft(
        data={
            "entities": [
                {"name": "old", "last_seen_chapter": 10, "importance": 1},
                {"name": "important", "last_seen_chapter": 30, "importance": 99},
                {"name": "recent", "last_seen_chapter": 98, "importance": 1},
            ]
        },
        issues=[],
    )

    issues = RecencyTruncateGate(window_chapters=50, max_entities=2).validate(_request(), draft)

    assert issues == []
    assert [item["name"] for item in draft.data["entities"]] == ["recent", "important"]


def test_explicit_empty_gates_list_remains_empty() -> None:
    assembler = ChapterContextAssembler(gates=[])

    assert assembler.gates == []
