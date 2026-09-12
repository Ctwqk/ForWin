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
        match: Any = None
        range: Any = None

    @dataclass
    class Range:
        lte: float | None = None

    @dataclass
    class Filter:
        must: list[Any] = field(default_factory=list)

    @dataclass
    class FilterSelector:
        filter: Any

    @dataclass
    class PointIdsList:
        points: list[str]


class FakeQdrantClient:
    def __init__(self) -> None:
        self.collections: dict[str, dict[str, Any]] = {}
        self.upsert_calls = 0
        self.upserted_point_count = 0
        self.delete_calls = 0
        self.deleted_point_ids: list[Any] = []

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

    def get_collection(self, collection_name: str):
        collection = self.collections[collection_name]
        return SimpleNamespace(
            config=SimpleNamespace(
                params=SimpleNamespace(vectors=collection.get("vectors_config"))
            )
        )

    def upsert(self, *, collection_name: str, points: list[Any]) -> None:
        self.upsert_calls += 1
        self.upserted_point_count += len(points)
        collection = self.collections.setdefault(collection_name, {"points": {}})
        for point in points:
            collection["points"][str(point.id)] = point

    def delete(self, *, collection_name: str, points_selector, wait: bool = True) -> None:  # noqa: ARG002
        self.delete_calls += 1
        collection = self.collections.setdefault(collection_name, {"points": {}})
        if hasattr(points_selector, "points"):
            self.deleted_point_ids.extend(points_selector.points)
            point_ids = [str(point_id) for point_id in points_selector.points]
        else:
            point_ids = [
                point_id
                for point_id, point in collection["points"].items()
                if _matches_filter(point.payload, points_selector.filter)
            ]
        for point_id in point_ids:
            collection["points"].pop(point_id, None)

    def query_points(self, *, collection_name: str, query: list[float], query_filter, limit: int, offset: int = 0):
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
        return SimpleNamespace(points=hits[offset:offset + limit])


def _matches_filter(payload: dict[str, Any], query_filter) -> bool:
    for condition in getattr(query_filter, "must", []) or []:
        value = payload.get(condition.key)
        condition_range = getattr(condition, "range", None)
        if condition_range is not None and (value is None or value > condition_range.lte):
            return False
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
