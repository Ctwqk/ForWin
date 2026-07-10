from __future__ import annotations

from typing import Any


class GenesisPromptBuilder:
    """Builds Genesis prompts without performing LLM calls or persistence."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def build_stage_generation_messages(
        self,
        *,
        project: Any,
        pack: dict[str, Any],
        stage_key: str,
        fallback: dict[str, Any],
    ) -> list[dict[str, str]]:
        return self.owner._build_stage_generation_messages(
            project=project,
            pack=pack,
            stage_key=stage_key,
            fallback=fallback,
        )

    def build_stage_refine_messages(
        self,
        *,
        pack: dict[str, Any],
        stage_key: str,
        instruction: str,
        target_path: str,
        current_payload: dict[str, Any],
        support_context: dict[str, Any],
        fallback_stage_payload: dict[str, Any],
        current_target: Any | None = None,
        wrap_scalar_value: bool = False,
    ) -> list[dict[str, str]]:
        return self.owner._build_stage_refine_messages(
            pack=pack,
            stage_key=stage_key,
            instruction=instruction,
            target_path=target_path,
            current_payload=current_payload,
            support_context=support_context,
            fallback_stage_payload=fallback_stage_payload,
            current_target=current_target,
            wrap_scalar_value=wrap_scalar_value,
        )

