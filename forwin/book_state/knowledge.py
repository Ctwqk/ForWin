from __future__ import annotations

from dataclasses import dataclass

from forwin.protocol.book_state import CognitionPatch, ObserverType

KNOWLEDGE_STATES = ("heard", "suspects", "believes", "knows", "disbelieves", "misled")
KNOWLEDGE_CHANNELS = ("witnessed", "told", "rumor", "document", "inferred", "seeded")
HIDDEN_SENSITIVITY = frozenset({"hidden", "secret", "must_not_reveal"})
FACT_REF_PREFIX = "fact:"


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


def knowledge_event_to_patch(event: KnowledgeEvent) -> CognitionPatch:
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
        new_value={event.fact_ref: event.state},
        evidence_refs=[knowledge_evidence_ref(event)],
    )


__all__ = [
    "FACT_REF_PREFIX",
    "HIDDEN_SENSITIVITY",
    "KNOWLEDGE_CHANNELS",
    "KNOWLEDGE_STATES",
    "KnowledgeEvent",
    "knowledge_event_to_patch",
    "knowledge_evidence_ref",
]
