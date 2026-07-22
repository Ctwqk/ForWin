from __future__ import annotations

import json
import hashlib
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forwin.audit.events import DecisionEventType
from forwin.models.base import new_id
from forwin.models.project import Project
from forwin.models.publisher import (
    PublisherConnectionState,
    PublisherUploadAttempt,
    PublisherUploadJob,
    PublisherUploadReceipt,
)
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.writer import WriterOutput
from forwin.review.publisher_compliance import PublisherComplianceReviewer
from .audit import PublisherAuditService, terminal_upload_event_type
from .attempts import (
    PublisherAttemptFenceError,
    PublisherInvalidTransitionError,
    PublisherReceiptRequiredError,
    PublisherResourceNotFoundError,
)
from .browser_sessions import as_utc, isoformat, utc_now
from .receipts import PublisherReceiptValidationError
from .connection_state import ExtensionConnectionService
from .platform_catalog import PlatformCatalog, PlatformSpec


LOGIN_FAILURE_ERROR_CODES = {
    "login-required",
    "login_required",
    "platform-login-required",
    "platform_login_required",
    "auth-required",
    "auth_required",
    "not-authenticated",
    "not_authenticated",
}

LOGIN_FAILURE_FRAGMENTS = (
    "login",
    "/login",
    "signin",
    "sign-in",
    "登录",
    "扫码",
    "未登录",
    "登录页",
    "登录过期",
    "请先完成扫码",
)


def _load_json_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _receipt_projection_payload(
    payload: dict[str, Any],
    receipt: dict[str, Any],
) -> dict[str, Any]:
    projected = dict(payload)
    for key in (
        "remote_book_id",
        "remote_chapter_id",
        "remote_url",
        "official_state",
        "content_sha256",
    ):
        projected[key] = receipt.get(key, "")
    return projected


def _upload_failure_is_login_failure(
    *,
    current_url: str,
    error_code: str,
    error: str,
    message: str,
    result_payload: dict[str, Any],
) -> bool:
    error_code = (
        str(
            error_code
            or result_payload.get("error_code")
            or result_payload.get("code")
            or result_payload.get("reason")
            or ""
        )
        .strip()
        .lower()
    )
    if error_code in LOGIN_FAILURE_ERROR_CODES:
        return True
    haystack = "\n".join(
        str(part or "")
        for part in (
            current_url,
            error,
            message,
            result_payload.get("phase", ""),
            result_payload.get("status", ""),
        )
    ).lower()
    return any(fragment.lower() in haystack for fragment in LOGIN_FAILURE_FRAGMENTS)


def _publisher_compliance_payload(verdict: Any) -> dict[str, Any]:
    issues = []
    for issue in getattr(verdict, "issues", []) or []:
        issues.append(
            {
                "rule_name": str(getattr(issue, "rule_name", "") or ""),
                "severity": str(getattr(issue, "severity", "") or ""),
                "description": str(getattr(issue, "description", "") or ""),
                "reviewer": str(getattr(issue, "reviewer", "") or ""),
                "issue_type": str(getattr(issue, "issue_type", "") or ""),
                "blocking": bool(getattr(issue, "blocking", False)),
            }
        )
    return {
        "verdict": str(getattr(verdict, "verdict", "") or ""),
        "issues": issues,
        "recommended_action": str(getattr(verdict, "recommended_action", "") or ""),
        "review_summary": str(getattr(verdict, "review_summary", "") or ""),
    }


