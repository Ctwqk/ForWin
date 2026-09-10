from __future__ import annotations

from typing import Any

_WRITER_HIDDEN_STATUSES = {"hidden", "blocked", "destroyed", "sealed", "closed"}


def genesis_edge_visibility(row: dict[str, Any]) -> dict[str, Any]:
    """One visibility normalization for imported routes and Genesis previews."""
    hidden = str(row.get("hidden", False)).lower() in {"true", "1", "yes"}
    status = str(row.get("status") or ("hidden" if hidden else "open")).strip().lower()
    if status in {"secret", "concealed"}:
        status = "hidden"
    visibility = str(row.get("visibility_default") or row.get("visibility") or ("hidden" if hidden or status == "hidden" else "visible")).strip().lower()
    if visibility in {"secret", "concealed"} or hidden:
        visibility = "hidden"
    discovered = str(row.get("discovered_by_default", not hidden and status != "hidden" and visibility != "hidden")).lower() not in {"false", "0", "no"}
    return {"status": status, "visibility_default": visibility, "discovered_by_default": discovered}


def is_writer_visible_map_edge(edge: Any) -> bool:
    status = str(getattr(edge, "status", "") or "").strip().lower()
    visibility = str(getattr(edge, "visibility_default", "") or "").strip().lower()
    edge_type = str(getattr(edge, "edge_type", "") or "").strip()
    if status in _WRITER_HIDDEN_STATUSES:
        return False
    if visibility == "hidden" or edge_type == "hidden_route":
        return False
    return bool(getattr(edge, "discovered_by_default", True))


__all__ = ["genesis_edge_visibility", "is_writer_visible_map_edge"]
