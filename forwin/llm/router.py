from __future__ import annotations

import json
import threading
import uuid
from dataclasses import dataclass, field
from typing import Any

from forwin.model_adapter import ModelCapabilities, adapter_capabilities


CODEX_ALLOWED_FAMILIES = {
    "arc_planning",
    "chapter_review_form",
    "entity_admission",
    "feedback",
    "genesis",
    "planning",
    "reader_feedback",
    "writer",
    "write_chapter",
    "review",
    "review_chapter",
    "reviewer",
    "repair",
    "phase4",
    "world_model",
}
CODEX_EXCLUDED_FAMILIES = {"chapter_plan_materialization"}
CODEX_PRIMARY_FAMILIES = {
    "arc_planning",
    "chapter_review_form",
    "entity_admission",
    "feedback",
    "genesis",
    "planning",
    "reader_feedback",
    "review",
    "review_chapter",
    "reviewer",
    "phase4",
    "world_model",
}
WRITER_FAMILIES = {"writer", "write_chapter"}
CODEX_PRIMARY_WRITER_STAGES = {
    "state_event_extraction",
    "thread_time_extraction",
    "lore_timeline_extraction",
    "scene_breakdown",
}
ORDINARY_PRIMARY_WRITER_STAGES = {
    "chapter_draft",
    "scene_generation",
    "scene_stitch",
    "chapter_rewrite",
}


@dataclass(frozen=True)
class LLMCallIntent:
    task_family: str = ""
    stage_key: str = ""
    latency_class: str = "sync"
    output_schema: dict[str, Any] | None = None
    codex_allowed: bool = True
    permission_profile: str = "prompt_only_readonly"
    codex_model: str = ""


@dataclass(frozen=True)
class LLMCallResult:
    content: str
    backend: str
    fallback_used: bool = False
    trace: dict[str, Any] = field(default_factory=dict)


