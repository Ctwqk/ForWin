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
