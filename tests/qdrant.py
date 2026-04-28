from __future__ import annotations

import math
from dataclasses import dataclass, field
from types import SimpleNamespace
from typing import Any


class FakeQdrantModels:
    class Distance:
        COSINE = "Cosine"

    @dataclass
    class VectorParams:
        size: int
        distance: str

    @dataclass
    class PointStruct:
        id: str
        vector: list[float]
        payload: dict[str, Any]

    @dataclass
    class MatchValue:
        value: Any

    @dataclass
    class MatchAny:
        any: list[Any]

    @dataclass
    class FieldCondition:
        key: str
        match: Any

    @dataclass
    class Filter:
        must: list[Any] = field(default_factory=list)

    @dataclass
    class FilterSelector:
        filter: Any


class FakeQdrantClient:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}

    def get_collections(self):
        return SimpleNamespace(
            collections=[
                SimpleNamespace(name=name)
                for name in sorted(self.collections)
            ]
        )

    def create_collection(self, *, collection_name: str, vectors_config) -> None:
        self.collections.setdefault(
            collection_name,
            {
                "vectors_config": vectors_config,
                "points": {},
            },
        )

    def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        collection = self.collections.setdefault(collection_name, {"points": {}})
        for point in points:
            collection["points"][str(point.id)] = point

    def delete(self, *, collection_name: str, points_selector, wait: bool = True) -> None:  # noqa: ARG002
        collection = self.collections.setdefault(collection_name, {"points": {}})
        point_ids = [
            point_id
            for point_id, point in collection["points"].items()
            if _matches_filter(point.payload, points_selector.filter)
        ]
        for point_id in point_ids:
            collection["points"].pop(point_id, None)

    def query_points(self, *, collection_name: str, query: list[float], query_filter, limit: int):
        collection = self.collections.setdefault(collection_name, {"points": {}})
        hits = []
        for point in collection["points"].values():
            if not _matches_filter(point.payload, query_filter):
                continue
            hits.append(
                SimpleNamespace(
                    payload=dict(point.payload),
                    score=_cosine(query, point.vector),
                )
            )
        hits.sort(key=lambda item: item.score, reverse=True)
        return SimpleNamespace(points=hits[:limit])


def _matches_filter(payload: dict[str, Any], query_filter) -> bool:
    for condition in getattr(query_filter, "must", []) or []:
        value = payload.get(condition.key)
        match = condition.match
        if hasattr(match, "value") and value != match.value:
            return False
        if hasattr(match, "any") and value not in set(match.any):
            return False
    return True


def _cosine(left: list[float], right: list[float]) -> float:
    left_norm = math.sqrt(sum(value * value for value in left))
    right_norm = math.sqrt(sum(value * value for value in right))
    if not left_norm or not right_norm:
        return 0.0
    return sum(a * b for a, b in zip(left, right)) / (left_norm * right_norm)
