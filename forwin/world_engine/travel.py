"""Travel time between map nodes for one mover, as the world gate measures it."""

from __future__ import annotations

from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.rule_predicates import active_rule_predicates, blocked_edge_types
from forwin.book_state.travel import (
    TravelEstimate,
    _mover_modes,
    _overlay_key,
    estimate_route_travel as _estimate_travel,
)
from forwin.protocol.book_state import PathResult


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

    def path(
        observer: tuple[str, str] | None, allowed_edge_types: frozenset[str] | None,
    ) -> PathResult:
        return graph.shortest_path(
            from_location_id, to_location_id, metric="travel_time", observer=observer,
            allowed_edge_types=allowed_edge_types,
        )

    return _estimate_travel(runtime, lexicon, mover_id=mover_id, path=path,
                            edge_types=frozenset(str(edge.edge_type) for edge in graph.edges_by_id.values()))


def estimate_travel_from(
    runtime: BookStateRuntime, lexicon: BookLexicon, *, mover_id: str | None, from_location_id: str,
) -> dict[str, TravelEstimate]:
    """Estimate every destination with one search per profile; None selects objective travel for news."""
    observer = _overlay_key(runtime, mover_id) if mover_id is not None else None
    profiles = list(dict.fromkeys([
        (None, None), (observer, None),
        *((observer, frozenset(mode.allowed_edge_types)) for mode in _mover_modes(runtime, lexicon, mover_id)),
    ]))
    blocked = blocked_edge_types(runtime, active_rule_predicates(runtime), mover_id=mover_id)
    if blocked:
        all_types = frozenset(str(edge.edge_type) for edge in runtime.map.edges_by_id.values())
        profiles.extend((observer, all_types - blocked if allowed is None else allowed - blocked)
                        for observer, allowed in list(profiles))
        profiles.extend((None, all_types - blocked_edge_types(runtime, [p], mover_id=mover_id))
                        for p in active_rule_predicates(runtime)
                        if blocked_edge_types(runtime, [p], mover_id=mover_id))
        profiles = list(dict.fromkeys(profiles))
    paths = dict(zip(profiles, runtime.map.single_source_travel_paths(from_location_id, profiles=profiles)))
    return {
        target: _estimate_travel(
            runtime, lexicon, mover_id=mover_id,
            path=lambda observer, allowed, target=target: paths[(observer, allowed)][target],
        )
        for target in runtime.map.nodes_by_id
    }
