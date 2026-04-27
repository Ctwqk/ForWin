from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from .protocol import MapAnchorNodeSpec, SubWorldMapSpec


_DEFAULT_REGION_ROLES = ["主舞台核心区", "权力中心区", "危险边缘区"]
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
        subworld_id = str(submap["id"])
        subworld_name = str(submap["name"])
        region_rows = _rows_for_subworld(regions, subworld_id, subworld_name, single_subworld=len(submaps) == 1)
        node_rows = _rows_for_subworld(nodes, subworld_id, subworld_name, single_subworld=len(submaps) == 1, ref_key="parent_subworld")
        region_roles, region_name_by_id = _region_roles(region_rows)
        anchors = _anchor_specs(
            submap=submap,
            node_rows=node_rows,
            region_roles=region_roles,
            region_name_by_id=region_name_by_id,
        )
        target_region_count = max(3, len(region_roles))
        target_node_count = max(12, target_region_count * 3 + len(anchors))
        specs.append(
            SubWorldMapSpec(
                project_id=project_id,
                subworld_id=subworld_id,
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
                required_anchor_nodes=anchors,
                required_connection_roles=_connection_roles(atlas, submap),
                danger_profile={"base": _danger_base(submap, region_rows, node_rows)},
                resource_profile={"themes": list(submap.get("resource_themes", []) or [])},
                faction_profile={"resident_factions": list(submap.get("resident_factions", []) or [])},
                narrative_functions=_unique([anchor.narrative_function for anchor in anchors]),
                generation_seed=_stable_seed(project_id, genesis_revision_id, subworld_id, atlas),
            )
        )
    return specs


def _normalized_submaps(atlas: dict[str, Any]) -> list[dict[str, Any]]:
    raw_submaps = [item for item in (atlas.get("submaps") or []) if isinstance(item, dict)]
    if not raw_submaps:
        return [
            {
                "id": "subworld-main-stage",
                "name": "主舞台总图",
                "scope": "macro_region",
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
        roles = list(_DEFAULT_REGION_ROLES)
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
        if not name:
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
    scope = str(submap.get("scope", "") or "").lower()
    name = str(submap.get("name", "") or "")
    if "realm" in scope or "界" in name or "秘境" in name:
        return "realm"
    if "plane" in scope:
        return "plane"
    if "underground" in scope or "地下" in name:
        return "underground"
    return "continent"


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


def _connection_roles(atlas: dict[str, Any], submap: dict[str, Any]) -> list[str]:
    values = [
        *[str(item or "") for item in (atlas.get("topology_rules") or [])],
        *[str(item or "") for item in (submap.get("travel_rules") or [])],
    ]
    roles: list[str] = []
    for value in values:
        if "传送" in value or "门" in value:
            roles.append("传送阵")
        if "山" in value:
            roles.append("山路")
        if "水" in value or "河" in value:
            roles.append("水路")
        if "路" in value or "移动" in value:
            roles.append("官道")
    return _unique(roles) or ["官道", "山路", "传送阵"]


def _danger_base(
    submap: dict[str, Any],
    region_rows: list[dict[str, Any]],
    node_rows: list[dict[str, Any]],
) -> float:
    text = " ".join(
        [
            str(submap.get("summary", "") or ""),
            str(submap.get("name", "") or ""),
            *[str(item.get("summary", "") or "") for item in region_rows],
            *[str(item.get("danger", "") or "") for item in node_rows],
        ]
    )
    if any(token in text for token in ("禁", "危险", "荒", "遗迹", "战", "魔")):
        return 3.0
    return 2.0


def _stable_seed(project_id: str, genesis_revision_id: str, subworld_id: str, atlas: dict[str, Any]) -> int:
    raw = json.dumps(atlas, ensure_ascii=False, sort_keys=True)
    digest = hashlib.sha256(f"{project_id}|{genesis_revision_id}|{subworld_id}|{raw}".encode("utf-8")).hexdigest()
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


__all__ = ["build_subworld_map_specs_from_genesis"]
