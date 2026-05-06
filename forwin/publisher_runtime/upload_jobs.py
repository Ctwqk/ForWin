from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select, update

from forwin.governance import DecisionEventType
from forwin.models.project import Project
from forwin.models.publisher import PublisherConnectionState, PublisherUploadJob
from .audit import PublisherAuditService, terminal_upload_event_type
from .browser_sessions import isoformat, utc_now
from .connection_state import ExtensionConnectionService
from .platform_catalog import PlatformCatalog, PlatformSpec


class UploadJobService:
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

    def list_upload_jobs(
        self,
        *,
        status: str = "",
        platform: str = "",
        limit: int = 30,
        include_deleted: bool = False,
    ) -> list[dict[str, Any]]:
        normalized_status = str(status or "").strip()
        normalized_platform = str(platform or "").strip()
        normalized_limit = max(1, min(int(limit or 30), 100))
        with self.session_factory() as session:
            stmt = select(PublisherUploadJob).order_by(PublisherUploadJob.updated_at.desc())
            if not include_deleted:
                stmt = stmt.where(PublisherUploadJob.deleted_at.is_(None))
            if normalized_status:
                stmt = stmt.where(PublisherUploadJob.status == normalized_status)
            if normalized_platform:
                stmt = stmt.where(PublisherUploadJob.platform_id == normalized_platform)
            jobs = session.execute(stmt.limit(normalized_limit)).scalars().all()
            return [self.serialize_upload_job(job) for job in jobs]

    def create_upload_job(
        self,
        *,
        project_id: str = "",
        platform: str,
        book_name: str,
        chapter_title: str,
        body: str,
        upload_url: str | None,
        publish: bool,
        create_if_missing: bool = False,
        book_meta: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        spec = self.platform_catalog.get(platform)
        normalized_book_meta = self.normalize_book_meta(book_meta)
        with self.session_factory() as session:
            resolved_project_id = self.resolve_project_id(
                session,
                explicit_project_id=project_id,
                work_name=book_name,
            )
            job = self.new_upload_job(
                spec=spec,
                resolved_project_id=resolved_project_id,
                platform=platform,
                book_name=book_name,
                chapter_title=chapter_title,
                body=body,
                upload_url=upload_url,
                publish=publish,
                create_if_missing=create_if_missing,
                normalized_book_meta=normalized_book_meta,
            )
            session.add(job)
            session.flush()
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=DecisionEventType.UPLOAD_JOB_CREATED,
                summary="发布上传任务已创建。",
                actor_type="api",
            )
            session.commit()
            session.refresh(job)
            return self.serialize_upload_job(job)

    def create_upload_jobs_batch(
        self,
        *,
        project_id: str = "",
        platform: str,
        book_name: str,
        jobs: list[dict[str, Any]],
        upload_url: str | None,
        publish: bool,
        create_if_missing: bool = False,
        book_meta: dict[str, Any] | None = None,
    ) -> int:
        spec = self.platform_catalog.get(platform)
        normalized_book_meta = self.normalize_book_meta(book_meta)
        normalized_jobs: list[tuple[str, str]] = []
        seen_titles: set[str] = set()
        for item in jobs:
            chapter_title = str(item.get("chapter_title", "")).strip()
            if not chapter_title or chapter_title in seen_titles:
                continue
            normalized_jobs.append((chapter_title, str(item.get("body", ""))))
            seen_titles.add(chapter_title)
        if not normalized_jobs:
            return 0

        with self.session_factory() as session:
            resolved_project_id = self.resolve_project_id(
                session,
                explicit_project_id=project_id,
                work_name=book_name,
            )
            chapter_titles = [chapter_title for chapter_title, _body in normalized_jobs]
            existing_titles: set[str] = set()
            if resolved_project_id and chapter_titles:
                existing_titles = {
                    str(title or "").strip()
                    for title in session.execute(
                        select(PublisherUploadJob.chapter_title).where(
                            PublisherUploadJob.project_id == resolved_project_id,
                            PublisherUploadJob.chapter_title.in_(chapter_titles),
                            PublisherUploadJob.deleted_at.is_(None),
                        )
                    ).scalars().all()
                    if str(title or "").strip()
                }

            rows = [
                self.new_upload_job(
                    spec=spec,
                    resolved_project_id=resolved_project_id,
                    platform=platform,
                    book_name=book_name,
                    chapter_title=chapter_title,
                    body=body,
                    upload_url=upload_url,
                    publish=publish,
                    create_if_missing=create_if_missing,
                    normalized_book_meta=normalized_book_meta,
                )
                for chapter_title, body in normalized_jobs
                if chapter_title not in existing_titles
            ]
            if not rows:
                return 0
            session.add_all(rows)
            session.flush()
            for job in rows:
                self.audit.record_upload_job_event(
                    session,
                    job=job,
                    event_type=DecisionEventType.UPLOAD_JOB_CREATED,
                    summary="发布上传任务已批量创建。",
                    actor_type="api",
                )
            session.commit()
            return len(rows)

    def get_upload_job(self, job_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")
            return self.serialize_upload_job(job)

    def terminate_upload_job(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")
            if job.status in {"succeeded", "failed", "cancelled"}:
                raise ValueError("终态上传任务不能再次终止。")
            if job.status == "pending":
                job.status = "cancelled"
                job.abort_requested = True
                job.finished_at = now
                job.result_message = "上传任务已在排队阶段取消。"
                job.error_message = ""
            else:
                job.status = "terminating"
                job.abort_requested = True
                job.result_message = "已请求浏览器扩展终止上传任务，等待执行端确认。"
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=DecisionEventType.UPLOAD_JOB_CANCELLED,
                summary="发布上传任务已请求取消。",
                actor_type="manual_ui",
                extra_payload={"abort_requested": bool(job.abort_requested)},
            )
            session.commit()
            session.refresh(job)
            return self.serialize_upload_job(job)

    def delete_upload_job(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")
            if job.status not in {"succeeded", "failed", "cancelled"}:
                raise ValueError("运行中的上传任务不能删除，请先终止。")
            job.deleted_at = now
            session.commit()
            session.refresh(job)
            return self.serialize_upload_job(job)

    def claim_next_upload_job(
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
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.status.in_(["running", "terminating"]),
                    PublisherUploadJob.finished_at.is_(None),
                    PublisherUploadJob.deleted_at.is_(None),
                    PublisherUploadJob.extension_client_id == client_id,
                    PublisherUploadJob.platform_id.in_(platforms),
                )
                .order_by(PublisherUploadJob.started_at.asc(), PublisherUploadJob.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if job is not None:
                return self.serialize_upload_job(job)

            claimable_platforms = self.connection_state.claimable_platforms(
                session,
                client_id=client_id,
                platforms=platforms,
            )
            if not claimable_platforms:
                return None

            while True:
                job = session.execute(
                    select(PublisherUploadJob)
                    .where(
                        PublisherUploadJob.status == "pending",
                        PublisherUploadJob.abort_requested.is_(False),
                        PublisherUploadJob.deleted_at.is_(None),
                        PublisherUploadJob.platform_id.in_(claimable_platforms),
                    )
                    .order_by(PublisherUploadJob.created_at.asc())
                    .limit(1)
                ).scalar_one_or_none()
                if job is None:
                    return None

                claimed_at = job.claimed_at or now
                started_at = job.started_at or now
                claimed = session.execute(
                    update(PublisherUploadJob)
                    .where(
                        PublisherUploadJob.id == job.id,
                        PublisherUploadJob.status == "pending",
                        PublisherUploadJob.abort_requested.is_(False),
                        PublisherUploadJob.deleted_at.is_(None),
                    )
                    .values(
                        status="running",
                        extension_client_id=client_id,
                        claimed_at=claimed_at,
                        started_at=started_at,
                        abort_requested=False,
                        result_message="上传任务已被浏览器扩展自动领取。",
                        error_message="",
                    )
                )
                if not claimed.rowcount:
                    session.rollback()
                    continue

                session.flush()
                job.status = "running"
                job.extension_client_id = client_id
                job.claimed_at = claimed_at
                job.started_at = started_at
                job.abort_requested = False
                job.result_message = "上传任务已被浏览器扩展自动领取。"
                job.error_message = ""
                self.audit.record_upload_job_event(
                    session,
                    job=job,
                    event_type=DecisionEventType.UPLOAD_JOB_CLAIMED,
                    summary="发布上传任务已被浏览器扩展领取。",
                    actor_type="extension",
                )
                session.commit()
                session.refresh(job)
                return self.serialize_upload_job(job)
            return None

    def requeue_interrupted_upload_jobs(self) -> list[str]:
        now = utc_now()
        recovered_platforms: set[str] = set()
        with self.session_factory() as session:
            jobs = session.execute(
                select(PublisherUploadJob).where(
                    PublisherUploadJob.status.in_(["running", "terminating"]),
                    PublisherUploadJob.finished_at.is_(None),
                    PublisherUploadJob.deleted_at.is_(None),
                )
            ).scalars().all()
            for job in jobs:
                if job.abort_requested:
                    job.status = "cancelled"
                    job.finished_at = now
                    job.result_message = "服务重启时检测到终止请求，任务已取消。"
                else:
                    job.status = "pending"
                    job.started_at = None
                    job.extension_client_id = ""
                    job.current_url = ""
                    job.error_message = ""
                    job.result_payload_json = json.dumps(
                        {"phase": "requeued-after-restart"},
                        ensure_ascii=False,
                    )
                    job.result_message = "服务重启后，上传任务已重新排队。"
                recovered_platforms.add(job.platform_id)

                state = session.get(PublisherConnectionState, job.platform_id)
                if state is not None:
                    state.last_heartbeat_at = now
            session.commit()
        return sorted(recovered_platforms)

    def update_upload_job_result(
        self,
        *,
        job_id: str,
        client_id: str,
        status: str,
        message: str,
        current_url: str,
        error: str,
        result_payload: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        if status not in {"running", "succeeded", "failed", "cancelled"}:
            raise ValueError("不支持的上传任务状态。")

        now = utc_now()
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")

            self.connection_state.ensure_extension_client(session, client_id)
            if client_id:
                job.extension_client_id = client_id
            effective_status = status
            if job.abort_requested and status in {"succeeded", "failed", "cancelled"}:
                effective_status = "cancelled"
            elif job.abort_requested and status == "running":
                effective_status = "terminating"

            if effective_status == "running":
                job.claimed_at = job.claimed_at or now
                job.started_at = job.started_at or now
                job.error_message = ""
            elif effective_status in {"succeeded", "failed", "cancelled"}:
                job.started_at = job.started_at or now
                job.finished_at = now

            job.status = effective_status
            job.current_url = current_url
            job.result_message = (
                "上传任务已取消。"
                if effective_status == "cancelled"
                else message
            )
            job.error_message = "" if effective_status == "cancelled" else error
            try:
                merged_payload = json.loads(job.result_payload_json or "{}")
            except json.JSONDecodeError:
                merged_payload = {}
            if not isinstance(merged_payload, dict):
                merged_payload = {}
            if result_payload:
                merged_payload.update(result_payload)
            job.result_payload_json = json.dumps(merged_payload, ensure_ascii=False)

            if self.platform_catalog.has(job.platform_id):
                state = session.get(PublisherConnectionState, job.platform_id)
                if state is None:
                    state = PublisherConnectionState(platform_id=job.platform_id)
                    session.add(state)
                if client_id:
                    state.extension_client_id = client_id
                state.last_heartbeat_at = now
                if effective_status == "succeeded":
                    state.connected = True
                    state.last_error = ""
                elif effective_status == "failed" and current_url and "login" in current_url:
                    state.connected = False
                    state.last_error = error or message
                self.connection_state.upsert_extension_platform_state(
                    session,
                    client_id=client_id,
                    platform_id=job.platform_id,
                    connected=state.connected,
                    login_method=state.login_method,
                    last_error=state.last_error,
                    status_payload={
                        "platform": job.platform_id,
                        "connected": state.connected,
                        "login_method": state.login_method,
                        "last_error": state.last_error,
                        "source": "upload-job-result",
                    },
                    last_heartbeat_at=now,
                )

            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=terminal_upload_event_type(effective_status),
                summary=f"发布上传任务状态更新为 {effective_status}。",
                actor_type="extension",
                extra_payload={
                    "requested_status": status,
                    "effective_status": effective_status,
                    "error_class": "publisher_upload_error" if job.error_message else "",
                    "error_message": job.error_message,
                    "remote_chapter_id": (
                        str(merged_payload.get("remote_chapter_id") or "")
                        if isinstance(merged_payload, dict)
                        else ""
                    ),
                },
            )
            session.commit()
            session.refresh(job)
            return self.serialize_upload_job(job)

    def serialize_upload_job(self, job: PublisherUploadJob) -> dict[str, Any]:
        spec = self.platform_catalog.get(job.platform_id)
        payload = json.loads(job.result_payload_json or "{}")
        terminal = job.status in {"succeeded", "failed", "cancelled"}
        return {
            "job_id": job.id,
            "project_id": job.project_id,
            "platform": job.platform_id,
            "display_name": spec.display_name,
            "status": job.status,
            "book_name": job.book_name,
            "chapter_title": job.chapter_title,
            "body": job.body_text,
            "upload_url": job.upload_url or None,
            "publish": bool(job.publish),
            "extension_client_id": job.extension_client_id,
            "current_url": job.current_url,
            "message": job.result_message,
            "error": job.error_message,
            "result_payload": payload,
            "abort_requested": bool(job.abort_requested),
            "created_at": isoformat(job.created_at),
            "updated_at": isoformat(job.updated_at),
            "claimed_at": isoformat(job.claimed_at),
            "started_at": isoformat(job.started_at),
            "finished_at": isoformat(job.finished_at),
            "terminable": bool(job.deleted_at is None and not terminal and not job.abort_requested),
            "deletable": bool(job.deleted_at is None and terminal),
        }

    def new_upload_job(
        self,
        *,
        spec: PlatformSpec,
        resolved_project_id: str,
        platform: str,
        book_name: str,
        chapter_title: str,
        body: str,
        upload_url: str | None,
        publish: bool,
        create_if_missing: bool,
        normalized_book_meta: dict[str, Any],
    ) -> PublisherUploadJob:
        payload: dict[str, Any] = {}
        if resolved_project_id:
            payload["project_id"] = resolved_project_id
        if create_if_missing:
            payload["create_if_missing"] = True
        if normalized_book_meta:
            payload["book_meta"] = normalized_book_meta
        return PublisherUploadJob(
            project_id=resolved_project_id,
            platform_id=platform,
            status="pending",
            book_name=book_name,
            chapter_title=chapter_title,
            body_text=body,
            upload_url=upload_url or "",
            publish=publish,
            abort_requested=False,
            result_message=f"{spec.display_name} 上传任务已创建，等待浏览器扩展执行。",
            result_payload_json=json.dumps(payload, ensure_ascii=False),
        )

    @staticmethod
    def normalize_book_meta(book_meta: dict[str, Any] | None) -> dict[str, Any]:
        if not isinstance(book_meta, dict):
            return {}

        def normalized_tags(key: str) -> list[str]:
            values = book_meta.get(key, [])
            if not isinstance(values, list):
                return []
            return [str(item).strip() for item in values if str(item).strip()]

        normalized: dict[str, Any] = {}
        audience = str(book_meta.get("audience", "")).strip()
        if audience:
            normalized["audience"] = audience
        primary_category = str(book_meta.get("primary_category", "")).strip()
        if primary_category:
            normalized["primary_category"] = primary_category
        protagonist_names = normalized_tags("protagonist_names")[:2]
        if protagonist_names:
            normalized["protagonist_names"] = protagonist_names
        intro = str(book_meta.get("intro", "")).strip()
        if intro:
            normalized["intro"] = intro
        for key in ("theme_tags", "role_tags", "plot_tags"):
            tags = normalized_tags(key)[:2]
            if tags:
                normalized[key] = tags
        return normalized

    @staticmethod
    def resolve_project_id(
        session,
        *,
        explicit_project_id: str = "",
        work_name: str = "",
    ) -> str:
        normalized_project_id = str(explicit_project_id or "").strip()
        if normalized_project_id:
            if session.get(Project, normalized_project_id) is not None:
                return normalized_project_id
        normalized_work_name = str(work_name or "").strip()
        if not normalized_work_name:
            return ""
        matches = session.execute(
            select(Project.id).where(Project.title == normalized_work_name).limit(2)
        ).scalars().all()
        if len(matches) == 1:
            return str(matches[0] or "").strip()
        return ""
