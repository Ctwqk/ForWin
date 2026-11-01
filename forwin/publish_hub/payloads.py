"""Request bodies for POST /v1/jobs (payload_version 1).

A body is the complete request: idempotency key, destination, source_ref, action and payload.
Task 4 stores the dict before sending it, and `PublishHubClient.submit` sends it unchanged.
The payload shapes are publish-hub's schemas (additionalProperties false): anything a schema
has no field for is not sent.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

from pydantic import BaseModel

from forwin.publisher_runtime.upload_jobs import UploadJobService

from .refs import (
    chapter_key,
    cover_identity,
    cover_key,
    cover_source_ref_for,
    create_key,
    job_payload,
    source_ref_for,
)

PAYLOAD_VERSION = 1
TAG_GROUPS = ("theme", "role", "plot")
_HASH_CHUNK = 1024 * 1024


def chapter_body(job: Any, destination_id: str) -> dict[str, Any]:
    """upload / novel_chapter for a Canon chapter job."""
    if job.task_kind != "chapter_upload":
        raise ValueError(f"not a chapter_upload job: {job.task_kind!r}")
    return {
        "idempotency_key": chapter_key(job),
        "destination_id": _destination(destination_id),
        "source_ref": source_ref_for(job.project_id, job.chapter_number),
        "content_kind": "novel_chapter",
        "action": "upload",
        "payload_version": PAYLOAD_VERSION,
        "payload": {
            "book_name": job.book_name,
            "chapter_title": job.chapter_title,
            "body_text": job.body_text,
            "mode": "publish" if job.publish else "draft",
        },
        "content_refs": [],
    }


def create_body(
    binding: Mapping[str, Any] | BaseModel,
    destination_id: str,
    project_id: str,
    *,
    cover: Any = None,
) -> dict[str, Any]:
    """create_destination: create the book on the platform for a destination without one.

    `binding` is the project's publish binding (a settings model, a Canon snapshot or the
    snapshot dict a job stores). `cover` is the selected PublisherCoverAsset when there is one;
    it becomes the single optional role=cover content ref.
    """
    fields = _binding_fields(binding)
    platform = str(fields.get("platform") or "").strip()
    if not platform:
        raise ValueError("binding has no platform")
    project = str(project_id or "").strip()
    if not project:
        raise ValueError("project_id must be non-empty")
    destination = _destination(destination_id)
    content_refs = []
    if cover is not None:
        content_refs.append(_file_content_ref(cover.file_path, expected_sha256=None))
    return {
        "idempotency_key": create_key(
            project, platform, binding_snapshot_sha256(binding, destination)
        ),
        "destination_id": destination,
        "action": "create_destination",
        "payload_version": PAYLOAD_VERSION,
        "payload": {"book_meta": hub_book_meta(fields)},
        "content_refs": content_refs,
    }


def cover_body(job: Any, destination_id: str) -> dict[str, Any]:
    """upload_cover / novel_cover for a cover_upload job.

    The file is hashed as ForWin sees it and must match the sha256 recorded when the job was
    created; the extension cannot check it (it only passes the path to the file input).
    """
    _, cover_asset_id = cover_identity(job)
    file_path = job_payload(job).get("file_path")
    return {
        "idempotency_key": cover_key(job),
        "destination_id": _destination(destination_id),
        "source_ref": cover_source_ref_for(job.project_id, cover_asset_id),
        "content_kind": "novel_cover",
        "action": "upload_cover",
        "payload_version": PAYLOAD_VERSION,
        "payload": {},
        "content_refs": [
            _file_content_ref(file_path, expected_sha256=str(job.body_sha256 or ""))
        ],
    }


def hub_book_meta(binding: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    """ForWin's book meta in the create_destination schema.

    Values are normalised the way the existing upload job does it. ForWin keeps theme, role and
    plot tags apart (Fanqie's form has three tag fields) but the schema has one flat `tags`
    list, so each tag carries its group in front: "<group>:<tag>" with group theme, role or
    plot, in that order. The reader splits on the FIRST ":" only, so a tag may contain ":".
    A tag with no group would stay a plain string (the reader treats an entry without a known
    group as ungrouped); every tag ForWin holds has a group, so none is plain today.
    """
    fields = _binding_fields(binding)
    meta = UploadJobService.normalize_book_meta(fields.get("book_meta"))
    return {
        "audience": meta.get("audience", ""),
        "category": meta.get("primary_category", ""),
        "tags": [
            f"{group}:{tag}"
            for group in TAG_GROUPS
            for tag in meta.get(f"{group}_tags", [])
        ],
        "protagonist_names": meta.get("protagonist_names", []),
        "intro": meta.get("intro", ""),
    }


def binding_snapshot_sha256(
    binding: Mapping[str, Any] | BaseModel, destination_id: str
) -> str:
    """Digest of what a create_destination request creates, and where.

    Destination (the one the request is sent to, not the one the binding happens to name),
    platform, book name and book meta. Pointing the same binding at another destination is
    another create and gets another key; route and upload settings are left out, so changing
    them does not give the same book a second key.
    """
    fields = _binding_fields(binding)
    snapshot = {
        "destination_id": _destination(destination_id),
        "platform": str(fields.get("platform") or "").strip(),
        "book_name": str(fields.get("book_name") or "").strip(),
        "book_meta": hub_book_meta(fields),
    }
    canonical = json.dumps(snapshot, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _binding_fields(binding: Mapping[str, Any] | BaseModel) -> dict[str, Any]:
    if isinstance(binding, BaseModel):
        return binding.model_dump(mode="json")
    if isinstance(binding, Mapping):
        return dict(binding)
    raise TypeError(f"unsupported binding type: {type(binding).__name__}")


def _destination(destination_id: str) -> str:
    destination = str(destination_id or "").strip()
    if not destination:
        raise ValueError("destination_id must be non-empty")
    return destination


def _file_content_ref(path: object, *, expected_sha256: str | None) -> dict[str, Any]:
    """A role=cover content ref for a file inside the container, read as ForWin sees it.

    The uri is file:// plus the path the cover_upload job already hands the extension. The extension
    passes that path to DOM.setFileInputFiles, so it must be the path inside publisher-browser.
    """
    file_path = str(path or "").strip()
    if not file_path:
        raise ValueError("cover file path is empty")
    if not os.path.isabs(file_path):
        raise ValueError(f"cover file path must be absolute: {file_path}")
    digest = hashlib.sha256()
    size = 0
    head = b""
    try:
        with open(file_path, "rb") as handle:
            while chunk := handle.read(_HASH_CHUNK):
                if not head:
                    head = chunk[:8]
                digest.update(chunk)
                size += len(chunk)
    except OSError as exc:
        raise ValueError(f"cover file cannot be read: {file_path}") from exc
    if size == 0:
        raise ValueError(f"cover file is empty: {file_path}")
    sha256 = digest.hexdigest()
    if expected_sha256 is not None and sha256 != str(expected_sha256).strip().lower():
        raise ValueError(
            f"cover file sha256 differs from the one recorded for the upload: {file_path}"
        )
    return {
        "uri": "file://" + quote(file_path, safe="/"),
        "sha256": sha256,
        "size": size,
        "mime": _image_mime(head),
        "role": "cover",
    }


def _image_mime(head: bytes) -> str:
    if head.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if head.startswith(b"\xff\xd8"):
        return "image/jpeg"
    return "application/octet-stream"


__all__ = [
    "binding_snapshot_sha256",
    "chapter_body",
    "cover_body",
    "create_body",
    "hub_book_meta",
]
