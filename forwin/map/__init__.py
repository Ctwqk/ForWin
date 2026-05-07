"""CANON Scheme C BookMap runtime: SubWorld -> Region -> MapNode -> MapEdge."""

from __future__ import annotations

from typing import Any

from .protocol import (
    BookMapRuntime,
    BookMapGenerationResult,
    InterSubWorldConnectionSpec,
    MapAnchorNodeSpec,
    MapEdge,
    MapGenerationResult,
    MapNode,
    MapValidationReport,
    PathMetric,
    PathResult,
    RegionEdge,
    RegionNode,
    SCHEME_C_NAME,
    SubWorldMapSpec,
)


class MapGraph:  # type: ignore[no-redef]
    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        from .pathfinding import MapGraph as _MapGraph

        return _MapGraph(*args, **kwargs)


def distance_between_world_nodes(*args: Any, **kwargs: Any) -> Any:
    from .pathfinding import distance_between_world_nodes as _distance_between_world_nodes

    return _distance_between_world_nodes(*args, **kwargs)


def generate_subworld_map(*args: Any, **kwargs: Any) -> Any:
    from .generator import generate_subworld_map as _generate_subworld_map

    return _generate_subworld_map(*args, **kwargs)


def build_subworld_map_specs_from_genesis(*args: Any, **kwargs: Any) -> Any:
    from .genesis_adapter import build_subworld_map_specs_from_genesis as _build_specs

    return _build_specs(*args, **kwargs)


class MapRepository:  # type: ignore[no-redef]
    def __new__(cls, *args: Any, **kwargs: Any) -> Any:
        from .repository import MapRepository as _MapRepository

        return _MapRepository(*args, **kwargs)


def compute_distance(*args: Any, **kwargs: Any) -> Any:
    from .service import compute_distance as _compute_distance

    return _compute_distance(*args, **kwargs)


def compute_known_distance(*args: Any, **kwargs: Any) -> Any:
    from .service import compute_known_distance as _compute_known_distance

    return _compute_known_distance(*args, **kwargs)


def create_or_update_subworld_map(*args: Any, **kwargs: Any) -> Any:
    from .service import create_or_update_subworld_map as _create_or_update_subworld_map

    return _create_or_update_subworld_map(*args, **kwargs)


def create_or_update_book_map(*args: Any, **kwargs: Any) -> Any:
    from .service import create_or_update_book_map as _create_or_update_book_map

    return _create_or_update_book_map(*args, **kwargs)


def ensure_book_map_from_genesis_atlas(*args: Any, **kwargs: Any) -> Any:
    from .service import ensure_book_map_from_genesis_atlas as _ensure_book_map_from_genesis_atlas

    return _ensure_book_map_from_genesis_atlas(*args, **kwargs)


def get_book_map_runtime(*args: Any, **kwargs: Any) -> Any:
    from .service import get_book_map_runtime as _get_book_map_runtime

    return _get_book_map_runtime(*args, **kwargs)


def get_subworld_map(*args: Any, **kwargs: Any) -> Any:
    from .service import get_subworld_map as _get_subworld_map

    return _get_subworld_map(*args, **kwargs)


def resolve_world_node_location_id(*args: Any, **kwargs: Any) -> Any:
    from .service import resolve_world_node_location_id as _resolve_world_node_location_id

    return _resolve_world_node_location_id(*args, **kwargs)

__all__ = [
    "BookMapRuntime",
    "BookMapGenerationResult",
    "InterSubWorldConnectionSpec",
    "MapAnchorNodeSpec",
    "MapEdge",
    "MapGenerationResult",
    "MapGraph",
    "MapNode",
    "MapRepository",
    "MapValidationReport",
    "PathMetric",
    "PathResult",
    "RegionEdge",
    "RegionNode",
    "SCHEME_C_NAME",
    "SubWorldMapSpec",
    "build_subworld_map_specs_from_genesis",
    "compute_distance",
    "compute_known_distance",
    "create_or_update_subworld_map",
    "create_or_update_book_map",
    "distance_between_world_nodes",
    "ensure_book_map_from_genesis_atlas",
    "generate_subworld_map",
    "get_book_map_runtime",
    "get_subworld_map",
    "resolve_world_node_location_id",
]
