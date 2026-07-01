#!/usr/bin/env python3
from __future__ import annotations

from typing import Any


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
