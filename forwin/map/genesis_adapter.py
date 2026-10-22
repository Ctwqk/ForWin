from __future__ import annotations

import hashlib
import re
from typing import Any

from forwin.models.subworld import project_scoped_subworld_id
from forwin.protocol.book_state import MAP_SUBMAP_SCOPES, MapEdgeType

from .protocol import MapAnchorNodeSpec, RequiredConnection, SubWorldMapSpec


_SAFE_NODE_TYPES = {
    "world_area",
    "region",
    "settlement",
    "site",
    "building",
    "room",
    "zone",
    "waypoint",
    "landmark",
    "camp",
    "dungeon_room",
}


def _has_atlas_node_name(node: dict[str, Any]) -> bool:
    return bool(str(node.get("name", "") or "").strip())


def buildable_atlas_node_ids(map_atlas: dict[str, Any] | None) -> frozenset[str]:
    """Return source ids of the atlas nodes selected for generated map anchors."""
    atlas = map_atlas if isinstance(map_atlas, dict) else {}
    submaps = _normalized_submaps(atlas)
    nodes = [item for item in (atlas.get("nodes") or []) if isinstance(item, dict)]
    return frozenset(
        node_id
        for submap in submaps
        for node in _rows_for_subworld(
            nodes, str(submap["id"]), str(submap["name"]),
            single_subworld=len(submaps) == 1, ref_key="parent_subworld",
        )
        if _has_atlas_node_name(node)
        if (node_id := str(node.get("id", "") or "").strip())
    )


def build_subworld_map_specs_from_genesis(
    *,
    project_id: str,
    genesis_revision_id: str = "",
    map_atlas: dict[str, Any] | None = None,
) -> list[SubWorldMapSpec]:
    atlas = map_atlas if isinstance(map_atlas, dict) else {}
    submaps = _normalized_submaps(atlas)
    regions = [item for item in (atlas.get("regions") or []) if isinstance(item, dict)]
    nodes = [item for item in (atlas.get("nodes") or []) if isinstance(item, dict)]
    specs: list[SubWorldMapSpec] = []
    for submap in submaps:
        logical_subworld_id = str(submap["id"])
        subworld_id = project_scoped_subworld_id(project_id, logical_subworld_id)
        subworld_name = str(submap["name"])
        region_rows = _rows_for_subworld(regions, logical_subworld_id, subworld_name, single_subworld=len(submaps) == 1)
        node_rows = _rows_for_subworld(nodes, logical_subworld_id, subworld_name, single_subworld=len(submaps) == 1, ref_key="parent_subworld")
        region_roles, region_name_by_id = _region_roles(region_rows)
        anchors = _anchor_specs(
            submap=submap,
            node_rows=node_rows,
            region_roles=region_roles,
            region_name_by_id=region_name_by_id,
        )
        target_region_count = max(3, len(region_roles))
        target_node_count = max(12, target_region_count * 3 + len(anchors))
        connection_roles, required_connections = _connection_roles(atlas, submap)
        specs.append(
            SubWorldMapSpec(
                project_id=project_id,
                subworld_id=subworld_id,
                logical_subworld_id=logical_subworld_id,
                name=subworld_name,
                subworld_type=_subworld_type(submap),
                culture_tags=_unique(
                    [
                        str(submap.get("culture_profile_id", "") or ""),
                        *[str(item or "") for item in submap.get("culture_traits", [])],
                    ]
                ),
                terrain_tags=_unique(
                    [
                        *[str(item or "") for item in submap.get("terrain", [])],
                        *[
                            str(item.get("terrain", "") or "")
                            for item in region_rows
                            if isinstance(item.get("terrain", ""), str)
                        ],
                    ]
                ),
                scale_level="world",
                target_region_count=target_region_count,
                target_node_count=target_node_count,
                target_edge_density=1.6,
                required_region_roles=region_roles,
                region_kinds={
                    str(item.get("name", "") or "").strip() or f"地区{index}": str(item["kind"]).strip()
                    for index, item in enumerate(region_rows, start=1)
                    if item.get("kind") and str(item["kind"]).strip()
                },
                required_anchor_nodes=anchors,
                required_connection_roles=connection_roles,
                required_connections=required_connections,
                danger_profile={"base": _danger_base(submap, region_rows, node_rows)},
                resource_profile={"themes": list(submap.get("resource_themes", []) or [])},
                faction_profile={"resident_factions": list(submap.get("resident_factions", []) or [])},
                narrative_functions=_unique([anchor.narrative_function for anchor in anchors]),
                generation_seed=_stable_seed(project_id, logical_subworld_id),
            )
        )
    return specs


