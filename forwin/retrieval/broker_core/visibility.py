from __future__ import annotations


def _book_state_edge_hidden(edge) -> bool:
    metadata = (
        getattr(edge, "metadata", {})
        if isinstance(getattr(edge, "metadata", {}), dict)
        else {}
    )
    return (
        str(getattr(edge, "status", "") or "").lower()
        in {"hidden", "secret", "must_not_reveal"}
        or str(getattr(edge, "visibility", "") or "").lower()
        in {"hidden", "secret", "must_not_reveal"}
        or str(getattr(edge, "visibility_default", "") or "").lower() == "hidden"
        or str(metadata.get("visibility", "") or "").lower()
        in {"hidden", "secret", "must_not_reveal"}
    )


def _book_state_fact_hidden(fact) -> bool:
    return str(getattr(fact, "sensitivity_level", "") or "").lower() in {
        "hidden",
        "secret",
        "must_not_reveal",
    }


def _map_edge_hidden(edge) -> bool:
    return (
        str(getattr(edge, "status", "") or "").lower()
        in {"hidden", "secret", "must_not_reveal"}
        or str(getattr(edge, "visibility_default", "") or "").lower() == "hidden"
        or not bool(getattr(edge, "discovered_by_default", True))
    )


__all__ = [
    "_book_state_edge_hidden",
    "_book_state_fact_hidden",
    "_map_edge_hidden",
]
