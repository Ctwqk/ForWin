"""Resolve map containers for planned locations and the current POV."""

from collections.abc import Sequence

from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id


def location_subworld_ids(
    runtime: BookStateRuntime, location_refs: Sequence[str], *, pov_ref: str = "",
) -> list[str]:
    """Keep location order, deduplicate containers, and optionally fall back to the POV."""
    nodes = (runtime.map.nodes_by_id.get(ref) for ref in location_refs)
    ids = list(dict.fromkeys(node.subworld_id for node in nodes if node is not None and node.subworld_id))
    if not ids and pov_ref:
        location = runtime.map.nodes_by_id.get(resolve_world_node_location_id(runtime.world, pov_ref))
        if location is not None and location.subworld_id:
            ids.append(location.subworld_id)
    return ids
