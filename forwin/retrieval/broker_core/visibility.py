from __future__ import annotations


def _book_state_node_hidden(node) -> bool:
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


def _map_node_hidden(node) -> bool:
    metadata = (
        getattr(node, "metadata", {})
        if isinstance(getattr(node, "metadata", {}), dict)
        else {}
    )
    return str(getattr(node, "status", "") or "").lower() in {
        "hidden",
        "secret",
        "must_not_reveal",
    } or str(metadata.get("visibility", "") or "").lower() in {
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


def _frontmatter_hidden(frontmatter: dict[str, object]) -> bool:
    visibility = str(frontmatter.get("visibility", "") or "").lower()
    truth = str(frontmatter.get("truth_relation", "") or "").lower()
    status = str(frontmatter.get("status", "") or "").lower()
    node_type = str(frontmatter.get("node_type", "") or "").lower()
    return (
        visibility in {"hidden", "secret", "must_not_reveal"}
        or truth in {"hidden", "secret"}
        or status in {"hidden", "secret"}
        or node_type in {"secret"}
    )


__all__ = [
    "_book_state_node_hidden",
    "_book_state_edge_hidden",
    "_book_state_fact_hidden",
    "_map_node_hidden",
    "_map_edge_hidden",
    "_frontmatter_hidden",
]
