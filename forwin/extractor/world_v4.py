from __future__ import annotations

from forwin.extractor.world_v4_rules import (
    find_hint_spans,
    find_offscreen_spans,
    infer_source_type,
)
from forwin.planning.world_contracts import ChapterWorldDeltaIntent
from forwin.protocol.world_v4 import (
    DeltaKind,
    DeltaSource,
    DeltaSourceType,
    ExtractedWorldChangeSet,
    KnowledgeUpdateEvent,
    KnowledgeUpdateType,
    ObserverType,
    ReaderExperienceDelta,
    RevealEvent,
    VisibilityState,
    WorldDelta,
)
from forwin.protocol.writer import WriterOutput


def _state_pair(transition: str) -> tuple[str, str]:
    if "->" not in transition:
        return "", ""
    before, after = transition.split("->", 1)
    return before.strip(), after.strip()


def _visibility(value: str) -> VisibilityState:
    normalized = str(value or "").strip()
    for candidate in VisibilityState:
        if candidate.value == normalized:
            return candidate
    return VisibilityState.UNKNOWN


class WorldDeltaExtractor:
    """Deterministic v4 extractor for writer output and chapter intent."""

    def extract(
        self,
        writer_output: WriterOutput,
        *,
        chapter_intent: ChapterWorldDeltaIntent | None = None,
    ) -> ExtractedWorldChangeSet:
        project_id = writer_output.project_id or (
            chapter_intent.project_id if chapter_intent is not None else ""
        )
        chapter_number = writer_output.chapter_number
        source_refs = ["writer_output:body"]
        if chapter_intent is not None:
            source_refs.append(f"chapter_intent:{chapter_intent.intent_id}")

        world_deltas = list(writer_output.world_deltas)
        reveal_events = list(writer_output.reveal_events)
        knowledge_update_events = []
        reader_experience_deltas = list(writer_output.reader_experience_deltas)
        hint_spans = find_hint_spans(writer_output.body)
        offscreen_spans = find_offscreen_spans(writer_output.body)

        if chapter_intent is not None and not world_deltas:
            if chapter_intent.visible_delta_intents:
                visible_summary = "；".join(chapter_intent.visible_delta_intents)
                world_deltas.append(
                    WorldDelta(
                        delta_id=f"delta_ch{chapter_number}_visible",
                        project_id=project_id,
                        world_line_id="line_colony_defense",
                        delta_kind=DeltaKind.VISIBLE,
                        summary=visible_summary,
                        narrative_chapter=chapter_number,
                        source=DeltaSource(source_type=infer_source_type(visible_summary)),
                        source_refs=["chapter_intent:visible_delta_intents"],
                    )
                )
            if chapter_intent.hint_delta_intents:
                hint_summary = "；".join(chapter_intent.hint_delta_intents)
                hint_refs = [span.source_ref for span in hint_spans] or [
                    "chapter_intent:hint_delta_intents",
                    "writer_output:body",
                ]
                world_deltas.append(
                    WorldDelta(
                        delta_id=f"delta_ch{chapter_number}_hint",
                        project_id=project_id,
                        world_line_id="line_homeworld_siege",
                        delta_kind=DeltaKind.HINT,
                        summary=hint_summary,
                        narrative_chapter=chapter_number,
                        source=DeltaSource(source_type=infer_source_type(hint_summary)),
                        source_refs=hint_refs,
                    )
                )
            if chapter_intent.offscreen_delta_intents or offscreen_spans:
                offscreen_summary = "；".join(chapter_intent.offscreen_delta_intents) or "；".join(
                    span.text for span in offscreen_spans
                )
                offscreen_refs = [span.source_ref for span in offscreen_spans] or [
                    "chapter_intent:offscreen_delta_intents",
                    "writer_output:body",
                ]
                world_deltas.append(
                    WorldDelta(
                        delta_id=f"delta_ch{chapter_number}_offscreen",
                        project_id=project_id,
                        world_line_id="line_homeworld_siege",
                        delta_kind=DeltaKind.OFFSCREEN,
                        summary=offscreen_summary,
                        narrative_chapter=chapter_number,
                        source=DeltaSource(source_type=infer_source_type(offscreen_summary)),
                        source_refs=offscreen_refs,
                    )
                )

        reader_transition = ""
        if chapter_intent is not None:
            reader_transition = chapter_intent.expected_observer_state_changes.get("reader", "")
        before, after = _state_pair(reader_transition)
        if chapter_intent is not None and chapter_intent.hint_delta_intents and not reveal_events:
            reveal_events.append(
                RevealEvent(
                    reveal_event_id=f"reveal_ch{chapter_number}_hint",
                    project_id=project_id,
                    related_gap_id=(
                        "gap_homeworld_siege"
                        if "father_sieged" in chapter_intent.must_not_reveal
                        else ""
                    ),
                    reveal_to_reader=True,
                    reveal_to_characters=["protagonist"]
                    if "protagonist" in chapter_intent.expected_observer_state_changes
                    else [],
                    reveal_method="hint",
                    from_state=_visibility(before),
                    to_state=_visibility(after),
                    narrative_function="按 ChapterWorldDeltaIntent 执行公平 hint",
                    fairness_evidence=list(chapter_intent.hint_delta_intents),
                    source_refs=["chapter_intent:hint_delta_intents", "writer_output:body"],
                )
            )
            knowledge_update_events.append(
                KnowledgeUpdateEvent(
                    update_event_id=f"knowledge_ch{chapter_number}_reader_hint",
                    project_id=project_id,
                    update_type=KnowledgeUpdateType.HINT,
                    observer_type=ObserverType.READER,
                    observer_id="reader",
                    related_gap_id=(
                        "gap_homeworld_siege"
                        if "father_sieged" in chapter_intent.must_not_reveal
                        else ""
                    ),
                    from_state=_visibility(before),
                    to_state=_visibility(after),
                    evidence_refs=list(chapter_intent.hint_delta_intents),
                    chapter_number=chapter_number,
                )
            )
            if not reader_experience_deltas:
                reader_experience_deltas.append(
                    ReaderExperienceDelta(
                        reader_experience_delta_id=f"reader_exp_ch{chapter_number}_hint",
                        project_id=project_id,
                        chapter_number=chapter_number,
                        reader_state_before=before,
                        reader_state_after=after,
                        cognition_transition=reader_transition,
                        payoff_type="short_term_hint",
                        reward_tags=["mystery"],
                        emotional_effect="不安与追问",
                        next_desire="异常通讯背后的真实危机是什么",
                        fairness_evidence=list(chapter_intent.hint_delta_intents),
                        source_refs=["chapter_intent:expected_observer_state_changes"],
                    )
                )

        return ExtractedWorldChangeSet(
            project_id=project_id,
            chapter_number=chapter_number,
            world_deltas=world_deltas,
            belief_updates=list(writer_output.belief_updates),
            knowledge_gap_updates=list(writer_output.knowledge_gap_updates),
            reveal_events=reveal_events,
            knowledge_update_events=knowledge_update_events,
            reader_experience_deltas=reader_experience_deltas,
            source_refs=source_refs,
            metadata={
                "observer_visibility_updates": dict(writer_output.observer_visibility_updates),
                "must_preserve_facts": list(writer_output.must_preserve_facts),
                "must_not_reveal_violations": list(writer_output.must_not_reveal_violations),
            },
        )
