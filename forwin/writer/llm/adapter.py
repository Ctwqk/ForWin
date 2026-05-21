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

from .embeddings import EmbeddingsMixin
from .errors import ErrorsMixin
from .profile_options import ProfileOptionsMixin
from .routing import RoutingMixin
from .telemetry import TelemetryMixin
from .transport import TransportMixin


class OpenAICompatibleAdapter(
    TransportMixin,
    TelemetryMixin,
    RoutingMixin,
    ProfileOptionsMixin,
    ErrorsMixin,
    EmbeddingsMixin,
):
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
        self._client_lock = threading.Lock()
        self.client = self._build_http_client()

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.85,
        max_tokens: int = 16384,
        response_format: dict | None = None,
        timeout_seconds: float | None = None,
        retry_on_timeout: bool = True,
        task_family: str = "",
        stage_key: str = "",
        output_schema: dict | None = None,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
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

        attempt_group_id = uuid.uuid4().hex
        llm_task_route = self._llm_task_route(
            task_family=task_family,
            stage_key=stage_key,
            response_format=response_format,
            output_schema=output_schema,
        )
        route_result = self._route_profiles_with_metadata(
            self._request_profiles(),
            task_family=task_family,
            stage_key=stage_key,
            response_format=response_format,
            output_schema=output_schema,
            preferred_provider_kind=preferred_provider_kind,
            preferred_model=preferred_model,
        )
        profiles = route_result["profiles"]
        candidate_chain = route_result["candidate_chain"]
        skipped_profiles = route_result["skipped_profiles"]
        if not profiles:
            self._record_llm_attempt(
                attempt_group_id=attempt_group_id,
                profile={},
                messages=messages,
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                request_timeout=request_timeout,
                attempt_no=0,
                error_class="NoUsableLLMProfile",
                error_message="no usable LLM profile after route policy filtering",
                error_category="no_usable_profile",
                retryable=False,
                fallback_eligible=False,
                final_failure=True,
                requested_temperature=temperature,
                requested_max_tokens=max_tokens,
                task_family=task_family,
                stage_key=stage_key,
                llm_task_route=llm_task_route,
                request_payload={
                    "messages": messages,
                    "temperature": temperature,
                    "max_tokens": max_tokens,
                    "response_format": response_format or {},
                },
                candidate_chain=candidate_chain,
                skipped_profiles=skipped_profiles,
                preferred_provider_kind=preferred_provider_kind,
                preferred_model=preferred_model,
            )
            raise RuntimeError("OpenAICompatibleAdapter.chat: no usable LLM profile")
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
                    attempt_group_id=attempt_group_id,
                    task_family=task_family,
                    stage_key=stage_key,
                    llm_task_route=llm_task_route,
                    explicit_timeout=timeout_seconds is not None,
                    fallback_eligible_on_profile_failure=profile_index < len(profiles) - 1,
                    candidate_chain=candidate_chain,
                    skipped_profiles=skipped_profiles,
                    preferred_provider_kind=preferred_provider_kind,
                    preferred_model=preferred_model,
                )
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if isinstance(exc, (httpx.ReadTimeout, httpx.TimeoutException)) and not retry_on_timeout:
                    raise
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
                    "attempt_group_id": attempt_group_id,
                    "task_family": str(task_family or ""),
                    "stage_key": str(stage_key or ""),
                    "llm_task_route": llm_task_route,
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
        attempt_group_id: str,
        task_family: str,
        stage_key: str,
        llm_task_route: str,
        explicit_timeout: bool,
        fallback_eligible_on_profile_failure: bool,
        candidate_chain: list[dict[str, str]],
        skipped_profiles: list[dict[str, str]],
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> str:
        requested_temperature = float(temperature)
        effective_temperature = self._effective_temperature_for_profile(
            profile,
            requested_temperature,
        )
        send_temperature = self._should_send_temperature(profile)
        requested_max_tokens = int(max_tokens)
        effective_max_tokens = self._effective_max_tokens_for_profile(
            profile,
            requested_max_tokens,
        )
        effective_request_timeout = self._effective_timeout_for_profile(
            profile,
            request_timeout,
            llm_task_route=llm_task_route,
            explicit_timeout=explicit_timeout,
        )
        effective_response_format = self._effective_response_format_for_profile(
            profile,
            response_format,
        )
        payload = {
            "model": profile["model"],
            "messages": messages,
        }
        payload["max_tokens"] = effective_max_tokens
        if send_temperature:
            payload["temperature"] = effective_temperature
        thinking = self._thinking_payload_for_profile(profile)
        if thinking is not None:
            payload["thinking"] = thinking
        if effective_response_format:
            payload["response_format"] = effective_response_format
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
                    effective_max_tokens,
                )
                response = self._post_with_wall_timeout(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=effective_request_timeout,
                )

                if response.status_code in _RETRYABLE_HTTP_STATUS_CODES:
                    retry_delay = self._retry_delay(attempt, response)
                    self._record_llm_attempt(
                        attempt_group_id=attempt_group_id,
                        profile=profile,
                        messages=messages,
                        temperature=effective_temperature,
                        max_tokens=effective_max_tokens,
                        response_format=effective_response_format,
                        request_timeout=effective_request_timeout,
                        attempt_no=attempt_no,
                        http_status=response.status_code,
                        provider_request_id=self._provider_request_id(response),
                        duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                        retry_after=retry_delay,
                        sleep_ms=int(retry_delay * 1000) if attempt < self.retry_attempts - 1 else 0,
                        error_class="HTTPStatusError" if attempt >= self.retry_attempts - 1 else "",
                        error_message=(
                            self._http_error_message_from_response(response, profile)
                            if attempt >= self.retry_attempts - 1
                            else ""
                        ),
                        error_category=self._error_category_for_status(response.status_code),
                        retryable=True,
                        fallback_eligible=(
                            fallback_eligible_on_profile_failure
                            if attempt >= self.retry_attempts - 1
                            else False
                        ),
                        final_failure=attempt >= self.retry_attempts - 1,
                        requested_temperature=requested_temperature,
                        requested_max_tokens=requested_max_tokens,
                        task_family=task_family,
                        stage_key=stage_key,
                        llm_task_route=llm_task_route,
                        request_payload=payload,
                        response_text=self._safe_response_text(response),
                        candidate_chain=candidate_chain,
                        skipped_profiles=skipped_profiles,
                        preferred_provider_kind=preferred_provider_kind,
                        preferred_model=preferred_model,
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

                response_text = self._safe_response_text(response)
                try:
                    data = response.json()
                    content: str = data["choices"][0]["message"]["content"]
                except Exception as exc:  # noqa: BLE001
                    self._record_llm_attempt(
                        attempt_group_id=attempt_group_id,
                        profile=profile,
                        messages=messages,
                        temperature=effective_temperature,
                        max_tokens=effective_max_tokens,
                        response_format=effective_response_format,
                        request_timeout=effective_request_timeout,
                        attempt_no=attempt_no,
                        http_status=response.status_code,
                        provider_request_id=self._provider_request_id(response),
                        duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                        error_class=exc.__class__.__name__,
                        error_message=str(exc),
                        error_category="parse_error",
                        retryable=False,
                        fallback_eligible=False,
                        final_failure=True,
                        requested_temperature=requested_temperature,
                        requested_max_tokens=requested_max_tokens,
                        task_family=task_family,
                        stage_key=stage_key,
                        llm_task_route=llm_task_route,
                        request_payload=payload,
                        response_text=response_text,
                        candidate_chain=candidate_chain,
                        skipped_profiles=skipped_profiles,
                        preferred_provider_kind=preferred_provider_kind,
                        preferred_model=preferred_model,
                    )
                    setattr(exc, _ATTEMPT_RECORDED_ATTR, True)
                    raise
                self._record_llm_attempt(
                    attempt_group_id=attempt_group_id,
                    profile=profile,
                    messages=messages,
                    temperature=effective_temperature,
                    max_tokens=effective_max_tokens,
                        response_format=effective_response_format,
                    request_timeout=effective_request_timeout,
                    attempt_no=attempt_no,
                    http_status=response.status_code,
                    provider_request_id=self._provider_request_id(response),
                    duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                    output_chars=len(content),
                    requested_temperature=requested_temperature,
                    requested_max_tokens=requested_max_tokens,
                    task_family=task_family,
                    stage_key=stage_key,
                    llm_task_route=llm_task_route,
                    request_payload=payload,
                    response_text=response_text,
                    candidate_chain=candidate_chain,
                    skipped_profiles=skipped_profiles,
                    preferred_provider_kind=preferred_provider_kind,
                    preferred_model=preferred_model,
                )
                logger.debug(
                    "LLMClient.chat success: %d chars returned in %.2fs",
                    len(content),
                    time.perf_counter() - attempt_started_at,
                )
                return content

            except (httpx.ReadTimeout, httpx.TimeoutException) as exc:
                final_failure = not (retry_on_timeout and attempt < self.retry_attempts - 1)
                self._record_llm_attempt(
                    attempt_group_id=attempt_group_id,
                    profile=profile,
                    messages=messages,
                    temperature=effective_temperature,
                    max_tokens=effective_max_tokens,
                        response_format=effective_response_format,
                    request_timeout=effective_request_timeout,
                    attempt_no=attempt_no,
                    duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                    error_class=exc.__class__.__name__,
                    error_message=str(exc),
                    error_category="timeout",
                    timeout_kind=self._timeout_kind(exc),
                    retryable=bool(retry_on_timeout),
                    fallback_eligible=fallback_eligible_on_profile_failure if final_failure else False,
                    final_failure=final_failure,
                    requested_temperature=requested_temperature,
                    requested_max_tokens=requested_max_tokens,
                    task_family=task_family,
                    stage_key=stage_key,
                    llm_task_route=llm_task_route,
                    request_payload=payload,
                    candidate_chain=candidate_chain,
                    skipped_profiles=skipped_profiles,
                    preferred_provider_kind=preferred_provider_kind,
                    preferred_model=preferred_model,
                )
                if retry_on_timeout and attempt < self.retry_attempts - 1:
                    delay = self._retry_delay(attempt)
                    if self.llm_attempt_events:
                        self.llm_attempt_events[-1]["sleep_ms"] = int(delay * 1000)
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
                if getattr(exc, _ATTEMPT_RECORDED_ATTR, False):
                    raise
                response = getattr(exc, "response", None)
                status_code = int(getattr(response, "status_code", 0) or 0)
                if status_code in _RETRYABLE_HTTP_STATUS_CODES:
                    raise
                self._record_llm_attempt(
                    attempt_group_id=attempt_group_id,
                    profile=profile,
                    messages=messages,
                    temperature=effective_temperature,
                    max_tokens=effective_max_tokens,
                        response_format=effective_response_format,
                    request_timeout=effective_request_timeout,
                    attempt_no=attempt_no,
                    http_status=status_code,
                    provider_request_id=(
                        self._provider_request_id(response)
                        if isinstance(response, httpx.Response)
                        else ""
                    ),
                    duration_ms=max(0, int((time.perf_counter() - attempt_started_at) * 1000)),
                    error_class=exc.__class__.__name__,
                    error_message=self._http_error_message(exc, profile),
                    error_category=(
                        self._error_category_for_status(status_code)
                        if status_code
                        else "network"
                    ),
                    retryable=self._is_fallback_retryable(exc),
                    fallback_eligible=(
                        fallback_eligible_on_profile_failure
                        and self._is_fallback_retryable(exc)
                    ),
                    final_failure=True,
                    requested_temperature=requested_temperature,
                    requested_max_tokens=requested_max_tokens,
                    task_family=task_family,
                    stage_key=stage_key,
                    llm_task_route=llm_task_route,
                    request_payload=payload,
                    response_text=(
                        self._safe_response_text(response)
                        if isinstance(response, httpx.Response)
                        else ""
                    ),
                    candidate_chain=candidate_chain,
                    skipped_profiles=skipped_profiles,
                    preferred_provider_kind=preferred_provider_kind,
                    preferred_model=preferred_model,
                )
                raise

        # Should never reach here, but make the type-checker happy.
        raise RuntimeError("OpenAICompatibleAdapter.chat: unexpected exit from retry loop")


class LLMClient(OpenAICompatibleAdapter):
    pass


__all__ = ["OpenAICompatibleAdapter", "LLMClient"]
