from __future__ import annotations

import logging
import time
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import json

import httpx

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.model_adapter import ModelCapabilities

logger = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}


class OpenAICompatibleAdapter:
    """Synchronous wrapper for OpenAI-compatible LLM APIs."""

    def __init__(
        self,
        api_key: str,
        base_url: str = DEFAULT_MINIMAX_BASE_URL,
        model: str = DEFAULT_MINIMAX_MODEL,
        timeout_seconds: float = 90.0,
        retry_attempts: int = 2,
        retry_initial_delay_seconds: float = 2.0,
        retry_max_delay_seconds: float = 15.0,
        fallback_profiles: list[dict[str, str]] | None = None,
    ) -> None:
        self.provider = "openai_compatible"
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.profile_id = ""
        self.profile_name = ""
        self.capabilities = ModelCapabilities(
            supports_json_schema=False,
            supports_response_format=True,
            supports_tool_calling=False,
            supports_system_messages=True,
        )
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self.retry_attempts = max(1, int(retry_attempts or 1))
        self.retry_initial_delay_seconds = max(0.0, float(retry_initial_delay_seconds))
        self.retry_max_delay_seconds = max(
            self.retry_initial_delay_seconds,
            float(retry_max_delay_seconds),
        )
        self.fallback_profiles = list(fallback_profiles or [])
        self.model_fallback_events: list[dict[str, str]] = []
        self.llm_attempt_events: list[dict[str, object]] = []
        self.client = httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds))
        )

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.85,
        max_tokens: int = 16384,
        response_format: dict | None = None,
        timeout_seconds: float | None = None,
        retry_on_timeout: bool = True,
    ) -> str:
        """Send a chat completion request and return the content string.

        Retry policy:
        - 429/5xx/529 and other transient HTTP statuses: retry with bounded backoff.
        - ReadTimeout / TimeoutException: retry with bounded backoff when retry_on_timeout is true.
        - Any other HTTP error: raise immediately.
        """
        request_timeout = httpx.Timeout(
            max(5.0, float(timeout_seconds if timeout_seconds is not None else self.timeout_seconds)),
            connect=min(
                10.0,
                max(5.0, float(timeout_seconds if timeout_seconds is not None else self.timeout_seconds)),
            ),
        )

        profiles = self._request_profiles()
        last_exc: Exception | None = None
        for profile_index, profile in enumerate(profiles):
            try:
                return self._chat_with_profile(
                    profile,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    request_timeout=request_timeout,
                    retry_on_timeout=retry_on_timeout,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if profile_index >= len(profiles) - 1 or not self._is_fallback_retryable(exc):
                    raise
                next_profile = profiles[profile_index + 1]
                event = {
                    "from_profile_id": profile.get("id", ""),
                    "from_model": profile["model"],
                    "from_base_url": profile["base_url"],
                    "to_profile_id": next_profile.get("id", ""),
                    "to_model": next_profile["model"],
                    "to_base_url": next_profile["base_url"],
                    "reason": str(exc),
                }
                self.model_fallback_events.append(event)
                logger.warning(
                    "LLM profile fallback: %s (%s) -> %s (%s) after retries failed: %s",
                    event["from_model"],
                    event["from_base_url"],
                    event["to_model"],
                    event["to_base_url"],
                    exc,
                )
                continue

        if last_exc is not None:
            raise last_exc
        raise RuntimeError("OpenAICompatibleAdapter.chat: no usable LLM profile")

    def _chat_with_profile(
        self,
        profile: dict[str, str],
        *,
        messages: list[dict],
        temperature: float,
        max_tokens: int,
        response_format: dict | None,
        request_timeout: httpx.Timeout,
        retry_on_timeout: bool,
    ) -> str:
        payload = {
            "model": profile["model"],
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        headers = {
            "Authorization": f"Bearer {profile['api_key']}",
            "Content-Type": "application/json",
        }
        url = f"{profile['base_url'].rstrip('/')}/chat/completions"

        for attempt in range(self.retry_attempts):
            attempt_started_at = time.perf_counter()
            attempt_no = attempt + 1
            try:
                logger.debug(
                    "LLMClient.chat attempt=%d/%d model=%s messages=%d max_tokens=%d",
                    attempt_no,
                    self.retry_attempts,
                    profile["model"],
                    len(messages),
                    max_tokens,
                )
                response = self.client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request_timeout,
                )

                if response.status_code in _RETRYABLE_HTTP_STATUS_CODES:
                    retry_delay = self._retry_delay(attempt, response)
                    self._record_llm_attempt(
                        profile=profile,
                        messages=messages,
                        temperature=temperature,
                        max_tokens=max_tokens,
                        response_format=response_format,
                        request_timeout=request_timeout,
                        attempt_no=attempt_no,
                        http_status=response.status_code,
                        provider_request_id=self._provider_request_id(response),
                        duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                        retry_after=retry_delay,
                        error_class="HTTPStatusError" if attempt >= self.retry_attempts - 1 else "",
                        error_message=(
                            f"HTTP {response.status_code}"
                            if attempt >= self.retry_attempts - 1
                            else ""
                        ),
                    )
                    if attempt < self.retry_attempts - 1:
                        logger.warning(
                            "Transient LLM HTTP status %d. Waiting %.1f s before retry %d/%d.",
                            response.status_code,
                            retry_delay,
                            attempt + 2,
                            self.retry_attempts,
                        )
                        time.sleep(retry_delay)
                        continue
                    response.raise_for_status()

                response.raise_for_status()

                data = response.json()
                content: str = data["choices"][0]["message"]["content"]
                self._record_llm_attempt(
                    profile=profile,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    request_timeout=request_timeout,
                    attempt_no=attempt_no,
                    http_status=response.status_code,
                    provider_request_id=self._provider_request_id(response),
                    duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                    output_chars=len(content),
                )
                logger.debug(
                    "LLMClient.chat success: %d chars returned in %.2fs",
                    len(content),
                    time.perf_counter() - attempt_started_at,
                )
                return content

            except (httpx.ReadTimeout, httpx.TimeoutException) as exc:
                self._record_llm_attempt(
                    profile=profile,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    request_timeout=request_timeout,
                    attempt_no=attempt_no,
                    duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                    error_class=exc.__class__.__name__,
                    error_message=str(exc),
                )
                if retry_on_timeout and attempt < self.retry_attempts - 1:
                    delay = self._retry_delay(attempt)
                    logger.warning(
                        "Request timed out (%s). Waiting %.1f s before retry %d/%d.",
                        exc,
                        delay,
                        attempt + 2,
                        self.retry_attempts,
                    )
                    time.sleep(delay)
                    continue
                raise
            except Exception as exc:  # noqa: BLE001
                response = getattr(exc, "response", None)
                self._record_llm_attempt(
                    profile=profile,
                    messages=messages,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    response_format=response_format,
                    request_timeout=request_timeout,
                    attempt_no=attempt_no,
                    http_status=int(getattr(response, "status_code", 0) or 0),
                    provider_request_id=(
                        self._provider_request_id(response)
                        if isinstance(response, httpx.Response)
                        else ""
                    ),
                    duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                    error_class=exc.__class__.__name__,
                    error_message=str(exc),
                )
                raise

        # Should never reach here, but make the type-checker happy.
        raise RuntimeError("OpenAICompatibleAdapter.chat: unexpected exit from retry loop")

    def _request_profiles(self) -> list[dict[str, str]]:
        candidates = [
            {
                "id": "",
                "name": "",
                "api_key": self.api_key,
                "base_url": self.base_url,
                "model": self.model,
            }
        ]
        candidates.extend(self.fallback_profiles)
        profiles: list[dict[str, str]] = []
        seen: set[tuple[str, str, str]] = set()
        for item in candidates:
            profile = {
                "id": str(item.get("id", "")).strip(),
                "name": str(item.get("name", "")).strip(),
                "api_key": str(item.get("api_key", "")).strip(),
                "base_url": str(item.get("base_url", "")).strip().rstrip("/"),
                "model": str(item.get("model", "")).strip(),
            }
            if not profile["api_key"] or not profile["base_url"] or not profile["model"]:
                continue
            key = (profile["api_key"], profile["base_url"], profile["model"])
            if key in seen:
                continue
            seen.add(key)
            profiles.append(profile)
        return profiles

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
        error_class: str = "",
        error_message: str = "",
    ) -> None:
        base_url = str(profile.get("base_url") or "")
        self.llm_attempt_events.append(
            {
                "model": str(profile.get("model") or ""),
                "base_url_host": urlparse(base_url).netloc or base_url,
                "temperature": temperature,
                "max_tokens": max_tokens,
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
                "retry_after": retry_after,
                "error_class": error_class,
                "error_message": error_message,
            }
        )

    @staticmethod
    def _provider_request_id(response: httpx.Response) -> str:
        return str(
            response.headers.get("x-request-id")
            or response.headers.get("x-minimax-request-id")
            or response.headers.get("request-id")
            or ""
        )

    @staticmethod
    def _timeout_seconds_value(timeout: httpx.Timeout) -> float:
        value = getattr(timeout, "read", None) or getattr(timeout, "connect", None) or 0.0
        try:
            return float(value)
        except (TypeError, ValueError):
            return 0.0

    @classmethod
    def _is_fallback_retryable(cls, exc: Exception) -> bool:
        if isinstance(exc, (httpx.ReadTimeout, httpx.TimeoutException, httpx.ConnectError, httpx.RemoteProtocolError, httpx.NetworkError)):
            return True
        if isinstance(exc, httpx.HTTPStatusError):
            status_code = exc.response.status_code if exc.response is not None else 0
            return status_code in _RETRYABLE_HTTP_STATUS_CODES
        current: BaseException | None = exc
        while current is not None:
            message = str(current).lower()
            if any(
                token in message
                for token in (
                    "http 529",
                    "status code 529",
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
                    "server disconnected",
                    "network error",
                    "timed out",
                    "timeout",
                )
            ):
                return True
            current = current.__cause__ or current.__context__
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

    def embed(
        self,
        inputs: list[str],
        *,
        model: str,
        dimensions: int | None = None,
    ) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": model,
            "input": inputs,
        }
        if dimensions is not None:
            payload["dimensions"] = dimensions
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/embeddings"
        response = self.client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        rows = data.get("data") or []
        return [list(item.get("embedding") or []) for item in rows]

    def close(self) -> None:
        """Close the underlying httpx client."""
        self.client.close()

    def __enter__(self) -> "OpenAICompatibleAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LLMClient(OpenAICompatibleAdapter):
    pass
