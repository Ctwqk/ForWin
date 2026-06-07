from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field

InvariantKind = Literal[
    "monotonic_numeric",
    "deadline",
    "terminal_state",
    "state_transition",
    "set_count",
    "active_rule",
    "custom",
]

InvariantStatus = Literal[
    "active",
    "paused",
    "closed",
    "fulfilled",
    "resolved",
    "revoked",
    "warning",
    "conflict",
    "unknown",
]


class CanonInvariant(BaseModel):
    invariant_key: str
    kind: InvariantKind = "custom"
    subject_key: str = ""
    label: str = ""
    current_value: Any | None = None
    value_unit: str = ""
    status: InvariantStatus = "active"
    valid_from_chapter: int = 0
    valid_until_chapter: int | None = None
    last_updated_chapter: int = 0
    constraints: dict[str, Any] = Field(default_factory=dict)
    allowed_bridges: list[str] = Field(default_factory=list)
    evidence_refs: list[str] = Field(default_factory=list)
    source: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)


class InvariantDriftTarget(BaseModel):
    patch_kind: str = "ledger_state_drift"
    suppression_key: str = ""
    invariant_key: str
    kind: InvariantKind = "custom"
    subject_key: str = ""
    label: str = ""
    task: str = ""
    expected: dict[str, Any] = Field(default_factory=dict)
    observed: dict[str, Any] = Field(default_factory=dict)
    allowed_bridges: list[str] = Field(default_factory=list)
    source_signal_id: str = ""
    source_mode: str = "chapter_review_form"


def invariant_from_countdown_state(
    *,
    key: str,
    label: str = "",
    remaining_minutes: int | None,
    status: str = "active",
    chapter_number: int = 0,
    raw_mention: str = "",
    evidence_refs: list[str] | None = None,
    payload: dict[str, Any] | None = None,
) -> CanonInvariant:
    normalized_key = str(key or "").strip() or "main"
    return CanonInvariant(
        invariant_key=f"countdown:{normalized_key}",
        kind="monotonic_numeric",
        subject_key=normalized_key,
        label=str(label or normalized_key),
        current_value=remaining_minutes,
        value_unit="minutes",
        status=_normalize_status(status),
        valid_from_chapter=0,
        last_updated_chapter=int(chapter_number or 0),
        constraints={
            "monotonic": True,
            "cannot_increase_without_bridge": True,
            "raw_mention": raw_mention,
        },
        allowed_bridges=["reset", "reopened", "branch_clock"],
        evidence_refs=list(evidence_refs or []),
        source="countdown_ledger",
        payload=dict(payload or {}),
    )


def invariant_from_active_rule(rule: Any) -> CanonInvariant:
    payload = dict(getattr(rule, "payload", {}) or {})
    raw_invariant = payload.get("invariant") if isinstance(payload, dict) else {}
    if not isinstance(raw_invariant, dict):
        raw_invariant = {}
    rule_key = str(getattr(rule, "rule_key", "") or raw_invariant.get("invariant_key") or "").strip()
    kind = _normalize_kind(raw_invariant.get("kind") or "active_rule")
    return CanonInvariant(
        invariant_key=rule_key,
        kind=kind,
        subject_key=str(raw_invariant.get("subject_key") or rule_key),
        label=str(raw_invariant.get("label") or getattr(rule, "summary", "") or rule_key),
        current_value=raw_invariant.get("current_value"),
        value_unit=str(raw_invariant.get("value_unit") or ""),
        status=_normalize_status(raw_invariant.get("status") or "active"),
        valid_from_chapter=int(getattr(rule, "valid_from_chapter", 0) or 0),
        valid_until_chapter=getattr(rule, "valid_until_chapter", None),
        last_updated_chapter=int(getattr(rule, "valid_from_chapter", 0) or 0),
        constraints=dict(raw_invariant.get("constraints") or {}),
        allowed_bridges=[str(item) for item in raw_invariant.get("allowed_bridges", []) or []],
        evidence_refs=[str(item) for item in raw_invariant.get("evidence_refs", []) or []],
        source="active_rule_store",
        payload=payload,
    )


def legacy_countdown_key_for_invariant(invariant_key: str) -> str:
    return str(invariant_key or "").strip().removeprefix("countdown:")


def _normalize_kind(value: Any) -> InvariantKind:
    normalized = str(value or "").strip()
    allowed = {
        "monotonic_numeric",
        "deadline",
        "terminal_state",
        "state_transition",
        "set_count",
        "active_rule",
        "custom",
    }
    if normalized in allowed:
        return normalized  # type: ignore[return-value]
    return "custom"


def _normalize_status(value: Any) -> InvariantStatus:
    normalized = str(value or "").strip()
    if normalized in {"active", "paused", "closed", "fulfilled", "resolved", "revoked", "warning", "conflict"}:
        return normalized  # type: ignore[return-value]
    if normalized in {"consistent", "open"}:
        return "active"
    if normalized in {"blocked", "error", "failed"}:
        return "conflict"
    return "unknown"


__all__ = [
    "CanonInvariant",
    "InvariantDriftTarget",
    "InvariantKind",
    "InvariantStatus",
    "invariant_from_active_rule",
    "invariant_from_countdown_state",
    "legacy_countdown_key_for_invariant",
]
