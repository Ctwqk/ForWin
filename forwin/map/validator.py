from __future__ import annotations

from collections import defaultdict, deque

from forwin.protocol.book_state import MapEdge, MapNode

from .protocol import MapGenerationResult, MapValidationReport, RegionEdge, RegionNode, SubWorldMapSpec


_BLOCKED_STATUSES = {"blocked", "destroyed", "sealed"}


def validate_subworld_map(
    *,
    spec: SubWorldMapSpec,
    regions: list[RegionNode],
    region_edges: list[RegionEdge],
    map_nodes: list[MapNode],
    map_edges: list[MapEdge],
) -> MapValidationReport:
    errors: list[str] = []
    warnings: list[str] = []
    region_by_id = {region.id: region for region in regions}
    node_by_id = {node.id: node for node in map_nodes}

    if len(regions) < spec.target_region_count:
        errors.append("region count below target")
    if len(map_nodes) < spec.target_node_count:
        errors.append("node count below target")

    for region in regions:
        if region.subworld_id != spec.subworld_id:
            errors.append(f"region {region.id} has wrong subworld_id")
        if not region.entry_node_ids:
            errors.append(f"region {region.id} has no entry node")
        for node_id in region.entry_node_ids:
            if node_id not in node_by_id:
                errors.append(f"region {region.id} entry node {node_id} is missing")

    for node in map_nodes:
        if not node.subworld_id:
            errors.append(f"node {node.id} missing subworld_id")
        if not node.region_id:
            errors.append(f"node {node.id} missing region_id")
        if node.region_id and node.region_id not in region_by_id:
            errors.append(f"node {node.id} references missing region {node.region_id}")

    for anchor in spec.required_anchor_nodes:
        matches = [
            node
            for node in map_nodes
            if node.name == anchor.name or node.metadata.get("anchor_name") == anchor.name
        ]
        if anchor.required and not matches:
            errors.append(f"required anchor node missing: {anchor.name}")
        for node in matches:
            if not node.subworld_id or not node.region_id:
                errors.append(f"required anchor node {node.id} missing location hierarchy")

    for edge in map_edges:
        if edge.from_node_id not in node_by_id:
            errors.append(f"edge {edge.id} missing from node {edge.from_node_id}")
        if edge.to_node_id not in node_by_id:
            errors.append(f"edge {edge.id} missing to node {edge.to_node_id}")
        if not edge.subworld_id:
            errors.append(f"edge {edge.id} missing subworld_id")
        for field_name in ("distance", "travel_time", "travel_cost", "risk_level", "narrative_cost"):
            if float(getattr(edge, field_name) or 0.0) < 0:
                errors.append(f"edge {edge.id} has negative {field_name}")

    reachable = _reachable_nodes(map_nodes, map_edges, include_hidden=True)
    if map_nodes and len(reachable) != len(map_nodes):
        missing = sorted(set(node_by_id) - reachable)
        errors.append(f"subworld graph is disconnected: {missing[:5]}")

    objective_visible = _reachable_nodes(
        map_nodes,
        [
            edge
            for edge in map_edges
            if edge.edge_type != "hidden_route" and edge.status != "hidden" and edge.discovered_by_default
        ],
        include_hidden=False,
    )
    if map_nodes and objective_visible and len(objective_visible) != len(map_nodes):
        warnings.append("some nodes require hidden or undiscovered routes")

    target_edges = max(0, round(spec.target_node_count * spec.target_edge_density))
    if len(map_edges) < max(1, int(target_edges * 0.6)):
        errors.append("map density too low")
    if len(map_edges) > max(1, int(target_edges * 1.6)):
        warnings.append("map density above target")

    for edge in region_edges:
        if edge.from_region_id not in region_by_id or edge.to_region_id not in region_by_id:
            errors.append(f"region edge {edge.id} references missing region")
        for field_name in ("distance", "travel_time", "risk_level"):
            if float(getattr(edge, field_name) or 0.0) < 0:
                errors.append(f"region edge {edge.id} has negative {field_name}")

    return MapValidationReport(
        valid=not errors,
        errors=errors,
        warnings=warnings,
        summary={
            "region_count": len(regions),
            "region_edge_count": len(region_edges),
            "node_count": len(map_nodes),
            "edge_count": len(map_edges),
            "target_edge_count": target_edges,
        },
    )


def validate_connectivity(map_nodes: list[MapNode], map_edges: list[MapEdge]) -> bool:
    return len(_reachable_nodes(map_nodes, map_edges, include_hidden=True)) == len(map_nodes)


def validate_edge_weights(map_edges: list[MapEdge]) -> bool:
    return all(
        float(getattr(edge, field_name) or 0.0) >= 0
        for edge in map_edges
        for field_name in ("distance", "travel_time", "travel_cost", "risk_level", "narrative_cost")
    )


def validate_required_anchors(result: MapGenerationResult, spec: SubWorldMapSpec) -> bool:
    names = {node.name for node in result.map_nodes}
    return all(not anchor.required or anchor.name in names for anchor in spec.required_anchor_nodes)


def validate_region_coverage(regions: list[RegionNode], map_nodes: list[MapNode]) -> bool:
    region_ids = {region.id for region in regions}
    return bool(region_ids) and all(node.region_id in region_ids for node in map_nodes)


def validate_inter_subworld_edges(map_edges: list[MapEdge], node_ids: set[str]) -> bool:
    return all(edge.from_node_id in node_ids and edge.to_node_id in node_ids for edge in map_edges)


def _reachable_nodes(
    map_nodes: list[MapNode],
    map_edges: list[MapEdge],
    *,
    include_hidden: bool,
) -> set[str]:
    if not map_nodes:
        return set()
    adjacency: dict[str, list[str]] = defaultdict(list)
    for edge in map_edges:
        if edge.status in _BLOCKED_STATUSES:
            continue
        if not include_hidden and (edge.edge_type == "hidden_route" or edge.status == "hidden" or not edge.discovered_by_default):
            continue
        adjacency[edge.from_node_id].append(edge.to_node_id)
        if edge.bidirectional:
            adjacency[edge.to_node_id].append(edge.from_node_id)
    start = map_nodes[0].id
    seen = {start}
    queue: deque[str] = deque([start])
    while queue:
        node_id = queue.popleft()
        for neighbor_id in adjacency.get(node_id, []):
            if neighbor_id in seen:
                continue
            seen.add(neighbor_id)
            queue.append(neighbor_id)
    return seen
