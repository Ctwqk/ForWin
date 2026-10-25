from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True)
class ModelCapabilities:
    supports_json_schema: bool = False
    supports_response_format: bool = True
    supports_tool_calling: bool = False
    supports_system_messages: bool = True
    max_context_tokens: int | None = None


class ModelAdapter(Protocol):
    provider: str
    model: str
    base_url: str
    profile_id: str
    profile_name: str
    capabilities: ModelCapabilities

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
        ...

    def drain_model_fallback_events(self) -> list[dict[str, str]]:
        ...

    def close(self) -> None:
        ...


def adapter_capabilities(adapter: object | None) -> ModelCapabilities:
    capabilities = getattr(adapter, "capabilities", None)
    if isinstance(capabilities, ModelCapabilities):
        return capabilities
    return ModelCapabilities()
