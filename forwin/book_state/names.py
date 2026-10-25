"""Name matching shared by extraction and world-engine actor admission."""


from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from forwin.book_state.runtime import ObjectiveWorldGraph
    from forwin.protocol.book_state import WorldNode


def normalized_name(text: str) -> str:
    return "".join(str(text or "").split()).casefold()


def matching_runtime_entities(world: ObjectiveWorldGraph, kind: str, name: str) -> list[WorldNode]:
    """Exact active-node matches shared with admission planning; never choose an ambiguous owner."""
    wanted = normalized_name(name)
    return [
        node for node in world.nodes_by_id.values()
        if wanted and str(node.node_type) == kind and node.is_active
        and node.status not in {"inactive", "retired", "deleted"}
        and wanted in {normalized_name(label) for label in (node.name, *node.aliases)}
    ]
