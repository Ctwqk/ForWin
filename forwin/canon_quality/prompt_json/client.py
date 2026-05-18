from __future__ import annotations

from typing import Any

from forwin.llm.compat import call_chat_compat
from forwin.utils.json_repair import parse_llm_json


class PromptJsonClient:
    def __init__(self, llm_client: object) -> None:
        self.llm_client = llm_client

    def complete_json(
        self,
        messages: list[dict[str, str]],
        *,
        output_schema: dict[str, Any],
        temperature: float = 0.0,
        max_tokens: int = 1800,
        timeout_seconds: float = 45.0,
    ) -> dict[str, Any]:
        raw = call_chat_compat(
            self.llm_client,
            messages,
            temperature=temperature,
            max_tokens=max_tokens,
            timeout_seconds=timeout_seconds,
            response_format={"type": "json_object"},
            output_schema=output_schema,
            task_family="canon_quality_prompt_json",
            stage_key=str(output_schema.get("analyzer") or "prompt_json_analyzer"),
        )
        if isinstance(raw, dict):
            return raw
        return parse_llm_json(str(raw or ""), error_prefix=str(output_schema.get("analyzer") or "PromptJsonAnalyzer"))
