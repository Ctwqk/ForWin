from __future__ import annotations

import io
import json
import zipfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from forwin.api_schema import (
    CommentSyncJobResultRequest,
    ExtensionBrowserSessionResponse,
    ExtensionClaimCommentSyncJobRequest,
    ExtensionClaimCommentSyncJobResponse,
    ExtensionClaimUploadJobRequest,
    ExtensionClaimUploadJobResponse,
    ExtensionUploadClaim,
    ExtensionUploadClaimAttempt,
    ExtensionCommentsBatchRequest,
    ExtensionCommentsBatchResponse,
    ExtensionHeartbeatRequest,
    ExtensionHeartbeatResponse,
    ExtensionLoginQrNotifyRequest,
    ExtensionLoginQrNotifyResponse,
    ExtensionSessionSyncRequest,
    ExtensionSessionSyncResponse,
    PublisherCommentSyncJobRequest,
    PublisherCommentSyncJobResponse,
    PublisherBrowserSessionSummaryResponse,
    PublisherAuditSyncRequest,
    PublisherChapterBindingResponse,
    PublisherCoverAssetResponse,
    PublisherCoverGenerateRequest,
    PublisherCoverSelectRequest,
    PublisherCoverUploadRequest,
    PublisherPlatformInfo,
    PublisherLoginQrOneShotRequest,
    PublisherLoginQrOneShotResponse,
    PublisherPreflightRequest,
    PublisherPreflightResponse,
    PublisherUploadJobCreateRequest,
    PublisherUploadJobResponse,
    PublisherWorkBindingResponse,
    TaskMutationResponse,
    UploadAttemptHeartbeatRequest,
    UploadAttemptPhaseRequest,
    UploadAttemptReceiptRequest,
    UploadAttemptReceiptResponse,
    UploadAttemptReconcileRequest,
    UploadAttemptReconcileResponse,
    UploadAttemptResultRequest,
    UploadAttemptResultResponse,
    UploadAttemptStateResponse,
)
from forwin.publisher_runtime.attempts import PublisherProtocolError
from forwin.publisher_runtime.auth import (
    PublisherExtensionAuthError,
    PublisherExtensionAuthNotConfigured,
)


def _firefox_manifest(source_manifest: dict[str, Any]) -> dict[str, Any]:
    manifest = json.loads(json.dumps(source_manifest))
    manifest["permissions"] = [
        permission
        for permission in manifest.get("permissions", [])
        if permission != "debugger"
    ]
    manifest["background"] = {
        "scripts": ["background.js"],
        "type": "module",
    }
    manifest.pop("options_page", None)
    manifest["options_ui"] = {
        "page": "options.html",
        "open_in_tab": True,
    }
    manifest["browser_specific_settings"] = {
        "gecko": {
            "id": "forwin-publisher@example.com",
        },
    }
    return manifest


def _build_extension_package(
    extension_root: Path, *, target: str = "chromium"
) -> bytes:
    if not extension_root.exists():
        raise HTTPException(404, "浏览器扩展目录不存在。")
    target = str(target or "chromium").strip().lower()
    if target not in {"chromium", "firefox"}:
        raise HTTPException(400, "不支持的扩展目标浏览器。")
    manifest_path = extension_root / "manifest.json"
    if not manifest_path.exists():
        raise HTTPException(404, "浏览器扩展 manifest.json 不存在。")

    try:
        source_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise HTTPException(500, "浏览器扩展 manifest.json 无法解析。") from exc
    manifest = (
        _firefox_manifest(source_manifest) if target == "firefox" else source_manifest
    )

    buffer = io.BytesIO()
    archive_root = Path(
        "forwin-publisher-firefox" if target == "firefox" else "forwin-publisher"
    )
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        paths = sorted(path for path in extension_root.rglob("*") if path.is_file())
        paths.sort(
            key=lambda path: (
                path.name != "manifest.json",
                str(path.relative_to(extension_root)),
            )
        )
        for path in paths:
            arcname = archive_root / path.relative_to(extension_root)
            if path == manifest_path:
                archive.writestr(
                    str(arcname),
                    f"{json.dumps(manifest, ensure_ascii=False, indent=2)}\n",
                )
            else:
                archive.write(path, arcname=arcname)
    buffer.seek(0)
    return buffer.getvalue()