def _normalized_submaps(atlas: dict[str, Any]) -> list[dict[str, Any]]:
    raw_submaps = [item for item in (atlas.get("submaps") or []) if isinstance(item, dict)]
    if not raw_submaps:
        return [
            {
                "id": "subworld-main-stage",
                "name": "未命名小世界",
                "scope": "continent",
                "summary": str(atlas.get("overview", "") or ""),
                "culture_traits": [],
                "terrain": [],
                "key_locations": [],
                "travel_rules": list(atlas.get("topology_rules", []) or []),
                "resource_themes": [],
                "resident_factions": [],
            }
        ]
    normalized: list[dict[str, Any]] = []
    seen: set[str] = set()
    for index, item in enumerate(raw_submaps, start=1):
        subworld_id = str(item.get("id", "") or "").strip() or f"subworld-{index}"
        if subworld_id in seen:
            subworld_id = f"{subworld_id}-{index}"
        seen.add(subworld_id)
        normalized.append(
            {
                **item,
                "id": subworld_id,
                "name": str(item.get("name", "") or "").strip() or f"SubWorld {index}",
                "scope": str(item.get("scope", "") or "").strip(),
                "culture_traits": [str(value).strip() for value in (item.get("culture_traits") or []) if str(value).strip()],
                "terrain": [str(value).strip() for value in (item.get("terrain") or []) if str(value).strip()],
                "key_locations": [str(value).strip() for value in (item.get("key_locations") or []) if str(value).strip()],
                "travel_rules": [str(value).strip() for value in (item.get("travel_rules") or []) if str(value).strip()],
                "resource_themes": [str(value).strip() for value in (item.get("resource_themes") or []) if str(value).strip()],
                "resident_factions": [str(value).strip() for value in (item.get("resident_factions") or []) if str(value).strip()],
            }
        )
    return normalized


def _rows_for_subworld(
    rows: list[dict[str, Any]],
    subworld_id: str,
    subworld_name: str,
    *,
    single_subworld: bool,
    ref_key: str = "subworld_name",
) -> list[dict[str, Any]]:
    result: list[dict[str, Any]] = []
    for item in rows:
        ref = str(item.get(ref_key, "") or "").strip()
        if ref in {subworld_id, subworld_name}:
            result.append(item)
        elif single_subworld and not ref:
            result.append(item)
    if not result and single_subworld:
        return list(rows)
    return result


def _region_roles(region_rows: list[dict[str, Any]]) -> tuple[list[str], dict[str, str]]:
    roles: list[str] = []
    names_by_id: dict[str, str] = {}
    for index, item in enumerate(region_rows, start=1):
        name = str(item.get("name", "") or "").strip() or f"地区{index}"
        region_id = str(item.get("id", "") or "").strip()
        if region_id:
            names_by_id[region_id] = name
        roles.append(name)
    roles = _unique(roles)
    if not roles:
        roles = [f"区域{i}" for i in range(1, 4)]
    return roles, names_by_id


def _anchor_specs(
    *,
    submap: dict[str, Any],
    node_rows: list[dict[str, Any]],
    region_roles: list[str],
    region_name_by_id: dict[str, str],
) -> list[MapAnchorNodeSpec]:
    anchors: list[MapAnchorNodeSpec] = []
    for index, item in enumerate(node_rows, start=1):
        name = str(item.get("name", "") or "").strip()
        if not _has_atlas_node_name(item):
            continue
        source_region_id = str(item.get("parent_region_id", "") or "").strip()
        anchors.append(
            MapAnchorNodeSpec(
                name=name,
                node_type=_node_type(str(item.get("kind", "") or "")),
                region_role=region_name_by_id.get(source_region_id, region_roles[(index - 1) % len(region_roles)]),
                narrative_function=str(item.get("description", "") or item.get("kind", "") or "Genesis 地图锚点").strip(),
                tags=_unique([str(item.get("kind", "") or ""), "genesis_node"]),
                source_node_id=str(item.get("id", "") or "").strip(),
                source_region_id=source_region_id,
                source_subworld_id=str(submap.get("id", "") or "").strip(),
            )
        )
    existing_names = {anchor.name for anchor in anchors}
    for index, name in enumerate([item for item in submap.get("key_locations", []) if str(item).strip()], start=1):
        if name in existing_names:
            continue
        anchors.append(
            MapAnchorNodeSpec(
                name=str(name),
                node_type="site",
                region_role=region_roles[(index - 1) % len(region_roles)],
                narrative_function="Genesis key location",
                tags=["genesis_key_location"],
                source_subworld_id=str(submap.get("id", "") or "").strip(),
            )
        )
        existing_names.add(str(name))
    return anchors


def _subworld_type(submap: dict[str, Any]) -> str:
    scope = str(submap.get("scope", "") or "").strip().lower()
    return scope if scope in MAP_SUBMAP_SCOPES else "continent"


