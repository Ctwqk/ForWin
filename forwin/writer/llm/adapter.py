from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import logging
import threading
import time
import uuid

import httpx

from forwin.config import ModelProfileConfig
from forwin.model_adapter import ModelCapabilities
from .embeddings import EmbeddingsMixin
from .errors import (
    EmptyLLMResponse,
    ErrorsMixin,
    _ATTEMPT_RECORDED_ATTR,
    _PARSE_ERROR_ATTR,
    _RETRYABLE_HTTP_STATUS_CODES,
)
from .profile_options import ProfileOptionsMixin
from .routing import RoutingMixin
from .telemetry import TelemetryMixin
from .transport import TransportMixin

logger = logging.getLogger(__name__)


def _provider_token_usage(
    data: object,
) -> tuple[int | None, int | None, int | None, str]:
    usage = data.get("usage") if isinstance(data, dict) else None
    if not isinstance(usage, dict):
        return None, None, None, "missing"

    def token_count(*keys: str) -> int | None:
        for key in keys:
            value = usage.get(key)
            if isinstance(value, bool) or value is None:
                continue
            try:
                return max(0, int(value))
            except (TypeError, ValueError):
                continue
        return None

    prompt_tokens = token_count("prompt_tokens", "input_tokens")
    completion_tokens = token_count("completion_tokens", "output_tokens")
    total_tokens = token_count("total_tokens")
    if (
        total_tokens is None
        and prompt_tokens is not None
        and completion_tokens is not None
    ):
        total_tokens = prompt_tokens + completion_tokens
    source = (
        "provider"
        if any(
            value is not None
            for value in (prompt_tokens, completion_tokens, total_tokens)
        )
        else "missing"
    )
    return prompt_tokens, completion_tokens, total_tokens, source


