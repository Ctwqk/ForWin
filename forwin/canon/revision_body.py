"""Strict reuse of the writer's extraction prompts for immutable historical text."""

from __future__ import annotations

from forwin.protocol.state_change import (
    DeliveredPayoffCandidate,
    EventCandidate,
    StateChangeCandidate,
    ThreadBeatCandidate,
    TimeAdvance,
)
from forwin.protocol.subworld import EntityMention
from forwin.protocol.writer import LoreCandidate, TimelineHint, WriterNote


def _extraction_parts():
    from forwin.writer.prompt_core.extraction import (
        build_lore_timeline_notes_extraction_prompt,
        build_state_event_extraction_prompt,
        build_thread_time_extraction_prompt,
    )

    return (
        (
            "state_event_extraction",
            build_state_event_extraction_prompt,
            {
                "state_changes": StateChangeCandidate,
                "new_events": EventCandidate,
                "delivered_payoffs": DeliveredPayoffCandidate,
            },
        ),
        (
            "thread_time_extraction",
            build_thread_time_extraction_prompt,
            {"thread_beats": ThreadBeatCandidate},
        ),
        (
            "lore_timeline_notes_extraction",
            build_lore_timeline_notes_extraction_prompt,
            {
                "lore_candidates": LoreCandidate,
                "timeline_hints": TimelineHint,
                "writer_notes": WriterNote,
                "entity_mentions": EntityMention,
            },
        ),
    )


def extract_revision_body(*, writer, context, title: str, body: str):
    """No body-window fallback, old extraction metadata or generated replacement text."""
    extracted = {}
    for name, builder, models in _extraction_parts():
        messages = builder(context, title, body)
        messages.append(
            {
                "role": "user",
                "content": "This is a historical revision validation. Extract from the ENTIRE supplied chapter. "
                "Do not shorten the body or limit the number of facts/entities to the ordinary writing caps. "
                "Return all requested arrays explicitly; an empty array means no matching observations.",
            }
        )
        if name == "lore_timeline_notes_extraction":
            messages[-1]["content"] += (
                ' Also return "end_of_chapter_summary" as a nonempty string summarizing '
                "the ENTIRE supplied body for subsequent chapters. Include its key events, "
                "outcomes and continuity facts; do not reuse the old draft summary or the plan."
            )
        value = writer._chat_json(
            messages,
            temperature=0.2,
            max_tokens=writer.max_tokens,
            timeout_seconds=writer.scene_call_timeout_seconds,
            max_attempts=1,
            retry_on_timeout=False,
            stage_key=name,
            required_list_models=models,
        )
        for field, model in models.items():
            if not isinstance(value.get(field), list):
                raise TypeError(f"incomplete historical extraction: {name}:{field}")
            for item in value[field]:
                model.model_validate(item)
        if name == "thread_time_extraction":
            if "time_advance" not in value:
                raise ValueError("incomplete historical extraction: time_advance")
            if value["time_advance"] is not None:
                TimeAdvance.model_validate(value["time_advance"])
        if name == "lore_timeline_notes_extraction":
            require_revision_summary(value.get("end_of_chapter_summary"))
        extracted.update(value)
    output = writer._writer_output_from_dict(
        context, {**extracted, "title": title, "body": body}
    )
    if output.body != body or output.title != title:
        raise ValueError("historical extraction changed immutable body/title")
    output.generation_meta = {
        "historical_full_body_extraction": True,
        "structured_extraction": "completed",
    }
    return output


def require_revision_summary(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("incomplete historical extraction: end_of_chapter_summary")
    return value
