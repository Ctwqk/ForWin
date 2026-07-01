#!/usr/bin/env python3
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


SENSITIVE_EXACT_KEYS = {"cookie", "cookies", "set-cookie", "image_data_url", "body"}
SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "csrf",
    "password",
    "secret",
    "token",
    "webhook",
)
TERMINAL_UPLOAD_STATUSES = {"succeeded", "failed", "cancelled"}


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")


def redact_report(value: Any) -> Any:
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if normalized in SENSITIVE_EXACT_KEYS or any(
                part in normalized for part in SENSITIVE_KEY_PARTS
            ):
                redacted[str(key)] = "[redacted]"
            else:
                redacted[str(key)] = redact_report(item)
        return redacted
    if isinstance(value, list):
        return [redact_report(item) for item in value]
    return value


def short_text(value: Any, limit: int = 240) -> str:
    text = str(value or "")
    if len(text) <= limit:
        return text
    return text[: max(0, limit - 3)] + "..."


def summarize_upload_job(job: dict[str, Any]) -> dict[str, Any]:
    payload = job.get("result_payload") if isinstance(job.get("result_payload"), dict) else {}
    return redact_report(
        {
            "job_id": job.get("job_id") or "",
            "task_kind": job.get("task_kind") or "chapter_upload",
            "project_id": job.get("project_id") or "",
            "platform": job.get("platform") or "",
            "status": job.get("status") or "",
            "book_name": short_text(job.get("book_name")),
            "chapter_title": short_text(job.get("chapter_title")),
            "publish": bool(job.get("publish")),
            "extension_client_id": job.get("extension_client_id") or "",
            "current_url": short_text(job.get("current_url")),
            "message": short_text(job.get("message")),
            "error": short_text(job.get("error")),
            "result_payload": payload,
            "abort_requested": bool(job.get("abort_requested")),
            "created_at": job.get("created_at") or "",
            "updated_at": job.get("updated_at") or "",
            "claimed_at": job.get("claimed_at") or "",
            "started_at": job.get("started_at") or "",
            "finished_at": job.get("finished_at") or "",
            "terminable": bool(job.get("terminable")),
            "deletable": bool(job.get("deletable")),
        }
    )


def safe_upload_payload(
    *,
    platform: str,
    book_name: str,
    chapter_title: str,
    body: str,
) -> dict[str, Any]:
    return {
        "platform": platform,
        "book_name": book_name,
        "chapter_title": chapter_title,
        "body": body,
        "publish": False,
        "create_if_missing": False,
        "cover_generation_enabled": False,
        "cover_confirmation_required": False,
        "cover_candidate_count": 1,
        "auto_cover_upload_enabled": False,
        "publisher_compliance_required": False,
    }


def _api_url(api_base: str, path: str) -> str:
    return f"{str(api_base).rstrip('/')}/{path.lstrip('/')}"


def http_json(
    method: str,
    url: str,
    *,
    payload: dict[str, Any] | None = None,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
) -> dict[str, Any]:
    body = None
    request_headers = {"Accept": "application/json", **(headers or {})}
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        request_headers["Content-Type"] = "application/json"
    request = Request(
        url,
        data=body,
        headers=request_headers,
        method=str(method or "GET").upper(),
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            status = int(getattr(response, "status", 0) or 0)
            raw_body = response.read(2_000_000).decode("utf-8", errors="replace")
    except HTTPError as exc:
        return {"ok": False, "status": exc.code, "error": str(exc)}
    except URLError as exc:
        return {"ok": False, "error": str(exc)}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "error": f"{exc.__class__.__name__}: {exc}"}
    try:
        parsed: Any = json.loads(raw_body) if raw_body else None
    except json.JSONDecodeError:
        parsed = {"raw": raw_body[:500]}
    return {
        "ok": 200 <= status < 300,
        "status": status,
        "payload": redact_report(parsed),
    }


def append_block(
    report: dict[str, Any],
    *,
    kind: str,
    severity: str,
    message: str,
    **extra: Any,
) -> None:
    item = {
        "kind": kind,
        "severity": severity,
        "message": short_text(message),
    }
    item.update({key: value for key, value in extra.items() if value not in (None, "")})
    report.setdefault("blocked_items", []).append(redact_report(item))