def _require_extension_auth(
    publisher_manager, x_forwin_extension_key: str | None
) -> None:
    try:
        publisher_manager.verify_extension_api_key(x_forwin_extension_key)
    except PublisherExtensionAuthNotConfigured as exc:
        raise HTTPException(
            503,
            {
                "code": "extension_auth_not_configured",
                "message": str(exc),
            },
        ) from exc
    except PublisherExtensionAuthError as exc:
        raise HTTPException(
            401,
            {
                "code": "invalid_extension_key",
                "message": str(exc),
            },
        ) from exc


def _raise_protocol_http_error(exc: ValueError) -> None:
    if isinstance(exc, PublisherProtocolError):
        raise HTTPException(exc.status_code, exc.detail()) from exc
    raise HTTPException(
        422,
        {"code": "validation_error", "message": str(exc)},
    ) from exc


def _server_time(payload: dict[str, Any]) -> str:
    value = str(payload.get("server_time") or "").strip()
    if value:
        return value
    return datetime.now(timezone.utc).isoformat()


def _claim_job_payload(payload: dict[str, Any]) -> dict[str, Any]:
    task_kind = str(payload.get("task_kind") or "chapter_upload")
    result_payload = payload.get("result_payload")
    result_payload = result_payload if isinstance(result_payload, dict) else {}
    common = {
        "job_id": str(payload.get("job_id") or ""),
        "idempotency_key": str(payload.get("idempotency_key") or ""),
        "task_kind": task_kind,
        "platform": str(payload.get("platform") or ""),
        "content_sha256": str(payload.get("body_sha256") or ""),
    }
    if task_kind == "chapter_upload":
        common["input"] = {
            "book_name": str(payload.get("book_name") or ""),
            "chapter_title": str(payload.get("chapter_title") or ""),
            "body": str(payload.get("body") or ""),
            "publish": bool(payload.get("publish")),
            "create_if_missing": bool(result_payload.get("create_if_missing")),
            "upload_url": payload.get("upload_url"),
            "book_meta": result_payload.get("book_meta") or None,
        }
    elif task_kind == "cover_upload":
        common["input"] = {
            "book_name": str(payload.get("book_name") or ""),
            "work_binding_id": str(result_payload.get("work_binding_id") or ""),
            "remote_book_id": str(result_payload.get("remote_book_id") or ""),
            "cover_asset_id": str(result_payload.get("cover_asset_id") or ""),
            "file_path": str(result_payload.get("file_path") or ""),
            "upload_url": payload.get("upload_url"),
            "remote_url": result_payload.get("remote_url") or None,
        }
    elif task_kind == "audit_sync":
        common["input"] = {
            "book_name": str(payload.get("book_name") or ""),
            "work_binding_id": str(result_payload.get("work_binding_id") or ""),
            "remote_book_id": str(result_payload.get("remote_book_id") or ""),
            "upload_url": payload.get("upload_url"),
            "remote_url": result_payload.get("remote_url") or None,
        }
    else:
        raise ValueError(f"unsupported extension upload task kind: {task_kind}")
    return common


def _next_attempt_action(payload: dict[str, Any]) -> str:
    job_status = str(payload.get("status") or "")
    if job_status == "paused":
        return "operator_review"
    if job_status in {"succeeded", "cancelled"}:
        return "stop"
    if (
        job_status in {"pending", "reconciling"}
        and str(payload.get("attempt_status") or "") != "running"
    ):
        return "retry_after"
    if str(payload.get("execution_mode") or "execute") == "reconcile":
        return "reconcile"
    if str(payload.get("attempt_phase") or "") == "receipt_observed":
        return "submit_result"
    if bool(payload.get("abort_requested")):
        return "stop"
    if str(payload.get("attempt_phase") or "claimed") == "claimed":
        return "execute"
    return "heartbeat"


def _attempt_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
    job_status = str(payload.get("status") or "")
    if job_status == "terminating":
        job_status = "running"
    attempt_status = str(payload.get("attempt_status") or "")
    attempt_status = {
        "indeterminate": "failed",
        "paused": "failed",
        "interrupted": "expired",
        "superseded": "cancelled",
    }.get(attempt_status, attempt_status)
    return {
        "ok": True,
        "server_time": _server_time(payload),
        "job_id": str(payload.get("job_id") or ""),
        "job_status": job_status,
        "attempt_id": str(payload.get("attempt_id") or ""),
        "attempt_status": attempt_status,
        "lease_epoch": int(payload.get("lease_epoch") or 0),
        "phase": str(payload.get("attempt_phase") or ""),
        "lease_expires_at": str(payload.get("lease_expires_at") or "") or None,
        "abort_requested": bool(payload.get("abort_requested")),
        "execution_mode": str(payload.get("execution_mode") or "execute"),
        "next_action": _next_attempt_action(payload),
    }


