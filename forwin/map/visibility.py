from __future__ import annotations

from typing import Any

_WRITER_HIDDEN_STATUSES = {"hidden", "blocked", "destroyed", "sealed", "closed"}


def is_writer_visible_map_edge(edge: Any) -> bool:
    status = str(getattr(edge, "status", "") or "").strip().lower()
    visibility = str(getattr(edge, "visibility_default", "") or "").strip().lower()
    edge_type = str(getattr(edge, "edge_type", "") or "").strip()
    if status in _WRITER_HIDDEN_STATUSES:
        return False
    if visibility == "hidden" or edge_type == "hidden_route":
        return False
    return bool(getattr(edge, "discovered_by_default", True))


__all__ = ["is_writer_visible_map_edge"]
