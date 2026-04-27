from __future__ import annotations

from typing import Any

from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionPatch,
    FactPatch,
    GraphDelta,
    GraphDeltaType,
    NarrativePatch,
    NodePatch,
)
from forwin.protocol.world_v4 import (
    ApprovedWorldChangeSet,
    Belief,
    BeliefStatus,
    ExtractedWorldChangeSet,
    KnowledgeGap,
    ReaderExperienceDelta,
    RevealEvent,
    VisibilityState,
    WorldDelta,
)


class BookStateDeltaAdapter:
    """Bridge the existing V4 extraction result into append-only BookState patches."""

    def from_world_change_set(
        self,
        changes: ExtractedWorldChangeSet | ApprovedWorldChangeSet,
        *,
        approved_by: list[str] | None = None,
        review_verdict_id: str = "",
        forced_accept_reason: str = "",
    ) -> ApprovedGraphDeltaSet:
        deltas: list[GraphDelta] = []
        for world_delta in changes.world_deltas:
            deltas.append(self._world_delta(world_delta))
        for belief in changes.belief_updates:
            deltas.append(self._belief_delta(changes.project_id, changes.chapter_number, belief))
        for gap in changes.knowledge_gap_updates:
            deltas.append(self._gap_delta(changes.project_id, changes.chapter_number, gap))
        for reveal in changes.reveal_events:
            deltas.append(self._reveal_delta(changes.project_id, changes.chapter_number, reveal))
        for reader_delta in changes.reader_experience_deltas:
            deltas.append(self._reader_experience_delta(reader_delta))
        verdict_id = review_verdict_id or getattr(changes, "review_verdict_id", "")
        if verdict_id:
            deltas = [delta.model_copy(update={"review_verdict_id": verdict_id}) for delta in deltas]

        return ApprovedGraphDeltaSet(
            project_id=changes.project_id,
            chapter_number=changes.chapter_number,
            graph_deltas=deltas,
            approved_by=approved_by or getattr(changes, "approved_by", []),
            review_verdict_id=verdict_id,
            forced_accept_reason=forced_accept_reason or getattr(changes, "forced_accept_reason", ""),
        )

    def _world_delta(self, delta: WorldDelta) -> GraphDelta:
        event_id = f"event_{delta.delta_id}"
        fact_id = f"fact_{delta.delta_id}"
        related_refs = [
            *(f"node:{item}" for item in delta.affected_entities),
            *(f"node:{item}" for item in delta.affected_factions),
            *(f"map_node:{item}" for item in delta.affected_locations),
            *(f"node:{item}" for item in delta.affected_resources),
            *(f"node:{item}" for item in delta.affected_rules),
        ]
        cognition_patches: list[CognitionPatch] = []
        for observer_state in delta.observer_states.values():
            field_path = _visibility_field_path(str(observer_state.visibility))
            if field_path:
                cognition_patches.append(
                    CognitionPatch(
                        observer_type=str(observer_state.observer_type),
                        observer_id=observer_state.observer_id,
                        op="append",
                        field_path=field_path,
                        new_value=f"fact:{fact_id}",
                        reason=delta.summary,
                        evidence_refs=list(delta.source_refs),
                    )
                )
        return GraphDelta(
            id=f"book_delta_{delta.delta_id}",
            project_id=delta.project_id,
            chapter_number=delta.narrative_chapter or 0,
            story_time=delta.objective_story_time,
            delta_type=GraphDeltaType.WORLD_STATE,
            operation="create_event_fact",
            target_type="world_delta",
            target_id=delta.delta_id,
            source_type=str(delta.source.source_type),
            source_id=delta.delta_id,
            world_line_id=delta.world_line_id,
            summary=delta.summary,
            node_patches=[
                NodePatch(
                    node_id=event_id,
                    node_type="event",
                    op="create",
                    new_value={
                        "project_id": delta.project_id,
                        "name": delta.summary[:80],
                        "description": delta.summary,
                        "created_at_chapter": delta.narrative_chapter or 0,
                        "profile": {
                            "event_type": str(delta.delta_kind),
                            "related_world_line_id": delta.world_line_id,
                            "planned_or_actual": "actual",
                            "expected_chapter": delta.narrative_chapter,
                            "expected_story_time": delta.objective_story_time,
                            "involved_node_refs": related_refs,
                            "narrative_function": str(delta.delta_kind),
                            "public_summary": delta.summary,
                        },
                        "state": {
                            "status": "occurred",
                            "actual_chapter": delta.narrative_chapter or 0,
                            "actual_story_time": delta.objective_story_time,
                            "participant_ids": delta.affected_entities,
                            "result_refs": [f"fact:{fact_id}"],
                            "visibility_level": str(delta.delta_kind),
                            "evidence_refs": list(delta.source_refs),
                            "state_summary": delta.summary,
                        },
                        "metadata": {"source": delta.model_dump(mode="json")},
                    },
                    reason=delta.summary,
                )
            ],
            fact_patches=[
                FactPatch(
                    fact_id=fact_id,
                    op="create",
                    proposition=delta.summary,
                    truth_value="true" if delta.allowed_for_canon else "unknown",
                    related_refs=[f"node:{event_id}", *related_refs],
                    new_value={
                        "project_id": delta.project_id,
                        "proposition": delta.summary,
                        "fact_type": str(delta.delta_kind),
                        "truth_value": "true" if delta.allowed_for_canon else "unknown",
                        "confidence": 1.0 if delta.allowed_for_canon else 0.5,
                        "related_node_refs": [ref for ref in related_refs if ref.startswith("node:")],
                        "source_refs": list(delta.source_refs),
                        "created_at_chapter": delta.narrative_chapter or 0,
                        "happened_at_story_time": delta.objective_story_time,
                        "narrative_function": str(delta.delta_kind),
                        "state": {
                            "status": "active",
                            "public_visibility": str(delta.delta_kind),
                            "last_updated_chapter": delta.narrative_chapter or 0,
                            "canonicality_level": "canon" if delta.allowed_for_canon else "candidate",
                            "state_summary": delta.summary,
                        },
                    },
                    reason=delta.summary,
                )
            ],
            cognition_patches=cognition_patches,
            narrative_patches=[
                NarrativePatch(
                    target_ref=f"world_line:{delta.world_line_id}",
                    op="create",
                    new_value={
                        "node_type": "world_line",
                        "title": delta.world_line_id,
                        "status": "active",
                        "payload": {"last_delta_id": delta.delta_id, "summary": delta.summary},
                    },
                    reason=delta.summary,
                ),
                NarrativePatch(
                    target_ref=f"world_line:{delta.world_line_id}",
                    op="set",
                    field_path="payload.latest_summary",
                    new_value=delta.summary,
                    reason=delta.summary,
                ),
            ],
            evidence_refs=list(delta.source_refs),
            allowed_for_canon=bool(delta.allowed_for_canon),
        )

    def _belief_delta(self, project_id: str, chapter_number: int, belief: Belief) -> GraphDelta:
        fact_id = f"fact_{belief.belief_id}"
        field_path = _belief_field_path(str(belief.belief_status))
        cognition_patches = []
        if field_path:
            cognition_patches.append(
                CognitionPatch(
                    observer_type=str(belief.holder_type),
                    observer_id=belief.holder_id,
                    op="append",
                    field_path=field_path,
                    new_value=f"fact:{fact_id}",
                    reason=belief.proposition,
                    evidence_refs=list(belief.evidence_sources),
                )
            )
        if str(belief.truth_relation) == "false":
            cognition_patches.append(
                CognitionPatch(
                    observer_type=str(belief.holder_type),
                    observer_id=belief.holder_id,
                    op="merge",
                    field_path="false_facts",
                    new_value={
                        fact_id: {
                            "id": fact_id,
                            "project_id": project_id,
                            "proposition": belief.proposition,
                            "truth_value": "false",
                            "confidence": belief.confidence,
                            "source_refs": list(belief.evidence_sources),
                        }
                    },
                    reason=belief.proposition,
                    evidence_refs=list(belief.evidence_sources),
                )
            )
        return GraphDelta(
            id=f"book_delta_{belief.belief_id}",
            project_id=project_id,
            chapter_number=chapter_number,
            story_time=belief.created_at_story_time,
            delta_type=GraphDeltaType.COGNITION,
            source_type="belief",
            source_id=belief.belief_id,
            summary=belief.proposition,
            fact_patches=[
                FactPatch(
                    fact_id=fact_id,
                    op="create",
                    proposition=belief.proposition,
                    truth_value=str(belief.truth_relation),
                    new_value={
                        "project_id": project_id,
                        "proposition": belief.proposition,
                        "fact_type": "belief",
                        "truth_value": str(belief.truth_relation),
                        "confidence": belief.confidence,
                        "source_refs": list(belief.evidence_sources),
                        "created_at_chapter": belief.created_at_chapter or chapter_number,
                        "happened_at_story_time": belief.created_at_story_time,
                        "contradiction_refs": list(belief.contradicted_by),
                        "state": {
                            "status": str(belief.belief_status),
                            "last_updated_chapter": belief.last_updated_at_chapter or chapter_number,
                            "state_summary": belief.proposition,
                        },
                    },
                    reason=belief.proposition,
                )
            ],
            cognition_patches=cognition_patches,
            evidence_refs=list(belief.evidence_sources),
        )

    def _gap_delta(self, project_id: str, chapter_number: int, gap: KnowledgeGap) -> GraphDelta:
        return GraphDelta(
            id=f"book_delta_{gap.gap_id}",
            project_id=project_id,
            chapter_number=chapter_number,
            story_time=gap.happened_at_story_time,
            delta_type=GraphDeltaType.NARRATIVE_CONTROL,
            source_type="knowledge_gap",
            source_id=gap.gap_id,
            world_line_id=gap.related_world_line_id,
            summary=gap.objective_truth,
            narrative_patches=[
                NarrativePatch(
                    target_ref=f"knowledge_gap:{gap.gap_id}",
                    op="create",
                    new_value={
                        "node_type": "knowledge_gap",
                        "title": gap.objective_truth[:80],
                        "status": str(gap.status),
                        "payload": gap.model_dump(mode="json"),
                    },
                    reason=gap.objective_truth,
                )
            ],
            evidence_refs=list(gap.source_refs),
        )

    def _reveal_delta(self, project_id: str, chapter_number: int, reveal: RevealEvent) -> GraphDelta:
        cognition_patches: list[CognitionPatch] = []
        if reveal.reveal_to_reader:
            cognition_patches.append(
                CognitionPatch(
                    observer_type="reader",
                    observer_id="reader",
                    op="append",
                    field_path=_visibility_field_path(str(reveal.to_state)) or "visible_refs",
                    new_value=f"fact:{reveal.reveals_fact_id or reveal.related_gap_id}",
                    reason=reveal.reveal_method,
                    evidence_refs=list(reveal.fairness_evidence),
                )
            )
        for character_id in reveal.reveal_to_characters:
            cognition_patches.append(
                CognitionPatch(
                    observer_type="character",
                    observer_id=character_id,
                    op="append",
                    field_path=_visibility_field_path(str(reveal.to_state)) or "visible_refs",
                    new_value=f"fact:{reveal.reveals_fact_id or reveal.related_gap_id}",
                    reason=reveal.reveal_method,
                    evidence_refs=list(reveal.fairness_evidence),
                )
            )
        return GraphDelta(
            id=f"book_delta_{reveal.reveal_event_id}",
            project_id=project_id,
            chapter_number=chapter_number,
            delta_type=GraphDeltaType.NARRATIVE_CONTROL,
            source_type="reveal_event",
            source_id=reveal.reveal_event_id,
            summary=reveal.reveal_method or reveal.narrative_function,
            cognition_patches=cognition_patches,
            narrative_patches=[
                NarrativePatch(
                    target_ref=f"reveal_plan:{reveal.reveal_event_id}",
                    op="create",
                    new_value={
                        "node_type": "reveal_plan",
                        "title": reveal.reveal_method or reveal.reveal_event_id,
                        "status": "resolved",
                        "payload": reveal.model_dump(mode="json"),
                    },
                    reason=reveal.reveal_method,
                )
            ],
            evidence_refs=list(reveal.source_refs),
        )

    def _reader_experience_delta(self, delta: ReaderExperienceDelta) -> GraphDelta:
        return GraphDelta(
            id=f"book_delta_{delta.reader_experience_delta_id}",
            project_id=delta.project_id,
            chapter_number=delta.chapter_number,
            delta_type=GraphDeltaType.NARRATIVE_CONTROL,
            operation="update_reader_promise",
            target_type="reader_experience",
            target_id=delta.reader_experience_delta_id,
            source_type="reader_experience_delta",
            source_id=delta.reader_experience_delta_id,
            summary=delta.reader_state_after or delta.next_desire,
            narrative_patches=[
                NarrativePatch(
                    target_ref=f"promise:{delta.reader_experience_delta_id}",
                    op="create",
                    new_value={
                        "node_type": "promise",
                        "title": delta.payoff_type or delta.reader_experience_delta_id,
                        "status": "active" if delta.promise_debt_change > 0 else "resolved",
                        "payload": delta.model_dump(mode="json"),
                    },
                    reason=delta.reader_state_after,
                )
            ],
            evidence_refs=list(delta.source_refs),
            metadata={"reader_experience_delta": delta.model_dump(mode="json")},
        )


