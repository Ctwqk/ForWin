from __future__ import annotations

"""方案 C：Graph-based Weighted Map Generation.

The generator is intentionally graph-first: narrative anchors create regions and
MapNodes, MST guarantees connectivity, extra edges add route variety, and
weighted directed MapEdges power path validation.
"""

import hashlib
import random
from dataclasses import dataclass
from uuid import NAMESPACE_URL, uuid5

from forwin.protocol.book_state import MapEdge, MapNode

from .protocol import SCHEME_C_NAME, MapGenerationResult, RegionEdge, RegionNode, SubWorldMapSpec
from .validator import validate_subworld_map


@dataclass(frozen=True)
class _CandidateEdge:
    from_node_id: str
    to_node_id: str
    edge_type: str
    score: float
    bidirectional: bool = True
    hidden: bool = False
    metadata: dict | None = None


def generate_subworld_map(spec: SubWorldMapSpec) -> MapGenerationResult:
    rng = random.Random(spec.generation_seed)
    regions, region_edges = _generate_region_graph(spec, rng)
    nodes = _generate_region_nodes(spec, regions, rng)
    candidates = _generate_candidate_edges(spec, regions, region_edges, nodes, rng)
    mst_candidates = _build_mst(nodes, [candidate for candidate in candidates if not candidate.hidden])
    target_edge_count = max(len(nodes) - 1, round(len(nodes) * spec.target_edge_density))
    selected = list(mst_candidates)
    selected_keys = {
        (edge.from_node_id, edge.to_node_id, edge.edge_type, edge.hidden)
        for edge in selected
    }
    remaining = [candidate for candidate in candidates if (candidate.from_node_id, candidate.to_node_id, candidate.edge_type, candidate.hidden) not in selected_keys]
    rng.shuffle(remaining)
    remaining.sort(key=lambda item: (item.hidden, item.score))
    for candidate in remaining:
        if len(selected) >= target_edge_count:
            break
        selected.append(candidate)
    map_edges = _compute_edge_weights(spec, selected, nodes, rng)
    regions = _attach_region_node_ids(regions, nodes)
    report = validate_subworld_map(
        spec=spec,
        regions=regions,
        region_edges=region_edges,
        map_nodes=nodes,
        map_edges=map_edges,
    )
    return MapGenerationResult(
        project_id=spec.project_id,
        subworld_id=spec.subworld_id,
        generation_seed=spec.generation_seed,
        algorithm=SCHEME_C_NAME,
        regions=regions,
        region_edges=region_edges,
        map_nodes=nodes,
        map_edges=map_edges,
        validation_report=report,
        summary={
            "algorithm": SCHEME_C_NAME,
            "region_count": len(regions),
            "region_edge_count": len(region_edges),
            "node_count": len(nodes),
            "edge_count": len(map_edges),
            "target_edge_count": target_edge_count,
        },
    )


def _generate_region_graph(
    spec: SubWorldMapSpec,
    rng: random.Random,
) -> tuple[list[RegionNode], list[RegionEdge]]:
    roles = list(dict.fromkeys(role.strip() for role in spec.required_region_roles if role.strip()))
    while len(roles) < spec.target_region_count:
        roles.append(_generated_region_role(len(roles), spec, rng))

    regions: list[RegionNode] = []
    for index, role in enumerate(roles[: spec.target_region_count]):
        region_id = _stable_id("region", spec, role, str(index))
        terrain = _pick(spec.terrain_tags, rng, fallback=_terrain_for_role(role))
        culture = _pick(spec.culture_tags, rng, fallback="")
        regions.append(
            RegionNode(
                id=region_id,
                project_id=spec.project_id,
                subworld_id=spec.subworld_id,
                region_type=_region_type_for_role(role),
                name=role,
                description=f"{spec.name}内的{role}",
                terrain=terrain,
                culture_tag=culture,
                danger_level=_danger_for_role(role, spec, rng),
                metadata={"role": role, "generation_index": index},
            )
        )

    edges: list[RegionEdge] = []
    connected = [regions[0]]
    for region in regions[1:]:
        target = min(
            connected,
            key=lambda candidate: _stable_score(spec, "region_edge", region.id, candidate.id, str(rng.random())),
        )
        edges.append(_region_edge(spec, region, target, len(edges), rng))
        connected.append(region)

    target_region_edges = max(len(regions) - 1, round(len(regions) * 1.35))
    pairs = [
        (left, right)
        for left_index, left in enumerate(regions)
        for right in regions[left_index + 1 :]
        if {left.id, right.id} not in [{edge.from_region_id, edge.to_region_id} for edge in edges]
    ]
    rng.shuffle(pairs)
    for left, right in pairs:
        if len(edges) >= target_region_edges:
            break
        edges.append(_region_edge(spec, left, right, len(edges), rng, edge_type="strategic_corridor"))
    return regions, edges


