from __future__ import annotations

from forwin.protocol.book_state import MapNode, WorldNode


def book_state_node_hidden(node) -> bool:
    tags = {str(tag).lower() for tag in getattr(node, "tags", []) or []}
    metadata = (
        getattr(node, "metadata", {})
        if isinstance(getattr(node, "metadata", {}), dict)
        else {}
    )
    return (
        str(getattr(node, "status", "") or "").lower()
        in {"hidden", "secret", "must_not_reveal"}
        or str(metadata.get("visibility", "") or "").lower()
        in {"hidden", "secret", "must_not_reveal"}
        or bool(tags.intersection({"hidden", "secret", "must_not_reveal"}))
    )


def node_page_visibility(node: WorldNode | MapNode) -> str:
    if book_state_node_hidden(node):
        return "hidden"
    return str(node.metadata.get(
        "reader_visibility", node.metadata.get("visibility", "reader_known")
    ))
