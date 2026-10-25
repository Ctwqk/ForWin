from __future__ import annotations

import hashlib
import logging
import time
from email.utils import parsedate_to_datetime

import httpx


logger = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, *range(500, 600)}
_ATTEMPT_RECORDED_ATTR = "_forwin_llm_attempt_recorded"
_PARSE_ERROR_ATTR = "_forwin_llm_parse_error"
_TRANSIENT_ERROR_TOKENS = (
    "http 529",
    "status code 529",
    "529 unknown status code",
    "429",
    "500",
    "502",
    "503",
    "504",
    "temporarily unavailable",
    "service unavailable",
    "rate limit",
    "too many requests",
    "overloaded",
    "connection reset",
    "connection refused",
    "remoteprotocolerror",
    "server disconnected",
    "network error",
    "timed out",
    "timeout",
)


def is_transient_error_chain(exc: BaseException) -> bool:
    """Return True when any exception in the cause/context chain reads as transient."""
    current: BaseException | None = exc
    while current is not None:
        message = " ".join(str(current).lower().split())
        if any(token in message for token in _TRANSIENT_ERROR_TOKENS):
            return True
        current = current.__cause__ or current.__context__
    return False


class EmptyLLMResponse(ValueError):
    pass


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
        if 500 <= status_code < 600:
            return "provider_overload"
        if status_code in {401, 402, 403}:
            return "auth_or_balance"
        if status_code == 404:
            return "model_not_found"
        if 400 <= status_code < 500:
            return "bad_request"
        if status_code:
            return "unknown"
        return ""

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
        if getattr(exc, _PARSE_ERROR_ATTR, False):
            return False
        if isinstance(exc, EmptyLLMResponse):
            return True
        if isinstance(exc, (httpx.ReadTimeout, httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code if exc.response is not None else 0
            return status_code in _RETRYABLE_HTTP_STATUS_CODES or status_code in {401, 402, 403, 404}
        return is_transient_error_chain(exc)

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
    'is_transient_error_chain',
]
