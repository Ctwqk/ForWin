from __future__ import annotations

from typing import Any

from .upload_jobs import CodexInterventionHandler


def build_codex_intervention_handler(config: Any) -> CodexInterventionHandler | None:
    if not bool(getattr(config, "codex_enabled", False)):
        return None
    bridge_url = str(getattr(config, "codex_bridge_url", "") or "").strip()
    if not bridge_url:
        return None

    token = str(getattr(config, "codex_bridge_token", "") or "")
    timeout_seconds = float(getattr(config, "codex_sync_timeout_seconds", 90.0) or 90.0)
    job_timeout_seconds = float(
        getattr(config, "codex_job_timeout_seconds", 900.0) or 900.0
    )

    def submit(intervention: dict[str, Any]) -> dict[str, Any] | None:
        from forwin.llm.codex_client import CodexBridgeClient

        client = CodexBridgeClient(
            bridge_url=bridge_url,
            token=token,
            timeout_seconds=timeout_seconds,
        )
        try:
            return client.submit_job(
                prompt=str(intervention.get("prompt") or ""),
                permission_profile="publisher_browser_operator",
                timeout_seconds=job_timeout_seconds,
            )
        finally:
            client.close()

    return submit
