from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from forwin.model_adapter import ModelCapabilities, adapter_capabilities


CODEX_ALLOWED_FAMILIES = {
    "genesis",
    "writer",
    "reviewer",
    "repair",
    "phase4",
    "world_model",
}
CODEX_EXCLUDED_FAMILIES = {"chapter_plan_materialization"}


@dataclass(frozen=True)
class LLMCallIntent:
    task_family: str = ""
    stage_key: str = ""
    latency_class: str = "sync"
    output_schema: dict[str, Any] | None = None
    codex_allowed: bool = True
    permission_profile: str = "prompt_only_readonly"


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
    ) -> None:
        self.ordinary_adapter = ordinary_adapter
        self.codex_client = codex_client
        self.codex_enabled = bool(codex_enabled and codex_client is not None)
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
        if self._should_use_codex(resolved_intent):
            try:
                content = self.codex_client.chat(messages, intent=resolved_intent, **kwargs)
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
        result = LLMCallResult(
            content=self.ordinary_adapter.chat(messages, **kwargs),
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

    def _should_use_codex(self, intent: LLMCallIntent) -> bool:
        family = str(intent.task_family or "").strip()
        if not self.codex_enabled or not intent.codex_allowed:
            return False
        if not family or family in CODEX_EXCLUDED_FAMILIES:
            return False
        return family in CODEX_ALLOWED_FAMILIES

    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        events = list(self._fallback_events)
        self._fallback_events.clear()
        ordinary_drain = getattr(self.ordinary_adapter, "drain_model_fallback_events", None)
        if callable(ordinary_drain):
            events.extend(list(ordinary_drain() or []))
        return events

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
    ) -> str:
        content = self.router.chat(
            messages,
            intent=LLMCallIntent(
                task_family=task_family,
                stage_key=stage_key,
                latency_class=latency_class,
                output_schema=output_schema,
                codex_allowed=codex_allowed,
                permission_profile=permission_profile,
            ),
            temperature=temperature,
            max_tokens=max_tokens,
            response_format=response_format,
            timeout_seconds=timeout_seconds,
            retry_on_timeout=retry_on_timeout,
        )
        self.last_call_result = self.router.last_call_result
        return content

    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        return self.router.drain_model_fallback_events()

    def close(self) -> None:
        self.router.close()
