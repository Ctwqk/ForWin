from types import SimpleNamespace

import pytest

from forwin.protocol.writer import WriterOutput


def _writer(*, timeout=False):
    calls = []

    class Writer:
        max_tokens = 8000
        scene_call_timeout_seconds = 30

        def _chat_json(self, messages, **kwargs):
            calls.append((messages, kwargs))
            if timeout:
                raise TimeoutError("full body extraction timeout")
            return {
                "state_changes": [],
                "new_events": [],
                "delivered_payoffs": [],
                "thread_beats": [],
                "time_advance": None,
                "lore_candidates": [],
                "timeline_hints": [],
                "writer_notes": [],
                "entity_mentions": [],
                "end_of_chapter_summary": "The archive remains still.",
            }

        def _writer_output_from_dict(self, context, payload):
            return WriterOutput(
                project_id=context.project_id,
                chapter_number=context.chapter_number,
                title=payload["title"],
                body=payload["body"],
                end_of_chapter_summary=payload.get("end_of_chapter_summary", ""),
            )

    return Writer(), calls


def test_historical_extraction_calls_all_existing_parts_with_entire_body(monkeypatch):
    from forwin.canon import revision_body
    from forwin.canon.revision_body import extract_revision_body

    writer, calls = _writer()
    monkeypatch.setattr(
        revision_body,
        "_extraction_parts",
        lambda: [
            (name, lambda context, title, body: [{"role": "user", "content": body}], {})
            for name in (
                "state_event_extraction",
                "thread_time_extraction",
                "lore_timeline_notes_extraction",
            )
        ],
    )
    body = "Beginning. " + "Middle fact. " * 500 + "End."
    output = extract_revision_body(
        writer=writer,
        context=SimpleNamespace(project_id="p", chapter_number=2),
        title="Same title",
        body=body,
    )
    assert len(calls) == 3
    assert all(body in messages[0]["content"] for messages, kwargs in calls)
    assert output.body == body
    assert output.generation_meta["historical_full_body_extraction"] is True


def test_historical_extraction_never_falls_back_to_short_windows(monkeypatch):
    from forwin.canon import revision_body
    from forwin.canon.revision_body import extract_revision_body

    writer, calls = _writer(timeout=True)
    monkeypatch.setattr(
        revision_body,
        "_extraction_parts",
        lambda: [
            (
                "state_event_extraction",
                lambda context, title, body: [{"role": "user", "content": body}],
                {},
            )
        ],
    )
    with pytest.raises(TimeoutError):
        extract_revision_body(
            writer=writer,
            context=SimpleNamespace(project_id="p", chapter_number=2),
            title="Title",
            body="Complete body" * 500,
        )
    assert len(calls) == 1
