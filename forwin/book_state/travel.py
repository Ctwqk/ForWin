"""Travel time between map nodes for one mover, as the world gate measures it."""

from __future__ import annotations

from collections.abc import Callable

from pydantic import BaseModel, ConfigDict

from forwin.book_state.knowledge import observer_overlay_key
from forwin.book_state.lexicon import BookLexicon, TravelMode
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.rule_predicates import active_rule_predicates, blocked_edge_types
from forwin.protocol.book_state import PathResult


# Map edges give travel_time in hours.
_MINUTES_PER_HOUR = 60


class TravelEstimate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    # An objective map path exists.
    reachable: bool
    # The mover's shortest time; None when the path is unreachable or unknown to the mover.
    minutes: float | None
    # The mover knows a route: its cognition overlay's, or the objective one when it has none.
    route_known: bool
    blocked_reason: str
    blocked_by_rule: str = ""


def estimate_route_travel(
    runtime: BookStateRuntime,
    lexicon: BookLexicon,
    *,
    mover_id: str | None,
    path: Callable[[tuple[str, str] | None, frozenset[str] | None], PathResult],
    edge_types: frozenset[str] | None = None,
) -> TravelEstimate:
    predicates = active_rule_predicates(runtime)
    blocked = blocked_edge_types(runtime, predicates, mover_id=mover_id)
    all_types = edge_types if edge_types is not None else frozenset(
        str(edge.edge_type) for edge in runtime.map.edges_by_id.values()
    )
    allowed = all_types - blocked if blocked else None
    unfiltered_path = path

    def filtered_path(observer, edge_types):
        types = allowed if edge_types is None else edge_types - blocked
        return unfiltered_path(observer, types)

    path = filtered_path
    objective = path(None, None)
    if not objective.reachable and blocked and unfiltered_path(None, None).reachable:
        # Name a rule that removes an edge of the otherwise usable route.
        relevant = next((p.rule_id for p in predicates
                         if blocked_edge_types(runtime, [p], mover_id=mover_id)
                         and not unfiltered_path(None, all_types - blocked_edge_types(runtime, [p], mover_id=mover_id)).reachable), "")
        if not relevant:
            relevant = next(p.rule_id for p in predicates
                            if all_types & blocked_edge_types(runtime, [p], mover_id=mover_id))
        return TravelEstimate(reachable=False, minutes=None, route_known=False,
                              blocked_reason=f"rule:{relevant} requires travel mode", blocked_by_rule=relevant)
    if not objective.reachable:
        return TravelEstimate(
            reachable=False, minutes=None, route_known=False, blocked_reason=objective.blocked_reason,
        )
    observer = observer_overlay_key(runtime, mover_id) if mover_id is not None else None
    route = objective
    if observer is not None:
        route = path(observer, None)
        if not route.reachable:
            return TravelEstimate(
                reachable=True, minutes=None, route_known=False, blocked_reason=route.blocked_reason,
            )
    candidates = [route.total_travel_time * _MINUTES_PER_HOUR]
    for mode in _mover_modes(runtime, lexicon, mover_id):
        by_mode = path(observer, frozenset(mode.allowed_edge_types))
        if by_mode.reachable:
            candidates.append(by_mode.total_travel_time * _MINUTES_PER_HOUR / mode.speed_multiplier)
    return TravelEstimate(reachable=True, minutes=min(candidates), route_known=True, blocked_reason="")


def _mover_modes(runtime: BookStateRuntime, lexicon: BookLexicon, mover_id: str | None) -> list[TravelMode]:
    modes = {mode.id: mode for mode in lexicon.travel_modes}
    ids = (runtime.world.get_state(mover_id).get("travel_mode_ids") or []) if mover_id is not None else []
    return [modes[mode_id] for mode_id in ids if mode_id in modes]
