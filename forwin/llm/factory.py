from __future__ import annotations

from forwin.config import InfrastructureConfig

from .codex_client import CodexBridgeClient
from .router import LLMCallRouter, RoutedModelAdapter


def maybe_wrap_with_codex_router(ordinary_adapter, config: InfrastructureConfig):
    if not bool(getattr(config, "codex_enabled", False)):
        return ordinary_adapter
    bridge_url = str(getattr(config, "codex_bridge_url", "") or "").strip()
    if not bridge_url:
        return ordinary_adapter
    codex_client = CodexBridgeClient(
        bridge_url=bridge_url,
        token=str(getattr(config, "codex_bridge_token", "") or ""),
        timeout_seconds=float(getattr(config, "codex_sync_timeout_seconds", 900.0) or 900.0),
    )
    return RoutedModelAdapter(
        LLMCallRouter(
            ordinary_adapter=ordinary_adapter,
            codex_client=codex_client,
            codex_enabled=True,
            codex_default_model=str(getattr(config, "codex_default_model", "") or ""),
        )
    )
