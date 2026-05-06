from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update

from forwin.governance import DecisionEventType
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherCommentSyncJob,
    PublisherConnectionState,
    PublisherRawComment,
)

from .audit import PublisherAuditService, comment_sync_event_type
from .browser_sessions import isoformat, utc_now
from .connection_state import ExtensionConnectionService
from .platform_catalog import PlatformCatalog
from .upload_jobs import UploadJobService


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
                project_id=UploadJobService.resolve_project_id(
                    session,
                    explicit_project_id=project_id,
                    work_name=work_name,
                ),
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

    def list_comment_sync_jobs(
        self,
        *,
        status: str = "",
        platform: str = "",
        limit: int = 30,
    ) -> list[dict[str, Any]]:
        normalized_status = str(status or "").strip()
        normalized_platform = str(platform or "").strip()
        normalized_limit = max(1, min(int(limit or 30), 100))
        with self.session_factory() as session:
            stmt = select(PublisherCommentSyncJob).order_by(
                PublisherCommentSyncJob.updated_at.desc()
            )
            if normalized_status:
                stmt = stmt.where(PublisherCommentSyncJob.status == normalized_status)
            if normalized_platform:
                stmt = stmt.where(PublisherCommentSyncJob.platform_id == normalized_platform)
            jobs = session.execute(stmt.limit(normalized_limit)).scalars().all()
            return [self.serialize_comment_sync_job(job) for job in jobs]

    def get_comment_sync_job(self, job_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.get(PublisherCommentSyncJob, job_id)
            if job is None:
                raise ValueError("评论同步任务不存在。")
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
                            {"phase": "claimed", "message": "评论同步任务已被浏览器扩展自动领取。"},
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
                    {"phase": "claimed", "message": "评论同步任务已被浏览器扩展自动领取。"},
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
            return None

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
            merged_payload.update({
                "message": str(message or "").strip(),
                "status": status,
            })
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
            resolved_job_project_id = ""
            sync_job = None
            if job_id:
                job = session.get(PublisherCommentSyncJob, job_id)
                if job is not None:
                    sync_job = job
                    job.extension_client_id = client_id
                    job.status = "running"
                    job.started_at = job.started_at or now
                    resolved_job_project_id = str(job.project_id or "").strip()

            remote_ids = [
                str(item.get("remote_comment_id", "")).strip()
                for item in comments
                if str(item.get("remote_comment_id", "")).strip()
            ]
            existing_rows = (
                session.execute(
                    select(PublisherRawComment).where(
                        PublisherRawComment.platform_id == platform,
                        PublisherRawComment.remote_comment_id.in_(remote_ids),
                    )
                ).scalars().all()
                if remote_ids
                else []
            )
            row_map = {row.remote_comment_id: row for row in existing_rows}
            valid_project_ids = set()
            unique_project_ids_by_work_name: dict[str, str] = {}
            if not resolved_job_project_id:
                explicit_project_ids = {
                    str(item.get("project_id", "")).strip()
                    for item in comments
                    if str(item.get("project_id", "")).strip()
                }
                if explicit_project_ids:
                    valid_project_ids = set(
                        session.execute(
                            select(Project.id).where(Project.id.in_(explicit_project_ids))
                        ).scalars().all()
                    )
                work_names = {
                    str(item.get("work_name", "")).strip()
                    for item in comments
                    if str(item.get("work_name", "")).strip()
                }
                if work_names:
                    title_rows = session.execute(
                        select(Project.title, Project.id)
                        .where(Project.title.in_(work_names))
                        .order_by(Project.title.asc(), Project.id.asc())
                    ).all()
                    title_matches: dict[str, list[str]] = {}
                    for title, project_id in title_rows:
                        normalized_title = str(title or "").strip()
                        normalized_project_id = str(project_id or "").strip()
                        if not normalized_title or not normalized_project_id:
                            continue
                        title_matches.setdefault(normalized_title, []).append(
                            normalized_project_id
                        )
                    unique_project_ids_by_work_name = {
                        title: project_ids[0]
                        for title, project_ids in title_matches.items()
                        if len(project_ids) == 1
                    }

            for item in comments:
                remote_comment_id = str(item.get("remote_comment_id", "")).strip()
                if not remote_comment_id:
                    continue
                row = row_map.get(remote_comment_id)
                if row is None:
                    row = PublisherRawComment(
                        project_id=resolved_job_project_id,
                        platform_id=platform,
                        remote_comment_id=remote_comment_id,
                    )
                    session.add(row)
                    row_map[remote_comment_id] = row
                    inserted += 1
                else:
                    updated += 1

                explicit_project_id = str(item.get("project_id", "")).strip()
                work_name = str(item.get("work_name", "")).strip()
                if resolved_job_project_id:
                    row.project_id = resolved_job_project_id
                elif explicit_project_id and explicit_project_id in valid_project_ids:
                    row.project_id = explicit_project_id
                else:
                    row.project_id = unique_project_ids_by_work_name.get(work_name, "")
                if row.project_id:
                    touched_project_ids.add(str(row.project_id))
                row.work_id = str(item.get("work_id", "")).strip()
                row.work_name = work_name
                row.chapter_id = str(item.get("chapter_id", "")).strip()
                row.chapter_title = str(item.get("chapter_title", "")).strip()
                row.author_id = str(item.get("author_id", "")).strip()
                row.author_name = str(item.get("author_name", "")).strip()
                row.body_text = str(item.get("body", "")).strip()
                row.parent_remote_comment_id = str(
                    item.get("parent_remote_comment_id", "")
                ).strip()
                row.remote_created_at = str(item.get("created_at", "")).strip()
                row.like_count = max(0, as_int(item.get("like_count", 0)))
                row.reply_count = max(0, as_int(item.get("reply_count", 0)))
                row.raw_payload_json = json.dumps(
                    item.get("raw_payload", item),
                    ensure_ascii=False,
                )
                row.synced_at = now

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

    def sync_comments_batch(
        self,
        *,
        client_id: str,
        platform: str,
        comments: list[dict[str, Any]],
        job_id: str = "",
    ) -> dict[str, Any]:
        return self.ingest_comments_batch(
            client_id=client_id,
            platform=platform,
            comments=comments,
            job_id=job_id,
        )

    def serialize_comment_sync_job(self, job: PublisherCommentSyncJob) -> dict[str, Any]:
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