def _generate_region_nodes(
    spec: SubWorldMapSpec,
    regions: list[RegionNode],
    rng: random.Random,
) -> list[MapNode]:
    nodes: list[MapNode] = []
    region_by_role = {str(region.metadata.get("role", region.name)): region for region in regions}
    for index, region in enumerate(regions):
        for role, node_type in (("entry", "waypoint"), ("hub", "settlement"), ("boundary", "waypoint")):
            nodes.append(_node(spec, region, node_type, _baseline_node_name(region.name, role), role, len(nodes), rng, region_index=index))

    for anchor in spec.required_anchor_nodes:
        region = region_by_role.get(anchor.region_role) or _closest_region(anchor.region_role, regions)
        nodes.append(
            _node(
                spec,
                region,
                anchor.node_type,
                anchor.name,
                "anchor",
                len(nodes),
                rng,
                narrative_function=anchor.narrative_function,
                anchor_name=anchor.name,
                source_anchor=anchor,
            )
        )

    generated_types = ["settlement", "site", "building", "zone", "landmark", "camp", "waypoint"]
    while len(nodes) < spec.target_node_count:
        region = regions[len(nodes) % len(regions)]
        node_type = _pick(generated_types, rng, fallback="site")
        name = f"{region.name}{_node_type_label(node_type)}{len([node for node in nodes if node.region_id == region.id]) + 1}"
        nodes.append(_node(spec, region, node_type, name, "generated", len(nodes), rng))
    return nodes[: spec.target_node_count]


