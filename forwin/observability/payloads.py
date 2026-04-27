from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from typing import Any

from forwin.observability.redaction import redact_payload


AUDIT_SCHEMA_VERSION = "v4.5.1-audit"


def audit_payload(
    *,
    stage: str = "",
    status: str = "",
    operation_id: str = "",
    duration_ms: int | None = None,
    error_category: str = "",
    **payload: Any,
) -> dict[str, Any]:
    """Build a small redacted DecisionEvent payload with stable audit keys."""
    result: dict[str, Any] = {"schema_version": AUDIT_SCHEMA_VERSION}
    if operation_id:
        result["operation_id"] = str(operation_id)
    if stage:
        result["stage"] = str(stage)
    if status:
        result["status"] = str(status)
    if duration_ms is not None:
        result["duration_ms"] = max(0, int(duration_ms or 0))
    if error_category:
        result["error_category"] = str(error_category)
    result.update({key: value for key, value in payload.items() if value is not None})
    return redact_payload(result)


def event_error_payload(
    exc: BaseException,
    *,
    stage: str = "",
    status: str = "failed",
    operation_id: str = "",
    duration_ms: int | None = None,
    error_category: str = "",
    **payload: Any,
) -> dict[str, Any]:
    return audit_payload(
        stage=stage,
        status=status,
        operation_id=operation_id,
        duration_ms=duration_ms,
        error_category=error_category,
        error_class=exc.__class__.__name__,
        error_summary=safe_error_summary(exc),
        **payload,
    )


def safe_error_summary(exc: BaseException | str, *, limit: int = 500) -> str:
    text = " ".join(str(exc or "").split())
    if len(text) > limit:
        return f"{text[:limit]}..."
    return text


def attempt_group_ids(attempts: Iterable[Mapping[str, Any]]) -> list[str]:
    ids: list[str] = []
    for attempt in attempts:
        value = str(attempt.get("attempt_group_id") or "").strip()
        if value and value not in ids:
            ids.append(value)
    return ids


def artifact_manifest_item(
    *,
    uri: str,
    kind: str = "",
    redaction_state: str = "redacted",
    source_event_id: str = "",
    trace_id: str = "",
    content: str | bytes = b"",
) -> dict[str, Any]:
    if isinstance(content, str):
        content_bytes = content.encode("utf-8")
    else:
        content_bytes = bytes(content or b"")
    return {
        "uri": str(uri or "").strip(),
        "kind": str(kind or "").strip(),
        "redaction_state": str(redaction_state or "redacted").strip() or "redacted",
        "source_event_id": str(source_event_id or "").strip(),
        "trace_id": str(trace_id or "").strip(),
        "hash": hashlib.sha256(content_bytes).hexdigest() if content_bytes else "",
        "size": len(content_bytes),
    }
