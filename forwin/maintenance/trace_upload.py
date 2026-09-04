"""Durable delivery of frozen post-Canon diagnostics, without business replay."""

from __future__ import annotations

from collections.abc import Callable
import hashlib
import json
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.outbox import OutboxEvent
from forwin.observability.llm_trace import post_canon_trace_artifact_key
from forwin.outbox.store import enqueue_outbox_event
from forwin.outbox.worker import OutboxClaim

TRACE_UPLOAD_REQUESTED = "maintenance.trace.upload.requested"


class TraceUploadPayload(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    schema_version: Literal[1] = 1
    project_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    canon_commit_id: str = Field(pattern=r"^[A-Za-z0-9_-]{1,128}$")
    chapter_number: int = Field(gt=0)
    step_name: Literal["planning", "arc", "world", "feedback", "order_controls"]
    content: str = Field(min_length=1)
    content_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    artifact_key: str = Field(min_length=1)


def _identity(commit_id: str, step: str, digest: str) -> tuple[str, str]:
    base = post_canon_trace_artifact_key(canon_commit_id=commit_id, step_name=step)
    return (
        f"maintenance-trace:{commit_id}:{step}:{digest}",
        base.removesuffix(".json") + f"_{digest}.json",
    )


def enqueue_trace_upload(session: Session, *, trace: dict[str, Any]) -> dict[str, str]:
    content = json.dumps(
        trace, ensure_ascii=False, indent=2, sort_keys=True, default=str
    )
    digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
    event_id, key = _identity(trace["canon_commit_id"], trace["step_name"], digest)
    payload = TraceUploadPayload(
        project_id=trace["project_id"],
        canon_commit_id=trace["canon_commit_id"],
        chapter_number=trace["chapter_number"],
        step_name=trace["step_name"],
        content=content,
        content_sha256=digest,
        artifact_key=key,
    ).model_dump(mode="json")
    existing = session.scalar(
        select(OutboxEvent).where(OutboxEvent.event_id == event_id)
    )
    if existing is None:
        enqueue_outbox_event(
            session,
            aggregate_type="project",
            aggregate_id=trace["project_id"],
            event_type=TRACE_UPLOAD_REQUESTED,
            event_id=event_id,
            payload=payload,
        )
    elif (
        existing.event_type != TRACE_UPLOAD_REQUESTED
        or existing.aggregate_type != "project"
        or existing.aggregate_id != trace["project_id"]
        or json.loads(existing.payload_json) != payload
    ):
        raise ValueError("trace upload identity has conflicting payload")
    return {"event_id": event_id, "artifact_key": key, "hash": digest}


def build_trace_upload_outbox_handlers(
    *,
    artifact_store_provider: Callable[[], Any],
) -> dict[str, Callable[[OutboxClaim], None]]:
    def handle(event: OutboxClaim) -> None:
        payload = TraceUploadPayload.model_validate(dict(event.payload))
        digest = hashlib.sha256(payload.content.encode("utf-8")).hexdigest()
        event_id, key = _identity(payload.canon_commit_id, payload.step_name, digest)
        if (
            event.event_type != TRACE_UPLOAD_REQUESTED
            or event.aggregate_type != "project"
            or event.aggregate_id != payload.project_id
            or event.event_id != event_id
            or payload.content_sha256 != digest
            or payload.artifact_key != key
        ):
            raise ValueError("trace upload envelope identity mismatch")
        trace = json.loads(payload.content)
        if not isinstance(trace, dict) or (
            trace.get("schema_version") != "post-canon-trace-v1"
            or any(
                trace.get(field) != getattr(payload, field)
                for field in (
                    "canon_commit_id",
                    "project_id",
                    "chapter_number",
                    "step_name",
                )
            )
            or not isinstance(trace.get("attempts"), list)
        ):
            raise ValueError("trace upload content identity mismatch")
        artifact_store_provider().save_keyed_artifact(
            project_id=payload.project_id,
            artifact_key=payload.artifact_key,
            content=payload.content,
            content_type="application/json",
        )

    return {TRACE_UPLOAD_REQUESTED: handle}
