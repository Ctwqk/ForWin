from __future__ import annotations

from collections.abc import Collection, Mapping
from dataclasses import dataclass
from typing import TYPE_CHECKING

from forwin.protocol.book_state import CognitionPatch, ObserverType, WorldNode

if TYPE_CHECKING:
    from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph

KNOWLEDGE_STATES = ("heard", "suspects", "believes", "knows", "disbelieves", "misled")
KNOWLEDGE_CHANNELS = ("witnessed", "told", "rumor", "document", "inferred", "seeded")
HIDDEN_SENSITIVITY = frozenset({"hidden", "secret", "must_not_reveal"})
EVENT_FACT_TYPE = "event"
FACT_REF_PREFIX = "fact:"
READER_OBSERVER = ObserverType.READER.value
READER_OVERLAY_KEY = (READER_OBSERVER, READER_OBSERVER)
READER_KNOWN_STATES = frozenset({"believes", "knows"})


def is_secret_fact(sensitivity: str, *, fact_id: str = "", protected: Collection[str] = ()) -> bool:
    """Whether writer-facing handling must treat a fact as secret."""
    return sensitivity in HIDDEN_SENSITIVITY or fact_id in protected


def observer_overlay_key(runtime: BookStateRuntime, node_id: str) -> tuple[str, str] | None:
    """The cognition overlay of a world node; ids are unique across observer types."""
    return next((key for key in runtime.cognition_by_observer
                 if key[1] == node_id and key[0] != READER_OBSERVER), None)


def is_concealed(world: ObjectiveWorldGraph, node: WorldNode) -> bool:
    level = world.get_state(node.id).get("visibility_level") or node.profile.get("visibility_level")
    return is_secret_fact(str(level or ""))


@dataclass(frozen=True)
class KnowledgeEvent:
    """One observer coming to hold a state about one fact; the only way cognition is written."""

    observer_type: str
    observer_id: str
    fact_ref: str
    state: str
    channel: str
    source_ref: str = ""
    at: int | None = None


def knowledge_evidence_ref(event: KnowledgeEvent) -> str:
    at = event.at if event.at is not None else "-"
    return f"knowledge:{event.channel}:{event.source_ref or '-'}:{at}"


def knowledge_event_to_patch(
    event: KnowledgeEvent, *, knowledge_by_ref: Mapping[str, str],
) -> CognitionPatch:
    """Compare the touched ref against the observer's pre-delta recorded knowledge."""
    if event.state not in KNOWLEDGE_STATES:
        raise ValueError(f"unknown knowledge state: {event.state!r}")
    if event.channel not in KNOWLEDGE_CHANNELS:
        raise ValueError(f"unknown knowledge channel: {event.channel!r}")
    if not event.fact_ref.startswith(FACT_REF_PREFIX):
        raise ValueError(f"knowledge fact_ref must start with {FACT_REF_PREFIX!r}: {event.fact_ref!r}")
    if event.observer_type not in {observer.value for observer in ObserverType}:
        raise ValueError(f"unknown observer type: {event.observer_type!r}")
    return CognitionPatch(
        observer_type=event.observer_type,
        observer_id=event.observer_id,
        op="merge",
        field_path="knowledge_by_ref",
        old_value={event.fact_ref: knowledge_by_ref.get(event.fact_ref)},
        new_value={event.fact_ref: event.state},
        evidence_refs=[knowledge_evidence_ref(event)],
    )


__all__ = [
    "EVENT_FACT_TYPE",
    "READER_OBSERVER",
    "READER_OVERLAY_KEY",
    "READER_KNOWN_STATES",
    "is_secret_fact",
    "observer_overlay_key",
    "FACT_REF_PREFIX",
    "HIDDEN_SENSITIVITY",
    "KNOWLEDGE_CHANNELS",
    "KNOWLEDGE_STATES",
    "KnowledgeEvent",
    "is_concealed",
    "knowledge_event_to_patch",
    "knowledge_evidence_ref",
]
