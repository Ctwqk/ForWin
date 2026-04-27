from __future__ import annotations

import hashlib
import traceback
from collections.abc import Mapping
from typing import Any


_SENSITIVE_KEY_PARTS = {
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "cookies",
    "session",
    "browser_session",
    "raw_browser_session",
    "login",
    "password",
    "secret",
    "token",
    "raw_prompt",
    "raw_response",
    "prompt",
    "response",
}

_SAFE_KEY_EXACT = {
    "request_artifact_uri",
    "raw_prompt_artifact_uri",
    "redacted_request_artifact_uri",
    "response_artifact_uri",
    "raw_response_artifact_uri",
    "raw_output_artifact_uri",
    "request_hash",
    "response_hash",
    "response_preview",
    "response_format",
    "response_size",
    "request_size",
    "raw_output_preview",
    "input_chars",
    "output_chars",
}


def _is_sensitive_key(key: object) -> bool:
    lowered = str(key or "").strip().lower()
    if lowered in _SAFE_KEY_EXACT:
        return False
    if lowered.endswith("_artifact_uri") or lowered.endswith("_hash"):
        return False
    return any(part in lowered for part in _SENSITIVE_KEY_PARTS)


def redact_payload(value: Any) -> Any:
    if isinstance(value, Mapping):
        return {
            str(key): "[REDACTED]" if _is_sensitive_key(key) else redact_payload(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [redact_payload(item) for item in value]
    if isinstance(value, tuple):
        return [redact_payload(item) for item in value]
    return value


def stack_hash(exc: BaseException) -> str:
    frames = traceback.extract_tb(exc.__traceback__)
    signature = "|".join(f"{frame.filename}:{frame.name}:{frame.lineno}" for frame in frames)
    if not signature:
        signature = exc.__class__.__name__
    return hashlib.sha256(signature.encode("utf-8")).hexdigest()[:16]
