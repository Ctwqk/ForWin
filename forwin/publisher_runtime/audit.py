from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select

from forwin.governance import DecisionEventInfo, DecisionEventType
from forwin.models.governance import DecisionEvent
from forwin.models.publisher import PublisherCommentSyncJob, PublisherUploadJob
from forwin.observability.context import OperationContext
from forwin.observability.ports import NullObservability
from forwin.state.updater import StateUpdater


def terminal_upload_event_type(status: str) -> str:
    if status == "succeeded":
        return DecisionEventType.UPLOAD_JOB_SUCCEEDED
    if status == "failed":
        return DecisionEventType.UPLOAD_JOB_FAILED
    if status == "cancelled":
        return DecisionEventType.UPLOAD_JOB_CANCELLED
    return DecisionEventType.UPLOAD_JOB_PROGRESS


def comment_sync_event_type(status: str) -> str:
    if status == "succeeded":
        return DecisionEventType.COMMENT_SYNC_SUCCEEDED
    if status == "failed":
        return DecisionEventType.COMMENT_SYNC_FAILED
    return DecisionEventType.COMMENT_SYNC_JOB_CLAIMED


class PublisherAuditService:
    def __init__(self, *, session_factory, observability=None) -> None:
        self.session_factory = session_factory
        self.observability = observability or NullObservability()

    def record_project_event(
        self,
        session,
        *,
        project_id: str,
        event_type: str,
        summary: str,
        payload: dict[str, Any],
        related_object_type: str,
        related_object_id: str,
        actor_type: str = "extension",
        event_family: str = "business_event",
    ) -> DecisionEvent | None:
        normalized_project_id = str(project_id or "").strip()
        if not normalized_project_id:
            return None
        parent = session.execute(
            select(DecisionEvent)
            .where(
                DecisionEvent.project_id == normalized_project_id,
                DecisionEvent.related_object_type == related_object_type,
                DecisionEvent.related_object_id == related_object_id,
            )
            .order_by(DecisionEvent.created_at.desc(), DecisionEvent.id.desc())
            .limit(1)
        ).scalar_one_or_none()
        parent_id = str(parent.id if parent is not None else "")
        causal_root_id = str(
            parent.causal_root_id
            if parent is not None and parent.causal_root_id
            else parent_id
        )
        obs_context = OperationContext(
            project_id=normalized_project_id,
            stage=f"publisher.{event_type}",
        )
        with self.observability.span(
            obs_context,
            f"publisher.{event_type}",
            span_kind="publisher",
            component="publisher",
            tags={
                "related_object_type": related_object_type,
                "related_object_id": related_object_id,
            },
        ):
            return StateUpdater(session).save_decision_event(
                DecisionEventInfo(
                    project_id=normalized_project_id,
                    scope="publisher",
                    event_family=event_family,
                    event_type=event_type,
                    actor_type=actor_type,
                    summary=summary,
                    payload=payload,
                    related_object_type=related_object_type,
                    related_object_id=related_object_id,
                    parent_event_id=parent_id,
                    causal_root_id=causal_root_id,
                )
            )

    def record_upload_job_event(
        self,
        session,
        *,
        job: PublisherUploadJob,
        event_type: str,
        summary: str,
        actor_type: str = "extension",
        extra_payload: dict[str, Any] | None = None,
    ) -> DecisionEvent | None:
        payload = {
            "platform_id": job.platform_id,
            "job_id": job.id,
            "task_kind": str(job.task_kind or "chapter_upload"),
            "status": job.status,
            "publish": bool(job.publish),
            "create_if_missing": False,
            "book_name": job.book_name,
            "chapter_title": job.chapter_title,
            "body_chars": len(str(job.body_text or "")),
            "upload_url_present": bool(str(job.upload_url or "").strip()),
            "extension_client_id": job.extension_client_id,
            "current_url": job.current_url,
        }
        try:
            result_payload = json.loads(job.result_payload_json or "{}")
        except json.JSONDecodeError:
            result_payload = {}
        if isinstance(result_payload, dict):
            payload["create_if_missing"] = bool(
                result_payload.get("create_if_missing", False)
            )
        if extra_payload:
            payload.update(extra_payload)
        return self.record_project_event(
            session,
            project_id=job.project_id,
            event_type=event_type,
            summary=summary,
            payload=payload,
            related_object_type="publisher_upload_job",
            related_object_id=job.id,
            actor_type=actor_type,
        )

    def record_comment_sync_event(
        self,
        session,
        *,
        job: PublisherCommentSyncJob,
        event_type: str,
        summary: str,
        actor_type: str = "extension",
        extra_payload: dict[str, Any] | None = None,
    ) -> DecisionEvent | None:
        payload = {
            "platform_id": job.platform_id,
            "job_id": job.id,
            "status": job.status,
            "work_id": job.work_id,
            "work_name": job.work_name,
            "chapter_id": job.chapter_id,
            "chapter_title": job.chapter_title,
            "limit": int(job.limit or 0),
            "extension_client_id": job.extension_client_id,
        }
        if extra_payload:
            payload.update(extra_payload)
        return self.record_project_event(
            session,
            project_id=job.project_id,
            event_type=event_type,
            summary=summary,
            payload=payload,
            related_object_type="publisher_comment_sync_job",
            related_object_id=job.id,
            actor_type=actor_type,
        )
