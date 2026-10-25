"""World tick steps for intents: the chapter's interactions, due selection and onstage bookkeeping."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from forwin.book_state.extraction.translator import _INTENT_STATUS_BY_EFFECT
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import ACTOR_TIERS, IMPACT_LEVELS
from forwin.protocol.book_state import WorldNode

from .conflict_types import ConflictDraft
from .types import EffectEntityChange, TickCandidate, WorldEffects


@dataclass(frozen=True)
class DueIntents:
    offstage: list[WorldNode]
    onstage: list[WorldNode]


@dataclass(frozen=True)
class OnstageResult:
    candidates: list[TickCandidate]
    # Each holds its slip candidate in proposed_change; that slip is not among the candidates.
    conflicts: list[ConflictDraft]


def apply_intent_interactions(
    runtime: BookStateRuntime, *, interactions: Sequence[Mapping],
) -> list[TickCandidate]:
    """One status change per intent the chapter disrupted or fulfilled; advanced changes nothing.

    ``interactions`` are the chapter_extraction delta's ``metadata["intent_interactions"]``.
    The chapter's last status-changing effect on an intent wins, as in the chapter's own delta;
    an intent already in that status, which the chapter's delta sets, needs no change.
    """
    statuses: dict[str, tuple[str, str]] = {}
    for interaction in interactions:
        intent_id = str(interaction.get("intent_id") or "").strip()
        status = _INTENT_STATUS_BY_EFFECT.get(str(interaction.get("effect") or ""))
        if status is not None and _is_intent(runtime, intent_id):
            statuses[intent_id] = (status, str(interaction.get("evidence") or "").strip())
    return [
        TickCandidate(
            candidate_id=f"interaction:{intent_id}",
            origin="intent_interaction",
            actor_id=_owner(runtime.world.nodes_by_id[intent_id]),
            intent_id=intent_id,
            summary=evidence or _summary(runtime.world.nodes_by_id[intent_id]),
            effects=WorldEffects(),
            intent_status_changes={intent_id: status},
        )
        for intent_id, (status, evidence) in statuses.items()
        if runtime.world.get_state(intent_id).get("status") != status
    ]


def release_held_intents(
    runtime: BookStateRuntime, *, bound_intent_refs: frozenset[str], interactions: Sequence[Mapping], now: int,
) -> list[TickCandidate]:
    """A canon chapter answered its bound holds; settlement takes precedence over release."""
    settled = {str(item.get("intent_id") or "").strip() for item in interactions
               if str(item.get("effect") or "") in _INTENT_STATUS_BY_EFFECT}
    return [TickCandidate(
        candidate_id=f"release:{intent.id}", origin="intent_interaction", intent_id=intent.id,
        actor_id=_owner(intent), summary=_summary(intent),
        intent_status_changes={intent.id: "planned"},
        effects=WorldEffects(entity_changes=[
            EffectEntityChange(ref=intent.id, field="scheduled_at", new_value=now),
        ]),
    ) for intent in _intents(runtime, status="held")
        if intent.id in bound_intent_refs and intent.id not in settled]


def select_due_intents(runtime: BookStateRuntime, *, now: int) -> DueIntents:
    """Planned intents with ``scheduled_at <= now``, by scheduled_at, owner tier and id.

    Onstage ones are left to planning; offstage ones are adjudicated behind the scenes.
    """
    due = [
        node for node in _intents(runtime, status="planned")
        if (scheduled_at := _scheduled_at(runtime, node)) is not None and scheduled_at <= now
    ]
    return DueIntents(
        offstage=[node for node in due if node.profile.get("requires_onstage") is not True],
        onstage=[node for node in due if node.profile.get("requires_onstage") is True],
    )


def due_onstage_intents(runtime: BookStateRuntime) -> list[WorldNode]:
    """Intents an earlier tick handed to planning: ``onstage_bookkeeping``'s ``already_due``."""
    return _intents(runtime, status="due")