def _node_type(kind: str) -> str:
    text = re.sub(r"[^a-zA-Z_]+", "_", kind.strip().lower()).strip("_")
    mapping = {
        "city": "settlement",
        "town": "settlement",
        "village": "settlement",
        "frontier": "zone",
        "ruin": "site",
        "ruins": "site",
        "region": "region",
        "location": "site",
        "place": "site",
    }
    normalized = mapping.get(text, text or "site")
    return normalized if normalized in _SAFE_NODE_TYPES else "site"


def _connection_roles(
    atlas: dict[str, Any], submap: dict[str, Any],
) -> tuple[list[str], list[RequiredConnection]]:
    submaps = _normalized_submaps(atlas)
    subworld_by_ref = {
        str(item[key]): str(item["id"])
        for item in submaps
        for key in ("id", "name")
    }
    node_subworld_by_ref: dict[str, str] = {}
    node_id_by_ref: dict[str, str] = {}
    for node in (atlas.get("nodes") or []):
        if not isinstance(node, dict):
            continue
        subworld_ref = str(node.get("parent_subworld", "") or "").strip()
        subworld_id = subworld_by_ref.get(subworld_ref, "")
        if not subworld_ref and len(submaps) == 1:
            subworld_id = str(submaps[0]["id"])
        if not subworld_id:
            continue
        for key in ("id", "name"):
            ref = str(node.get(key, "") or "").strip()
            if ref:
                if key == "id":
                    node_subworld_by_ref[ref] = subworld_id
                else:
                    node_subworld_by_ref.setdefault(ref, subworld_id)
                node_id = str(node.get("id", "") or "").strip()
                if node_id and _has_atlas_node_name(node):
                    if key == "id":
                        node_id_by_ref[ref] = node_id
                    else:
                        node_id_by_ref.setdefault(ref, node_id)
    edge_types = {edge_type.value for edge_type in MapEdgeType}
    roles: list[str] = []
    connections: list[RequiredConnection] = []
    for edge in (atlas.get("edges") or []):
        if not isinstance(edge, dict):
            continue
        from_subworld_id = _edge_endpoint_subworld(edge, ("from", "source"), subworld_by_ref, node_subworld_by_ref)
        to_subworld_id = _edge_endpoint_subworld(edge, ("to", "target"), subworld_by_ref, node_subworld_by_ref)
        if from_subworld_id != str(submap["id"]) or to_subworld_id != str(submap["id"]):
            continue
        edge_type = str(edge.get("edge_type") or edge.get("kind") or "")
        if edge_type in edge_types:
            roles.append(edge_type)
            from_node_id = _edge_endpoint_node_id(edge, ("from", "source"), node_id_by_ref)
            to_node_id = _edge_endpoint_node_id(edge, ("to", "target"), node_id_by_ref)
            if from_node_id and to_node_id:
                connections.append(RequiredConnection(
                    from_source_node_id=from_node_id,
                    to_source_node_id=to_node_id,
                    edge_type=edge_type,
                    bidirectional=edge.get("bidirectional", True),
                    hidden=edge.get("hidden", False),
                    source_edge_id=str(edge.get("id") or edge.get("edge_id") or edge.get("source_edge_id") or ""),
                ))
    return _unique(roles) or ["road"], connections


def _edge_endpoint_node_id(
    edge: dict[str, Any], prefixes: tuple[str, str], node_id_by_ref: dict[str, str],
) -> str:
    for suffix in ("_node_id", "_node", ""):
        for prefix in prefixes:
            ref = str(edge.get(f"{prefix}{suffix}", "") or "").strip()
            if ref in node_id_by_ref:
                return node_id_by_ref[ref]
    return ""


def _edge_endpoint_subworld(
    edge: dict[str, Any],
    prefixes: tuple[str, str],
    subworld_by_ref: dict[str, str],
    node_subworld_by_ref: dict[str, str],
) -> str:
    for suffix in ("_subworld_id", "_subworld", "_node_id", "_node", ""):
        for prefix in prefixes:
            ref = str(edge.get(f"{prefix}{suffix}", "") or "").strip()
            if suffix in {"_subworld_id", "_subworld"}:
                resolved = subworld_by_ref.get(ref, "")
            else:
                resolved = node_subworld_by_ref.get(ref) or subworld_by_ref.get(ref, "")
            if resolved:
                return resolved
    return ""


def _danger_base(
    submap: dict[str, Any],
    region_rows: list[dict[str, Any]],
    node_rows: list[dict[str, Any]],
) -> float:
    value = submap.get("danger_level")
    return float(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else 2.0


def _stable_seed(project_id: str, subworld_id: str) -> int:
    digest = hashlib.sha256(f"{project_id}|{subworld_id}".encode("utf-8")).hexdigest()
    return int(digest[:12], 16) % 2_000_000_000


def _unique(values: list[str]) -> list[str]:
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        text = str(value or "").strip()
        if text and text not in seen:
            result.append(text)
            seen.add(text)
    return result


__all__ = ["buildable_atlas_node_ids", "build_subworld_map_specs_from_genesis"]
