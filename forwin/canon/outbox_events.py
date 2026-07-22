from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from forwin.publishers.platforms import normalize_supported_platform


CANON_PROJECTION_REQUESTED = "canon.projection.requested"
CANON_PHASE3_REQUESTED = "canon.phase3.requested"
CANON_PUBLISHER_REQUESTED = "canon.publisher.requested"
CANON_RECOVERY_EVENT_TYPES = (
    CANON_PROJECTION_REQUESTED,
    CANON_PHASE3_REQUESTED,
    CANON_PUBLISHER_REQUESTED,
)
CANON_EVENT_SCHEMA_VERSION = 1


class _FrozenCanonEventModel(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")


class CanonPublisherBookMetaSnapshot(_FrozenCanonEventModel):
    audience: str = ""
    primary_category: str = ""
    theme_tags: tuple[str, ...] = ()
    role_tags: tuple[str, ...] = ()
    plot_tags: tuple[str, ...] = ()
    protagonist_names: tuple[str, ...] = ()
    intro: str = ""

    @field_validator("audience", "primary_category", "intro")
    @classmethod
    def _optional_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator(
        "theme_tags",
        "role_tags",
        "plot_tags",
        "protagonist_names",
        mode="before",
    )
    @classmethod
    def _normalized_tags(cls, value: object) -> tuple[str, ...]:
        if value is None:
            return ()
        if not isinstance(value, (list, tuple)):
            raise ValueError("publisher book metadata tags must be a list")
        return tuple(
            normalized
            for item in value
            if (normalized := str(item or "").strip())
        )


class CanonPublisherBindingSnapshot(_FrozenCanonEventModel):
    platform: str
    book_name: str
    upload_url: str = ""
    create_if_missing: bool = False
    cover_generation_enabled: bool = True
    cover_confirmation_required: bool = False
    cover_candidate_count: int = 4
    cover_style_hint: str = ""
    auto_cover_upload_enabled: bool = True
    publisher_compliance_required: bool = True
    book_meta: CanonPublisherBookMetaSnapshot = Field(
        default_factory=CanonPublisherBookMetaSnapshot
    )

    @field_validator("platform")
    @classmethod
    def _supported_platform(cls, value: str) -> str:
        return normalize_supported_platform(value)

    @field_validator("book_name")
    @classmethod
    def _required_text(cls, value: str, info) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError(f"{info.field_name} must be non-empty")
        return normalized

    @field_validator("upload_url", "cover_style_hint")
    @classmethod
    def _optional_text(cls, value: str) -> str:
        return str(value or "").strip()

    @field_validator("cover_candidate_count")
    @classmethod
    def _candidate_count(cls, value: int) -> int:
        return max(1, min(int(value or 4), 8))


class CanonEventPayload(_FrozenCanonEventModel):
    schema_version: Literal[1] = CANON_EVENT_SCHEMA_VERSION
    canon_commit_id: str = Field(min_length=1)
    canon_idempotency_key: str = Field(min_length=1)
    project_id: str = Field(min_length=1)
    chapter_number: int = Field(gt=0)
    candidate_id: str = Field(min_length=1)

    @field_validator(
        "canon_commit_id",
        "canon_idempotency_key",
        "project_id",
        "candidate_id",
    )
    @classmethod
    def _identity_text(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("Canon event identity must be non-empty")
        return normalized


class CanonProjectionEventPayload(CanonEventPayload):
    trigger: Literal["canon_commit"] = "canon_commit"


class CanonPhase3EventPayload(CanonEventPayload):
    pass


class CanonPublisherEventPayload(CanonEventPayload):
    chapter_title: str = Field(min_length=1)
    body_sha256: str = Field(min_length=1)
    publisher_bindings: tuple[CanonPublisherBindingSnapshot, ...] = ()
    publish: bool = False

    @field_validator("chapter_title")
    @classmethod
    def _chapter_title(cls, value: str) -> str:
        normalized = str(value or "").strip()
        if not normalized:
            raise ValueError("chapter_title must be non-empty")
        return normalized


class CanonOutboxEvent(_FrozenCanonEventModel):
    event_type: str
    payload: dict[str, Any]
    aggregate_type: Literal["project"] = "project"
    aggregate_id: str = Field(min_length=1)
    event_id: str = Field(min_length=1)


_PAYLOAD_MODELS = {
    CANON_PROJECTION_REQUESTED: CanonProjectionEventPayload,
    CANON_PHASE3_REQUESTED: CanonPhase3EventPayload,
    CANON_PUBLISHER_REQUESTED: CanonPublisherEventPayload,
}


def canon_commit_id(canon_idempotency_key: str) -> str:
    key = _required_text(canon_idempotency_key, "canon_idempotency_key")
    return hashlib.sha256(f"canon-commit:v1\0{key}".encode("utf-8")).hexdigest()


def canon_event_id(canon_idempotency_key: str, event_type: str) -> str:
    key = _required_text(canon_idempotency_key, "canon_idempotency_key")
    normalized_type = str(event_type or "").strip()
    if normalized_type not in CANON_RECOVERY_EVENT_TYPES:
        raise ValueError(f"unsupported Canon recovery event: {normalized_type}")
    return f"{key}:{normalized_type}"


def publisher_binding_snapshot(
    automation: str | Mapping[str, Any] | None,
    *,
    default_book_name: str,
) -> tuple[CanonPublisherBindingSnapshot, ...]:
    payload = _automation_payload(automation)
    raw_bindings = payload.get("publish_bindings")
    candidates = list(raw_bindings) if isinstance(raw_bindings, list) else []
    primary_binding = payload.get("publish")
    configured_platforms = {
        str(item.get("platform") or "").strip()
        for item in candidates
        if isinstance(item, Mapping)
    }
    primary_platform = (
        str(primary_binding.get("platform") or "").strip()
        if isinstance(primary_binding, Mapping)
        else ""
    )
    if primary_platform and primary_platform not in configured_platforms:
        candidates.insert(0, primary_binding)

    bindings: list[CanonPublisherBindingSnapshot] = []
    seen_platforms: set[str] = set()
    fallback_name = _required_text(default_book_name, "default_book_name")
    for raw in candidates:
        if not isinstance(raw, Mapping):
            raise ValueError("publisher binding snapshot must contain objects")
        platform = str(raw.get("platform") or "").strip()
        if not platform:
            continue
        if platform in seen_platforms:
            raise ValueError(f"duplicate publisher platform in Canon snapshot: {platform}")
        normalized = dict(raw)
        normalized["platform"] = platform
        normalized["book_name"] = str(raw.get("book_name") or "").strip() or fallback_name
        bindings.append(CanonPublisherBindingSnapshot.model_validate(normalized))
        seen_platforms.add(platform)
    return tuple(sorted(bindings, key=lambda item: item.platform))


def build_canon_recovery_events(
    *,
    canon_idempotency_key: str,
    canon_commit_id_value: str,
    project_id: str,
    chapter_number: int,
    candidate_id: str,
    chapter_title: str,
    body_sha256: str,
    publisher_bindings: Sequence[
        Mapping[str, Any] | CanonPublisherBindingSnapshot
    ] = (),
) -> tuple[CanonOutboxEvent, ...]:
    normalized_bindings = _normalize_bindings(publisher_bindings)
    common = {
        "schema_version": CANON_EVENT_SCHEMA_VERSION,
        "canon_commit_id": _required_text(canon_commit_id_value, "canon_commit_id"),
        "canon_idempotency_key": _required_text(
            canon_idempotency_key,
            "canon_idempotency_key",
        ),
        "project_id": _required_text(project_id, "project_id"),
        "chapter_number": int(chapter_number or 0),
        "candidate_id": _required_text(candidate_id, "candidate_id"),
    }
    payloads: tuple[tuple[str, CanonEventPayload], ...] = (
        (
            CANON_PROJECTION_REQUESTED,
            CanonProjectionEventPayload(**common),
        ),
        (
            CANON_PHASE3_REQUESTED,
            CanonPhase3EventPayload(**common),
        ),
        (
            CANON_PUBLISHER_REQUESTED,
            CanonPublisherEventPayload(
                **common,
                chapter_title=chapter_title,
                body_sha256=_required_text(body_sha256, "body_sha256"),
                publisher_bindings=normalized_bindings,
                publish=False,
            ),
        ),
    )
    return tuple(
        CanonOutboxEvent(
            event_type=event_type,
            aggregate_id=common["project_id"],
            event_id=canon_event_id(common["canon_idempotency_key"], event_type),
            payload=payload.model_dump(mode="json"),
        )
        for event_type, payload in payloads
    )


def parse_canon_event_payload(
    event_type: str,
    payload: Mapping[str, object],
) -> CanonEventPayload:
    normalized_type = str(event_type or "").strip()
    model = _PAYLOAD_MODELS.get(normalized_type)
    if model is None:
        raise ValueError(f"unsupported Canon recovery event: {normalized_type}")
    return model.model_validate(dict(payload))


def parse_canon_event_envelope(
    *,
    event_type: str,
    event_id: str,
    aggregate_type: str,
    aggregate_id: str,
    payload: Mapping[str, object],
) -> CanonEventPayload:
    parsed = parse_canon_event_payload(event_type, payload)
    expected_commit_id = canon_commit_id(parsed.canon_idempotency_key)
    if parsed.canon_commit_id != expected_commit_id:
        raise ValueError("Canon recovery event commit ID is not deterministic")
    if str(aggregate_type or "").strip() != "project":
        raise ValueError("Canon recovery event aggregate type must be project")
    if str(aggregate_id or "").strip() != parsed.project_id:
        raise ValueError("Canon recovery event aggregate identity mismatch")
    expected_event_id = canon_event_id(parsed.canon_idempotency_key, event_type)
    if str(event_id or "").strip() != expected_event_id:
        raise ValueError("Canon recovery event ID is not deterministic")
    return parsed


def _normalize_bindings(
    bindings: Sequence[Mapping[str, Any] | CanonPublisherBindingSnapshot],
) -> tuple[CanonPublisherBindingSnapshot, ...]:
    normalized: list[CanonPublisherBindingSnapshot] = []
    seen_platforms: set[str] = set()
    for item in bindings:
        binding = (
            item
            if isinstance(item, CanonPublisherBindingSnapshot)
            else CanonPublisherBindingSnapshot.model_validate(item)
        )
        if binding.platform in seen_platforms:
            raise ValueError(
                f"duplicate publisher platform in Canon snapshot: {binding.platform}"
            )
        normalized.append(binding)
        seen_platforms.add(binding.platform)
    return tuple(sorted(normalized, key=lambda item: item.platform))


def _automation_payload(
    automation: str | Mapping[str, Any] | None,
) -> dict[str, Any]:
    if isinstance(automation, Mapping):
        return dict(automation)
    try:
        payload = json.loads(str(automation or "{}"))
    except (TypeError, json.JSONDecodeError) as exc:
        raise ValueError("project publisher automation is invalid JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("project publisher automation must be an object")
    return payload


def _required_text(value: object, field_name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{field_name} must be non-empty")
    return normalized


__all__ = [
    "CANON_EVENT_SCHEMA_VERSION",
    "CANON_PHASE3_REQUESTED",
    "CANON_PROJECTION_REQUESTED",
    "CANON_PUBLISHER_REQUESTED",
    "CANON_RECOVERY_EVENT_TYPES",
    "CanonEventPayload",
    "CanonOutboxEvent",
    "CanonPhase3EventPayload",
    "CanonProjectionEventPayload",
    "CanonPublisherBindingSnapshot",
    "CanonPublisherBookMetaSnapshot",
    "CanonPublisherEventPayload",
    "build_canon_recovery_events",
    "canon_commit_id",
    "canon_event_id",
    "parse_canon_event_payload",
    "parse_canon_event_envelope",
    "publisher_binding_snapshot",
]
