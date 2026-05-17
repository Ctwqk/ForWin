from __future__ import annotations

import logging
import queue
import threading
import time
import uuid
import hashlib
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse
import json

import httpx

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.model_adapter import ModelCapabilities

logger = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_LLM_ROUTE_POLICY_VERSION = "v3.8-stage-aware-hard-replacement"
_ATTEMPT_RECORDED_ATTR = "_forwin_llm_attempt_recorded"


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
        self._client_lock = threading.Lock()
        self.client = self._build_http_client()

    def _build_http_client(self) -> httpx.Client:
        return httpx.Client(
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
        if self._is_minimax_profile(profile):
            payload["max_completion_tokens"] = effective_max_tokens
        else:
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

    def _post_with_wall_timeout(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        with self._client_lock:
            client = self.client

        def _worker() -> None:
            try:
                result_queue.put(
                    (
                        "response",
                        client.post(url, json=json, headers=headers, timeout=timeout),
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                result_queue.put(("exception", exc))

        wall_timeout = max(0.1, self._timeout_seconds_value(timeout))
        thread = threading.Thread(
            target=_worker,
            name="forwin-llm-http-post",
            daemon=True,
        )
        thread.start()
        thread.join(wall_timeout)
        if thread.is_alive():
            self._replace_timed_out_client(client)
            raise httpx.ReadTimeout(
                f"LLM HTTP request exceeded wall timeout ({wall_timeout:.1f}s)"
            )
        kind, value = result_queue.get_nowait()
        if kind == "exception":
            raise value  # type: ignore[misc]
        return value  # type: ignore[return-value]

    def _replace_timed_out_client(self, timed_out_client: object) -> None:
        close = getattr(timed_out_client, "close", None)
        with self._client_lock:
            if self.client is timed_out_client:
                try:
                    if callable(close):
                        close()
                finally:
                    self.client = self._build_http_client()
                return
        if callable(close):
            close()

    def _request_profiles(self) -> list[dict[str, str]]:
        candidates = [
            {
                "id": str(self.profile_id or ""),
                "name": str(self.profile_name or ""),
                "api_key": self.api_key,
                "base_url": self.base_url,
                "model": self.model,
            }
        ]
        candidates.extend(self.fallback_profiles)
        profiles: list[dict[str, str]] = []
        seen: dict[tuple[str, str, str], int] = {}
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
                existing = profiles[seen[key]]
                if not existing.get("id") and profile.get("id"):
                    existing["id"] = profile["id"]
                if not existing.get("name") and profile.get("name"):
                    existing["name"] = profile["name"]
                continue
            seen[key] = len(profiles)
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

    @classmethod
    def _route_profiles(
        cls,
        profiles: list[dict[str, str]],
        *,
        task_family: str = "",
        stage_key: str = "",
        response_format: dict | None = None,
        output_schema: dict | None = None,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> list[dict[str, str]]:
        return cls._route_profiles_with_metadata(
            profiles,
            task_family=task_family,
            stage_key=stage_key,
            response_format=response_format,
            output_schema=output_schema,
            preferred_provider_kind=preferred_provider_kind,
            preferred_model=preferred_model,
        )["profiles"]

    @classmethod
    def _route_profiles_with_metadata(
        cls,
        profiles: list[dict[str, str]],
        *,
        task_family: str = "",
        stage_key: str = "",
        response_format: dict | None = None,
        output_schema: dict | None = None,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> dict[str, list[dict[str, str]]]:
        route = cls._llm_task_route(
            task_family=task_family,
            stage_key=stage_key,
            response_format=response_format,
            output_schema=output_schema,
        )
        candidate_chain = [cls._profile_public_info(profile) for profile in profiles]
        skipped_profiles: list[dict[str, str]] = []
        kinds = {cls._profile_kind(profile) for profile in profiles}
        has_kimi = "kimi" in kinds
        primary_kind = cls._profile_kind(profiles[0]) if profiles else ""
        has_deepseek_or_kimi = has_kimi or "deepseek" in kinds
        indexed = list(enumerate(profiles))
        replacement_filtered: list[tuple[int, dict[str, str]]] = []
        for index, profile in indexed:
            kind = cls._profile_kind(profile)
            reason = ""
            if (
                kind == "deepseek"
                and has_kimi
                and primary_kind != "deepseek"
                and route not in {"prose_generation", "repair_generation"}
            ):
                reason = "replaced_by_kimi"
            elif kind == "kimi" and primary_kind == "deepseek":
                reason = "primary_deepseek_no_kimi_fallback"
            elif kind == "gemini" and has_deepseek_or_kimi:
                reason = "replaced_by_deepseek" if "deepseek" in kinds else "replaced_by_kimi"
            if reason:
                skipped_profiles.append(
                    {
                        **cls._profile_public_info(profile),
                        "reason": reason,
                        "llm_task_route": route,
                    }
                )
                continue
            replacement_filtered.append((index, profile))
        suitable = [
            (index, profile)
            for index, profile in replacement_filtered
            if cls._profile_suitable_for_route(profile, route)
        ]
        for index, profile in replacement_filtered:
            if (index, profile) not in suitable:
                skipped_profiles.append(
                    {
                        **cls._profile_public_info(profile),
                        "reason": "route_not_allowed",
                        "llm_task_route": route,
                    }
                )
        routed = suitable
        preferred_kind = str(preferred_provider_kind or "").strip().lower()
        preferred_model_text = str(preferred_model or "").strip().lower()
        if preferred_kind or preferred_model_text:
            preferred = [
                item
                for item in routed
                if cls._profile_preference_rank(
                    item[1],
                    preferred_provider_kind=preferred_kind,
                    preferred_model=preferred_model_text,
                )
                < 99
            ]
            if preferred:
                routed.sort(
                    key=lambda item: (
                        cls._profile_preference_rank(
                            item[1],
                            preferred_provider_kind=preferred_kind,
                            preferred_model=preferred_model_text,
                        ),
                        cls._profile_route_priority(item[1], route),
                        item[0],
                    )
                )
                return {
                    "profiles": [profile for _index, profile in routed],
                    "candidate_chain": candidate_chain,
                    "skipped_profiles": skipped_profiles,
                }
        primary = (
            next((item for item in routed if item[0] == 0), None)
            if primary_kind == "deepseek"
            else None
        )
        if primary is not None:
            rest = [item for item in routed if item[0] != 0]
            rest.sort(
                key=lambda item: (
                    cls._profile_route_priority(item[1], route),
                    item[0],
                )
            )
            routed = [primary, *rest]
        else:
            routed.sort(
                key=lambda item: (
                    cls._profile_route_priority(item[1], route),
                    item[0],
                )
            )
        return {
            "profiles": [profile for _index, profile in routed],
            "candidate_chain": candidate_chain,
            "skipped_profiles": skipped_profiles,
        }

    @classmethod
    def _profile_preference_rank(
        cls,
        profile: dict[str, str],
        *,
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> int:
        model = str(profile.get("model") or "").strip().lower()
        kind = cls._profile_kind(profile)
        if preferred_model:
            if model == preferred_model:
                return 0
            if preferred_model in model or model in preferred_model:
                return 1
        if preferred_provider_kind and kind == preferred_provider_kind:
            return 2
        return 99

    @classmethod
    def _llm_task_route(
        cls,
        *,
        task_family: str = "",
        stage_key: str = "",
        response_format: dict | None = None,
        output_schema: dict | None = None,
    ) -> str:
        family = str(task_family or "").strip().lower()
        stage = str(stage_key or "").strip().lower()
        wants_json = bool(response_format or output_schema)
        if any(token in stage for token in ("state_event", "thread_time", "lore_timeline")):
            return "canon_extraction"
        if stage in {"writer_preview", "writer_preview_fallback", "chapter_preview_fallback"}:
            return "writer_preview"
        if stage in {"comment_analysis", "npc_intents", "world_pressure"} or family in {
            "feedback",
            "phase4",
            "reader_feedback",
        }:
            return "feedback_analysis"
        if stage in {
            "chapter_review",
            "chapter_review_json_repair",
            "repair_verification",
        } or family in {"reviewer", "review"}:
            return "review_json"
        if any(token in stage for token in ("chapter_rewrite", "repair")) or family == "repair":
            return "repair_generation"
        if stage in {
            "chapter_draft",
            "scene_generation",
            "scene_stitch",
        } or (family == "writer" and not wants_json):
            return "prose_generation"
        if stage == "provisional_preview":
            return "prose_generation"
        if stage == "chapter_preview":
            return "writer_preview"
        if stage in {"scene_breakdown", "genesis_brief", "brief", "arc_plan"} or stage.startswith("launch_arc_"):
            return "planning_json_low_risk" if wants_json else "planning_prose"
        if stage in {
            "world",
            "map",
            "story_engine",
            "book_blueprint",
            "bootstrap",
            "band_plan",
            "chapter_plan",
        } or family in {
            "genesis",
            "planning",
            "arc_planning",
            "world_model",
        }:
            return "planning_json_general" if wants_json else "planning_prose"
        if wants_json:
            return "planning_json_general"
        return "general"

    @classmethod
    def _profile_suitable_for_route(cls, profile: dict[str, str], route: str) -> bool:
        kind = cls._profile_kind(profile)
        if kind == "gemini":
            return False
        if kind == "minimax" and route in {
            "prose_generation",
            "repair_generation",
            "canon_extraction",
            "planning_json_general",
            "general",
        }:
            return False
        return True

    @classmethod
    def _profile_route_priority(cls, profile: dict[str, str], route: str) -> int:
        kind = cls._profile_kind(profile)
        priorities = {
            "prose_generation": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "other": 3,
                "minimax": 99,
            },
            "repair_generation": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "other": 3,
                "minimax": 99,
            },
            "canon_extraction": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "other": 4,
                "minimax": 99,
            },
            "writer_preview": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "minimax": 4,
                "other": 5,
            },
            "planning_json_low_risk": {
                "spark": 0,
                "kimi": 1,
                "minimax": 2,
                "openai": 3,
                "deepseek": 4,
                "other": 5,
            },
            "planning_json_general": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "other": 4,
                "minimax": 99,
            },
            "planning_prose": {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "other": 4,
                "minimax": 99,
            },
            "review_json": {
                "spark": 0,
                "kimi": 1,
                "minimax": 2,
                "openai": 3,
                "deepseek": 4,
                "other": 5,
            },
            "feedback_analysis": {
                "spark": 0,
                "kimi": 1,
                "minimax": 2,
                "openai": 3,
                "deepseek": 4,
                "other": 5,
            },
        }
        route_priorities = priorities.get(
            route,
            {
                "spark": 0,
                "kimi": 1,
                "openai": 2,
                "deepseek": 3,
                "other": 4,
                "minimax": 99,
            },
        )
        return int(route_priorities.get(kind, route_priorities.get("other", 50)))

    @staticmethod
    def _profile_kind(profile: dict[str, str]) -> str:
        text = " ".join(
            str(profile.get(key) or "").strip().lower()
            for key in ("id", "name", "base_url", "model")
        )
        if "codex-spark" in text or "gpt-5.3-codex-spark" in text:
            return "spark"
        if "minimax" in text or "minimaxi" in text:
            return "minimax"
        if "kimi" in text or "moonshot" in text:
            return "kimi"
        if "deepseek" in text:
            return "deepseek"
        if "gemini" in text or "generativelanguage" in text:
            return "gemini"
        if "openai" in text or "gpt-" in text:
            return "openai"
        return "other"

    @classmethod
    def _profile_public_info(cls, profile: dict[str, str]) -> dict[str, str]:
        base_url = str(profile.get("base_url") or "")
        return {
            "profile_id": str(profile.get("id") or ""),
            "profile_name": str(profile.get("name") or ""),
            "model": str(profile.get("model") or ""),
            "base_url_host": urlparse(base_url).netloc or base_url,
            "provider_kind": cls._profile_kind(profile),
        }

    @classmethod
    def _effective_temperature_for_profile(
        cls,
        profile: dict[str, str],
        requested_temperature: float,
    ) -> float:
        if cls._is_kimi_k25_profile(profile):
            return 0.6
        return float(requested_temperature)

    @classmethod
    def _should_send_temperature(cls, profile: dict[str, str]) -> bool:
        return not cls._is_kimi_k25_profile(profile)

    @classmethod
    def _thinking_payload_for_profile(cls, profile: dict[str, str]) -> dict[str, str] | None:
        if cls._is_kimi_k25_profile(profile):
            return {"type": "disabled"}
        return None

    @classmethod
    def _effective_max_tokens_for_profile(
        cls,
        profile: dict[str, str],
        requested_max_tokens: int,
    ) -> int:
        if cls._is_kimi_k25_profile(profile):
            return max(int(requested_max_tokens), 1800)
        if cls._is_minimax_profile(profile):
            return max(1, min(int(requested_max_tokens), 2048))
        return int(requested_max_tokens)

    @classmethod
    def _effective_response_format_for_profile(
        cls,
        profile: dict[str, str],
        response_format: dict | None,
    ) -> dict | None:
        if cls._is_minimax_profile(profile):
            return None
        return response_format

    @classmethod
    def _effective_timeout_for_profile(
        cls,
        profile: dict[str, str],
        request_timeout: httpx.Timeout,
        *,
        llm_task_route: str = "",
        explicit_timeout: bool = False,
    ) -> httpx.Timeout:
        is_kimi = cls._is_kimi_k25_profile(profile)
        is_deepseek = cls._is_deepseek_profile(profile)
        if not (is_kimi or is_deepseek):
            return request_timeout
        route = str(llm_task_route or "").strip().lower()
        if explicit_timeout:
            return request_timeout
        read_timeout = max(
            120.0,
            float(getattr(request_timeout, "read", None) or 0.0),
        )
        connect_timeout = max(
            min(10.0, read_timeout),
            float(getattr(request_timeout, "connect", None) or 0.0),
        )
        return httpx.Timeout(read_timeout, connect=connect_timeout)

    @staticmethod
    def _is_kimi_k25_profile(profile: dict[str, str]) -> bool:
        base_url = str(profile.get("base_url") or "").lower()
        model = str(profile.get("model") or "").strip().lower()
        return ("moonshot" in base_url or "kimi" in base_url) and model.startswith("kimi-k2.5")

    @staticmethod
    def _is_deepseek_profile(profile: dict[str, str]) -> bool:
        text = " ".join(
            str(profile.get(key) or "").strip().lower()
            for key in ("id", "name", "base_url", "model")
        )
        return "deepseek" in text

    @classmethod
    def _is_minimax_profile(cls, profile: dict[str, str]) -> bool:
        return cls._profile_kind(profile) == "minimax"

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
        with self._client_lock:
            self.client.close()

    def __enter__(self) -> "OpenAICompatibleAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


class LLMClient(OpenAICompatibleAdapter):
    pass
