from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class GateOutcome(BaseModel):
    """Versioned measurement record embedded in an existing gate event."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: Literal[1] = 1
    gate_id: str
    gate_version: str = "v1"
    responsibility_domain: str
    scope: Literal["chapter", "band", "arc", "project"]
    candidate_id: str = ""
    chapter_number: int = 0
    band_id: str = ""
    policy_version: int = 0
    evaluated: bool = True
    fired: bool = False
    decision: Literal[
        "pass",
        "warn",
        "block",
        "pause",
        "approve",
        "reject",
        "error",
    ]
    blocked: bool = False
    overridden_by: Literal["", "manual", "spark", "force_accept"] = ""
    issue_keys: list[str] = Field(default_factory=list)
    issue_groups: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    trace_ids: list[str] = Field(default_factory=list)


def attach_gate_outcome(
    payload: Mapping[str, Any] | None,
    outcome: GateOutcome,
) -> dict[str, Any]:
    return {
        **dict(payload or {}),
        "gate_outcome": outcome.model_dump(mode="json"),
    }


def parse_gate_outcome(payload: Mapping[str, Any] | None) -> GateOutcome | None:
    if not isinstance(payload, Mapping):
        return None
    raw = payload.get("gate_outcome")
    if not isinstance(raw, Mapping):
        return None
    try:
        return GateOutcome.model_validate(raw)
    except ValidationError:
        return None


__all__ = ["GateOutcome", "attach_gate_outcome", "parse_gate_outcome"]
