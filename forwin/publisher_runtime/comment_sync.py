from __future__ import annotations

import json
from dataclasses import asdict
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.dialects.postgresql import insert as pg_insert

from forwin.audit.events import DecisionEventType
from forwin.models.publisher import (
    PublisherCommentSyncJob,
    PublisherConnectionState,
    PublisherRawComment,
)

from .audit import PublisherAuditService, comment_sync_event_type
from .browser_sessions import isoformat, utc_now
from .comment_source import (
    body_hash,
    observed_time,
    resolve_comment_source,
    source_hash,
)
from .connection_state import ExtensionConnectionService
from .platform_catalog import PlatformCatalog


def as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class CommentSyncService:
    def __init__(
        self,
        *,
        session_factory,
        platform_catalog: PlatformCatalog,
        connection_state: ExtensionConnectionService,
        audit: PublisherAuditService,
    ) -> None:
        self.session_factory = session_factory
        self.platform_catalog = platform_catalog
        self.connection_state = connection_state
        self.audit = audit

    def create_comment_sync_job(
        self,
        *,
        project_id: str = "",
        platform: str,
        work_id: str,
        work_name: str,
        chapter_id: str,
        chapter_title: str,
        limit: int,
    ) -> dict[str, Any]:
        self.platform_catalog.get(platform)
        with self.session_factory() as session:
            job = PublisherCommentSyncJob(
                project_id=resolve_comment_source(
                    session,
                    platform=platform,
                    item={"project_id": project_id, "work_id": work_id},
                ).project_id,
                platform_id=platform,
                status="pending",
                work_id=work_id,
                work_name=work_name,
                chapter_id=chapter_id,
                chapter_title=chapter_title,
                limit=limit,
            )
            session.add(job)
            session.flush()
            self.audit.record_comment_sync_event(
                session,
                job=job,
                event_type=DecisionEventType.COMMENT_SYNC_JOB_CREATED,
                summary="评论同步任务已创建。",
                actor_type="api",
            )
            session.commit()
            session.refresh(job)
            return self.serialize_comment_sync_job(job)

    def claim_next_comment_sync_job(
        self,
        *,
        client_id: str,
        connected_platforms: list[str],
    ) -> dict[str, Any] | None:
        platforms = [
            platform
            for platform in connected_platforms
            if self.platform_catalog.has(platform)
        ]
        if not platforms:
            return None

        now = utc_now()
        with self.session_factory() as session:
            job = session.execute(
                select(PublisherCommentSyncJob)
                .where(
                    PublisherCommentSyncJob.status == "running",
                    PublisherCommentSyncJob.finished_at.is_(None),
                    PublisherCommentSyncJob.extension_client_id == client_id,
                    PublisherCommentSyncJob.platform_id.in_(platforms),
                )
                .order_by(
                    PublisherCommentSyncJob.started_at.asc(),
                    PublisherCommentSyncJob.created_at.asc(),
                )
                .limit(1)
            ).scalar_one_or_none()
            if job is not None:
                return self.serialize_comment_sync_job(job)

            claimable_platforms = self.connection_state.claimable_platforms(
                session,
                client_id=client_id,
                platforms=platforms,
            )
            if not claimable_platforms:
                return None

            while True:
                job = session.execute(
                    select(PublisherCommentSyncJob)
                    .where(
                        PublisherCommentSyncJob.status == "pending",
                        PublisherCommentSyncJob.platform_id.in_(claimable_platforms),
                    )
                    .order_by(PublisherCommentSyncJob.created_at.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if job is None:
                    return None

                started_at = job.started_at or now
                claimed = session.execute(
                    update(PublisherCommentSyncJob)
                    .where(
                        PublisherCommentSyncJob.id == job.id,
                        PublisherCommentSyncJob.status == "pending",
                    )
                    .values(
                        status="running",
                        extension_client_id=client_id,
                        started_at=started_at,
                        error_message="",
                        result_summary_json=json.dumps(
                            {
                                "phase": "claimed",
                                "message": "评论同步任务已被浏览器扩展自动领取。",
                            },
                            ensure_ascii=False,
                        ),
                    )
                )
                if not claimed.rowcount:
                    session.rollback()
                    continue

                session.flush()
                job.status = "running"
                job.extension_client_id = client_id
                job.started_at = started_at
                job.error_message = ""
                job.result_summary_json = json.dumps(
                    {
                        "phase": "claimed",
                        "message": "评论同步任务已被浏览器扩展自动领取。",
                    },
                    ensure_ascii=False,
                )
                self.audit.record_comment_sync_event(
                    session,
                    job=job,
                    event_type=DecisionEventType.COMMENT_SYNC_JOB_CLAIMED,
                    summary="评论同步任务已被浏览器扩展领取。",
                    actor_type="extension",
                )
                session.commit()
                session.refresh(job)
                return self.serialize_comment_sync_job(job)

    def update_comment_sync_job_result(
        self,
        *,
        job_id: str,
        client_id: str,
        status: str,
        message: str,
        error: str,
        result_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"running", "succeeded", "failed"}:
            raise ValueError("不支持的评论同步任务状态。")

        now = utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherCommentSyncJob, job_id)
            if job is None:
                raise ValueError("评论同步任务不存在。")

            self.connection_state.ensure_extension_client(session, client_id)
            if client_id:
                job.extension_client_id = client_id
            if status == "running":
                job.started_at = job.started_at or now
                job.error_message = ""
            elif status in {"succeeded", "failed"}:
                job.started_at = job.started_at or now
                job.finished_at = now

            try:
                merged_payload = json.loads(job.result_summary_json or "{}")
            except json.JSONDecodeError:
                merged_payload = {}
            if not isinstance(merged_payload, dict):
                merged_payload = {}
            merged_payload.update(
                {
                    "message": str(message or "").strip(),
                    "status": status,
                }
            )
            if result_payload:
                merged_payload.update(result_payload)

            job.status = status
            job.error_message = str(error or "").strip()
            job.result_summary_json = json.dumps(merged_payload, ensure_ascii=False)

            self.audit.record_comment_sync_event(
                session,
                job=job,
                event_type=comment_sync_event_type(status),
                summary=f"评论同步任务状态更新为 {status}。",
                actor_type="extension",
                extra_payload={
                    "error_class": "comment_sync_error" if job.error_message else "",
                    "error_message": job.error_message,
                    "comment_count": int(merged_payload.get("comment_count") or 0)
                    if isinstance(merged_payload, dict)
                    else 0,
                },
            )
            session.commit()
            session.refresh(job)
            return self.serialize_comment_sync_job(job)

    def ingest_comments_batch(
        self,
        *,
        client_id: str,
        platform: str,
        comments: list[dict[str, Any]],
        job_id: str = "",
    ) -> dict[str, Any]:
        self.platform_catalog.get(platform)
        now = utc_now()
        inserted = 0
        updated = 0
        touched_project_ids: set[str] = set()

        with self.session_factory() as session:
            self.connection_state.ensure_extension_client(session, client_id)
            sync_job = None
            if job_id:
                job = session.get(PublisherCommentSyncJob, job_id)
                if job is not None:
                    sync_job = job
                    job.extension_client_id = client_id
                    job.status = "running"
                    job.started_at = job.started_at or now

            for item in sorted(
                comments,
                key=lambda row: (
                    str(row.get("work_id", "")),
                    str(row.get("remote_comment_id", "")),
                    str(row.get("account_id", "")),
                ),
            ):
                remote_comment_id = str(item.get("remote_comment_id", "")).strip()
                if not remote_comment_id:
                    continue
                source = resolve_comment_source(
                    session, platform=platform, item=item, job=sync_job
                )
                inserted_id = session.scalar(
                    pg_insert(PublisherRawComment)
                    .values(
                        platform_id=platform,
                        remote_comment_id=remote_comment_id,
                        ingested_at=now,
                        **asdict(source),
                    )
                    .on_conflict_do_nothing(
                        constraint="uq_publisher_raw_comments_scoped_remote"
                    )
                    .returning(PublisherRawComment.id)
                )
                row = session.scalar(
                    select(PublisherRawComment)
                    .where(
                        PublisherRawComment.platform_id == platform,
                        PublisherRawComment.source_scope == source.source_scope,
                        PublisherRawComment.work_id == source.work_id,
                        PublisherRawComment.remote_comment_id == remote_comment_id,
                    )
                    .with_for_update()
                )
                if inserted_id:
                    inserted += 1
                else:
                    updated += 1
                observed = observed_time(item.get("observed_at")) or now.replace(
                    tzinfo=None
                )
                if row.observed_at and observed < row.observed_at.replace(tzinfo=None):
                    continue
                if (
                    row.project_id
                    and source.project_id
                    and row.project_id != source.project_id
                ):
                    raise ValueError(
                        "comment remote identity conflicts with its persisted project"
                    )
                if (
                    row.chapter_id
                    and source.chapter_id
                    and row.chapter_id != source.chapter_id
                ):
                    raise ValueError(
                        "comment remote identity conflicts with its persisted chapter"
                    )
                # Work identity can be proven even when no remote chapter is known.
                for name in ("project_id", "work_binding_id", "account_id", "chapter_id"):
                    value = getattr(source, name)
                    if value:
                        setattr(row, name, value)
                if source.source_status != "unknown":
                    if row.source_status != "unknown" and (
                        row.source_chapter_plan_id != source.source_chapter_plan_id
                        or (
                            row.source_canon_commit_id
                            and source.source_canon_commit_id
                            and row.source_canon_commit_id
                            != source.source_canon_commit_id
                        )
                    ):
                        raise ValueError(
                            "comment source conflicts with previously proven publication identity"
                        )
                    for name, value in asdict(source).items():
                        if value is not None and value != "":
                            setattr(row, name, value)
                if row.project_id:
                    touched_project_ids.add(str(row.project_id))
                row.work_name = str(
                    item.get("work_name") or getattr(sync_job, "work_name", "") or ""
                ).strip()
                row.chapter_title = str(
                    item.get("chapter_title")
                    or getattr(sync_job, "chapter_title", "")
                    or ""
                ).strip()
                row.author_id = str(item.get("author_id", "")).strip()
                row.author_name = str(item.get("author_name", "")).strip()
                row.body_text = str(item.get("body", "")).strip()
                content_sha256 = body_hash(row.body_text)
                if row.content_sha256 != content_sha256:
                    row.active_analysis_id = ""
                row.content_sha256 = content_sha256
                row.parent_remote_comment_id = str(
                    item.get("parent_remote_comment_id", "")
                ).strip()
                row.remote_created_at = str(item.get("created_at", "")).strip()
                row.observed_at = observed
                row.like_count = max(0, as_int(item.get("like_count", 0)))
                row.reply_count = max(0, as_int(item.get("reply_count", 0)))
                row.raw_payload_json = json.dumps(
                    item.get("raw_payload", item), ensure_ascii=False
                )
                row.synced_at = now
                source_sha256 = source_hash(row)
                if row.source_sha256 != source_sha256:
                    row.active_analysis_id = ""
                row.source_sha256 = source_sha256
                session.flush()

            if job_id:
                job = session.get(PublisherCommentSyncJob, job_id)
                if job is not None:
                    sync_job = job
                    job.status = "succeeded"
                    job.finished_at = now
                    job.result_summary_json = json.dumps(
                        {"inserted": inserted, "updated": updated},
                        ensure_ascii=False,
                    )
                    self.audit.record_comment_sync_event(
                        session,
                        job=job,
                        event_type=DecisionEventType.COMMENT_SYNC_SUCCEEDED,
                        summary="评论同步任务已完成并入库。",
                        actor_type="extension",
                        extra_payload={
                            "inserted": inserted,
                            "updated": updated,
                            "comment_count": inserted + updated,
                        },
                    )

            if self.platform_catalog.has(platform):
                state = session.get(PublisherConnectionState, platform)
                if state is None:
                    state = PublisherConnectionState(platform_id=platform)
                    session.add(state)
                state.extension_client_id = client_id
                state.last_heartbeat_at = now
                self.connection_state.upsert_extension_platform_state(
                    session,
                    client_id=client_id,
                    platform_id=platform,
                    connected=bool(state.connected),
                    login_method=state.login_method,
                    last_error=state.last_error,
                    status_payload={
                        "platform": platform,
                        "connected": bool(state.connected),
                        "login_method": state.login_method,
                        "last_error": state.last_error,
                        "source": "comment-batch-ingest",
                    },
                    last_heartbeat_at=now,
                )

            for project_id in sorted(touched_project_ids):
                self.audit.record_project_event(
                    session,
                    project_id=project_id,
                    event_type=DecisionEventType.RAW_COMMENTS_INGESTED,
                    summary="原始评论批次已入库。",
                    payload={
                        "platform_id": platform,
                        "job_id": str(job_id or ""),
                        "inserted": inserted,
                        "updated": updated,
                        "comment_count": inserted + updated,
                        "duplicate_count": updated,
                        "sync_job_status": str(getattr(sync_job, "status", "") or ""),
                    },
                    related_object_type=(
                        "publisher_comment_sync_job"
                        if job_id
                        else "publisher_raw_comment_batch"
                    ),
                    related_object_id=str(job_id or f"{platform}:{now.timestamp()}"),
                    actor_type="extension",
                )
            session.commit()

        return {
            "ok": True,
            "message": "评论批次已入库。",
            "inserted": inserted,
            "updated": updated,
            "synced_at": isoformat(now),
        }

    def serialize_comment_sync_job(
        self, job: PublisherCommentSyncJob
    ) -> dict[str, Any]:
        payload = json.loads(job.result_summary_json or "{}")
        return {
            "job_id": job.id,
            "project_id": job.project_id,
            "platform": job.platform_id,
            "status": job.status,
            "work_id": job.work_id,
            "work_name": job.work_name,
            "chapter_id": job.chapter_id,
            "chapter_title": job.chapter_title,
            "limit": int(job.limit or 0),
            "extension_client_id": job.extension_client_id,
            "message": str(payload.get("message", "")).strip(),
            "error": job.error_message,
            "result_payload": payload,
            "created_at": isoformat(job.created_at),
            "started_at": isoformat(job.started_at),
            "finished_at": isoformat(job.finished_at),
        }
