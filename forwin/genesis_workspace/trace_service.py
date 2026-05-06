from __future__ import annotations

from typing import Any


class GenesisTraceService:
    """Trace helper facade for Genesis LLM calls and PromptTrace side effects."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def call_json_with_trace(
        self,
        *,
        messages: list[dict[str, str]],
        fallback: dict[str, Any],
        stage_key: str,
        temperature: float = 0.45,
        max_tokens: int | None = None,
    ) -> tuple[dict[str, Any], dict[str, Any]]:
        return self.owner._call_json_with_trace_impl(
            messages=messages,
            fallback=fallback,
            stage_key=stage_key,
            temperature=temperature,
            max_tokens=max_tokens,
        )

    def prepare_trace_payload_for_save(self, trace_payload: dict[str, Any], *, project_id: str) -> dict[str, Any]:
        return self.owner._prepare_trace_payload_for_save(trace_payload, project_id=project_id)

    def record_llm_events_for_trace(
        self,
        *,
        updater: Any,
        project_id: str,
        trace_id: str,
        trace_payload: dict[str, Any],
        decision_event_id: str = "",
    ) -> None:
        self.owner._record_llm_events_for_trace(
            updater=updater,
            project_id=project_id,
            trace_id=trace_id,
            trace_payload=trace_payload,
            decision_event_id=decision_event_id,
        )