class LLMCallRouter:
    def __init__(
        self,
        *,
        ordinary_adapter,
        codex_client=None,
        codex_enabled: bool = False,
        codex_default_model: str = "",
    ) -> None:
        self.ordinary_adapter = ordinary_adapter
        self.codex_client = codex_client
        self.codex_enabled = bool(codex_enabled and codex_client is not None)
        self.codex_default_model = str(codex_default_model or "").strip()
        self._fallback_events: list[dict[str, str]] = []
        self.last_call_result: LLMCallResult | None = None
        self._last_codex_trace: dict[str, Any] = {}
        self._attempt_events: list[dict[str, object]] = []
        self._call_lock = threading.RLock()
        self._thread_state = threading.local()

    def chat(
        self,
        messages: list[dict],
        *,
        intent: LLMCallIntent | None = None,
        **kwargs: Any,
    ) -> str:
        return self.chat_with_result(messages, intent=intent, **kwargs).content

    def chat_with_result(
        self,
        messages: list[dict],
        *,
        intent: LLMCallIntent | None = None,
        **kwargs: Any,
    ) -> LLMCallResult:
        with self._call_lock:
            return self._chat_with_result_unlocked(
                messages,
                intent=intent,
                **kwargs,
            )

    def _chat_with_result_unlocked(
        self,
        messages: list[dict],
        *,
        intent: LLMCallIntent | None = None,
        **kwargs: Any,
    ) -> LLMCallResult:
        self.last_call_result = None
        self._last_codex_trace = {}
        resolved_intent = intent or LLMCallIntent(codex_allowed=False)
        route_call_id = uuid.uuid4().hex
        self._thread_state.last_attempt_group_id = route_call_id
        fallback_used = False
        failed_codex_trace: dict[str, Any] = {}
        codex_policy = self._codex_policy(resolved_intent)
        if codex_policy == "codex_primary":
            try:
                content = self._chat_with_codex(
                    messages,
                    intent=resolved_intent,
                    attempt_group_id=route_call_id,
                    **kwargs,
                )
                result = LLMCallResult(
                    content=content,
                    backend="codex_bridge",
                    trace={
                        **self._last_codex_trace,
                        "backend": "codex_bridge",
                        "task_family": resolved_intent.task_family,
                        "stage_key": resolved_intent.stage_key,
                        "permission_profile": resolved_intent.permission_profile,
                    },
                )
                self.last_call_result = result
                return result
            except Exception as exc:  # noqa: BLE001
                fallback_used = True
                failed_codex_trace = dict(self._last_codex_trace)
                self._fallback_events.append(
                    {
                        "from_backend": "codex_bridge",
                        "to_backend": "ordinary",
                        "task_family": resolved_intent.task_family,
                        "stage_key": resolved_intent.stage_key,
                        "reason": str(exc),
                    }
                )
        ordinary_kwargs = dict(kwargs)
        ordinary_kwargs.setdefault("task_family", resolved_intent.task_family)
        ordinary_kwargs.setdefault("stage_key", resolved_intent.stage_key)
        ordinary_kwargs.setdefault("output_schema", resolved_intent.output_schema)
        try:
            try:
                ordinary_content = self.ordinary_adapter.chat(
                    messages, **ordinary_kwargs
                )
            finally:
                self._capture_ordinary_attempts(attempt_group_id=route_call_id)
        except Exception as ordinary_exc:  # noqa: BLE001
            if codex_policy != "ordinary_primary":
                self.last_call_result = self._failed_result(
                    resolved_intent,
                    fallback_used=fallback_used,
                    failed_codex_trace=failed_codex_trace,
                    ordinary_error=ordinary_exc,
                )
                raise
            ordinary_reason = str(ordinary_exc)
            try:
                content = self._chat_with_codex(
                    messages,
                    intent=resolved_intent,
                    attempt_group_id=route_call_id,
                    **kwargs,
                )
            except Exception as codex_exc:
                self.last_call_result = self._failed_result(
                    resolved_intent,
                    fallback_used=True,
                    failed_codex_trace=dict(self._last_codex_trace),
                    ordinary_error=ordinary_exc,
                    codex_error=codex_exc,
                )
                raise ordinary_exc
            self._fallback_events.append(
                {
                    "from_backend": "ordinary",
                    "to_backend": "codex_bridge",
                    "task_family": resolved_intent.task_family,
                    "stage_key": resolved_intent.stage_key,
                    "reason": ordinary_reason,
                }
            )
            result = LLMCallResult(
                content=content,
                backend="codex_bridge",
                fallback_used=True,
                trace={
                    **self._last_codex_trace,
                    "backend": "codex_bridge",
                    "task_family": resolved_intent.task_family,
                    "stage_key": resolved_intent.stage_key,
                    "permission_profile": resolved_intent.permission_profile,
                },
            )
            self.last_call_result = result
            return result
        result = LLMCallResult(
            content=ordinary_content,
            backend="ordinary",
            fallback_used=fallback_used,
            trace={
                "backend": "ordinary",
                "task_family": resolved_intent.task_family,
                "stage_key": resolved_intent.stage_key,
                "permission_profile": resolved_intent.permission_profile,
                "failed_codex_trace": failed_codex_trace,
            },
        )
        self.last_call_result = result
        return result

    @staticmethod
    def _failed_result(
        intent: LLMCallIntent,
        *,
        fallback_used: bool,
        failed_codex_trace: dict[str, Any],
        ordinary_error: Exception,
        codex_error: Exception | None = None,
    ) -> LLMCallResult:
        trace = {
            "backend": "failed",
            "task_family": intent.task_family,
            "stage_key": intent.stage_key,
            "permission_profile": intent.permission_profile,
            "failed_codex_trace": failed_codex_trace,
            "ordinary_error": f"{ordinary_error.__class__.__name__}: {ordinary_error}",
        }
        if codex_error is not None:
            trace["codex_error"] = f"{codex_error.__class__.__name__}: {codex_error}"
        return LLMCallResult(
            content="",
            backend="failed",
            fallback_used=fallback_used,
            trace=trace,
        )

    def _chat_with_codex(
        self,
        messages: list[dict],
        *,
        intent: LLMCallIntent,
        attempt_group_id: str,
        **kwargs: Any,
    ) -> str:
        codex_kwargs = dict(kwargs)
        model = str(intent.codex_model or self.codex_default_model or "").strip()
        if model:
            codex_kwargs.setdefault("model", model)
        self._last_codex_trace = {"model": model, "requested_model": model}
        if isinstance(getattr(self.codex_client, "last_call_trace", None), dict):
            self.codex_client.last_call_trace = {}
        content = ""
        failure: Exception | None = None
        try:
            content = self.codex_client.chat(messages, intent=intent, **codex_kwargs)
        except Exception as exc:
            failure = exc
            raise
        finally:
            client_trace = getattr(self.codex_client, "last_call_trace", None)
            if isinstance(client_trace, dict):
                self._last_codex_trace = {
                    **client_trace,
                    "model": str(
                        client_trace.get("actual_model")
                        or client_trace.get("model")
                        or model
                    ),
                    "requested_model": str(
                        client_trace.get("requested_model") or model
                    ),
                }
            self._attempt_events.append(
                self._codex_attempt(
                    messages=messages,
                    intent=intent,
                    kwargs=codex_kwargs,
                    content=content,
                    trace=self._last_codex_trace,
                    failure=failure,
                    attempt_group_id=attempt_group_id,
                )
            )
        return content

    @staticmethod
    def _codex_attempt(
        *,
        messages: list[dict],
        intent: LLMCallIntent,
        kwargs: dict[str, Any],
        content: str,
        trace: dict[str, Any],
        failure: Exception | None,
        attempt_group_id: str,
    ) -> dict[str, object]:
        input_chars = int(
            trace.get("input_chars") or len(json.dumps(messages, ensure_ascii=False))
        )
        output_chars = int(trace.get("output_chars") or len(content))
        prompt_tokens = LLMCallRouter._optional_token_count(trace.get("prompt_tokens"))
        completion_tokens = LLMCallRouter._optional_token_count(
            trace.get("completion_tokens")
        )
        total_tokens = LLMCallRouter._optional_token_count(trace.get("total_tokens"))
        usage_source = str(trace.get("usage_source") or "").strip()
        if prompt_tokens is None and completion_tokens is None and total_tokens is None:
            raw_usage = LLMCallRouter._usage_from_raw_events(trace)
            prompt_tokens = raw_usage.get("prompt_tokens")
            completion_tokens = raw_usage.get("completion_tokens")
            total_tokens = raw_usage.get("total_tokens")
            if raw_usage:
                usage_source = "codex_bridge"
        if prompt_tokens is None and completion_tokens is None and total_tokens is None:
            prompt_tokens = LLMCallRouter._estimate_tokens_from_chars(input_chars)
            completion_tokens = LLMCallRouter._estimate_tokens_from_chars(output_chars)
            total_tokens = prompt_tokens + completion_tokens
            usage_source = "estimated"
        elif (
            total_tokens is None
            and prompt_tokens is not None
            and completion_tokens is not None
        ):
            total_tokens = prompt_tokens + completion_tokens
        if not usage_source:
            usage_source = "codex_bridge"
        http_status = int(trace.get("http_status") or 0)
        if not http_status and failure is None:
            http_status = 200
        raw_response = trace.get("raw_response_text")
        if not raw_response and trace.get("response") is not None:
            raw_response = json.dumps(
                trace["response"], ensure_ascii=False, sort_keys=True
            )
        return {
            "attempt_group_id": attempt_group_id,
            "attempt_no": 1,
            "profile_id": "codex_bridge",
            "profile_name": "Codex Bridge",
            "provider": "codex_bridge",
            "model": str(trace.get("actual_model") or trace.get("model") or ""),
            "base_url_host": "codex_bridge",
            "http_status": http_status,
            "provider_request_id": str(trace.get("thread_id") or ""),
            "duration_ms": int(trace.get("duration_ms") or 0),
            "input_chars": input_chars,
            "output_chars": output_chars,
            "prompt_tokens": prompt_tokens,
            "completion_tokens": completion_tokens,
            "total_tokens": total_tokens,
            "usage_source": usage_source,
            "task_family": intent.task_family,
            "stage_key": intent.stage_key,
            "llm_task_route": "codex_bridge",
            "requested_temperature": float(kwargs["temperature"] if kwargs.get("temperature") is not None else 0.85),
            "requested_max_tokens": int(kwargs.get("max_tokens") or 16384),
            "error_class": failure.__class__.__name__ if failure else "",
            "error_message": str(failure or ""),
            "error_category": "codex_bridge" if failure else "",
            "retryable": False,
            "fallback_eligible": failure is not None,
            "final_failure": failure is not None,
            "_raw_request_payload": trace.get("request") or {"messages": messages},
            "_raw_response_text": str(raw_response or content or ""),
        }

    @staticmethod
    def _usage_from_raw_events(trace: dict[str, Any]) -> dict[str, int | None]:
        for event in reversed(list(trace.get("raw_events") or [])):
            if not isinstance(event, dict):
                continue
            usage = event.get("usage")
            if not isinstance(usage, dict):
                continue
            prompt_tokens = LLMCallRouter._optional_token_count(
                usage.get("prompt_tokens", usage.get("input_tokens"))
            )
            completion_tokens = LLMCallRouter._optional_token_count(
                usage.get("completion_tokens", usage.get("output_tokens"))
            )
            total_tokens = LLMCallRouter._optional_token_count(
                usage.get("total_tokens")
            )
            if (
                total_tokens is None
                and prompt_tokens is not None
                and completion_tokens is not None
            ):
                total_tokens = prompt_tokens + completion_tokens
            if any(
                value is not None
                for value in (prompt_tokens, completion_tokens, total_tokens)
            ):
                return {
                    "prompt_tokens": prompt_tokens,
                    "completion_tokens": completion_tokens,
                    "total_tokens": total_tokens,
                }
        return {}

    @staticmethod
    def _optional_token_count(value: object) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _estimate_tokens_from_chars(chars: int) -> int:
        return max(0, int(max(0, chars) * 0.25))

    def _should_use_codex(self, intent: LLMCallIntent) -> bool:
        return self._codex_policy(intent) == "codex_primary"

    def _codex_policy(self, intent: LLMCallIntent) -> str:
        family = str(intent.task_family or "").strip().lower()
        stage = str(intent.stage_key or "").strip().lower()
        if not self.codex_enabled or not intent.codex_allowed:
            return "ordinary_only"
        if not family or family in CODEX_EXCLUDED_FAMILIES:
            return "ordinary_only"
        if family not in CODEX_ALLOWED_FAMILIES:
            return "ordinary_only"
        if str(intent.codex_model or "").strip():
            return "codex_primary"
        if family in CODEX_PRIMARY_FAMILIES:
            return "codex_primary"
        if family in WRITER_FAMILIES:
            if stage in CODEX_PRIMARY_WRITER_STAGES or any(
                token in stage
                for token in ("state_event", "thread_time", "lore_timeline")
            ):
                return "codex_primary"
            if stage in ORDINARY_PRIMARY_WRITER_STAGES or any(
                token in stage for token in ("chapter_rewrite", "repair")
            ):
                return "ordinary_primary"
            return "ordinary_primary"
        if family == "repair":
            return "ordinary_primary"
        return "codex_primary"

    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        with self._call_lock:
            events = list(self._fallback_events)
            self._fallback_events.clear()
            ordinary_drain = getattr(
                self.ordinary_adapter, "drain_model_fallback_events", None
            )
            if callable(ordinary_drain):
                events.extend(list(ordinary_drain() or []))
            return events

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        with self._call_lock:
            self._capture_ordinary_attempts()
            events = list(self._attempt_events)
            self._attempt_events.clear()
            return events

    def peek_llm_attempt_events(self) -> list[dict[str, object]]:
        with self._call_lock:
            self._capture_ordinary_attempts()
            return list(self._attempt_events)

    @property
    def llm_attempt_group_id(self) -> str:
        return str(getattr(self._thread_state, "last_attempt_group_id", "") or "")

    def _capture_ordinary_attempts(self, *, attempt_group_id: str = "") -> None:
        ordinary_drain = getattr(
            self.ordinary_adapter, "drain_llm_attempt_events", None
        )
        if callable(ordinary_drain):
            events = list(ordinary_drain() or [])
            if attempt_group_id:
                for event in events:
                    backend_group_id = str(event.get("attempt_group_id") or "")
                    if backend_group_id:
                        event["backend_attempt_group_id"] = backend_group_id
                    event["attempt_group_id"] = attempt_group_id
            self._attempt_events.extend(events)

    def close(self) -> None:
        close_codex = getattr(self.codex_client, "close", None)
        if callable(close_codex):
            close_codex()
        close_ordinary = getattr(self.ordinary_adapter, "close", None)
        if callable(close_ordinary):
            close_ordinary()


