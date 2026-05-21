from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field


class RetrievalBudget(BaseModel):
    recent: int = Field(default=3, ge=0, le=12)
    promise: int = Field(default=2, ge=0, le=12)
    enemy: int = Field(default=2, ge=0, le=12)
    wealth_status: int = Field(default=2, ge=0, le=12)
    relationship: int = Field(default=1, ge=0, le=12)
    world: int = Field(default=3, ge=0, le=12)


def _memory_type(item: Any) -> str:
    if isinstance(item, dict):
        value = item.get("memory_type") or item.get("type") or "recent"
    else:
        value = getattr(item, "memory_type", "") or getattr(item, "type", "") or "recent"
    normalized = str(value or "recent").strip()
    if normalized in {"wealth", "status", "item", "wealth_status"}:
        return "wealth_status"
    if normalized in {"obligation", "promise", "reader_promise"}:
        return "promise"
    if normalized in {"enemy", "obstacle", "antagonist"}:
        return "enemy"
    if normalized in {"relationship", "faction"}:
        return "relationship"
    if normalized == "world":
        return "world"
    return "recent"


def bucket_memory_results(memories: list[Any], budget: RetrievalBudget) -> dict[str, list[Any]]:
    limits = budget.model_dump()
    buckets = {name: [] for name in limits}
    for memory in memories:
        key = _memory_type(memory)
        if len(buckets[key]) < int(limits[key]):
            buckets[key].append(memory)
    return buckets
