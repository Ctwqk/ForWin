"""Travel time between map nodes for one mover, as the world gate measures it."""

from __future__ import annotations

from pydantic import BaseModel, ConfigDict

from forwin.book_state.lexicon import BookLexicon, TravelMode
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime
from forwin.protocol.book_state import ObserverType, PathResult


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
    mover_id: str | None,
    from_location_id: str,
    to_location_id: str,
    graph: MapGraph | None = None,
    _paths: dict[tuple[tuple[str, str] | None, frozenset[str] | None], dict[str, PathResult]] | None = None,
) -> TravelEstimate:
    """The mover's shortest time from one map node to another.

    The objective map must connect the nodes, and a mover with a cognition overlay travels only
    the routes it knows. The time is the least of the route at speed 1.0 and, per travel mode of
    the mover, the fastest route that mode may use divided by its speed. ``graph`` defaults to
    the runtime's map; the world gate passes its map with the chapter's own patches.
    ``mover_id=None`` uses objective routes. Only ``estimate_travel_from`` supplies ``_paths``,
    computed for this source and runtime snapshot.
    """
    graph = graph if graph is not None else runtime.map

    def path(
        observer: tuple[str, str] | None = None, allowed_edge_types: frozenset[str] | None = None,
    ) -> PathResult:
        if _paths is not None:
            return _paths[(observer, allowed_edge_types)][to_location_id]
        return graph.shortest_path(
            from_location_id, to_location_id, metric="travel_time", observer=observer,
            allowed_edge_types=allowed_edge_types,
        )

    objective = path()
    if not objective.reachable:
        return TravelEstimate(
            reachable=False, minutes=None, route_known=False, blocked_reason=objective.blocked_reason,
        )
    observer = _overlay_key(runtime, mover_id) if mover_id is not None else None
    route = objective
    if observer is not None:
        route = path(observer)
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


def estimate_travel_from(
    runtime: BookStateRuntime, lexicon: BookLexicon, *, mover_id: str | None, from_location_id: str,
) -> dict[str, TravelEstimate]:
    """Estimate every destination with one search; None selects objective travel for news."""
    observer = _overlay_key(runtime, mover_id) if mover_id is not None else None
    profiles = list(dict.fromkeys([
        (None, None), (observer, None),
        *((observer, frozenset(mode.allowed_edge_types)) for mode in _mover_modes(runtime, lexicon, mover_id)),
    ]))
    paths = dict(zip(profiles, runtime.map.single_source_travel_paths(from_location_id, profiles=profiles)))
    return {
        target: estimate_travel(runtime, lexicon, mover_id=mover_id, from_location_id=from_location_id,
                                to_location_id=target, _paths=paths)
        for target in runtime.map.nodes_by_id
    }


def _overlay_key(runtime: BookStateRuntime, node_id: str) -> tuple[str, str] | None:
    """The cognition overlay of a world node; node ids are unique across observer types."""
    return next(
        (
            key for key in runtime.cognition_by_observer
            if key[1] == node_id and key[0] != ObserverType.READER.value
        ),
        None,
    )