def _result_state_payload(payload: dict[str, Any]) -> dict[str, Any]:
    return {
        **_attempt_state_payload(payload),
        "disposition": str(payload.get("disposition") or "applied"),
        "available_at": str(payload.get("available_at") or "") or None,
        "reconcile_after": str(payload.get("reconcile_after") or "") or None,
        "pause_reason": str(payload.get("pause_reason") or ""),
    }


def _protocol_receipt_payload(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    return {
        "receipt_id": str(value.get("receipt_id") or ""),
        "receipt_key": str(value.get("receipt_key") or ""),
        "remote_book_id": str(value.get("remote_book_id") or ""),
        "remote_chapter_id": str(value.get("remote_chapter_id") or ""),
        "remote_url": str(value.get("remote_url") or ""),
        "official_state": str(value.get("official_state") or ""),
        "content_sha256": str(value.get("content_sha256") or ""),
        "observed_at": str(value.get("observed_at") or ""),
    }


def download_publisher_extension_package(*, extension_root: Path) -> StreamingResponse:
    payload = _build_extension_package(extension_root, target="chromium")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="forwin-publisher-extension.zip"',
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Forwin-Extension-Target": "chromium",
        },
    )


def download_publisher_firefox_extension_package(
    *, extension_root: Path
) -> StreamingResponse:
    payload = _build_extension_package(extension_root, target="firefox")
    return StreamingResponse(
        io.BytesIO(payload),
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="forwin-publisher-firefox-extension.zip"',
            "Cache-Control": "no-store",
            "Pragma": "no-cache",
            "X-Forwin-Extension-Target": "firefox",
        },
    )


def list_publisher_platforms(*, publisher_manager) -> list[PublisherPlatformInfo]:
    return [
        PublisherPlatformInfo(**item) for item in publisher_manager.list_platforms()
    ]


