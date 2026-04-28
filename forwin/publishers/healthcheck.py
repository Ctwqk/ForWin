from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

from sqlalchemy import text

from forwin.models.base import get_engine


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _parse_datetime(raw: object) -> datetime | None:
    text = str(raw or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


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
    database_url: str | Path,
    *,
    preferred_client_id: str = "",
    profile_dir: str | Path = "",
    stale_seconds: int = 90,
    allow_latest_recent_fallback: bool = False,
) -> PreferredClientHeartbeat:
    resolved_client_id = resolve_target_client_id(
        preferred_client_id,
        profile_dir=profile_dir,
    )
    client_id = resolved_client_id

    try:
        engine = get_engine(str(database_url))
    except Exception as exc:  # noqa: BLE001
        return PreferredClientHeartbeat(
            ok=False,
            client_id=client_id,
            message=f"database URL is invalid: {exc}",
        )

    cutoff = _utc_now() - timedelta(seconds=max(int(stale_seconds or 90), 1))
    try:
        with engine.connect() as conn:
            latest_recent = conn.execute(
                text(
                    """
                    SELECT client_id, backend_base_url, last_heartbeat_at
                    FROM publisher_extension_clients
                    ORDER BY last_heartbeat_at DESC
                    LIMIT 1
                    """
                )
            ).mappings().first()
            latest_recent_client_id = str(latest_recent["client_id"] or "").strip() if latest_recent else ""
            if not client_id and allow_latest_recent_fallback:
                client_id = latest_recent_client_id
            row = (
                conn.execute(
                    text(
                        """
                        SELECT client_id, backend_base_url, last_heartbeat_at
                        FROM publisher_extension_clients
                        WHERE client_id = :client_id
                        """
                    ),
                    {"client_id": client_id},
                ).mappings().first()
                if client_id
                else None
            )
            recent_platform_rows = conn.execute(
                text(
                    """
                    SELECT platform_id
                    FROM publisher_extension_platform_states
                    WHERE client_id = :client_id
                      AND last_heartbeat_at >= :cutoff
                    ORDER BY platform_id
                    """
                ),
                {"client_id": client_id, "cutoff": cutoff.replace(tzinfo=None)},
            ).mappings().all()
    except Exception as exc:  # noqa: BLE001
        return PreferredClientHeartbeat(
            ok=False,
            client_id=client_id,
            message=f"database heartbeat query failed: {exc}",
        )
    finally:
        engine.dispose()

    latest_recent_backend_base_url = str(latest_recent["backend_base_url"] or "").strip() if latest_recent else ""
    latest_recent_heartbeat_at = str(latest_recent["last_heartbeat_at"] or "").strip() if latest_recent else ""
    recent_platforms = tuple(str(item["platform_id"] or "").strip() for item in recent_platform_rows)
    using_latest_recent = bool(
        allow_latest_recent_fallback
        and client_id
        and latest_recent_client_id
        and client_id == latest_recent_client_id
        and not resolved_client_id
    )

    if not client_id:
        return PreferredClientHeartbeat(
            ok=False,
            message="preferred publisher client id is empty and no recent publisher client heartbeat was found",
            latest_recent_client_id=latest_recent_client_id,
            latest_recent_backend_base_url=latest_recent_backend_base_url,
            latest_recent_heartbeat_at=latest_recent_heartbeat_at,
        )

    if row is None:
        return PreferredClientHeartbeat(
            ok=False,
            client_id=client_id,
            message="preferred publisher client id is not registered in publisher_extension_clients",
            latest_recent_client_id=latest_recent_client_id,
            latest_recent_backend_base_url=latest_recent_backend_base_url,
            latest_recent_heartbeat_at=latest_recent_heartbeat_at,
        )

    last_heartbeat_at = str(row["last_heartbeat_at"] or "").strip()
    parsed_heartbeat = _parse_datetime(last_heartbeat_at)
    if parsed_heartbeat is None:
        return PreferredClientHeartbeat(
            ok=False,
            client_id=client_id,
            backend_base_url=str(row["backend_base_url"] or "").strip(),
            last_heartbeat_at=last_heartbeat_at,
            recent_platforms=recent_platforms,
            message="preferred publisher client heartbeat timestamp is missing or invalid",
            latest_recent_client_id=latest_recent_client_id,
            latest_recent_backend_base_url=latest_recent_backend_base_url,
            latest_recent_heartbeat_at=latest_recent_heartbeat_at,
        )

    if parsed_heartbeat < cutoff:
        return PreferredClientHeartbeat(
            ok=False,
            client_id=client_id,
            backend_base_url=str(row["backend_base_url"] or "").strip(),
            last_heartbeat_at=last_heartbeat_at,
            recent_platforms=recent_platforms,
            message="latest publisher client heartbeat is stale" if using_latest_recent else "preferred publisher client heartbeat is stale",
            latest_recent_client_id=latest_recent_client_id,
            latest_recent_backend_base_url=latest_recent_backend_base_url,
            latest_recent_heartbeat_at=latest_recent_heartbeat_at,
        )

    return PreferredClientHeartbeat(
        ok=True,
        client_id=client_id,
        backend_base_url=str(row["backend_base_url"] or "").strip(),
        last_heartbeat_at=last_heartbeat_at,
        recent_platforms=recent_platforms,
        message="latest publisher client heartbeat is recent" if using_latest_recent else "preferred publisher client heartbeat is recent",
        latest_recent_client_id=latest_recent_client_id,
        latest_recent_backend_base_url=latest_recent_backend_base_url,
        latest_recent_heartbeat_at=latest_recent_heartbeat_at,
    )
