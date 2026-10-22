"""Travel time between map nodes for one mover, as the world gate measures it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime
from forwin.protocol.book_state import ObserverType


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


def estimate_travel(
    runtime: BookStateRuntime,
    lexicon: BookLexicon,
    *,
    mover_id: str,
    from_location_id: str,
    to_location_id: str,
    graph: MapGraph | None = None,
) -> TravelEstimate:
    """The mover's shortest time from one map node to another.

    The objective map must connect the nodes, and a mover with a cognition overlay travels only
    the routes it knows. The time is the least of the route at speed 1.0 and, per travel mode of
    the mover, the fastest route that mode may use divided by its speed. ``graph`` defaults to
    the runtime's map; the world gate passes its map with the chapter's own patches.
    """
    graph = graph if graph is not None else runtime.map
    objective = graph.shortest_path(from_location_id, to_location_id, metric="travel_time")
    if not objective.reachable:
        return TravelEstimate(
            reachable=False, minutes=None, route_known=False, blocked_reason=objective.blocked_reason,
        )
    observer = _overlay_key(runtime, mover_id)
    route = objective
    if observer is not None:
        route = graph.shortest_path(from_location_id, to_location_id, metric="travel_time", observer=observer)
        if not route.reachable:
            return TravelEstimate(
                reachable=True, minutes=None, route_known=False, blocked_reason=route.blocked_reason,
            )
    modes = {mode.id: mode for mode in lexicon.travel_modes}
    candidates = [route.total_travel_time * _MINUTES_PER_HOUR]
    for mode_id in runtime.world.get_state(mover_id).get("travel_mode_ids") or []:
        mode = modes.get(mode_id)
        if mode is None:
            continue
        by_mode = graph.shortest_path(
            from_location_id, to_location_id, metric="travel_time", observer=observer,
            allowed_edge_types=frozenset(mode.allowed_edge_types),
        )
        if by_mode.reachable:
            candidates.append(by_mode.total_travel_time * _MINUTES_PER_HOUR / mode.speed_multiplier)
    return TravelEstimate(reachable=True, minutes=min(candidates), route_known=True, blocked_reason="")


def _overlay_key(runtime: BookStateRuntime, node_id: str) -> tuple[str, str] | None:
    """The cognition overlay of a world node; node ids are unique across observer types."""
    return next(
        (
            key for key in runtime.cognition_by_observer
            if key[1] == node_id and key[0] != ObserverType.READER.value
        ),
        None,
    )
