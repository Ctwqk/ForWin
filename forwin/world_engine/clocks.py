"""World tick step: running clocks that fall due expire."""

from __future__ import annotations

from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.protocol.book_state import WorldNode

from .types import EffectEntityChange, EffectEvent, TickCandidate, WorldEffects


def expire_clocks(runtime: BookStateRuntime, *, now: int, window_start: int | None = None) -> list[TickCandidate]:
    """One candidate per running clock with ``due_at <= now``, in due_at then id order.

    The clock's status becomes expired. An ``on_expire`` naming an intent node brings that intent
    forward to now; any other ``on_expire`` text becomes an event of the label and that text,
    public or secret by the clock's scope, with the owner taking part. With the tick's
    ``window_start`` the event happens when the clock ran out, but no earlier than the window;
    without it, at now.
    """
    world = runtime.world
    due: list[tuple[int, str, WorldNode]] = []
    for node in world.nodes_by_id.values():
        if str(node.node_type) != "clock":
            continue
        state = world.get_state(node.id)
        due_at = state.get("due_at")
        if state.get("status") == "running" and type(due_at) is int and due_at <= now:
            due.append((due_at, node.id, node))
    return [
        _expire(runtime, clock, now, at=now if window_start is None else max(due_at, window_start))
        for due_at, _clock_id, clock in sorted(due, key=lambda item: item[:2])
    ]


def _expire(runtime: BookStateRuntime, clock: WorldNode, now: int, *, at: int) -> TickCandidate:
    world = runtime.world
    label = str(clock.profile.get("label") or clock.name).strip()
    on_expire = str(clock.profile.get("on_expire") or "").strip()
    changes = [EffectEntityChange(ref=clock.id, field="status", new_value="expired")]
    target = world.get_node(on_expire) if on_expire else None
    if target is not None and str(target.node_type) == "intent":
        changes.append(EffectEntityChange(ref=target.id, field="scheduled_at", new_value=now))
        return TickCandidate(
            candidate_id=f"clock:{clock.id}", origin="clock", summary=label,
            effects=WorldEffects(entity_changes=changes),
        )
    owner = str(clock.profile.get("owner_ref") or "").strip()
    known_owner = owner in world.nodes_by_id
    summary = ": ".join(text for text in (label, on_expire) if text)
    event = EffectEvent(
        summary=summary,
        # A dead owner takes part in nothing; the event still happens where the owner is.
        participant_refs=[owner] if known_owner and world.get_state(owner).get("alive_status") != "dead" else [],
        location_id=resolve_world_node_location_id(world, owner) if known_owner else "",
        # Only a clock declared public spreads as news.
        visibility="public" if clock.profile.get("scope") == "public" else "secret",
        # A clock left running past an earlier window still lands inside this one.
        at=at,
    )
    return TickCandidate(
        candidate_id=f"clock:{clock.id}", origin="clock", summary=summary,
        effects=WorldEffects(entity_changes=changes, events=[event]),
    )


__all__ = ["expire_clocks"]
