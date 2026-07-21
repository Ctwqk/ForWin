from __future__ import annotations

import json
import re
import hashlib
from collections.abc import Callable
from typing import Any

from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from forwin.audit.events import DecisionEventType
from forwin.models.project import Project
from forwin.models.publisher import PublisherConnectionState, PublisherUploadJob
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.writer import WriterOutput
from forwin.review.publisher_compliance import PublisherComplianceReviewer
from .audit import PublisherAuditService, terminal_upload_event_type
from .browser_sessions import isoformat, utc_now
from .connection_state import ExtensionConnectionService
from .platform_catalog import PlatformCatalog, PlatformSpec


AUTO_UPLOAD_MAX_ATTEMPTS = 3
EXTENSION_CLAIMABLE_UPLOAD_TASK_KINDS = (
    "chapter_upload",
    "cover_upload",
    "audit_sync",
)

QIDIAN_REAL_CCID_RE = re.compile(r"(?:[?#&])ccid=(\d{6,})")

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

CodexInterventionHandler = Callable[[dict[str, Any]], dict[str, Any] | None]


def _load_json_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}
    return payload if isinstance(payload, dict) else {}


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _upload_failure_is_login_failure(
    *,
    current_url: str,
    error: str,
    message: str,
    result_payload: dict[str, Any],
) -> bool:
    error_code = (
        str(
            result_payload.get("error_code")
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


def _failed_qidian_draft_has_real_ccid(
    job: PublisherUploadJob,
    *,
    current_url: str,
    error: str,
    result_payload: dict[str, Any],
) -> bool:
    if str(job.platform_id or "").strip() != "qidian" or bool(job.publish):
        return False
    error_code = str(result_payload.get("error_code") or "").strip()
    if error_code != "extension-upload-timeout" and "执行超时" not in str(error or ""):
        return False
    url = str(current_url or "").strip()
    if "write.qq.com" not in url or "/chaptertmp/" not in url:
        return False
    return QIDIAN_REAL_CCID_RE.search(url) is not None


def _upload_retry_history(
    history: Any,
    *,
    failure_count: int,
    failed_at: str,
    current_url: str,
    error: str,
    message: str,
) -> list[dict[str, Any]]:
    rows = history if isinstance(history, list) else []
    normalized = [row for row in rows if isinstance(row, dict)][-7:]
    normalized.append(
        {
            "attempt": failure_count,
            "failure_count": failure_count,
            "failed_at": failed_at,
            "current_url": current_url,
            "message": message,
            "error": error,
        }
    )
    return normalized


def _clear_terminal_failure_payload(payload: dict[str, Any]) -> dict[str, Any]:
    cleaned = dict(payload)
    retry = cleaned.get("auto_retry")
    if isinstance(retry, dict):
        history = retry.get("history")
        if isinstance(history, list) and history:
            cleaned["retry_history"] = [row for row in history if isinstance(row, dict)]
    for key in (
        "auto_retry",
        "codex_intervention_required",
        "codex_intervention",
        "error_code",
        "error_class",
        "last_error",
        "failure_phase",
        "failed_at",
        "last_failed_at",
    ):
        cleaned.pop(key, None)
    return cleaned


def _build_codex_intervention_payload(
    job: PublisherUploadJob,
    *,
    failure_count: int,
    max_attempts: int,
    current_url: str,
    error: str,
    message: str,
) -> dict[str, Any]:
    prompt = "\n".join(
        [
            "ForWin 上传任务需要 Codex 介入。",
            f"job_id: {job.id}",
            f"platform: {job.platform_id}",
            f"book_name: {job.book_name}",
            f"chapter_title: {job.chapter_title}",
            f"publish: {bool(job.publish)}",
            f"attempts: {failure_count}/{max_attempts}",
            f"current_url: {current_url}",
            f"message: {message}",
            f"error: {error}",
            "",
            "请连接 Linux 发布浏览器 CDP，检查平台页面状态，确认章节是否已经保存为草稿或需要手动继续上传。",
            "不要绕过登录；除非 publish=true，不要执行正式发布。",
        ]
    )
    return {
        "status": "requested",
        "runner": "codex",
        "prompt": prompt,
    }


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
        codex_intervention_handler: CodexInterventionHandler | None = None,
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
        self.codex_intervention_handler = codex_intervention_handler
        self.publisher_compliance_reviewer = (
            publisher_compliance_reviewer or PublisherComplianceReviewer()
        )

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

    def request_codex_intervention(self, intervention: dict[str, Any]) -> None:
        handler = self.codex_intervention_handler
        if handler is None:
            return
        try:
            result = handler(intervention)
        except Exception as exc:  # noqa: BLE001
            intervention["status"] = "request_failed"
            intervention["error"] = f"{exc.__class__.__name__}: {exc}"
            return
        intervention["status"] = "submitted"
        if isinstance(result, dict):
            intervention["call"] = result

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
            job = session.get(PublisherUploadJob, job_id)
            if job is None or job.deleted_at is not None:
                raise ValueError("上传任务不存在。")
            if job.status in {"succeeded", "failed", "cancelled"}:
                raise ValueError("终态上传任务不能再次终止。")
            if job.status in {"scheduled", "pending"}:
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
                    PublisherUploadJob.task_kind.in_(
                        EXTENSION_CLAIMABLE_UPLOAD_TASK_KINDS
                    ),
                )
                .order_by(
                    PublisherUploadJob.started_at.asc(),
                    PublisherUploadJob.created_at.asc(),
                )
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
                        PublisherUploadJob.task_kind.in_(
                            EXTENSION_CLAIMABLE_UPLOAD_TASK_KINDS
                        ),
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
                        PublisherUploadJob.task_kind.in_(
                            EXTENSION_CLAIMABLE_UPLOAD_TASK_KINDS
                        ),
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
                if job.canon_commit_id:
                    claim_payload = _load_json_object(job.result_payload_json)
                    claim_payload.setdefault("publish_mode_frozen_at", isoformat(now))
                    job.result_payload_json = json.dumps(
                        claim_payload,
                        ensure_ascii=False,
                        sort_keys=True,
                    )
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

    def requeue_interrupted_upload_jobs(self) -> list[str]:
        now = utc_now()
        recovered_platforms: set[str] = set()
        with self.session_factory() as session:
            jobs = (
                session.execute(
                    select(PublisherUploadJob).where(
                        PublisherUploadJob.status.in_(["running", "terminating"]),
                        PublisherUploadJob.finished_at.is_(None),
                        PublisherUploadJob.deleted_at.is_(None),
                    )
                )
                .scalars()
                .all()
            )
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
            if job.status in {"succeeded", "failed", "cancelled"}:
                return self.serialize_upload_job(job)

            self.connection_state.ensure_extension_client(session, client_id)
            if client_id:
                job.extension_client_id = client_id
            effective_status = status
            if job.abort_requested and status in {"succeeded", "failed", "cancelled"}:
                effective_status = "cancelled"
            elif job.abort_requested and status == "running":
                effective_status = "terminating"

            merged_payload = _load_json_object(job.result_payload_json)
            if result_payload:
                merged_payload.update(result_payload)

            requeued_after_failure = False
            if effective_status == "failed" and _failed_qidian_draft_has_real_ccid(
                job,
                current_url=current_url,
                error=error,
                result_payload=merged_payload,
            ):
                recovered_error_code = str(merged_payload.pop("error_code", "") or "")
                effective_status = "succeeded"
                message = "章节草稿已保存到起点。"
                error = ""
                merged_payload.update(
                    {
                        "phase": "server-timeout-ccid-recovered",
                        "mode": "draft",
                        "official_status": "drafted",
                        "verified_via": "qidian-real-ccid-timeout-recovery",
                    }
                )
                if recovered_error_code:
                    merged_payload["recovered_error_code"] = recovered_error_code
            if effective_status == "failed":
                failed_at = isoformat(now)
                existing_retry = merged_payload.get("auto_retry", {})
                if not isinstance(existing_retry, dict):
                    existing_retry = {}
                failure_count = _as_int(existing_retry.get("failure_count"), 0) + 1
                login_failure = _upload_failure_is_login_failure(
                    current_url=current_url,
                    error=error,
                    message=message,
                    result_payload=merged_payload,
                )
                exhausted = failure_count >= AUTO_UPLOAD_MAX_ATTEMPTS
                requeued_after_failure = (
                    not login_failure and not exhausted and not job.abort_requested
                )
                merged_payload["auto_retry"] = {
                    "failure_count": failure_count,
                    "max_attempts": AUTO_UPLOAD_MAX_ATTEMPTS,
                    "next_attempt": (
                        failure_count + 1 if requeued_after_failure else 0
                    ),
                    "login_failure": login_failure,
                    "exhausted": bool(not login_failure and exhausted),
                    "last_failed_at": failed_at,
                    "last_current_url": current_url,
                    "last_message": message,
                    "last_error": error,
                    "history": _upload_retry_history(
                        existing_retry.get("history", []),
                        failure_count=failure_count,
                        failed_at=failed_at,
                        current_url=current_url,
                        error=error,
                        message=message,
                    ),
                }
                if requeued_after_failure:
                    effective_status = "pending"
                    message = (
                        "上传失败，已自动重新排队"
                        f"（第 {failure_count + 1}/{AUTO_UPLOAD_MAX_ATTEMPTS} 次尝试）。"
                    )
                    error = ""
                    current_url = ""
                elif not login_failure and exhausted:
                    merged_payload["codex_intervention_required"] = True
                    intervention = _build_codex_intervention_payload(
                        job,
                        failure_count=failure_count,
                        max_attempts=AUTO_UPLOAD_MAX_ATTEMPTS,
                        current_url=current_url,
                        error=error,
                        message=message,
                    )
                    self.request_codex_intervention(intervention)
                    merged_payload["codex_intervention"] = intervention

            if effective_status in {"succeeded", "cancelled"}:
                merged_payload = _clear_terminal_failure_payload(merged_payload)

            if effective_status == "running":
                job.claimed_at = job.claimed_at or now
                job.started_at = job.started_at or now
                job.finished_at = None
                job.error_message = ""
            elif effective_status in {"succeeded", "failed", "cancelled"}:
                job.started_at = job.started_at or now
                job.finished_at = now
            elif effective_status == "pending":
                job.claimed_at = None
                job.started_at = None
                job.finished_at = None
                job.extension_client_id = ""

            job.status = effective_status
            job.current_url = current_url
            job.result_message = (
                "上传任务已取消。" if effective_status == "cancelled" else message
            )
            job.error_message = (
                "" if effective_status in {"cancelled", "pending"} else error
            )

            task_kind = (
                str(job.task_kind or "chapter_upload").strip() or "chapter_upload"
            )
            if (
                self.bindings is not None
                and effective_status == "succeeded"
                and task_kind == "chapter_upload"
            ):
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
                merged_payload["chapter_binding"] = (
                    self.bindings.serialize_chapter_binding(chapter_binding)
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
            elif (
                self.bindings is not None
                and effective_status == "succeeded"
                and task_kind == "cover_upload"
            ):
                work_binding = self.bindings.update_from_cover_upload_result(
                    session,
                    job=job,
                    result_payload=merged_payload,
                    current_url=current_url,
                )
                if work_binding is not None:
                    merged_payload["work_binding"] = (
                        self.bindings.serialize_work_binding(work_binding)
                    )
            elif (
                self.bindings is not None
                and effective_status == "succeeded"
                and task_kind == "audit_sync"
            ):
                work_binding = self.bindings.update_from_audit_sync_result(
                    session,
                    job=job,
                    result_payload=merged_payload,
                    current_url=current_url,
                )
                if work_binding is not None:
                    merged_payload["work_binding"] = (
                        self.bindings.serialize_work_binding(work_binding)
                    )

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
                elif effective_status == "failed" and _upload_failure_is_login_failure(
                    current_url=current_url,
                    error=error,
                    message=message,
                    result_payload=merged_payload,
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

            self.audit.record_upload_job_event(
                session,
                job=job,
                event_type=terminal_upload_event_type(effective_status),
                summary=f"发布上传任务状态更新为 {effective_status}。",
                actor_type="extension",
                extra_payload={
                    "requested_status": status,
                    "effective_status": effective_status,
                    "error_class": "publisher_upload_error"
                    if job.error_message
                    else "",
                    "error_message": job.error_message,
                    "requeued_after_failure": requeued_after_failure,
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
            project_id=resolved_project_id,
            platform_id=platform,
            task_kind=str(task_kind or "chapter_upload").strip() or "chapter_upload",
            status=str(status or "pending").strip() or "pending",
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
