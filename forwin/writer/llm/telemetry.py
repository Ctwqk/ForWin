from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.model_adapter import ModelCapabilities

logger = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_LLM_ROUTE_POLICY_VERSION = "v3.8-stage-aware-hard-replacement"
_ATTEMPT_RECORDED_ATTR = "_forwin_llm_attempt_recorded"


class TelemetryMixin:
    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        events = list(self.model_fallback_events)
        self.model_fallback_events.clear()
        return events

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        events = list(self.llm_attempt_events)
        self.llm_attempt_events.clear()
        return events

    def _record_llm_attempt(
        self,
        *,
        attempt_group_id: str,
        profile: dict[str, str],
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        response_format: dict | None,
        request_timeout: httpx.Timeout,
        attempt_no: int,
        http_status: int = 0,
        provider_request_id: str = "",
        duration_ms: int = 0,
        input_chars: int | None = None,
        output_chars: int = 0,
        retry_after: float | None = None,
        sleep_ms: int = 0,
        error_class: str = "",
        error_message: str = "",
        error_category: str = "",
        timeout_kind: str = "",
        retryable: bool = False,
        fallback_eligible: bool = False,
        final_failure: bool = False,
        requested_temperature: float | None = None,
        requested_max_tokens: int | None = None,
        task_family: str = "",
        stage_key: str = "",
        llm_task_route: str = "",
        request_payload: dict[str, object] | None = None,
        response_text: str = "",
        candidate_chain: list[dict[str, str]] | None = None,
        skipped_profiles: list[dict[str, str]] | None = None,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> None:
        base_url = str(profile.get("base_url") or "")
        request_text = json.dumps(request_payload or {}, ensure_ascii=False, sort_keys=True)
        response_text = str(response_text or "")
        self.llm_attempt_events.append(
            {
                "attempt_group_id": attempt_group_id,
                "profile_id": str(profile.get("id") or ""),
                "profile_name": str(profile.get("name") or ""),
                "model": str(profile.get("model") or ""),
                "base_url_host": urlparse(base_url).netloc or base_url,
                "temperature": temperature,
                "requested_temperature": (
                    float(requested_temperature)
                    if requested_temperature is not None
                    else temperature
                ),
                "max_tokens": max_tokens,
                "requested_max_tokens": (
                    int(requested_max_tokens)
                    if requested_max_tokens is not None
                    else int(max_tokens)
                ),
                "timeout_seconds": self._timeout_seconds_value(request_timeout),
                "attempt_no": attempt_no,
                "http_status": int(http_status or 0),
                "provider_request_id": provider_request_id,
                "duration_ms": int(duration_ms or 0),
                "input_chars": (
                    int(input_chars)
                    if input_chars is not None
                    else len(json.dumps(messages, ensure_ascii=False))
                ),
                "output_chars": int(output_chars or 0),
                "response_format": response_format or {},
                "task_family": str(task_family or ""),
                "stage_key": str(stage_key or ""),
                "llm_task_route": str(llm_task_route or ""),
                "preferred_provider_kind": str(preferred_provider_kind or ""),
                "preferred_model": str(preferred_model or ""),
                "retry_after": retry_after,
                "sleep_ms": int(sleep_ms or 0),
                "error_class": error_class,
                "error_message": error_message,
                "error_category": error_category,
                "timeout_kind": timeout_kind,
                "retryable": bool(retryable),
                "fallback_eligible": bool(fallback_eligible),
                "final_failure": bool(final_failure),
                "route_policy_version": _LLM_ROUTE_POLICY_VERSION,
                "candidate_chain": list(candidate_chain or []),
                "skipped_profiles": list(skipped_profiles or []),
                "request_hash": self._hash_text(request_text) if request_payload else "",
                "response_hash": self._hash_text(response_text) if response_text else "",
                "response_preview": self._redact_error_preview(response_text, profile) if response_text else "",
                "_raw_request_payload": request_payload or {},
                "_raw_response_text": response_text,
            }
        )


__all__ = [
    'TelemetryMixin',
]
