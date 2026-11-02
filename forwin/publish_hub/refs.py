"""The identities ForWin gives publish-hub, in one place.

Import, submit and sync all build `source_ref` and idempotency keys here, so the same chapter
or cover always gets the same names.

* `source_ref` names the thing being published and survives regeneration. A chapter's does not
  contain the candidate id: a regenerated candidate of chapter 3 is still publication
  project/<id>/chapter/3, which is what lets publish-hub refuse a second publish.
* the idempotency key names one request and is built from the existing ForWin identities, so a
  resend of the same request finds the same hub job.
"""

from __future__ import annotations

import json
from typing import Any

from forwin.publisher_runtime.idempotency import (
    publisher_cover_upload_idempotency_key,
    publisher_job_idempotency_key,
)

KEY_PREFIX = "forwin:"


def source_ref_for(project_id: str, chapter_number: int | str) -> str:
    """The publication of a chapter. Never includes the candidate id."""
    project = _required(project_id, "project_id")
    chapter = int(chapter_number or 0)
    if chapter <= 0:
        raise ValueError("chapter_number must be positive")
    return f"project/{project}/chapter/{chapter}"


def cover_source_ref_for(project_id: str, cover_asset_id: str) -> str:
    """The publication of one cover asset.

    One per asset, not one per project: publish-hub never accepts a second successful upload on a
    publication, so a fixed per-project ref would block every later cover replacement, while a
    per-asset ref still stops the same asset from being uploaded twice.
    """
    project = _required(project_id, "project_id")
    asset = _required(cover_asset_id, "cover_asset_id")
    return f"project/{project}/cover/{asset}"


def chapter_key(job: Any) -> str:
    """Idempotency key of a Canon chapter job, from the same fields that make job.idempotency_key."""
    canon = job_payload(job).get("canon_identity")
    canon = canon if isinstance(canon, dict) else {}
    return KEY_PREFIX + publisher_job_idempotency_key(
        canon_idempotency_key=canon.get("canon_idempotency_key", ""),
        project_id=job.project_id,
        chapter_number=job.chapter_number,
        candidate_id=job.candidate_id,
        platform_id=job.platform_id,
    )


def cover_key(job: Any) -> str:
    """Idempotency key of a cover upload job, from the fields that make job.idempotency_key."""
    work_binding_id, cover_asset_id = cover_identity(job)
    return KEY_PREFIX + publisher_cover_upload_idempotency_key(
        work_binding_id=work_binding_id,
        platform_id=job.platform_id,
        cover_asset_id=cover_asset_id,
        content_sha256=job.body_sha256,
    )


def create_key(project_id: str, platform: str, snapshot_sha: str) -> str:
    """Idempotency key of the create_destination job for one binding snapshot."""
    return (
        f"{KEY_PREFIX}create:{_required(project_id, 'project_id')}"
        f":{_required(platform, 'platform')}:{_required(snapshot_sha, 'snapshot_sha')}"
    )


def cover_identity(job: Any) -> tuple[str, str]:
    """(work_binding_id, cover_asset_id) of a cover_upload job."""
    if job.task_kind != "cover_upload":
        raise ValueError(f"not a cover_upload job: {job.task_kind!r}")
    payload = job_payload(job)
    return (
        _required(payload.get("work_binding_id"), "work_binding_id"),
        _required(payload.get("cover_asset_id"), "cover_asset_id"),
    )


def job_payload(job: Any) -> dict[str, Any]:
    """The job's result_payload_json as a dict (empty when it holds no object)."""
    try:
        payload = json.loads(job.result_payload_json or "{}")
    except ValueError as exc:
        raise ValueError("job result_payload_json is not valid JSON") from exc
    return payload if isinstance(payload, dict) else {}


def _required(value: object, name: str) -> str:
    text = str(value or "").strip()
    if not text:
        raise ValueError(f"{name} must be non-empty")
    return text


__all__ = [
    "KEY_PREFIX",
    "chapter_key",
    "cover_identity",
    "cover_key",
    "cover_source_ref_for",
    "create_key",
    "job_payload",
    "source_ref_for",
]
