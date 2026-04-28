from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import httpx


def load_profile_marker(profile_dir: str | Path) -> dict[str, object]:
    path = Path(profile_dir).expanduser() / ".forwin-extension-profile.json"
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def resolve_target_client_id(
    preferred_client_id: str = "",
    *,
    profile_dir: str | Path = "",
) -> str:
    normalized = str(preferred_client_id or "").strip()
    if normalized:
        return normalized
    if profile_dir:
        marker = load_profile_marker(profile_dir)
        marker_client = str(marker.get("clientId") or "").strip()
        if marker_client:
            return marker_client
    return ""


@dataclass(slots=True)
class PreferredClientHeartbeat:
    ok: bool
    client_id: str = ""
    backend_base_url: str = ""
    last_heartbeat_at: str = ""
    recent_platforms: tuple[str, ...] = ()
    message: str = ""
    latest_recent_client_id: str = ""
    latest_recent_backend_base_url: str = ""
    latest_recent_heartbeat_at: str = ""

    @classmethod
    def from_payload(cls, payload: dict[str, Any]) -> "PreferredClientHeartbeat":
        return cls(
            ok=bool(payload.get("ok")),
            client_id=str(payload.get("client_id") or ""),
            backend_base_url=str(payload.get("backend_base_url") or ""),
            last_heartbeat_at=str(payload.get("last_heartbeat_at") or ""),
            recent_platforms=tuple(str(item) for item in payload.get("recent_platforms") or []),
            message=str(payload.get("message") or ""),
            latest_recent_client_id=str(payload.get("latest_recent_client_id") or ""),
            latest_recent_backend_base_url=str(payload.get("latest_recent_backend_base_url") or ""),
            latest_recent_heartbeat_at=str(payload.get("latest_recent_heartbeat_at") or ""),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "ok": self.ok,
            "client_id": self.client_id,
            "backend_base_url": self.backend_base_url,
            "last_heartbeat_at": self.last_heartbeat_at,
            "recent_platforms": list(self.recent_platforms),
            "message": self.message,
            "latest_recent_client_id": self.latest_recent_client_id,
            "latest_recent_backend_base_url": self.latest_recent_backend_base_url,
            "latest_recent_heartbeat_at": self.latest_recent_heartbeat_at,
        }


def get_preferred_client_heartbeat(
    api_base_url: str,
    *,
    preferred_client_id: str = "",
    profile_dir: str | Path = "",
    stale_seconds: int = 90,
    allow_latest_recent_fallback: bool = False,
) -> PreferredClientHeartbeat:
    client_id = resolve_target_client_id(preferred_client_id, profile_dir=profile_dir)
    base_url = str(api_base_url or "").strip().rstrip("/")
    if not base_url:
        return PreferredClientHeartbeat(ok=False, client_id=client_id, message="api base url is empty")
    try:
        response = httpx.get(
            f"{base_url}/api/publishers/extension/heartbeat-status",
            params={
                "client_id": client_id,
                "stale_seconds": max(int(stale_seconds or 90), 1),
                "allow_latest_recent_fallback": bool(
                    allow_latest_recent_fallback and not client_id
                ),
            },
            timeout=10.0,
        )
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:  # noqa: BLE001
        return PreferredClientHeartbeat(
            ok=False,
            client_id=client_id,
            message=f"publisher heartbeat API check failed: {exc}",
        )
    return PreferredClientHeartbeat.from_payload(payload if isinstance(payload, dict) else {})
