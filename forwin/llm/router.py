from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forwin.model_adapter import ModelCapabilities, adapter_capabilities


CODEX_ALLOWED_FAMILIES = {
    "arc_planning",
    "chapter_review_form",
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
        resolved_intent = intent or LLMCallIntent(codex_allowed=False)
        fallback_used = False
        codex_policy = self._codex_policy(resolved_intent)
        if codex_policy == "codex_primary":
            try:
                content = self._chat_with_codex(messages, intent=resolved_intent, **kwargs)
                result = LLMCallResult(
                    content=content,
                    backend="codex_bridge",
                    trace={
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
            ordinary_content = self.ordinary_adapter.chat(messages, **ordinary_kwargs)
        except Exception as ordinary_exc:  # noqa: BLE001
            if codex_policy != "ordinary_primary":
                raise
            ordinary_reason = str(ordinary_exc)
            try:
                content = self._chat_with_codex(messages, intent=resolved_intent, **kwargs)
            except Exception:
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
            },
        )
        self.last_call_result = result
        return result

    def _chat_with_codex(
        self,
        messages: list[dict],
        *,
        intent: LLMCallIntent,
        **kwargs: Any,
    ) -> str:
        codex_kwargs = dict(kwargs)
        model = str(intent.codex_model or self.codex_default_model or "").strip()
        if model:
            codex_kwargs.setdefault("model", model)
        return self.codex_client.chat(messages, intent=intent, **codex_kwargs)

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
                token in stage for token in ("state_event", "thread_time", "lore_timeline")
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
        events = list(self._fallback_events)
        self._fallback_events.clear()
        ordinary_drain = getattr(self.ordinary_adapter, "drain_model_fallback_events", None)
        if callable(ordinary_drain):
            events.extend(list(ordinary_drain() or []))
        return events

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        ordinary_drain = getattr(self.ordinary_adapter, "drain_llm_attempt_events", None)
        if callable(ordinary_drain):
            return list(ordinary_drain() or [])
        return []

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
        self.capabilities: ModelCapabilities = adapter_capabilities(self.ordinary_adapter)
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
        content = self.router.chat(
            messages,
            intent=LLMCallIntent(
                task_family=task_family,
                stage_key=stage_key,
                latency_class=latency_class,
                output_schema=output_schema,
                codex_allowed=bool(codex_allowed and (not deterministic_route_requested or preferred_codex_requested)),
                permission_profile=permission_profile,
                codex_model=preferred_model_text if preferred_codex_requested else "",
            ),
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            timeout_seconds=timeout_seconds,
            retry_on_timeout=retry_on_timeout,
            preferred_provider_kind=preferred_provider_kind,
            preferred_model=preferred_model,
        )
        self.last_call_result = self.router.last_call_result
        return content

    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        return self.router.drain_model_fallback_events()

    def drain_llm_attempt_events(self) -> list[dict[str, object]]:
        return self.router.drain_llm_attempt_events()

    def close(self) -> None:
        self.router.close()