def append_action(report: dict[str, Any], action: str, **extra: Any) -> None:
    item = {"action": action, **extra}
    report.setdefault("actions_taken", []).append(redact_report(item))


def summarize_platform(item: dict[str, Any]) -> dict[str, Any]:
    preferred = item.get("preferred_client_state") if isinstance(item.get("preferred_client_state"), dict) else {}
    latest = item.get("latest_client_state") if isinstance(item.get("latest_client_state"), dict) else {}
    session = item.get("browser_session_state") if isinstance(item.get("browser_session_state"), dict) else {}
    return redact_report(
        {
            "platform_id": str(item.get("platform_id") or ""),
            "connected": bool(item.get("connected")),
            "preferred_connected": bool(preferred.get("connected")),
            "latest_connected": bool(latest.get("connected")),
            "session_connected": bool(session.get("connected")),
            "extension_online": bool(item.get("extension_online")),
            "last_heartbeat_at": item.get("last_heartbeat_at") or "",
            "last_error": short_text(item.get("last_error")),
        }
    )


def summarize_browser_session(platform: str, payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return {"platform": platform, "ok": False, "connected": False}
    return redact_report(
        {
            "platform": payload.get("platform") or platform,
            "ok": True,
            "connected": bool(payload.get("connected")),
            "client_id": payload.get("client_id") or "",
            "cookie_count": int(payload.get("cookie_count") or 0),
            "cookie_names": payload.get("cookie_names") if isinstance(payload.get("cookie_names"), list) else [],
            "cookies_redacted": bool(payload.get("cookies_redacted", True)),
            "synced_at": payload.get("synced_at") or "",
            "last_error": short_text(payload.get("last_error")),
        }
    )


def extension_headers_from_env(args: Any, report: dict[str, Any]) -> dict[str, str] | None:
    env_name = str(getattr(args, "extension_key_env", "") or "").strip()
    if not env_name:
        append_block(
            report,
            kind="extension_key_missing",
            severity="operator",
            message="Extension heartbeat-status check requires an env var name.",
        )
        return None
    key = os.environ.get(env_name)
    if not key:
        append_block(
            report,
            kind="extension_key_missing",
            severity="operator",
            message=f"Extension heartbeat-status skipped because {env_name} is not set.",
        )
        return None
    return {"X-Forwin-Extension-Key": key}


def _payload_list(response: dict[str, Any]) -> list[Any]:
    payload = response.get("payload")
    return payload if isinstance(payload, list) else []


def cleanup_api_smoke_job(args: Any, report: dict[str, Any], job_id: str) -> dict[str, Any]:
    cleanup: dict[str, Any] = {"job_id": job_id, "terminated": False, "deleted": False}
    terminate = http_json(
        "POST",
        _api_url(args.api_base, f"/api/publishers/upload-jobs/{job_id}/terminate"),
    )
    cleanup["terminate"] = redact_report(terminate)
    cleanup["terminated"] = bool(terminate.get("ok"))
    delete = http_json(
        "DELETE",
        _api_url(args.api_base, f"/api/publishers/upload-jobs/{job_id}"),
    )
    cleanup["delete"] = redact_report(delete)
    cleanup["deleted"] = bool(delete.get("ok"))
    if not cleanup["deleted"]:
        append_block(
            report,
            kind="api_smoke_job_cleanup_failed",
            severity="operator",
            message=f"API smoke job {job_id} could not be deleted.",
            job_id=job_id,
        )
    return cleanup


def platform_connected(report: dict[str, Any], platform: str) -> bool:
    for item in report.get("platforms", []):
        if not isinstance(item, dict):
            continue
        if item.get("platform_id") == platform:
            return bool(item.get("connected"))
    return False


def poll_upload_job(
    args: Any,
    job_id: str,
    *,
    initial_job: dict[str, Any] | None = None,
) -> dict[str, Any]:
    deadline = time.monotonic() + max(float(getattr(args, "poll_seconds", 120.0) or 0.0), 0.0)
    interval = max(float(getattr(args, "poll_interval_seconds", 5.0) or 0.0), 0.01)
    states: list[str] = []
    last_job = initial_job or {}
    if initial_job:
        status = str(initial_job.get("status") or "")
        if status:
            states.append(status)
        if status in TERMINAL_UPLOAD_STATUSES:
            return {
                **summarize_upload_job(initial_job),
                "states": states,
                "terminal_state": status,
            }

    while True:
        response = http_json(
            "GET",
            _api_url(args.api_base, f"/api/publishers/upload-jobs/{job_id}"),
        )
        payload = response.get("payload") if isinstance(response.get("payload"), dict) else {}
        if payload:
            last_job = payload
        status = str(payload.get("status") or "")
        if status:
            states.append(status)
        if status in TERMINAL_UPLOAD_STATUSES:
            return {
                **summarize_upload_job(payload),
                "states": states,
                "terminal_state": status,
            }
        if time.monotonic() >= deadline:
            terminate = http_json(
                "POST",
                _api_url(args.api_base, f"/api/publishers/upload-jobs/{job_id}/terminate"),
            )
            return {
                **summarize_upload_job(last_job),
                "job_id": job_id,
                "states": states,
                "terminal_state": "timeout",
                "terminate": redact_report(terminate),
            }
        time.sleep(interval)


def _upload_platforms(args: Any) -> list[str]:
    explicit = list(getattr(args, "upload_platform", []) or [])
    if explicit:
        return explicit
    return list(getattr(args, "expect_platform_connected", []) or [])


def run_upload_smoke(args: Any, report: dict[str, Any]) -> None:
    report["upload_jobs"] = []
    if not bool(getattr(args, "run_upload_smoke", False)):
        return

    for platform in _upload_platforms(args):
        if not platform_connected(report, platform):
            append_block(
                report,
                kind="publisher_login_required",
                severity="human",
                message=f"{platform} publisher login is not connected.",
                platform=platform,
            )
            continue
        created = http_json(
            "POST",
            _api_url(args.api_base, "/api/publishers/upload-jobs"),
            payload=safe_upload_payload(
                platform=platform,
                book_name=getattr(args, "book_name", "ForWin Smoke Test"),
                chapter_title=getattr(args, "chapter_title", "ForWin smoke chapter"),
                body=getattr(args, "body", "This is a safe smoke chapter body."),
            ),
        )
        payload = created.get("payload") if isinstance(created.get("payload"), dict) else {}
        job_id = str(payload.get("job_id") or "")
        if not created.get("ok") or not job_id:
            append_block(
                report,
                kind="upload_job_create_failed",
                severity="failed",
                message=f"{platform} upload smoke job could not be created.",
                platform=platform,
            )
            continue
        result = poll_upload_job(args, job_id, initial_job=payload)
        report["upload_jobs"].append(redact_report(result))
        append_action(
            report,
            "ran_upload_smoke",
            platform=platform,
            job_id=job_id,
            terminal_state=result.get("terminal_state"),
        )
        if result.get("terminal_state") == "timeout":
            append_block(
                report,
                kind="upload_job_timeout",
                severity="operator",
                message=f"{platform} upload smoke job timed out before terminal state.",
                platform=platform,
                job_id=job_id,
            )


def run_endpoint_smoke(args: Any, report: dict[str, Any]) -> None:
    expected = list(getattr(args, "expect_platform_connected", []) or [])
    report["publisher_api"] = {}
    endpoint: dict[str, Any] = {
        "preflight": {},
        "work_bindings": {},
        "chapter_bindings": {},
    }
    report["endpoint_smoke"] = endpoint

    platforms_response = http_json("GET", _api_url(args.api_base, "/api/publishers/platforms"))
    raw_platforms = _payload_list(platforms_response)
    platforms = [summarize_platform(item) for item in raw_platforms if isinstance(item, dict)]
    by_id = {item["platform_id"]: item for item in platforms}
    missing = sorted(platform for platform in expected if not by_id.get(platform, {}).get("connected"))
    report["platforms"] = platforms
    report["publisher_api"]["platforms"] = {
        "ok": bool(platforms_response.get("ok")) and not missing,
        "status": platforms_response.get("status"),
        "missing_expected": missing,
        "platforms": platforms,
    }
    if not platforms_response.get("ok"):
        append_block(
            report,
            kind="publisher_platforms_unavailable",
            severity="failed",
            message=str(platforms_response.get("error") or "publisher platforms API unavailable"),
        )
    for platform in missing:
        append_block(
            report,
            kind="publisher_login_required",
            severity="human",
            message=f"{platform} publisher login is not connected.",
            platform=platform,
        )

    sessions: list[dict[str, Any]] = []
    for platform in expected:
        response = http_json(
            "GET",
            _api_url(args.api_base, f"/api/publishers/browser-sessions/{platform}"),
        )
        sessions.append(summarize_browser_session(platform, response.get("payload")))
    report["publisher_api"]["browser_sessions"] = sessions

    headers = extension_headers_from_env(args, report)
    if headers:
        heartbeat = http_json(
            "GET",
            _api_url(args.api_base, "/api/publishers/extension/heartbeat-status"),
            headers=headers,
        )
        report["publisher_api"]["heartbeat_status"] = redact_report(
            {
                "ok": bool(heartbeat.get("ok")),
                "status": heartbeat.get("status"),
                "payload": heartbeat.get("payload") if isinstance(heartbeat.get("payload"), dict) else {},
            }
        )
    else:
        report["publisher_api"]["heartbeat_status"] = {"ok": False, "skipped": True}

    preflight_payload = {
        "platform": getattr(args, "endpoint_platform", "fanqie"),
        "book_name": getattr(args, "book_name", "ForWin Smoke Test"),
        "chapter_title": getattr(args, "chapter_title", "ForWin smoke chapter"),
        "body": getattr(args, "body", "This is a safe smoke chapter body."),
        "create_if_missing": False,
    }
    preflight = http_json(
        "POST",
        _api_url(args.api_base, "/api/publishers/preflight"),
        payload=preflight_payload,
    )
    endpoint["preflight"] = redact_report(preflight)

    work_bindings = http_json("GET", _api_url(args.api_base, "/api/publishers/work-bindings"))
    chapter_bindings = http_json("GET", _api_url(args.api_base, "/api/publishers/chapter-bindings"))
    endpoint["work_bindings"] = {
        "ok": bool(work_bindings.get("ok")),
        "status": work_bindings.get("status"),
        "count": len(_payload_list(work_bindings)),
    }
    endpoint["chapter_bindings"] = {
        "ok": bool(chapter_bindings.get("ok")),
        "status": chapter_bindings.get("status"),
        "count": len(_payload_list(chapter_bindings)),
    }

    if bool(getattr(args, "create_api_smoke_job", False)):
        created = http_json(
            "POST",
            _api_url(args.api_base, "/api/publishers/upload-jobs"),
            payload=safe_upload_payload(
                platform=getattr(args, "endpoint_platform", "fanqie"),
                book_name=getattr(args, "book_name", "ForWin Smoke Test"),
                chapter_title=getattr(args, "chapter_title", "ForWin smoke chapter"),
                body=getattr(args, "body", "This is a safe smoke chapter body."),
            ),
        )
        created_payload = created.get("payload") if isinstance(created.get("payload"), dict) else {}
        job_id = str(created_payload.get("job_id") or "")
        endpoint["api_job"] = summarize_upload_job(created_payload) if job_id else redact_report(created)
        endpoint["api_job_list"] = http_json(
            "GET",
            _api_url(args.api_base, "/api/publishers/upload-jobs?limit=10"),
        )
        if job_id:
            endpoint["api_job_get"] = redact_report(
                http_json("GET", _api_url(args.api_base, f"/api/publishers/upload-jobs/{job_id}"))
            )
            endpoint["api_job_cleanup"] = cleanup_api_smoke_job(args, report, job_id)
        else:
            append_block(
                report,
                kind="api_smoke_job_create_failed",
                severity="failed",
                message="API smoke job create did not return a job_id.",
            )


def _rollup_status(report: dict[str, Any]) -> str:
    severities = {str(item.get("severity") or "") for item in report.get("blocked_items", [])}
    if "failed" in severities:
        return "failed"
    if severities:
        return "degraded"
    return "ok"


def build_report(args: Any) -> dict[str, Any]:
    report: dict[str, Any] = {
        "checked_at": utc_now(),
        "phase": "publisher_upload_chain_smoke",
        "status": "ok",
        "actions_taken": [],
        "blocked_items": [],
    }
    run_endpoint_smoke(args, report)
    run_upload_smoke(args, report)
    report["status"] = _rollup_status(report)
    return redact_report(report)
