from __future__ import annotations

import json
from typing import Any

from pydantic import BaseModel, Field, field_validator


class ArcMacroProgression(BaseModel):
    status_promise: str = ""
    status_tier_from: int = 0
    status_tier_to: int = 0
    wealth_tier_from: int = 0
    wealth_tier_to: int = 0
    enemy_tier_from: int = 0
    enemy_tier_to: int = 0
    market_space_from: str = ""
    market_space_to: str = ""
    ladder_rung_target: str = ""
    required_boundary_evidence: list[str] = Field(default_factory=list)
    forbidden_repetition_patterns: list[str] = Field(default_factory=list)

    @field_validator(
        "status_promise",
        "market_space_from",
        "market_space_to",
        "ladder_rung_target",
        mode="before",
    )
    @classmethod
    def _clean_text(cls, value: object) -> str:
        return str(value or "").strip()

    @field_validator(
        "status_tier_from",
        "status_tier_to",
        "wealth_tier_from",
        "wealth_tier_to",
        "enemy_tier_from",
        "enemy_tier_to",
        mode="before",
    )
    @classmethod
    def _clean_tier(cls, value: object) -> int:
        try:
            return max(0, int(value or 0))
        except (TypeError, ValueError):
            return 0

    @field_validator("required_boundary_evidence", "forbidden_repetition_patterns", mode="before")
    @classmethod
    def _clean_text_list(cls, value: object) -> list[str]:
        if not isinstance(value, list):
            return []
        return [str(item).strip() for item in value if str(item or "").strip()]


def load_arc_macro_progression(arc: object) -> ArcMacroProgression:
    raw = getattr(arc, "macro_progression_json", "{}") or "{}"
    try:
        payload: Any = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        payload = {}
    if not isinstance(payload, dict):
        payload = {}
    return ArcMacroProgression.model_validate(payload)


def dump_arc_macro_progression(progression: ArcMacroProgression) -> str:
    return json.dumps(progression.model_dump(mode="json"), ensure_ascii=False, sort_keys=True)


__all__ = [
    "ArcMacroProgression",
    "dump_arc_macro_progression",
    "load_arc_macro_progression",
]