class UploadJobService:
    def __init__(
        self,
        *,
        session_factory,
        platform_catalog: PlatformCatalog,
        platform_metadata_catalog=None,
        preflight=None,
        connection_state: ExtensionConnectionService,
        audit: PublisherAuditService,
        bindings=None,
        cover_service=None,
        publisher_compliance_reviewer=None,
    ) -> None:
        self.session_factory = session_factory
        self.platform_catalog = platform_catalog
        self.platform_metadata_catalog = platform_metadata_catalog
        self.preflight = preflight
        self.connection_state = connection_state
        self.audit = audit
        self.bindings = bindings
        self.cover_service = cover_service
        self.publisher_compliance_reviewer = (
            publisher_compliance_reviewer or PublisherComplianceReviewer()
        )
        self.attempts = None
        self.receipts = None

    def review_publisher_compliance(
        self,
        *,
        project_id: str,
        platform: str,
        book_name: str,
        chapter_title: str,
        body: str,
        book_meta: dict[str, Any],
    ) -> dict[str, Any]:
        context = ChapterContextPack(
            project_id=str(project_id or ""),
            project_title=str(book_name or ""),
            premise="",
            genre="",
            setting_summary="",
            chapter_number=0,
            chapter_plan_title=str(chapter_title or ""),
            chapter_plan_one_line="",
            chapter_goals=[],
        )
        writer_output = WriterOutput(
            project_id=str(project_id or ""),
            chapter_number=0,
            title=str(chapter_title or ""),
            body=str(body or ""),
            char_count=len(str(body or "")),
            end_of_chapter_summary="",
            generation_meta={
                "publisher_platform": str(platform or ""),
                "publisher_intro": str(book_meta.get("intro") or ""),
            },
        )
        verdict = self.publisher_compliance_reviewer.review(context, writer_output)
        return _publisher_compliance_payload(verdict)

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
            stmt = select(PublisherUploadJob).order_by(
                PublisherUploadJob.updated_at.desc()
            )
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
        cover_generation_enabled: bool = True,
        cover_confirmation_required: bool = False,
        cover_candidate_count: int = 4,
        cover_style_hint: str = "",
        auto_cover_upload_enabled: bool = True,
        publisher_compliance_required: bool = False,
    ) -> dict[str, Any]:
        spec = self.platform_catalog.get(platform)
        normalized_book_meta = self.normalize_book_meta(book_meta)
        platform_meta = (
            self.platform_metadata_catalog.resolve_for_platform(
                platform, normalized_book_meta
            )
            if self.platform_metadata_catalog is not None
            else {}
        )
        latest_publisher_compliance = (
            self.review_publisher_compliance(
                project_id=project_id,
                platform=platform,
                book_name=book_name,
                chapter_title=chapter_title,
                body=body,
                book_meta=normalized_book_meta,
            )
            if publisher_compliance_required
            else None
        )
        preflight = (
            self.preflight.check_upload_readiness(
                platform_id=platform,
                book_name=book_name,
                chapter_title=chapter_title,
                body=body,
                create_if_missing=create_if_missing,
                book_meta=normalized_book_meta,
                publisher_compliance_required=publisher_compliance_required,
                latest_publisher_compliance=latest_publisher_compliance,
            )
            if self.preflight is not None
            else {
                "ok": True,
                "blocking": [],
                "warnings": [],
                "platform_meta": platform_meta,
            }
        )
        if not preflight.get("ok", False):
            details = "；".join(
                str(item.get("message") or item.get("code") or "")
                for item in preflight.get("blocking", [])
                if isinstance(item, dict)
            )
            raise ValueError(f"发布预检失败：{details or '请补全平台必填信息。'}")
        with self.session_factory() as session:
            resolved_project_id = self.resolve_project_id(
                session,
                explicit_project_id=project_id,
                work_name=book_name,
            )
            if (
                create_if_missing
                and cover_generation_enabled
                and self.cover_service is not None
                and self.cover_service.selected_cover_for_project(
                    session,
                    project_id=resolved_project_id,
                )
                is None
            ):
                cover_job = self.new_upload_job(
                    spec=spec,
                    resolved_project_id=resolved_project_id,
                    platform=platform,
                    book_name=book_name,
                    chapter_title="",
                    body="",
                    upload_url=upload_url,
                    publish=False,
                    create_if_missing=create_if_missing,
                    normalized_book_meta=normalized_book_meta,
                    platform_meta=platform_meta,
                    preflight={
                        "ok": True,
                        "blocking": [],
                        "warnings": [],
                        "platform_meta": platform_meta,
                    },
                    task_kind="cover_generate",
                )
                cover_payload = _load_json_object(cover_job.result_payload_json)
                cover_payload.update(
                    {
                        "project_id": resolved_project_id,
                        "cover_candidate_count": max(
                            1, min(int(cover_candidate_count or 4), 8)
                        ),
                        "cover_style_hint": str(cover_style_hint or "").strip(),
                        "cover_confirmation_required": bool(
                            cover_confirmation_required
                        ),
                        "auto_cover_upload_enabled": bool(auto_cover_upload_enabled),
                    }
                )
                cover_job.result_payload_json = json.dumps(
                    cover_payload, ensure_ascii=False
                )
                cover_job.result_message = "封面生成任务已创建，等待后端执行。"
                session.add(cover_job)
                session.flush()
                self.audit.record_upload_job_event(
                    session,
                    job=cover_job,
                    event_type=DecisionEventType.UPLOAD_JOB_CREATED,
                    summary="封面生成任务已创建。",
                    actor_type="api",
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
                platform_meta=platform_meta,
                preflight=preflight,
            )
            chapter_payload = _load_json_object(job.result_payload_json)
            chapter_payload.update(
                {
                    "cover_generation_enabled": bool(cover_generation_enabled),
                    "cover_confirmation_required": bool(cover_confirmation_required),
                    "cover_candidate_count": max(
                        1, min(int(cover_candidate_count or 4), 8)
                    ),
                    "cover_style_hint": str(cover_style_hint or "").strip(),
                    "auto_cover_upload_enabled": bool(auto_cover_upload_enabled),
                    "publisher_compliance_required": bool(
                        publisher_compliance_required
                    ),
                }
            )
            job.result_payload_json = json.dumps(chapter_payload, ensure_ascii=False)
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

    def create_idempotent_canon_job(
        self,
        session,
        *,
        idempotency_key: str,
        canon_commit_id: str,
        canon_idempotency_key: str,
        project_id: str,
        candidate_id: str,
        chapter_number: int,
        chapter_title: str,
        body: str,
        binding: dict[str, Any],
        publish: bool,
    ) -> tuple[PublisherUploadJob, bool]:
        platform = str(binding.get("platform") or "").strip()
        book_name = str(binding.get("book_name") or "").strip()
        upload_url = str(binding.get("upload_url") or "").strip()
        spec = self.platform_catalog.get(platform)
        normalized_book_meta = self.normalize_book_meta(binding.get("book_meta"))
        immutable_binding = {
            "platform": platform,
            "book_name": book_name,
            "upload_url": upload_url,
            "create_if_missing": bool(binding.get("create_if_missing", False)),
            "cover_generation_enabled": bool(
                binding.get("cover_generation_enabled", True)
            ),
            "cover_confirmation_required": bool(
                binding.get("cover_confirmation_required", False)
            ),
            "cover_candidate_count": max(
                1, min(int(binding.get("cover_candidate_count", 4) or 4), 8)
            ),
            "cover_style_hint": str(binding.get("cover_style_hint") or "").strip(),
            "auto_cover_upload_enabled": bool(
                binding.get("auto_cover_upload_enabled", True)
            ),
            "publisher_compliance_required": bool(
                binding.get("publisher_compliance_required", False)
            ),
            "book_meta": normalized_book_meta,
        }
        body_text = str(body or "")
        body_sha256 = hashlib.sha256(body_text.encode("utf-8")).hexdigest()
        expected = {
            "project_id": str(project_id or "").strip(),
            "canon_commit_id": str(canon_commit_id or "").strip(),
            "candidate_id": str(candidate_id or "").strip(),
            "chapter_number": int(chapter_number or 0),
            "idempotency_key": str(idempotency_key or "").strip(),
            "platform_id": platform,
            "task_kind": "chapter_upload",
            "book_name": book_name,
            "chapter_title": str(chapter_title or "").strip(),
            "body_text": body_text,
            "body_sha256": body_sha256,
            "upload_url": upload_url,
            "publisher_binding": immutable_binding,
            "canon_idempotency_key": str(canon_idempotency_key or "").strip(),
        }
        existing = session.execute(
            select(PublisherUploadJob)
            .where(PublisherUploadJob.idempotency_key == expected["idempotency_key"])
            .with_for_update()
        ).scalar_one_or_none()
        if existing is not None:
            self._assert_immutable_canon_job(existing, expected)
            return existing, False

        job = self.new_upload_job(
            spec=spec,
            resolved_project_id=expected["project_id"],
            platform=platform,
            book_name=book_name,
            chapter_title=expected["chapter_title"],
            body=body_text,
            upload_url=upload_url,
            publish=bool(publish),
            create_if_missing=immutable_binding["create_if_missing"],
            normalized_book_meta=normalized_book_meta,
            status="scheduled",
        )
        job.canon_commit_id = expected["canon_commit_id"]
        job.candidate_id = expected["candidate_id"]
        job.chapter_number = expected["chapter_number"]
        job.idempotency_key = expected["idempotency_key"]
        job.body_sha256 = body_sha256
        payload = _load_json_object(job.result_payload_json)
        payload.update(
            {
                "cover_generation_enabled": immutable_binding[
                    "cover_generation_enabled"
                ],
                "cover_confirmation_required": immutable_binding[
                    "cover_confirmation_required"
                ],
                "cover_candidate_count": immutable_binding["cover_candidate_count"],
                "cover_style_hint": immutable_binding["cover_style_hint"],
                "auto_cover_upload_enabled": immutable_binding[
                    "auto_cover_upload_enabled"
                ],
                "publisher_compliance_required": immutable_binding[
                    "publisher_compliance_required"
                ],
                "publisher_identity": expected["idempotency_key"],
                "canon_identity": {
                    "canon_commit_id": expected["canon_commit_id"],
                    "canon_idempotency_key": expected["canon_idempotency_key"],
                    "project_id": expected["project_id"],
                    "chapter_number": expected["chapter_number"],
                    "candidate_id": expected["candidate_id"],
                    "body_sha256": body_sha256,
                },
                "publisher_binding": immutable_binding,
            }
        )
        job.result_payload_json = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
        )
        job.result_message = f"{spec.display_name} Canon 发布任务已物化，等待配额释放。"
        try:
            with session.begin_nested():
                session.add(job)
                session.flush()
        except IntegrityError:
            existing = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.idempotency_key == expected["idempotency_key"]
                )
                .with_for_update()
            ).scalar_one_or_none()
            if existing is None:
                raise
            self._assert_immutable_canon_job(existing, expected)
            return existing, False
        self.audit.record_upload_job_event(
            session,
            job=job,
            event_type=DecisionEventType.UPLOAD_JOB_CREATED,
            summary="Canon 发布上传任务已按不可变身份物化。",
            actor_type="worker",
            extra_payload={
                "canon_commit_id": expected["canon_commit_id"],
                "candidate_id": expected["candidate_id"],
                "chapter_number": expected["chapter_number"],
                "idempotency_key": expected["idempotency_key"],
            },
        )
        return job, True

    def release_scheduled_canon_jobs(
        self,
        *,
        project_id: str,
        job_ids: list[str],
        publish: bool,
        actor_type: str,
        session=None,
    ) -> list[dict[str, Any]]:
        normalized_project_id = str(project_id or "").strip()
        normalized_ids = list(
            dict.fromkeys(str(job_id or "").strip() for job_id in job_ids)
        )
        normalized_ids = [job_id for job_id in normalized_ids if job_id]
        if not normalized_project_id or not normalized_ids:
            return []
        if session is not None:
            released = self._release_scheduled_canon_jobs(
                session,
                project_id=normalized_project_id,
                job_ids=normalized_ids,
                publish=bool(publish),
                actor_type=actor_type,
            )
            session.flush()
            return [self.serialize_upload_job(job) for job in released]

        with self.session_factory() as managed_session:
            released = self._release_scheduled_canon_jobs(
                managed_session,
                project_id=normalized_project_id,
                job_ids=normalized_ids,
                publish=bool(publish),
                actor_type=actor_type,
            )
            managed_session.commit()
            for job in released:
                managed_session.refresh(job)
            return [self.serialize_upload_job(job) for job in released]

    def _release_scheduled_canon_jobs(
        self,
        session,
        *,
        project_id: str,
        job_ids: list[str],
        publish: bool,
        actor_type: str,
    ) -> list[PublisherUploadJob]:
        jobs = (
            session.execute(
                select(PublisherUploadJob)
                .where(PublisherUploadJob.id.in_(job_ids))
                .with_for_update()
            )
            .scalars()
            .all()
        )
        by_id = {job.id: job for job in jobs}
        missing = [job_id for job_id in job_ids if job_id not in by_id]
        if missing:
            raise ValueError(f"publisher jobs not found: {', '.join(missing)}")

        released: list[PublisherUploadJob] = []
        for job_id in job_ids:
            job = by_id[job_id]
            if (
                job.project_id != project_id
                or not job.canon_commit_id
                or not job.idempotency_key
                or job.task_kind != "chapter_upload"
            ):
                raise ValueError(f"publisher job is not a Canon job: {job.id}")
            if job.deleted_at is not None:
                raise ValueError(f"publisher job is deleted: {job.id}")

            payload = _load_json_object(job.result_payload_json)
            if bool(job.publish) != publish:
                if self._canon_publish_mode_is_frozen(job, payload):
                    raise ValueError(
                        "publisher execution mode is frozen after first claim: "
                        f"{job.id}"
                    )
                old_publish = bool(job.publish)
                job.publish = publish
                self.audit.record_upload_job_event(
                    session,
                    job=job,
                    event_type=DecisionEventType.UPLOAD_JOB_PROGRESS,
                    summary="Canon 发布执行模式已在首次领取前更新。",
                    actor_type=str(actor_type or "system"),
                    extra_payload={
                        "transition": "publish_mode_updated",
                        "publish_mode_from": old_publish,
                        "publish_mode_to": publish,
                    },
                )
            if job.status != "scheduled":
                continue

            preflight = self._canon_release_preflight(job, payload)
            payload["preflight"] = preflight
            platform_meta = preflight.get("platform_meta")
            if isinstance(platform_meta, dict) and platform_meta:
                payload["platform_meta"] = platform_meta
            job.result_payload_json = json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            if not preflight.get("ok", False):
                job.result_message = "Canon 发布任务未通过释放预检，继续保持排程状态。"
                self.audit.record_upload_job_event(
                    session,
                    job=job,
                    event_type=DecisionEventType.UPLOAD_JOB_PROGRESS,
                    summary="Canon 发布上传任务释放预检被阻断。",
                    actor_type=str(actor_type or "system"),
                    extra_payload={"transition": "release_preflight_blocked"},
                )
                continue

            job.status = "pending"
            job.abort_requested = False
            job.result_message = "Canon 发布任务已通过配额门并进入执行队列。"
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=DecisionEventType.UPLOAD_JOB_PROGRESS,
                summary="Canon 发布上传任务已释放。",
                actor_type=str(actor_type or "system"),
                extra_payload={"transition": "scheduled_to_pending"},
            )
            released.append(job)
        return released

    def _canon_release_preflight(
        self,
        job: PublisherUploadJob,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        binding = payload.get("publisher_binding")
        if not isinstance(binding, dict):
            raise ValueError(f"Canon publisher binding snapshot is missing: {job.id}")
        book_meta = self.normalize_book_meta(binding.get("book_meta"))
        platform_meta = (
            self.platform_metadata_catalog.resolve_for_platform(
                job.platform_id,
                book_meta,
            )
            if self.platform_metadata_catalog is not None
            else {}
        )
        compliance_required = bool(binding.get("publisher_compliance_required", False))
        compliance = (
            self.review_publisher_compliance(
                project_id=job.project_id,
                platform=job.platform_id,
                book_name=job.book_name,
                chapter_title=job.chapter_title,
                body=job.body_text,
                book_meta=book_meta,
            )
            if compliance_required
            else None
        )
        if self.preflight is None:
            return {
                "ok": True,
                "blocking": [],
                "warnings": [],
                "platform_meta": platform_meta,
            }
        return self.preflight.check_upload_readiness(
            platform_id=job.platform_id,
            book_name=job.book_name,
            chapter_title=job.chapter_title,
            body=job.body_text,
            create_if_missing=bool(binding.get("create_if_missing", False)),
            book_meta=book_meta,
            publisher_compliance_required=compliance_required,
            latest_publisher_compliance=compliance,
        )

    @staticmethod
    def _canon_publish_mode_is_frozen(
        job: PublisherUploadJob,
        payload: dict[str, Any],
    ) -> bool:
        return bool(
            payload.get("publish_mode_frozen_at")
            or job.current_attempt_id
            or job.claimed_at
            or job.started_at
            or job.status not in {"scheduled", "pending"}
        )

    @staticmethod
    def _assert_immutable_canon_job(
        job: PublisherUploadJob,
        expected: dict[str, Any],
    ) -> None:
        payload = _load_json_object(job.result_payload_json)
        stored = {
            "project_id": str(job.project_id or ""),
            "canon_commit_id": str(job.canon_commit_id or ""),
            "candidate_id": str(job.candidate_id or ""),
            "chapter_number": int(job.chapter_number or 0),
            "idempotency_key": str(job.idempotency_key or ""),
            "platform_id": str(job.platform_id or ""),
            "task_kind": str(job.task_kind or ""),
            "book_name": str(job.book_name or ""),
            "chapter_title": str(job.chapter_title or ""),
            "body_text": str(job.body_text or ""),
            "body_sha256": str(job.body_sha256 or ""),
            "upload_url": str(job.upload_url or ""),
            "publisher_binding": payload.get("publisher_binding"),
            "canon_idempotency_key": str(
                (payload.get("canon_identity") or {}).get("canon_idempotency_key", "")
            ),
        }
        mismatches = [
            field
            for field, expected_value in expected.items()
            if stored.get(field) != expected_value
        ]
        if mismatches:
            raise ValueError(
                "immutable publisher job mismatch: " + ", ".join(mismatches)
            )

    def get_upload_job(self, job_id: str) -> dict[str, Any]:
        with self.session_factory() as session:
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")
            return self.serialize_upload_job(job)

    def terminate_upload_job(self, job_id: str) -> dict[str, Any]:
        now = utc_now()
        with self.session_factory() as session:
            job = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.id == job_id,
                    PublisherUploadJob.deleted_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise ValueError("上传任务不存在。")
            if job.status in {"succeeded", "failed", "cancelled"}:
                raise ValueError("终态上传任务不能再次终止。")
            if job.status in {"scheduled", "pending"}:
                job.status = "cancelled"
                job.abort_requested = True
                job.finished_at = now
                job.result_message = "上传任务已在排队阶段取消。"
                job.error_message = ""
            elif job.status == "reconciling":
                job.abort_requested = True
                job.result_message = "已请求终止远端写入；任务将只读对账后收敛。"
            elif job.status == "paused":
                job.abort_requested = True
                job.result_message = "已请求终止；风险暂停状态等待操作员处理。"
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
            job = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.id == job_id,
                    PublisherUploadJob.deleted_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
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
        return self._attempt_service().claim(
            client_id=client_id,
            connected_platforms=connected_platforms,
        )

    def recover_interrupted_upload_attempts(self) -> list[str]:
        return self._attempt_service().recover_interrupted()

    def update_upload_job_result(
        self,
        *,
        job_id: str,
        client_id: str,
        attempt_id: str,
        lease_epoch: int,
        outcome: str,
        message: str,
        current_url: str,
        error_code: str,
        error_message: str,
        details: dict[str, Any] | None = None,
        now: datetime | None = None,
    ) -> dict[str, Any]:
        if outcome not in {"succeeded", "failed", "cancelled"}:
            raise ValueError("不支持的上传任务状态。")
        completed_at = now or utc_now()
        with self.session_factory() as session:
            self.connection_state.ensure_extension_client(session, client_id)
            attempt_service = self._attempt_service()
            job, attempt = attempt_service.require_fence(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=client_id,
                lease_epoch=lease_epoch,
                now=completed_at,
            )
            if attempt.attempt_kind == "reconcile" and job.task_kind != "audit_sync":
                raise PublisherInvalidTransitionError(
                    "chapter and cover reconciliation must use the reconcile endpoint"
                )
            if (
                job.task_kind == "audit_sync"
                and outcome == "succeeded"
                and attempt.phase != "observation_started"
            ):
                raise PublisherInvalidTransitionError(
                    "audit success requires observation_started phase acknowledgement"
                )
            merged_payload = _load_json_object(job.result_payload_json)
            result_details = details if isinstance(details, dict) else {}
            if outcome == "succeeded":
                if job.task_kind == "chapter_upload" and result_details:
                    raise PublisherInvalidTransitionError(
                        "chapter success details must be empty; receipt is authoritative"
                    )
                if (
                    job.task_kind == "cover_upload"
                    and not str(result_details.get("cover_state") or "").strip()
                ):
                    raise PublisherInvalidTransitionError(
                        "cover success requires typed cover_state details"
                    )
                if job.task_kind == "cover_upload" and set(result_details) - {
                    "cover_state",
                    "audit_state",
                    "platform_message",
                }:
                    raise PublisherInvalidTransitionError(
                        "cover success contains unsupported result details"
                    )
                if job.task_kind == "audit_sync":
                    audit_work = result_details.get("work")
                    if not (
                        set(result_details)
                        == {"work", "chapters", "cover", "milestones"}
                        and isinstance(audit_work, dict)
                        and isinstance(result_details.get("chapters"), list)
                        and isinstance(result_details.get("cover"), dict)
                        and isinstance(result_details.get("milestones"), list)
                    ):
                        raise PublisherInvalidTransitionError(
                            "audit success requires complete typed observation details"
                        )
                    if (
                        str(audit_work.get("work_binding_id") or "").strip()
                        != str(merged_payload.get("work_binding_id") or "").strip()
                        or str(audit_work.get("remote_book_id") or "").strip()
                        != str(merged_payload.get("remote_book_id") or "").strip()
                    ):
                        raise PublisherInvalidTransitionError(
                            "audit observation identity does not match the claimed job"
                        )
            merged_payload["last_result"] = result_details
            receipt_row = session.execute(
                select(PublisherUploadReceipt)
                .where(
                    PublisherUploadReceipt.upload_job_id == job.id,
                    PublisherUploadReceipt.upload_attempt_id == attempt.id,
                    PublisherUploadReceipt.content_sha256 == job.body_sha256,
                )
                .order_by(PublisherUploadReceipt.created_at.asc())
                .limit(1)
            ).scalar_one_or_none()
            if receipt_row is not None and outcome != "succeeded":
                raise PublisherInvalidTransitionError(
                    "receipt-confirmed publisher attempt cannot report a negative result"
                )
            if outcome == "succeeded" and job.task_kind in {
                "chapter_upload",
                "cover_upload",
            }:
                if receipt_row is None:
                    raise PublisherReceiptRequiredError(
                        "mutating publisher success requires a durable receipt"
                    )
                serialized_receipt = self._receipt_service().serialize(receipt_row)
                merged_payload["receipt"] = serialized_receipt

            job, attempt = attempt_service.finish(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=client_id,
                lease_epoch=lease_epoch,
                status=outcome,
                now=completed_at,
            )
            job.current_url = (
                receipt_row.remote_url
                if receipt_row is not None and receipt_row.remote_url
                else current_url
            )
            attempt.error_message = str(error_message or "")
            attempt.error_code = str(error_code or "")
            attempt.result_json = json.dumps(
                {
                    "requested_status": outcome,
                    "effective_job_status": job.status,
                    "message": message,
                    "current_url": current_url,
                    "error_code": error_code,
                    "error_message": error_message,
                    "details": result_details,
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            if job.status == "succeeded":
                job.result_message = message or "发布任务已完成。"
                job.error_message = ""
                side_effect_payload = {**result_details, **merged_payload}
                if receipt_row is not None:
                    side_effect_payload = _receipt_projection_payload(
                        side_effect_payload,
                        serialized_receipt,
                    )
                side_effect_url = (
                    receipt_row.remote_url
                    if receipt_row is not None and receipt_row.remote_url
                    else current_url
                )
                self._apply_success_side_effects(
                    session,
                    job=job,
                    merged_payload=side_effect_payload,
                    current_url=side_effect_url,
                )
                for key in (
                    "work_binding",
                    "chapter_binding",
                    "cover_upload_job_id",
                ):
                    if key in side_effect_payload:
                        merged_payload[key] = side_effect_payload[key]
            elif job.status == "reconciling":
                job.result_message = "远端写入结果不确定，任务等待只读对账。"
                job.error_message = str(error_message or message or "")
            elif job.status == "pending":
                job.result_message = "发布尝试在远端写入前失败，任务等待重试。"
                job.error_message = ""
            elif job.status == "cancelled":
                job.result_message = "上传任务已取消。"
                job.error_message = ""
            else:
                job.result_message = message
                job.error_message = ""
            job.result_payload_json = json.dumps(merged_payload, ensure_ascii=False)
            self._update_extension_connection_state(
                session,
                job=job,
                client_id=client_id,
                requested_status=outcome,
                message=message,
                current_url=current_url,
                error_code=error_code,
                error=error_message,
                result_payload=result_details,
                now=completed_at,
            )
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=terminal_upload_event_type(job.status),
                summary=f"发布上传任务状态更新为 {job.status}。",
                actor_type="extension",
                extra_payload={
                    "attempt_id": attempt.id,
                    "attempt_kind": attempt.attempt_kind,
                    "attempt_phase": attempt.phase,
                    "lease_epoch": attempt.lease_epoch,
                    "worker_id": attempt.worker_id,
                    "requested_status": outcome,
                    "effective_status": job.status,
                    "error_class": "publisher_upload_error"
                    if job.error_message
                    else "",
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
            session.refresh(attempt)
            payload = attempt_service.serialize_state(
                job,
                attempt,
                now=completed_at,
            )
            payload["disposition"] = "applied"
            if receipt_row is not None:
                payload["protocol_receipt"] = self._receipt_service().serialize(
                    receipt_row
                )
            return payload

    def reconcile_upload_job(
        self,
        *,
        job_id: str,
        client_id: str,
        attempt_id: str,
        lease_epoch: int,
        outcome: str,
        receipt: dict[str, Any] | None,
        evidence: dict[str, Any] | None,
        client_observed_at: str = "",
        current_url: str = "",
        error_code: str = "",
        error_message: str = "",
        now: datetime | None = None,
    ) -> dict[str, Any]:
        received_at = now or utc_now()
        with self.session_factory() as session:
            self.connection_state.ensure_extension_client(session, client_id)
            attempt_service = self._attempt_service()
            job, attempt = attempt_service.require_fence(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=client_id,
                lease_epoch=lease_epoch,
                now=received_at,
            )
            if attempt.attempt_kind != "reconcile" or attempt.phase not in {
                "observation_started",
                "receipt_observed",
            }:
                raise PublisherInvalidTransitionError(
                    "reconciliation requires observation_started phase acknowledgement"
                )
            receipt_row = None
            if receipt is not None:
                receipt_row, _created = self._receipt_service().record(
                    session,
                    job=job,
                    attempt=attempt,
                    receipt=receipt,
                    source="reconcile",
                    observed_at=received_at,
                )
            job, attempt = attempt_service.resolve_reconciliation(
                session,
                job_id=job_id,
                attempt_id=attempt_id,
                worker_id=client_id,
                lease_epoch=lease_epoch,
                outcome=outcome,
                receipt_recorded=receipt_row is not None,
                pause_reason=str((evidence or {}).get("reason") or error_message or ""),
                now=received_at,
            )
            canonical_current_url = (
                receipt_row.remote_url
                if receipt_row is not None and receipt_row.remote_url
                else str(current_url or "").strip()
            )
            if canonical_current_url:
                job.current_url = canonical_current_url
            merged_payload = _load_json_object(job.result_payload_json)
            merged_payload["reconciliation"] = {
                "outcome": str(outcome or "").strip(),
                "evidence": evidence if isinstance(evidence, dict) else {},
                "observed_at": str(client_observed_at or ""),
                "attempt_id": attempt.id,
                "lease_epoch": attempt.lease_epoch,
                "current_url": str(current_url or ""),
                "error_code": str(error_code or ""),
                "error_message": str(error_message or ""),
            }
            if receipt_row is not None:
                serialized_receipt = self._receipt_service().serialize(receipt_row)
                merged_payload["receipt"] = serialized_receipt
            attempt.result_json = json.dumps(
                merged_payload["reconciliation"],
                ensure_ascii=False,
                sort_keys=True,
            )
            if job.status == "succeeded":
                job.result_message = "只读对账确认远端章节已存在。"
                job.error_message = ""
                side_effect_payload = _receipt_projection_payload(
                    merged_payload,
                    serialized_receipt,
                )
                self._apply_success_side_effects(
                    session,
                    job=job,
                    merged_payload=side_effect_payload,
                    current_url=canonical_current_url,
                )
                for key in ("work_binding", "chapter_binding", "cover_upload_job_id"):
                    if key in side_effect_payload:
                        merged_payload[key] = side_effect_payload[key]
            elif job.status == "pending":
                job.result_message = "只读对账权威确认不存在，允许新的执行尝试。"
                job.error_message = ""
            elif job.status == "paused":
                job.result_message = "平台风险状态已暂停，等待操作员处理。"
                job.error_message = str(error_message or "")
            else:
                job.result_message = "只读对账结果仍不确定，保持对账状态。"
            job.result_payload_json = json.dumps(
                merged_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=terminal_upload_event_type(job.status),
                summary=f"发布只读对账结果为 {outcome}。",
                actor_type="extension",
                extra_payload={
                    "attempt_id": attempt.id,
                    "attempt_kind": attempt.attempt_kind,
                    "lease_epoch": attempt.lease_epoch,
                    "worker_id": attempt.worker_id,
                    "reconciliation_outcome": str(outcome or "").strip(),
                    "receipt_id": receipt_row.id if receipt_row is not None else "",
                },
            )
            session.commit()
            session.refresh(job)
            session.refresh(attempt)
            payload = attempt_service.serialize_state(
                job,
                attempt,
                now=received_at,
            )
            payload["disposition"] = "applied"
            payload["reconciliation_outcome"] = str(outcome or "").strip()
            if receipt_row is not None:
                payload["protocol_receipt"] = self._receipt_service().serialize(
                    receipt_row
                )
            return payload

    def record_upload_receipt(
        self,
        *,
        job_id: str,
        attempt_id: str,
        client_id: str,
        lease_epoch: int,
        receipt: dict[str, Any],
        now: datetime | None = None,
    ) -> dict[str, Any]:
        observed_at = now or utc_now()
        with self.session_factory() as session:
            job = session.execute(
                select(PublisherUploadJob)
                .where(
                    PublisherUploadJob.id == str(job_id or "").strip(),
                    PublisherUploadJob.deleted_at.is_(None),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if job is None:
                raise PublisherResourceNotFoundError("job", str(job_id or ""))
            attempt = session.execute(
                select(PublisherUploadAttempt)
                .where(
                    PublisherUploadAttempt.id == str(attempt_id or "").strip(),
                    PublisherUploadAttempt.upload_job_id == str(job_id or "").strip(),
                )
                .with_for_update()
            ).scalar_one_or_none()
            if attempt is None:
                raise PublisherResourceNotFoundError(
                    "attempt",
                    str(attempt_id or ""),
                )
            if attempt.worker_id != str(
                client_id or ""
            ).strip() or attempt.lease_epoch != int(lease_epoch or 0):
                raise PublisherAttemptFenceError(
                    "publisher attempt fence is no longer current",
                    job_status=job.status,
                    current_attempt_id=job.current_attempt_id,
                )
            if job.task_kind not in {"chapter_upload", "cover_upload"}:
                raise PublisherReceiptValidationError(
                    "read-only publisher jobs do not accept receipts"
                )
            was_succeeded = job.status == "succeeded"
            receipt_row, created = self._receipt_service().record(
                session,
                job=job,
                attempt=attempt,
                receipt=receipt,
                source="journal_sync",
                observed_at=observed_at,
            )
            lease_expires_at = as_utc(attempt.lease_expires_at)
            current_time = as_utc(observed_at)
            live_current = bool(
                job.current_attempt_id == attempt.id
                and attempt.status == "running"
                and lease_expires_at is not None
                and current_time is not None
                and lease_expires_at > current_time
            )
            attempt_service = self._attempt_service()
            if live_current:
                if attempt.attempt_kind == "execute" and attempt.phase not in {
                    "mutation_started",
                    "receipt_observed",
                }:
                    raise PublisherInvalidTransitionError(
                        "execute receipt requires mutation_started phase acknowledgement"
                    )
                if attempt.attempt_kind == "reconcile" and attempt.phase not in {
                    "observation_started",
                    "receipt_observed",
                }:
                    raise PublisherInvalidTransitionError(
                        "reconcile receipt requires observation_started phase acknowledgement"
                    )
                attempt_service.advance_phase(attempt, "receipt_observed")
                attempt.heartbeat_at = observed_at
                attempt.lease_expires_at = observed_at + timedelta(
                    seconds=attempt_service.default_lease_seconds
                )
                if receipt_row.remote_url:
                    job.current_url = receipt_row.remote_url
                receipt_disposition = "created" if created else "duplicate"
            else:
                job, attempt = attempt_service.accept_late_receipt(
                    session,
                    job_id=job_id,
                    attempt_id=attempt_id,
                    worker_id=client_id,
                    lease_epoch=lease_epoch,
                    now=observed_at,
                )
                receipt_disposition = "duplicate" if was_succeeded else "late_applied"
            if receipt_row.remote_url:
                job.current_url = receipt_row.remote_url
            merged_payload = _load_json_object(job.result_payload_json)
            serialized_receipt = self._receipt_service().serialize(receipt_row)
            merged_payload["receipt"] = serialized_receipt
            job.result_payload_json = json.dumps(
                merged_payload,
                ensure_ascii=False,
                sort_keys=True,
            )
            job.result_message = (
                "发布回执已持久化，等待终态确认。"
                if live_current
                else "迟到发布回执已持久化，任务确认成功。"
            )
            job.error_message = ""
            if not live_current and not was_succeeded:
                side_effect_payload = _receipt_projection_payload(
                    merged_payload,
                    serialized_receipt,
                )
                self._apply_success_side_effects(
                    session,
                    job=job,
                    merged_payload=side_effect_payload,
                    current_url=receipt_row.remote_url,
                )
                for key in ("work_binding", "chapter_binding", "cover_upload_job_id"):
                    if key in side_effect_payload:
                        merged_payload[key] = side_effect_payload[key]
            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=(
                    DecisionEventType.UPLOAD_JOB_PROGRESS
                    if live_current
                    else DecisionEventType.UPLOAD_JOB_SUCCEEDED
                ),
                summary="发布回执已幂等记录。",
                actor_type="extension",
                extra_payload={
                    "attempt_id": attempt.id,
                    "lease_epoch": attempt.lease_epoch,
                    "receipt_id": receipt_row.id,
                    "receipt_key": receipt_row.receipt_key,
                    "receipt_source": receipt_row.source,
                    "worker_id": attempt.worker_id,
                },
            )
            session.commit()
            session.refresh(job)
            session.refresh(attempt)
            payload = attempt_service.serialize_state(
                job,
                attempt,
                now=observed_at,
            )
            payload["receipt_disposition"] = receipt_disposition
            payload["protocol_receipt"] = self._receipt_service().serialize(receipt_row)
            return payload

    def _apply_success_side_effects(
        self,
        session,
        *,
        job: PublisherUploadJob,
        merged_payload: dict[str, Any],
        current_url: str,
    ) -> None:
        if self.bindings is None:
            return
        task_kind = str(job.task_kind or "chapter_upload").strip() or "chapter_upload"
        if task_kind == "chapter_upload":
            work_binding = self.bindings.upsert_work_binding_from_upload_job(
                session,
                job=job,
                result_payload=merged_payload,
                current_url=current_url,
            )
            chapter_binding = self.bindings.upsert_chapter_binding_from_upload_job(
                session,
                job=job,
                work_binding=work_binding,
                result_payload=merged_payload,
                current_url=current_url,
            )
            merged_payload["work_binding"] = self.bindings.serialize_work_binding(
                work_binding
            )
            merged_payload["chapter_binding"] = self.bindings.serialize_chapter_binding(
                chapter_binding
            )
            if self.cover_service is not None:
                cover_upload_job = self.cover_service.enqueue_cover_upload_if_ready(
                    session,
                    job=job,
                    payload=merged_payload,
                    work_binding=work_binding,
                )
                if cover_upload_job is not None:
                    merged_payload["cover_upload_job_id"] = cover_upload_job.id
                    self.audit.record_upload_job_event(
                        session,
                        job=cover_upload_job,
                        event_type=DecisionEventType.UPLOAD_JOB_CREATED,
                        summary="封面上传任务已创建。",
                        actor_type="api",
                    )
        elif task_kind == "cover_upload":
            work_binding = self.bindings.update_from_cover_upload_result(
                session,
                job=job,
                result_payload=merged_payload,
                current_url=current_url,
            )
            if work_binding is not None:
                merged_payload["work_binding"] = self.bindings.serialize_work_binding(
                    work_binding
                )
        elif task_kind == "audit_sync":
            work_binding = self.bindings.update_from_audit_sync_result(
                session,
                job=job,
                result_payload=merged_payload,
                current_url=current_url,
            )
            if work_binding is not None:
                merged_payload["work_binding"] = self.bindings.serialize_work_binding(
                    work_binding
                )

    def _update_extension_connection_state(
        self,
        session,
        *,
        job: PublisherUploadJob,
        client_id: str,
        requested_status: str,
        message: str,
        current_url: str,
        error_code: str,
        error: str,
        result_payload: dict[str, Any],
        now: datetime,
    ) -> None:
        if not self.platform_catalog.has(job.platform_id):
            return
        state = session.get(PublisherConnectionState, job.platform_id)
        if state is None:
            state = PublisherConnectionState(platform_id=job.platform_id)
            session.add(state)
        if client_id:
            state.extension_client_id = client_id
        state.last_heartbeat_at = now
        if job.status == "succeeded":
            state.connected = True
            state.last_error = ""
        elif requested_status == "failed" and _upload_failure_is_login_failure(
            current_url=current_url,
            error_code=error_code,
            error=error,
            message=message,
            result_payload=result_payload,
        ):
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

    def _attempt_service(self):
        if self.attempts is None:
            raise RuntimeError("publisher attempt service is not configured")
        return self.attempts

    def _receipt_service(self):
        if self.receipts is None:
            raise RuntimeError("publisher receipt service is not configured")
        return self.receipts

    def serialize_upload_job(self, job: PublisherUploadJob) -> dict[str, Any]:
        spec = self.platform_catalog.get(job.platform_id)
        payload = json.loads(job.result_payload_json or "{}")
        terminal = job.status in {"succeeded", "failed", "cancelled"}
        return {
            "task_kind": str(job.task_kind or "chapter_upload"),
            "job_id": job.id,
            "project_id": job.project_id,
            "canon_commit_id": str(job.canon_commit_id or ""),
            "candidate_id": str(job.candidate_id or ""),
            "chapter_number": int(job.chapter_number or 0),
            "idempotency_key": str(job.idempotency_key or ""),
            "body_sha256": str(job.body_sha256 or ""),
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
            "terminable": bool(
                job.deleted_at is None and not terminal and not job.abort_requested
            ),
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
        platform_meta: dict[str, Any] | None = None,
        preflight: dict[str, Any] | None = None,
        task_kind: str = "chapter_upload",
        status: str = "pending",
    ) -> PublisherUploadJob:
        job_id = new_id()
        payload: dict[str, Any] = {}
        if resolved_project_id:
            payload["project_id"] = resolved_project_id
        if create_if_missing:
            payload["create_if_missing"] = True
        if normalized_book_meta:
            payload["book_meta"] = normalized_book_meta
        if platform_meta:
            payload["platform_meta"] = platform_meta
        if preflight:
            if not preflight.get("ok", False):
                details = "；".join(
                    str(item.get("message") or item.get("code") or "")
                    for item in preflight.get("blocking", [])
                    if isinstance(item, dict)
                )
                raise ValueError(f"发布预检失败：{details or '请补全平台必填信息。'}")
            payload["preflight"] = preflight
        return PublisherUploadJob(
            id=job_id,
            project_id=resolved_project_id,
            idempotency_key=f"publisher-job:v1:{job_id}",
            platform_id=platform,
            task_kind=str(task_kind or "chapter_upload").strip() or "chapter_upload",
            status=str(status or "pending").strip() or "pending",
            book_name=book_name,
            chapter_title=chapter_title,
            body_text=body,
            body_sha256=hashlib.sha256(body.encode("utf-8")).hexdigest(),
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
        matches = (
            session.execute(
                select(Project.id).where(Project.title == normalized_work_name).limit(2)
            )
            .scalars()
            .all()
        )
        if len(matches) == 1:
            return str(matches[0] or "").strip()
        return ""