@dataclass(frozen=True)
class LLMChatResult:
    content: str
    profile_id: str
    model: str


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
        profiles: Mapping[str, ModelProfileConfig],
        chains: Mapping[str, tuple[str, ...]],
        pinned_profile_id: str = "",
        *,
        timeout_seconds: float = 90.0,
        retry_attempts: int = 2,
        retry_initial_delay_seconds: float = 2.0,
        retry_max_delay_seconds: float = 15.0,
        embedding_api_key: str = "",
        embedding_base_url: str = "",
        embedding_model: str = "",
    ) -> None:
        self.provider = "openai_compatible"
        self.profiles = dict(profiles)
        self.chains = {route: tuple(chain) for route, chain in chains.items()}
        self.pinned_profile_id = str(pinned_profile_id or "").strip()
        self.profile_id = self.pinned_profile_id
        pinned = self.profiles.get(self.pinned_profile_id)
        self.profile_name = pinned.name if pinned else ""
        self.model = pinned.model if pinned else ""
        self.base_url = pinned.base_url if pinned else ""
        self.embedding_api_key = embedding_api_key
        self.embedding_base_url = embedding_base_url.rstrip("/")
        self.embedding_model = embedding_model
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
    ) -> str:
        return self.chat_with_meta(
            messages, temperature=temperature, max_tokens=max_tokens,
            response_format=response_format, timeout_seconds=timeout_seconds,
            retry_on_timeout=retry_on_timeout, task_family=task_family,
            stage_key=stage_key, output_schema=output_schema,
        ).content

    def chat_with_meta(
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
    ) -> LLMChatResult:
        """Return the completion and the winning profile from the configured chain.

        Transient failures retry within a profile before falling back. Authentication,
        balance, missing-model and empty-response failures advance immediately.
        Other HTTP and response parsing failures propagate to the caller.
        """
        request_timeout = httpx.Timeout(
            max(
                5.0,
                float(
                    timeout_seconds
                    if timeout_seconds is not None
                    else self.timeout_seconds
                ),
            ),
            connect=min(
                10.0,
                max(
                    5.0,
                    float(
                        timeout_seconds
                        if timeout_seconds is not None
                        else self.timeout_seconds
                    ),
                ),
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
            task_family=task_family,
            stage_key=stage_key,
            response_format=response_format,
            output_schema=output_schema,
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
                error_message=f"no configured LLM profile for route {llm_task_route}",
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
            )
            raise RuntimeError(f"no configured LLM profile for route {llm_task_route}")
        last_exc: Exception | None = None
        for profile_index, profile in enumerate(profiles):
            try:
                content = self._chat_with_profile(
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
                    fallback_eligible_on_profile_failure=profile_index
                    < len(profiles) - 1,
                    candidate_chain=candidate_chain,
                    skipped_profiles=skipped_profiles,
                )
                return LLMChatResult(content, profile["id"], profile["model"])
            except Exception as exc:  # noqa: BLE001
                last_exc = exc
                if (
                    isinstance(exc, (httpx.ReadTimeout, httpx.TimeoutException))
                    and not retry_on_timeout
                ):
                    raise
                if profile_index >= len(
                    profiles
                ) - 1 or not self._is_fallback_retryable(exc):
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
        raise RuntimeError(f"no configured LLM profile for route {llm_task_route}")

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
            explicit_timeout=explicit_timeout,
        )
        effective_response_format = self._effective_response_format_for_profile(
            profile,
            response_format,
        )
        effective_messages = self._effective_messages_for_profile(
            profile, messages,
        )
        payload = {
            "model": profile["model"],
            "messages": effective_messages,
        }
        max_tokens_key = self._max_tokens_payload_key_for_profile(
            profile,
        )
        payload[max_tokens_key] = effective_max_tokens
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
                        messages=effective_messages,
                        temperature=effective_temperature,
                        max_tokens=effective_max_tokens,
                        response_format=effective_response_format,
                        request_timeout=effective_request_timeout,
                        attempt_no=attempt_no,
                        http_status=response.status_code,
                        provider_request_id=self._provider_request_id(response),
                        duration_ms=max(
                            0, int((time.perf_counter() - attempt_started_at) * 1000)
                        ),
                        retry_after=retry_delay,
                        sleep_ms=int(retry_delay * 1000)
                        if attempt < self.retry_attempts - 1
                        else 0,
                        error_class="HTTPStatusError"
                        if attempt >= self.retry_attempts - 1
                        else "",
                        error_message=(
                            self._http_error_message_from_response(response, profile)
                            if attempt >= self.retry_attempts - 1
                            else ""
                        ),
                        error_category=self._error_category_for_status(
                            response.status_code
                        ),
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
                prompt_tokens = None
                completion_tokens = None
                total_tokens = None
                usage_source = "missing"
                try:
                    data = response.json()
                    (
                        prompt_tokens,
                        completion_tokens,
                        total_tokens,
                        usage_source,
                    ) = _provider_token_usage(data)
                    choices = data.get("choices") or []
                    message = choices[0].get("message") or {} if choices else {}
                    content = message.get("content")
                    if content is None or (isinstance(content, str) and not content.strip()):
                        raise EmptyLLMResponse("empty LLM completion")
                    if not isinstance(content, str):
                        raise ValueError("LLM completion content must be a string")
                except Exception as exc:  # noqa: BLE001
                    self._record_llm_attempt(
                        attempt_group_id=attempt_group_id,
                        profile=profile,
                        messages=effective_messages,
                        temperature=effective_temperature,
                        max_tokens=effective_max_tokens,
                        response_format=effective_response_format,
                        request_timeout=effective_request_timeout,
                        attempt_no=attempt_no,
                        http_status=response.status_code,
                        provider_request_id=self._provider_request_id(response),
                        duration_ms=max(
                            0, int((time.perf_counter() - attempt_started_at) * 1000)
                        ),
                        prompt_tokens=prompt_tokens,
                        completion_tokens=completion_tokens,
                        total_tokens=total_tokens,
                        usage_source=usage_source,
                        output_chars=len(response_text),
                        error_class=exc.__class__.__name__,
                        error_message=str(exc),
                        error_category="empty_response" if isinstance(exc, EmptyLLMResponse) else "parse_error",
                        retryable=isinstance(exc, EmptyLLMResponse),
                        fallback_eligible=(fallback_eligible_on_profile_failure and isinstance(exc, EmptyLLMResponse)),
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
                    )
                    setattr(exc, _ATTEMPT_RECORDED_ATTR, True)
                    setattr(exc, _PARSE_ERROR_ATTR, not isinstance(exc, EmptyLLMResponse))
                    raise
                self._record_llm_attempt(
                    attempt_group_id=attempt_group_id,
                    profile=profile,
                    messages=effective_messages,
                    temperature=effective_temperature,
                    max_tokens=effective_max_tokens,
                    response_format=effective_response_format,
                    request_timeout=effective_request_timeout,
                    attempt_no=attempt_no,
                    http_status=response.status_code,
                    provider_request_id=self._provider_request_id(response),
                    duration_ms=max(
                        0, int((time.perf_counter() - attempt_started_at) * 1000)
                    ),
                    output_chars=len(content),
                    prompt_tokens=prompt_tokens,
                    completion_tokens=completion_tokens,
                    total_tokens=total_tokens,
                    usage_source=usage_source,
                    requested_temperature=requested_temperature,
                    requested_max_tokens=requested_max_tokens,
                    task_family=task_family,
                    stage_key=stage_key,
                    llm_task_route=llm_task_route,
                    request_payload=payload,
                    response_text=response_text,
                    candidate_chain=candidate_chain,
                    skipped_profiles=skipped_profiles,
                )
                logger.debug(
                    "LLMClient.chat success: %d chars returned in %.2fs",
                    len(content),
                    time.perf_counter() - attempt_started_at,
                )
                return content

            except (httpx.ReadTimeout, httpx.TimeoutException) as exc:
                final_failure = not (
                    retry_on_timeout and attempt < self.retry_attempts - 1
                )
                self._record_llm_attempt(
                    attempt_group_id=attempt_group_id,
                    profile=profile,
                    messages=effective_messages,
                    temperature=effective_temperature,
                    max_tokens=effective_max_tokens,
                    response_format=effective_response_format,
                    request_timeout=effective_request_timeout,
                    attempt_no=attempt_no,
                    duration_ms=max(
                        0, int((time.perf_counter() - attempt_started_at) * 1000)
                    ),
                    error_class=exc.__class__.__name__,
                    error_message=str(exc),
                    error_category="timeout",
                    timeout_kind=self._timeout_kind(exc),
                    retryable=bool(retry_on_timeout),
                    fallback_eligible=fallback_eligible_on_profile_failure
                    if final_failure
                    else False,
                    final_failure=final_failure,
                    requested_temperature=requested_temperature,
                    requested_max_tokens=requested_max_tokens,
                    task_family=task_family,
                    stage_key=stage_key,
                    llm_task_route=llm_task_route,
                    request_payload=payload,
                    candidate_chain=candidate_chain,
                    skipped_profiles=skipped_profiles,
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
                    messages=effective_messages,
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
                    duration_ms=max(
                        0, int((time.perf_counter() - attempt_started_at) * 1000)
                    ),
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
                )
                raise

        # Should never reach here, but make the type-checker happy.
        raise RuntimeError(
            "OpenAICompatibleAdapter.chat: unexpected exit from retry loop"
        )


class LLMClient(OpenAICompatibleAdapter):
    pass


__all__ = ["OpenAICompatibleAdapter", "LLMClient", "LLMChatResult"]