def _generate_candidate_edges(
    spec: SubWorldMapSpec,
    regions: list[RegionNode],
    region_edges: list[RegionEdge],
    nodes: list[MapNode],
    rng: random.Random,
) -> list[_CandidateEdge]:
    by_region: dict[str, list[MapNode]] = {region.id: [] for region in regions}
    for node in nodes:
        by_region.setdefault(node.region_id, []).append(node)
    candidates: list[_CandidateEdge] = []
    for region in regions:
        region_nodes = by_region.get(region.id, [])
        if not region_nodes:
            continue
        hub = _first_by_role(region_nodes, "hub") or region_nodes[0]
        for node in region_nodes:
            if node.id == hub.id:
                continue
            candidates.append(_candidate(hub, node, _edge_type_for_nodes(hub, node, rng), rng, bidirectional=True))
        for left, right in zip(region_nodes, region_nodes[1:]):
            if left.id != right.id:
                candidates.append(_candidate(left, right, "path", rng, bidirectional=True))
        for left_index, left in enumerate(region_nodes):
            for right in region_nodes[left_index + 1 : left_index + 4]:
                candidates.append(_candidate(left, right, "path", rng, bidirectional=True))

    regions_by_id = {region.id: region for region in regions}
    for region_edge in region_edges:
        left_region = regions_by_id.get(region_edge.from_region_id)
        right_region = regions_by_id.get(region_edge.to_region_id)
        if left_region is None or right_region is None:
            continue
        left_node = _first_by_role(by_region.get(left_region.id, []), "boundary") or _first_by_role(by_region.get(left_region.id, []), "entry")
        right_node = _first_by_role(by_region.get(right_region.id, []), "entry") or _first_by_role(by_region.get(right_region.id, []), "boundary")
        if left_node and right_node:
            candidates.append(_candidate(left_node, right_node, "border_crossing", rng, bidirectional=True))

    anchors = [node for node in nodes if node.metadata.get("node_role") == "anchor"]
    for left, right in zip(anchors, anchors[1:]):
        edge_type = _connection_role_to_edge_type(_pick(spec.required_connection_roles, rng, fallback="road"))
        candidates.append(_candidate(left, right, edge_type, rng, bidirectional=True, metadata={"required_connection": True}))

    visible_candidates = list(candidates)
    rng.shuffle(visible_candidates)
    for candidate in visible_candidates[: max(1, len(nodes) // 8)]:
        candidates.append(
            _CandidateEdge(
                from_node_id=candidate.from_node_id,
                to_node_id=candidate.to_node_id,
                edge_type="hidden_route",
                score=candidate.score * 0.55,
                bidirectional=False,
                hidden=True,
                metadata={"hidden_route": True, **(candidate.metadata or {})},
            )
        )
    return candidates


def _build_mst(nodes: list[MapNode], candidates: list[_CandidateEdge]) -> list[_CandidateEdge]:
    parent = {node.id: node.id for node in nodes}

    def find(node_id: str) -> str:
        while parent[node_id] != node_id:
            parent[node_id] = parent[parent[node_id]]
            node_id = parent[node_id]
        return node_id

    def union(left: str, right: str) -> bool:
        left_root = find(left)
        right_root = find(right)
        if left_root == right_root:
            return False
        parent[right_root] = left_root
        return True

    result: list[_CandidateEdge] = []
    for candidate in sorted(candidates, key=lambda item: item.score):
        if union(candidate.from_node_id, candidate.to_node_id):
            result.append(candidate)
        if len(result) >= len(nodes) - 1:
            break

    if len(result) < len(nodes) - 1:
        for left, right in zip(nodes, nodes[1:]):
            if union(left.id, right.id):
                result.append(_CandidateEdge(left.id, right.id, "path", 9999.0, bidirectional=True))
    return result


def _compute_edge_weights(
    spec: SubWorldMapSpec,
    candidates: list[_CandidateEdge],
    nodes: list[MapNode],
    rng: random.Random,
) -> list[MapEdge]:
    node_by_id = {node.id: node for node in nodes}
    seen_ids: set[str] = set()
    edges: list[MapEdge] = []
    for index, candidate in enumerate(candidates):
        from_node = node_by_id[candidate.from_node_id]
        to_node = node_by_id[candidate.to_node_id]
        terrain_multiplier = _terrain_multiplier(from_node.terrain or to_node.terrain)
        route_multiplier = _route_multiplier(candidate.edge_type)
        base_distance = max(1.0, _coordinate_distance(from_node, to_node) * 12.0)
        distance = round(base_distance * terrain_multiplier * route_multiplier, 2)
        speed = _speed_for_edge_type(candidate.edge_type)
        travel_time = round(distance / speed * _time_multiplier(candidate.edge_type), 2)
        travel_cost = round(_base_cost(candidate.edge_type) + distance * 0.05, 2)
        risk = round(from_node.default_danger_level + to_node.default_danger_level + _route_risk(candidate.edge_type), 2)
        narrative_cost = round((2.0 if candidate.hidden else 0.5) + _narrative_cost(candidate.edge_type), 2)
        edge_id = _stable_id("edge", spec, candidate.from_node_id, candidate.to_node_id, candidate.edge_type, str(index))
        while edge_id in seen_ids:
            edge_id = _stable_id("edge", spec, candidate.from_node_id, candidate.to_node_id, candidate.edge_type, str(index), str(rng.random()))
        seen_ids.add(edge_id)
        edges.append(
            MapEdge(
                id=edge_id,
                project_id=spec.project_id,
                subworld_id=spec.subworld_id,
                from_node_id=candidate.from_node_id,
                to_node_id=candidate.to_node_id,
                edge_type=candidate.edge_type,
                bidirectional=candidate.bidirectional,
                distance=distance,
                travel_time=travel_time,
                travel_cost=travel_cost,
                risk_level=risk,
                narrative_cost=narrative_cost,
                status="hidden" if candidate.hidden else "open",
                discovered_by_default=not candidate.hidden,
                visibility_default="hidden" if candidate.hidden else "visible",
                metadata=dict(candidate.metadata or {}),
            )
        )
    return edges


def _attach_region_node_ids(regions: list[RegionNode], nodes: list[MapNode]) -> list[RegionNode]:
    by_region: dict[str, list[MapNode]] = {region.id: [] for region in regions}
    for node in nodes:
        by_region.setdefault(node.region_id, []).append(node)
    updated: list[RegionNode] = []
    for region in regions:
        region_nodes = by_region.get(region.id, [])
        entry_ids = [node.id for node in region_nodes if node.metadata.get("node_role") == "entry"]
        boundary_ids = [node.id for node in region_nodes if node.metadata.get("node_role") == "boundary"]
        updated.append(
            region.model_copy(
                update={
                    "node_ids": [node.id for node in region_nodes],
                    "entry_node_ids": entry_ids or [region_nodes[0].id] if region_nodes else [],
                    "boundary_node_ids": boundary_ids,
                }
            )
        )
    return updated


def _node(
    spec: SubWorldMapSpec,
    region: RegionNode,
    node_type: str,
    name: str,
    role: str,
    index: int,
    rng: random.Random,
    *,
    region_index: int = 0,
    narrative_function: str = "",
    anchor_name: str = "",
    source_anchor=None,
) -> MapNode:
    base_x = float(region.metadata.get("generation_index", region_index)) * 10.0
    coordinates = {
        "x": round(base_x + rng.uniform(0.0, 8.0), 3),
        "y": round(rng.uniform(0.0, 8.0), 3),
    }
    return MapNode(
        id=_stable_id("loc", spec, region.id, name, str(index)),
        project_id=spec.project_id,
        subworld_id=spec.subworld_id,
        region_id=region.id,
        node_type=node_type,
        name=name,
        description=f"{region.name}内的{name}",
        hierarchy_path=f"{spec.subworld_id}/{region.id}",
        scale_level="local",
        coordinates=coordinates,
        terrain=region.terrain,
        culture_tag=region.culture_tag,
        default_danger_level=region.danger_level,
        metadata={
            "generation_seed": spec.generation_seed,
            "node_role": role,
            "region_role": region.metadata.get("role", region.name),
            "narrative_function": narrative_function,
            "anchor_name": anchor_name,
            "source_node_id": str(getattr(source_anchor, "source_node_id", "") or ""),
            "source_region_id": str(getattr(source_anchor, "source_region_id", "") or ""),
            "source_subworld_id": str(getattr(source_anchor, "source_subworld_id", "") or ""),
        },
    )


def _candidate(
    left: MapNode,
    right: MapNode,
    edge_type: str,
    rng: random.Random,
    *,
    bidirectional: bool,
    metadata: dict | None = None,
) -> _CandidateEdge:
    return _CandidateEdge(
        from_node_id=left.id,
        to_node_id=right.id,
        edge_type=edge_type,
        score=_coordinate_distance(left, right) + rng.random(),
        bidirectional=bidirectional,
        metadata=metadata or {},
    )


def _region_edge(
    spec: SubWorldMapSpec,
    left: RegionNode,
    right: RegionNode,
    index: int,
    rng: random.Random,
    *,
    edge_type: str = "adjacent",
) -> RegionEdge:
    distance = round(50 + rng.random() * 150, 2)
    risk = round((left.danger_level + right.danger_level) / 2, 2)
    return RegionEdge(
        id=_stable_id("region_edge", spec, left.id, right.id, str(index)),
        project_id=spec.project_id,
        subworld_id=spec.subworld_id,
        from_region_id=left.id,
        to_region_id=right.id,
        edge_type=edge_type,
        bidirectional=True,
        distance=distance,
        travel_time=round(distance / 30.0, 2),
        risk_level=risk,
        metadata={"generation_index": index},
    )


def _stable_id(prefix: str, spec: SubWorldMapSpec, *parts: str) -> str:
    raw = "|".join([spec.project_id, spec.subworld_id, str(spec.generation_seed), prefix, *parts])
    return f"{prefix}_{uuid5(NAMESPACE_URL, raw).hex[:16]}"


def _stable_score(spec: SubWorldMapSpec, *parts: str) -> float:
    raw = "|".join([spec.project_id, spec.subworld_id, str(spec.generation_seed), *parts])
    return int(hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12], 16) / float(0xFFFFFFFFFFFF)


def _pick(values: list[str], rng: random.Random, *, fallback: str) -> str:
    cleaned = [value for value in values if str(value).strip()]
    return str(rng.choice(cleaned)) if cleaned else fallback


def _generated_region_role(index: int, spec: SubWorldMapSpec, rng: random.Random) -> str:
    base = ["边境", "山脉", "荒原", "旧城圈", "资源带", "海岸", "禁区", "林地", "战线"]
    return f"{spec.name}{rng.choice(base)}{index + 1}"


def _region_type_for_role(role: str) -> str:
    if "宗门" in role or "山门" in role:
        return "sect_domain"
    if "山" in role:
        return "mountain"
    if "海" in role:
        return "sea"
    if "遗迹" in role:
        return "ruin_zone"
    if "战" in role or "边境" in role:
        return "battlefront"
    if "帝" in role or "国" in role:
        return "kingdom"
    return "province"


def _terrain_for_role(role: str) -> str:
    if "山" in role:
        return "mountain"
    if "海" in role:
        return "sea"
    if "荒" in role:
        return "wasteland"
    if "林" in role:
        return "forest"
    return "mixed"


def _danger_for_role(role: str, spec: SubWorldMapSpec, rng: random.Random) -> float:
    base = float(spec.danger_profile.get("base", 2.0) or 2.0)
    if any(token in role for token in ("禁", "魔", "战", "遗迹", "荒")):
        base += 2.0
    return round(max(0.0, base + rng.uniform(-0.5, 1.5)), 2)


def _baseline_node_name(region_name: str, role: str) -> str:
    suffix = {"entry": "入口", "hub": "枢纽", "boundary": "边界站"}[role]
    return f"{region_name}{suffix}"


def _closest_region(role: str, regions: list[RegionNode]) -> RegionNode:
    for region in regions:
        if role and (role in region.name or region.name in role):
            return region
    return regions[0]


def _node_type_label(node_type: str) -> str:
    return {
        "settlement": "聚落",
        "site": "遗址",
        "building": "楼",
        "zone": "地带",
        "landmark": "地标",
        "camp": "营地",
        "waypoint": "驿站",
    }.get(node_type, "地点")


def _first_by_role(nodes: list[MapNode], role: str) -> MapNode | None:
    return next((node for node in nodes if node.metadata.get("node_role") == role), None)


def _edge_type_for_nodes(left: MapNode, right: MapNode, rng: random.Random) -> str:
    if left.node_type == "waypoint" or right.node_type == "waypoint":
        return "road"
    if left.node_type == "site" or right.node_type == "site":
        return "path"
    return rng.choice(["road", "path", "flight_route"])


def _connection_role_to_edge_type(role: str) -> str:
    mapping = {
        "官道": "road",
        "山路": "mountain_pass",
        "传送阵": "portal",
        "边境关隘": "border_crossing",
        "水路": "river",
        "航线": "flight_route",
        "世界门": "world_gate",
    }
    return mapping.get(role, "road")


def _coordinate_distance(left: MapNode, right: MapNode) -> float:
    left_coordinates = left.coordinates or {}
    right_coordinates = right.coordinates or {}
    dx = float(left_coordinates.get("x", 0.0)) - float(right_coordinates.get("x", 0.0))
    dy = float(left_coordinates.get("y", 0.0)) - float(right_coordinates.get("y", 0.0))
    return max(0.1, (dx * dx + dy * dy) ** 0.5)


def _terrain_multiplier(terrain: str) -> float:
    if terrain in {"mountain", "forest", "wasteland"}:
        return 1.4
    if terrain in {"sea"}:
        return 1.8
    return 1.0


def _route_multiplier(edge_type: str) -> float:
    return {
        "portal": 0.05,
        "world_gate": 0.05,
        "flight_route": 0.45,
        "hidden_route": 0.35,
        "mountain_pass": 1.5,
    }.get(edge_type, 1.0)


def _speed_for_edge_type(edge_type: str) -> float:
    return {
        "portal": 200.0,
        "world_gate": 200.0,
        "flight_route": 80.0,
        "river": 25.0,
        "sea_route": 30.0,
        "road": 20.0,
        "rail": 60.0,
        "hidden_route": 18.0,
    }.get(edge_type, 12.0)


def _time_multiplier(edge_type: str) -> float:
    return {"mountain_pass": 1.6, "border_crossing": 1.3, "hidden_route": 0.8}.get(edge_type, 1.0)


def _base_cost(edge_type: str) -> float:
    return {"portal": 80.0, "world_gate": 120.0, "flight_route": 20.0, "sea_route": 10.0, "rail": 8.0}.get(edge_type, 1.0)


def _route_risk(edge_type: str) -> float:
    return {"hidden_route": 2.0, "mountain_pass": 1.5, "border_crossing": 1.0, "portal": 0.5}.get(edge_type, 0.3)


def _narrative_cost(edge_type: str) -> float:
    return {"portal": 4.0, "world_gate": 7.0, "hidden_route": 5.0, "border_crossing": 2.0}.get(edge_type, 0.5)
