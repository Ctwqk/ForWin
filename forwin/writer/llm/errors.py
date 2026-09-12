from __future__ import annotations

import hashlib
import logging
import re
import time
from email.utils import parsedate_to_datetime

import httpx


logger = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_LLM_ROUTE_POLICY_VERSION = "v4.0-capable-provider-fallbacks"
_ATTEMPT_RECORDED_ATTR = "_forwin_llm_attempt_recorded"


class LLMInputLimitError(ValueError):
    """Provider-declared input limit; repeating this input cannot succeed."""

    error_category = "input_limit"

    def __init__(self, message: str, *, response: httpx.Response):
        super().__init__(message)
        self.response = response


class ErrorsMixin:
    @classmethod
    def _http_error_message(
        cls,
        exc: BaseException,
        profile: dict[str, str],
    ) -> str:
        response = getattr(exc, "response", None)
        if isinstance(response, httpx.Response):
            return cls._http_error_message_from_response(response, profile)
        return str(exc)

    @classmethod
    def _http_error_message_from_response(
        cls,
        response: httpx.Response,
        profile: dict[str, str],
    ) -> str:
        message = f"HTTP {response.status_code}"
        try:
            preview = response.text
        except Exception:  # noqa: BLE001
            preview = ""
        preview = cls._redact_error_preview(preview, profile)
        if preview:
            message = f"{message}: {preview}"
        return message

    @staticmethod
    def _redact_error_preview(text: str, profile: dict[str, str]) -> str:
        preview = " ".join(str(text or "").split())
        api_key = str(profile.get("api_key") or "").strip()
        for secret in (api_key, f"Bearer {api_key}" if api_key else ""):
            if secret:
                preview = preview.replace(secret, "***")
        return preview[:500]

    @staticmethod
    def _provider_request_id(response: httpx.Response) -> str:
        return str(
            response.headers.get("x-request-id")
            or response.headers.get("x-minimax-request-id")
            or response.headers.get("request-id")
            or ""
        )

    @staticmethod
    def _safe_response_text(response: httpx.Response) -> str:
        try:
            return str(response.text or "")
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _hash_text(text: str) -> str:
        return hashlib.sha256(str(text or "").encode("utf-8")).hexdigest()

    @staticmethod
    def _timeout_seconds_value(timeout: httpx.Timeout) -> float:
        value = getattr(timeout, "read", None) or getattr(timeout, "connect", None) or 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @staticmethod
    def _error_category_for_status(status_code: int) -> str:
        if status_code in {429}:
            return "rate_limit"
        if status_code in {529, 500, 502, 503, 504}:
            return "provider_overload"
        if status_code in {401, 403}:
            return "auth"
        if 400 <= status_code < 500:
            return "bad_request"
        if status_code:
            return "unknown"
        return ""

    @staticmethod
    def _provider_input_limit(response: httpx.Response) -> bool:
        if response.status_code == 413:
            return True
        try:
            data = response.json()
        except (ValueError, TypeError):
            return False
        error = data.get("error") if isinstance(data, dict) else None
        if not error:
            return False
        if isinstance(error, dict):
            code = str(error.get("code") or error.get("type") or "").lower()
            if code in {"context_length_exceeded", "input_limit", "input_too_long", "prompt_too_long", "max_tokens_exceeded"}:
                return True
            message = str(error.get("message") or "").lower()
        else:
            message = str(error).lower()
        return any(marker in message for marker in (
            "maximum context length", "context length exceeded", "context window exceeded",
            "input is too long", "input too long", "prompt is too long", "prompt too long",
            "too many input tokens", "input token limit", "request exceeds the model's context",
        ))

    @staticmethod
    def _timeout_kind(exc: BaseException) -> str:
        if isinstance(exc, httpx.ConnectTimeout):
            return "connect_timeout"
        if isinstance(exc, httpx.ReadTimeout):
            return "read_timeout"
        if isinstance(exc, httpx.TimeoutException):
            return "overall_timeout"
        return ""

    @classmethod
    def _is_fallback_retryable(cls, exc: Exception) -> bool:
        chain: list[BaseException] = []
        current: BaseException | None = exc
        while current is not None and current not in chain:
            chain.append(current)
            current = current.__cause__ or current.__context__
        if any(isinstance(error, LLMInputLimitError) for error in chain):
            return False
        for error in chain:
            if isinstance(error, (httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError)):
                return True
            if isinstance(error, httpx.HTTPStatusError):
                if cls._provider_input_limit(error.response):
                    return False
                return error.response.status_code in _RETRYABLE_HTTP_STATUS_CODES
            message = str(error).lower()
            if re.search(r"\b(?:http|status code)\s*(?:408|409|425|429|500|502|503|504|529)\b", message):
                return True
            if any(token in message for token in (
                "temporarily unavailable", "service unavailable", "rate limit",
                "too many requests", "overloaded", "connection reset", "server disconnected",
                "network error", "timed out", "timeout",
            )):
                return True
        return False

    def _retry_delay(
        self,
        attempt: int,
        response: httpx.Response | None = None,
    ) -> float:
        retry_after = response.headers.get("retry-after") if response is not None else None
        parsed_retry_after = self._parse_retry_after(retry_after)
        if parsed_retry_after is not None:
            return min(self.retry_max_delay_seconds, max(0.0, parsed_retry_after))
        delay = self.retry_initial_delay_seconds * (2 ** max(0, attempt))
        return min(self.retry_max_delay_seconds, max(0.0, delay))

    @staticmethod
    def _parse_retry_after(value: str | None) -> float | None:
        if not value:
            return None
        stripped = value.strip()
        if not stripped:
            return None
        try:
            return float(stripped)
        except ValueError:
            pass
        try:
            retry_at = parsedate_to_datetime(stripped)
        except (TypeError, ValueError, IndexError, OverflowError):
            return None
        return max(0.0, retry_at.timestamp() - time.time())


__all__ = [
    'ErrorsMixin',
]