def _visibility_field_path(visibility: str) -> str:
    if visibility in {VisibilityState.CONFIRMED.value, "confirmed"}:
        return "confirmed_refs"
    if visibility in {
        VisibilityState.SUSPECTED.value,
        VisibilityState.HINTED.value,
        VisibilityState.PARTIALLY_KNOWN.value,
        VisibilityState.PARTIALLY_REVEALED.value,
        "suspected",
        "hinted",
        "partially_known",
        "partially_revealed",
    }:
        return "suspected_refs"
    if visibility in {VisibilityState.KNOWN.value, "known"}:
        return "visible_refs"
    if visibility in {VisibilityState.HIDDEN.value, VisibilityState.UNKNOWN.value, "hidden", "unknown"}:
        return "hidden_refs"
    return "visible_refs"


def _belief_field_path(status: str) -> str:
    if status in {BeliefStatus.CONFIRMED.value, "confirmed"}:
        return "confirmed_refs"
    if status in {BeliefStatus.SUSPECTED.value, BeliefStatus.DISPUTED.value, "suspected", "disputed"}:
        return "suspected_refs"
    if status in {BeliefStatus.REJECTED.value, "rejected"}:
        return "hidden_refs"
    return "visible_refs"


__all__ = ["BookStateDeltaAdapter"]