def onstage_bookkeeping(
    runtime: BookStateRuntime,
    *,
    onstage_due: Sequence[WorldNode],
    already_due: Sequence[WorldNode],
    interactions: Sequence[Mapping],
    now: int,
    max_slips: int,
    chapter_number: int,
    bound_intent_refs: frozenset[str],
    band_ended: bool,
    band_bound_intent_refs: frozenset[str],
    tick_id: str = "",
) -> OnstageResult:
    """Slip missed plan bindings, or due intents never bound in the band when the band ends.

    Newly due, unbound intents pass to the next band without a same-tick slip (R29).
    A slip counts one more ``slip_count`` and puts ``scheduled_at`` off to now (the tick
    builder writes both). A slip past ``max_slips`` is held in an ``onstage_slip`` conflict
    that blocks the next chapter.
    """
    touched = {str(interaction.get("intent_id") or "").strip() for interaction in interactions}
    newly_due = {intent.id for intent in onstage_due}
    intents = {intent.id: intent for intent in [*onstage_due, *already_due]}
    for ref in sorted(bound_intent_refs):
        node = runtime.world.get_node(ref)
        if (node is not None and str(node.node_type) == "intent"
                and node.profile.get("requires_onstage") is True
                and runtime.world.get_state(ref).get("status") in {"planned", "due"}):
            intents.setdefault(ref, node)
            if runtime.world.get_state(ref).get("status") == "planned" and ref not in touched:
                newly_due.add(ref)
    candidates: list[TickCandidate] = []
    conflicts: list[ConflictDraft] = []
    for intent in intents.values():
        statuses = {intent.id: "due"} if intent.id in newly_due else {}
        should_slip = intent.id not in touched and (
            intent.id in bound_intent_refs
            or (band_ended and intent.id not in newly_due and intent.id not in band_bound_intent_refs)
        )
        if not should_slip:
            if statuses:
                candidates.append(_onstage_candidate(intent, intent_status_changes=statuses))
            continue
        current = runtime.world.get_state(intent.id).get("slip_count")
        slips = (current if type(current) is int else 0) + 1
        candidate = _onstage_candidate(intent, intent_status_changes=statuses, slip_updates={intent.id: slips})
        if slips <= max_slips:
            candidates.append(candidate)
            continue
        owner = _owner(intent)
        impact = intent.profile.get("impact_level")
        conflicts.append(ConflictDraft(
            project_id=runtime.project_id,
            source="world_tick",
            origin_key=tick_id,
            detected_after_chapter=chapter_number,
            story_time=now,
            involved_refs=[ref for ref in (intent.id, owner) if ref],
            violated="onstage_slip",
            impact_level=impact if impact in IMPACT_LEVELS else "minor",
            summary=candidate.summary,
            proposed_change=candidate.model_dump(mode="json"),
            intent_ref=intent.id,
            actor_ref=owner,
            blocks_from_chapter=chapter_number + 1,
        ))
    return OnstageResult(candidates=candidates, conflicts=conflicts)


def _onstage_candidate(
    intent: WorldNode,
    *,
    intent_status_changes: dict[str, str] | None = None,
    slip_updates: dict[str, int] | None = None,
) -> TickCandidate:
    # Bookkeeping keeps the default minor impact: the intent's own impact lands when it is resolved.
    return TickCandidate(
        candidate_id=f"onstage:{intent.id}",
        origin="onstage",
        actor_id=_owner(intent),
        intent_id=intent.id,
        summary=_summary(intent),
        effects=WorldEffects(),
        intent_status_changes=intent_status_changes or {},
        slip_updates=slip_updates or {},
    )


def _intents(runtime: BookStateRuntime, *, status: str) -> list[WorldNode]:
    """Intents in ``status``, by scheduled_at, owner tier (major first) and id."""
    world = runtime.world
    intents = [
        node for node in world.nodes_by_id.values()
        if str(node.node_type) == "intent" and world.get_state(node.id).get("status") == status
    ]

    def order(node: WorldNode) -> tuple[int, int, str]:
        scheduled_at = _scheduled_at(runtime, node)
        tier = world.get_state(_owner(node)).get("actor_tier")
        return (
            scheduled_at if scheduled_at is not None else 0,
            ACTOR_TIERS.index(tier) if tier in ACTOR_TIERS else len(ACTOR_TIERS),
            node.id,
        )

    return sorted(intents, key=order)


def _is_intent(runtime: BookStateRuntime, node_id: str) -> bool:
    node = runtime.world.get_node(node_id)
    return node is not None and str(node.node_type) == "intent"


def _scheduled_at(runtime: BookStateRuntime, node: WorldNode) -> int | None:
    value = runtime.world.get_state(node.id).get("scheduled_at")
    return value if type(value) is int else None


def _owner(intent: WorldNode) -> str:
    return str(intent.profile.get("owner_ref") or "").strip()


def _summary(intent: WorldNode) -> str:
    return str(intent.profile.get("summary") or intent.summary or intent.name).strip()


__all__ = [
    "DueIntents",
    "OnstageResult",
    "apply_intent_interactions",
    "due_onstage_intents",
    "onstage_bookkeeping",
    "release_held_intents",
    "select_due_intents",
]
