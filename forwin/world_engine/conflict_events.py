"""World conflict drafts and their detection event, in one shape for every source.

The caller stores the conflict and adds the event inside its own transaction.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from typing import Any

from forwin.audit.events import DecisionEventType
from forwin.book_state.schema import IMPACT_LEVELS
from forwin.models.audit import DecisionEvent
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import GraphDelta

from .conflict_types import ConflictDecider, ConflictDraft, ConflictOption


# A rewrite cannot replay the tick, so a human vetoes what the world did.
REPLAY_CAS_DECIDER: ConflictDecider = "human"
REPLAY_CAS_OPTIONS: tuple[ConflictOption, ...] = ("veto_world",)


def highest_candidate_impact(delta: GraphDelta) -> str:
    """The highest impact among a tick delta's candidates; "moderate" without any."""
    impacts = delta.metadata.get("candidate_impacts")
    ranks = (
        [IMPACT_LEVELS.index(level) for level in impacts.values() if level in IMPACT_LEVELS]
        if isinstance(impacts, dict)
        else []
    )
    return IMPACT_LEVELS[max(ranks)] if ranks else "moderate"


def replay_cas_conflict_draft(
    delta: GraphDelta,
    *,
    rewritten_chapter: int,
    reasons: Sequence[str],
    involved_refs: Sequence[str],
    blocks_from_chapter: int,
    superseded_conflict_ids: Sequence[str] = (),
    cancelled_obligation_ids: Sequence[str] = (),
    reverted_plan_patch_ids: Sequence[str] = (),
) -> ConflictDraft:
    """The conflict for a tick delta a historical rewrite could no longer replay.

    The ``superseded_conflict_ids`` are the resolved conflicts the delta carried; the obligations
    and plan patches are their side effects the rewrite cancelled and reverted.
    """
    tick_id = str(delta.metadata.get("tick_id") or "")
    summary = (
        f"Rewriting chapter {rewritten_chapter} changed what world tick {tick_id} read "
        f"({'; '.join(reasons)}); its delta {delta.id} no longer applies."
    )
    if superseded_conflict_ids:
        summary += (
            f" It supersedes {len(superseded_conflict_ids)} resolved conflict(s): "
            f"{len(cancelled_obligation_ids)} obligation(s) cancelled, "
            f"{len(reverted_plan_patch_ids)} plan patch(es) reverted."
        )
    return ConflictDraft(
        project_id=delta.project_id,
        source="historical_rewrite",
        detected_after_chapter=delta.chapter_number,
        story_time=delta.story_time_end or 0,
        involved_refs=list(dict.fromkeys(involved_refs)),
        violated="replay_cas",
        impact_level=highest_candidate_impact(delta),
        summary=summary,
        proposed_change={
            "graph_delta_id": delta.id,
            "tick_id": tick_id,
            "superseded_conflict_ids": list(superseded_conflict_ids),
            "cancelled_obligation_ids": list(cancelled_obligation_ids),
            "reverted_plan_patch_ids": list(reverted_plan_patch_ids),
        },
        blocks_from_chapter=blocks_from_chapter,
    )


def conflict_detected_payload(
    row: WorldPlanConflictRow, *, extra: Mapping[str, Any] | None = None
) -> dict[str, Any]:
    """The WORLD_CONFLICT_DETECTED payload: the stored conflict, then what its source adds."""
    return {
        "conflict_id": row.id,
        "source": row.source,
        "violated": row.violated,
        "impact_level": row.impact_level,
        "required_decider": row.required_decider,
        "blocks_from_chapter": row.blocks_from_chapter,
        "proposed_change": json.loads(row.proposed_change_json or "{}"),
        **(extra or {}),
    }


def conflict_detected_event(
    row: WorldPlanConflictRow, *, chapter_number: int, extra: Mapping[str, Any] | None = None
) -> DecisionEvent:
    """An unsaved WORLD_CONFLICT_DETECTED event for a stored conflict."""
    return DecisionEvent(
        project_id=row.project_id,
        chapter_number=chapter_number,
        scope="chapter",
        event_family="business_event",
        event_type=DecisionEventType.WORLD_CONFLICT_DETECTED,
        actor_type="system",
        summary=row.summary,
        payload_json=json.dumps(
            conflict_detected_payload(row, extra=extra), ensure_ascii=False, sort_keys=True
        ),
        related_object_type="world_conflict",
        related_object_id=row.id,
    )


__all__ = [
    "REPLAY_CAS_DECIDER",
    "REPLAY_CAS_OPTIONS",
    "conflict_detected_event",
    "conflict_detected_payload",
    "highest_candidate_impact",
    "replay_cas_conflict_draft",
]