class RoutedModelAdapter:
    provider = "routed"

    def __init__(self, router: LLMCallRouter) -> None:
        self.router = router
        self.ordinary_adapter = router.ordinary_adapter
        self.codex_enabled = router.codex_enabled
        self.model = getattr(self.ordinary_adapter, "model", "")
        self.base_url = getattr(self.ordinary_adapter, "base_url", "")
        self.api_key = getattr(self.ordinary_adapter, "api_key", "")
        self.profile_id = getattr(self.ordinary_adapter, "profile_id", "")
        self.profile_name = getattr(self.ordinary_adapter, "profile_name", "")
        self.capabilities: ModelCapabilities = adapter_capabilities(
            self.ordinary_adapter
        )
        self.last_call_result: LLMCallResult | None = None

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
        latency_class: str = "sync",
        output_schema: dict[str, Any] | None = None,
        codex_allowed: bool = True,
        permission_profile: str = "prompt_only_readonly",
        preferred_provider_kind: str = "",
        preferred_model: str = "",
    ) -> str:
        preferred_kind = str(preferred_provider_kind or "").strip().lower()
        preferred_model_text = str(preferred_model or "").strip()
        deterministic_route_requested = bool(preferred_kind or preferred_model_text)
        preferred_codex_requested = (
            preferred_kind in {"codex", "codex_bridge", "spark"}
            or "codex" in preferred_model_text.lower()
            or "gpt-5.3" in preferred_model_text.lower()
        )
        self.last_call_result = None
        try:
            content = self.router.chat(
                messages,
                intent=LLMCallIntent(
                    task_family=task_family,
                    stage_key=stage_key,
                    latency_class=latency_class,
                    output_schema=output_schema,
                    codex_allowed=bool(
                        codex_allowed
                        and (
                            not deterministic_route_requested
                            or preferred_codex_requested
                        )
                    ),
                    permission_profile=permission_profile,
                    codex_model=preferred_model_text
                    if preferred_codex_requested
                    else "",
                ),
                temperature=temperature,
                max_tokens=max_tokens,
                response_format=response_format,
                timeout_seconds=timeout_seconds,
                retry_on_timeout=retry_on_timeout,
                preferred_provider_kind=preferred_provider_kind,
                preferred_model=preferred_model,
            )
        finally:
            self.last_call_result = self.router.last_call_result
        return content

    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        return self.router.drain_model_fallback_events()

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        return self.router.drain_llm_attempt_events()

    @property
    def llm_attempt_events(self) -> list[dict[str, object]]:
        return self.router.peek_llm_attempt_events()

    @property
    def llm_attempt_group_id(self) -> str:
        return self.router.llm_attempt_group_id

    def close(self) -> None:
        self.router.close()
