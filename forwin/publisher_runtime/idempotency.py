from __future__ import annotations

import hashlib


_PUBLISHER_CHAPTER_IDENTITY_VERSION = "publisher-chapter:v1"
_PUBLISHER_COVER_IDENTITY_VERSION = "publisher-cover:v1"


def publisher_job_idempotency_key(
    *,
    canon_idempotency_key: str,
    project_id: str,
    chapter_number: int,
    candidate_id: str,
    platform_id: str,
) -> str:
    chapter = int(chapter_number or 0)
    if chapter <= 0:
        raise ValueError("chapter_number must be positive")
    components = (
        _PUBLISHER_CHAPTER_IDENTITY_VERSION,
        _identity_component(canon_idempotency_key, "canon_idempotency_key"),
        _identity_component(project_id, "project_id"),
        str(chapter),
        _identity_component(candidate_id, "candidate_id"),
        _identity_component(platform_id, "platform_id"),
    )
    return hashlib.sha256("\0".join(components).encode("utf-8")).hexdigest()


def publisher_cover_upload_idempotency_key(
    *,
    work_binding_id: str,
    platform_id: str,
    cover_asset_id: str,
    content_sha256: str,
) -> str:
    normalized_hash = _identity_component(content_sha256, "content_sha256")
    if len(normalized_hash) != 64 or any(
        character not in "0123456789abcdef" for character in normalized_hash
    ):
        raise ValueError("content_sha256 must be lowercase SHA-256 hex")
    components = (
        _PUBLISHER_COVER_IDENTITY_VERSION,
        _identity_component(work_binding_id, "work_binding_id"),
        _identity_component(platform_id, "platform_id"),
        _identity_component(cover_asset_id, "cover_asset_id"),
        normalized_hash,
    )
    return hashlib.sha256("\0".join(components).encode("utf-8")).hexdigest()


def _identity_component(value: str, name: str) -> str:
    normalized = str(value or "").strip()
    if not normalized:
        raise ValueError(f"{name} must be non-empty")
    if "\0" in normalized:
        raise ValueError(f"{name} cannot contain NUL")
    return normalized


__all__ = [
    "publisher_cover_upload_idempotency_key",
    "publisher_job_idempotency_key",
]
