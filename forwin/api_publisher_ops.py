from __future__ import annotations

import io
import json
import zipfile
from pathlib import Path
from typing import Any

from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from forwin.api_schemas import (
    CommentSyncJobResultRequest,
    ExtensionBrowserSessionResponse,
    ExtensionClaimCommentSyncJobRequest,
    ExtensionClaimCommentSyncJobResponse,
    ExtensionClaimUploadJobRequest,
    ExtensionClaimUploadJobResponse,
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
    UploadJobResultRequest,
)
from forwin.publishers.manager import (
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


def _build_extension_package(extension_root: Path, *, target: str = "chromium") -> bytes:
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
    manifest = _firefox_manifest(source_manifest) if target == "firefox" else source_manifest

    buffer = io.BytesIO()
    archive_root = Path("forwin-publisher-firefox" if target == "firefox" else "forwin-publisher")
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


def _require_extension_auth(publisher_manager, x_forwin_extension_key: str | None) -> None:
    try:
        publisher_manager.verify_extension_api_key(x_forwin_extension_key)
    except PublisherExtensionAuthNotConfigured as exc:
        raise HTTPException(503, str(exc)) from exc
    except PublisherExtensionAuthError as exc:
        raise HTTPException(401, str(exc)) from exc


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


def download_publisher_firefox_extension_package(*, extension_root: Path) -> StreamingResponse:
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
    return [PublisherPlatformInfo(**item) for item in publisher_manager.list_platforms()]


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


def get_publisher_upload_job(job_id: str, *, publisher_manager) -> PublisherUploadJobResponse:
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


def terminate_publisher_upload_job(job_id: str, *, publisher_manager) -> TaskMutationResponse:
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


def delete_publisher_upload_job(job_id: str, *, publisher_manager) -> TaskMutationResponse:
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


def update_publisher_upload_job_result(
    job_id: str,
    req: UploadJobResultRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> PublisherUploadJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    try:
        payload = publisher_manager.update_upload_job_result(
            job_id=job_id,
            client_id=req.client_id,
            status=req.status,
            message=req.message,
            current_url=req.current_url,
            error=req.error,
            result_payload=req.result_payload,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return PublisherUploadJobResponse(**payload)


def claim_publisher_upload_job(
    req: ExtensionClaimUploadJobRequest,
    *,
    publisher_manager,
    x_forwin_extension_key: str | None = None,
) -> ExtensionClaimUploadJobResponse:
    _require_extension_auth(publisher_manager, x_forwin_extension_key)
    payload = publisher_manager.claim_next_upload_job(
        client_id=req.client_id,
        connected_platforms=req.connected_platforms,
    )
    if payload is None:
        return ExtensionClaimUploadJobResponse(found=False, job=None)
    return ExtensionClaimUploadJobResponse(
        found=True,
        job=PublisherUploadJobResponse(**payload),
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