def create_publisher_upload_job(
    req: PublisherUploadJobCreateRequest,
    *,
    publisher_manager,
) -> PublisherUploadJobResponse:
    try:
        payload = publisher_manager.create_upload_job(
            project_id=str(req.project_id or "").strip(),
            platform=req.platform,
            book_name=req.book_name,
            chapter_title=req.chapter_title,
            body=req.body,
            upload_url=req.upload_url,
            publish=req.publish,
            create_if_missing=req.create_if_missing,
            cover_generation_enabled=req.cover_generation_enabled,
            cover_confirmation_required=req.cover_confirmation_required,
            cover_candidate_count=req.cover_candidate_count,
            cover_style_hint=req.cover_style_hint,
            auto_cover_upload_enabled=req.auto_cover_upload_enabled,
            publisher_compliance_required=req.publisher_compliance_required,
            book_meta=req.book_meta.model_dump() if req.book_meta else None,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


def list_publisher_work_bindings(
    *,
    publisher_manager,
    project_id: str = "",
    platform: str = "",
) -> list[PublisherWorkBindingResponse]:
    return [
        PublisherWorkBindingResponse(**item)
        for item in publisher_manager.list_work_bindings(
            project_id=project_id,
            platform=platform,
        )
    ]


def list_publisher_chapter_bindings(
    *,
    publisher_manager,
    project_id: str = "",
    platform: str = "",
    work_binding_id: str = "",
) -> list[PublisherChapterBindingResponse]:
    return [
        PublisherChapterBindingResponse(**item)
        for item in publisher_manager.list_chapter_bindings(
            project_id=project_id,
            platform=platform,
            work_binding_id=work_binding_id,
        )
    ]


def list_publisher_cover_assets(
    *,
    publisher_manager,
    project_id: str = "",
    work_binding_id: str = "",
) -> list[PublisherCoverAssetResponse]:
    return [
        PublisherCoverAssetResponse(**item)
        for item in publisher_manager.list_cover_assets(
            project_id=project_id,
            work_binding_id=work_binding_id,
        )
    ]


def generate_publisher_cover_candidates(
    req: PublisherCoverGenerateRequest,
    *,
    publisher_manager,
) -> dict[str, Any]:
    try:
        return publisher_manager.generate_cover_candidates(
            project_id=req.project_id,
            platform=req.platform,
            book_name=req.book_name,
            book_meta=req.book_meta.model_dump() if req.book_meta else None,
            cover_candidate_count=req.cover_candidate_count,
            cover_style_hint=req.cover_style_hint,
            cover_confirmation_required=req.cover_confirmation_required,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def select_publisher_cover_asset(
    req: PublisherCoverSelectRequest,
    *,
    publisher_manager,
) -> PublisherCoverAssetResponse:
    try:
        return PublisherCoverAssetResponse(
            **publisher_manager.select_cover_asset(req.cover_asset_id)
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def approve_publisher_cover_asset(
    req: PublisherCoverSelectRequest,
    *,
    publisher_manager,
) -> PublisherCoverAssetResponse:
    try:
        return PublisherCoverAssetResponse(
            **publisher_manager.approve_cover_asset(req.cover_asset_id)
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def reject_publisher_cover_asset(
    req: PublisherCoverSelectRequest,
    *,
    publisher_manager,
) -> PublisherCoverAssetResponse:
    try:
        return PublisherCoverAssetResponse(
            **publisher_manager.reject_cover_asset(req.cover_asset_id)
        )
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc


def enqueue_publisher_cover_upload(
    req: PublisherCoverUploadRequest,
    *,
    publisher_manager,
) -> PublisherUploadJobResponse:
    try:
        return PublisherUploadJobResponse(
            **publisher_manager.enqueue_cover_upload(req.cover_asset_id)
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def enqueue_publisher_audit_sync(
    req: PublisherAuditSyncRequest,
    *,
    publisher_manager,
) -> PublisherUploadJobResponse:
    try:
        return PublisherUploadJobResponse(
            **publisher_manager.enqueue_audit_sync(
                project_id=req.project_id,
                platform=req.platform,
                work_binding_id=req.work_binding_id,
                book_name=req.book_name,
            )
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc


def publisher_preflight(
    req: PublisherPreflightRequest,
    *,
    publisher_manager,
) -> PublisherPreflightResponse:
    return PublisherPreflightResponse(
        **publisher_manager.get_preflight(
            platform=req.platform,
            book_name=req.book_name,
            chapter_title=req.chapter_title,
            body=req.body,
            create_if_missing=req.create_if_missing,
            book_meta=req.book_meta.model_dump() if req.book_meta else None,
        )
    )


def get_publisher_upload_job(
    job_id: str, *, publisher_manager
) -> PublisherUploadJobResponse:
    try:
        payload = publisher_manager.get_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


def list_publisher_upload_jobs(
    *,
    publisher_manager,
    status: str = "",
    platform: str = "",
    limit: int = 30,
) -> list[PublisherUploadJobResponse]:
    payload = publisher_manager.list_upload_jobs(
        status=status,
        platform=platform,
        limit=limit,
    )
    return [PublisherUploadJobResponse(**item) for item in payload]


def terminate_publisher_upload_job(
    job_id: str, *, publisher_manager
) -> TaskMutationResponse:
    try:
        payload = publisher_manager.terminate_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return TaskMutationResponse(
        ok=True,
        task_kind="upload",
        task_id=job_id,
        status=str(payload.get("status", "")),
        message=str(payload.get("message", "")),
    )


def delete_publisher_upload_job(
    job_id: str, *, publisher_manager
) -> TaskMutationResponse:
    try:
        publisher_manager.delete_upload_job(job_id)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return TaskMutationResponse(
        ok=True,
        task_kind="upload",
        task_id=job_id,
        status="deleted",
        message="任务已删除。",
    )


def start_publisher_login_qr_one_shot(
    req: PublisherLoginQrOneShotRequest,
    *,
    publisher_manager,
) -> PublisherLoginQrOneShotResponse:
    try:
        payload = publisher_manager.start_login_qr_one_shot(
            platform=req.platform,
            webhook_url=req.webhook_url,
            ttl_seconds=req.ttl_seconds,
            max_dispatches=req.max_dispatches,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherLoginQrOneShotResponse(**payload)


def publisher_extension_heartbeat(
    req: ExtensionHeartbeatRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionHeartbeatResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)

    def _platform_state(item):
        extra = getattr(item, "model_extra", None) or {}
        raw_state = {
            **extra,
            **item.raw_state,
        }
        return {
            "platform": item.platform,
            "connected": item.connected,
            "login_method": item.login_method,
            "last_error": item.last_error,
            **raw_state,
        }

    payload = publisher_manager.record_extension_heartbeat(
        client_id=req.client_id,
        extension_version=req.extension_version,
        browser_name=req.browser_name,
        browser_version=req.browser_version,
        backend_base_url=req.backend_base_url,
        platforms=[_platform_state(item) for item in req.platforms],
    )
    return ExtensionHeartbeatResponse(**payload)


def publisher_extension_login_qr_notify(
    req: ExtensionLoginQrNotifyRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionLoginQrNotifyResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.notify_login_qr(
            client_id=req.client_id,
            platform=req.platform,
            current_url=req.current_url,
            image_data_url=req.image_data_url,
            source=req.source,
            captured_at=req.captured_at,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc
    return ExtensionLoginQrNotifyResponse(**payload)


def publisher_extension_session_sync(
    req: ExtensionSessionSyncRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionSessionSyncResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.record_browser_session(
        client_id=req.client_id,
        platform=req.platform,
        cookies=[item.model_dump() for item in req.cookies],
        raw_state=req.raw_state,
    )
    return ExtensionSessionSyncResponse(**payload)


def publisher_extension_get_browser_session(
    platform: str,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionBrowserSessionResponse | None:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.get_browser_session(platform)
    if payload is None:
        return None
    return ExtensionBrowserSessionResponse(**payload)


def get_publisher_browser_session_summary(
    platform: str,
    *,
    publisher_manager,
) -> PublisherBrowserSessionSummaryResponse | None:
    payload = publisher_manager.get_browser_session_summary(platform)
    if payload is None:
        return None
    return PublisherBrowserSessionSummaryResponse(**payload)


def publisher_extension_heartbeat_status(
    *,
    publisher_manager,
    client_id: str = "",
    stale_seconds: int = 90,
    allow_latest_recent_fallback: bool = False,
    x_forwin_extension_key: str | None = None,
):
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    return publisher_manager.preferred_client_heartbeat(
        preferred_client_id=client_id,
        stale_seconds=stale_seconds,
        allow_latest_recent_fallback=allow_latest_recent_fallback,
    )


def finish_publisher_upload_attempt(
    job_id: str,
    attempt_id: str,
    req: UploadAttemptResultRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> UploadAttemptResultResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.update_upload_job_result(
            job_id=job_id,
            client_id=req.client_id,
            attempt_id=attempt_id,
            lease_epoch=req.lease_epoch,
            outcome=req.outcome,
            message=req.message,
            current_url=req.current_url,
            error_code=req.error_code,
            error_message=req.error_message,
            details=req.details.model_dump(
                mode="json", exclude_defaults=True, exclude_none=True
            ),
        )
    except ValueError as exc:
        _raise_protocol_http_error(exc)
    return UploadAttemptResultResponse(**_result_state_payload(payload))


def heartbeat_publisher_upload_attempt(
    job_id: str,
    attempt_id: str,
    req: UploadAttemptHeartbeatRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> UploadAttemptStateResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.heartbeat_upload_attempt(
            job_id=job_id,
            attempt_id=attempt_id,
            worker_id=req.client_id,
            lease_epoch=req.lease_epoch,
        )
    except ValueError as exc:
        _raise_protocol_http_error(exc)
    return UploadAttemptStateResponse(**_attempt_state_payload(payload))


def transition_publisher_upload_attempt(
    job_id: str,
    attempt_id: str,
    req: UploadAttemptPhaseRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> UploadAttemptStateResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.transition_upload_attempt(
            job_id=job_id,
            attempt_id=attempt_id,
            worker_id=req.client_id,
            lease_epoch=req.lease_epoch,
            phase=req.phase,
            current_url=req.current_url,
        )
    except ValueError as exc:
        _raise_protocol_http_error(exc)
    return UploadAttemptStateResponse(**_attempt_state_payload(payload))


def reconcile_publisher_upload_attempt(
    job_id: str,
    attempt_id: str,
    req: UploadAttemptReconcileRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> UploadAttemptReconcileResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.reconcile_upload_job(
            job_id=job_id,
            client_id=req.client_id,
            attempt_id=attempt_id,
            lease_epoch=req.lease_epoch,
            outcome=req.outcome,
            receipt=(
                req.receipt.model_dump(
                    mode="json", exclude_defaults=True, exclude_none=True
                )
                if req.receipt is not None
                else None
            ),
            evidence=req.evidence.model_dump(
                mode="json", exclude_defaults=True, exclude_none=True
            ),
            observed_at=req.observed_at,
            current_url=req.current_url,
            error_code=req.error_code,
            error_message=req.error_message,
        )
    except ValueError as exc:
        _raise_protocol_http_error(exc)
    response_payload = {
        **_result_state_payload(payload),
        "outcome": req.outcome,
        "receipt": _protocol_receipt_payload(payload.get("protocol_receipt")),
    }
    return UploadAttemptReconcileResponse(**response_payload)


def record_publisher_upload_receipt(
    job_id: str,
    attempt_id: str,
    req: UploadAttemptReceiptRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> UploadAttemptReceiptResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.record_upload_receipt(
            job_id=job_id,
            client_id=req.client_id,
            attempt_id=attempt_id,
            lease_epoch=req.lease_epoch,
            receipt=req.model_dump(
                mode="json",
                exclude={"client_id", "lease_epoch"},
                exclude_defaults=True,
                exclude_none=True,
            ),
        )
    except ValueError as exc:
        _raise_protocol_http_error(exc)
    response_payload = {
        **_attempt_state_payload(payload),
        "disposition": str(payload.get("receipt_disposition") or "created"),
        "receipt": _protocol_receipt_payload(payload.get("protocol_receipt")),
    }
    return UploadAttemptReceiptResponse(**response_payload)


def claim_publisher_upload_job(
    req: ExtensionClaimUploadJobRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionClaimUploadJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.claim_next_upload_job(
        client_id=req.client_id,
        connected_platforms=list(dict.fromkeys(req.connected_platforms)),
    )
    if payload is None:
        return ExtensionClaimUploadJobResponse(
            found=False,
            server_time=datetime.now(timezone.utc).isoformat(),
            retry_after_seconds=5,
            claim=None,
        )
    return ExtensionClaimUploadJobResponse(
        found=True,
        server_time=_server_time(payload),
        retry_after_seconds=0,
        claim=ExtensionUploadClaim(
            execution_mode=str(payload.get("execution_mode") or "execute"),
            job=_claim_job_payload(payload),
            attempt=ExtensionUploadClaimAttempt(
                attempt_id=str(payload.get("attempt_id") or ""),
                attempt_number=int(payload.get("attempt_number") or 0),
                lease_epoch=int(payload.get("lease_epoch") or 0),
                phase="claimed",
                lease_expires_at=str(payload.get("lease_expires_at") or ""),
                heartbeat_interval_seconds=int(
                    payload.get("heartbeat_interval_seconds") or 30
                ),
            ),
        ),
    )


def claim_publisher_comment_sync_job(
    req: ExtensionClaimCommentSyncJobRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionClaimCommentSyncJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.claim_next_comment_sync_job(
        client_id=req.client_id,
        connected_platforms=req.connected_platforms,
    )
    if payload is None:
        return ExtensionClaimCommentSyncJobResponse(found=False, job=None)
    return ExtensionClaimCommentSyncJobResponse(
        found=True,
        job=PublisherCommentSyncJobResponse(**payload),
    )


def create_publisher_comment_sync_job(
    req: PublisherCommentSyncJobRequest,
    *,
    publisher_manager,
) -> PublisherCommentSyncJobResponse:
    try:
        payload = publisher_manager.create_comment_sync_job(
            project_id=req.project_id,
            platform=req.platform,
            work_id=req.work_id,
            work_name=req.work_name,
            chapter_id=req.chapter_id,
            chapter_title=req.chapter_title,
            limit=req.limit,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherCommentSyncJobResponse(**payload)


def update_publisher_comment_sync_job_result(
    job_id: str,
    req: CommentSyncJobResultRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> PublisherCommentSyncJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.update_comment_sync_job_result(
            job_id=job_id,
            client_id=req.client_id,
            status=req.status,
            message=req.message,
            error=req.error,
            result_payload=req.result_payload,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherCommentSyncJobResponse(**payload)


def ingest_publisher_comments_batch(
    req: ExtensionCommentsBatchRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionCommentsBatchResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.ingest_comments_batch(
            client_id=req.client_id,
            platform=req.platform,
            job_id=req.job_id,
            comments=[item.model_dump() for item in req.comments],
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return ExtensionCommentsBatchResponse(**payload)
