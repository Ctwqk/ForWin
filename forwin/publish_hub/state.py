"""What the publish-hub sync process keeps for the release gate (R35).

Row "destinations" of publish_hub_sync_state holds the last /v1/destinations snapshot,
keyed by destination id:

    {"<destination_id>": {"account_status": str, "remote_id": str | None, "platform": str}}

`updated_at` is when it was refreshed. The sync process (Task 6) writes it with
`write_destinations`. The release gate reads it with `cached_destination`. A destination that is
not in the snapshot is unknown: an account publish-hub disabled, or a destination created after
the last refresh.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from forwin.models.publisher import PublishHubSyncState

DESTINATIONS_KEY = "destinations"


@dataclass(frozen=True)
class CachedDestination:
    destination_id: str
    platform: str
    account_status: str
    remote_id: str | None


def write_destinations(
    session, destinations: Iterable[Mapping[str, Any]], *, now: datetime
) -> None:
    """Replace the snapshot with `destinations`, the items of GET /v1/destinations.

    Each item needs `id`. Only `account_status`, `remote_id` and `platform` are kept. The
    caller commits.
    """
    snapshot: dict[str, dict[str, Any]] = {}
    for item in destinations:
        destination_id = str(item.get("id") or "").strip()
        if not destination_id:
            raise ValueError("destination without an id")
        snapshot[destination_id] = {
            "account_status": str(item.get("account_status") or ""),
            "remote_id": str(item.get("remote_id") or "").strip() or None,
            "platform": str(item.get("platform") or ""),
        }
    row = session.get(PublishHubSyncState, DESTINATIONS_KEY, with_for_update=True)
    if row is None:
        session.add(
            PublishHubSyncState(key=DESTINATIONS_KEY, cursor=0, value_json=snapshot, updated_at=now)
        )
    else:
        row.value_json = snapshot
        row.updated_at = now
    session.flush()


def cached_destination(session, destination_id: str) -> CachedDestination | None:
    """The cached state of one destination, or None when the snapshot does not list it."""
    row = session.get(PublishHubSyncState, DESTINATIONS_KEY)
    snapshot = row.value_json if row is not None and isinstance(row.value_json, dict) else {}
    entry = snapshot.get(str(destination_id or "").strip())
    if not isinstance(entry, dict):
        return None
    return CachedDestination(
        destination_id=str(destination_id),
        platform=str(entry.get("platform") or ""),
        account_status=str(entry.get("account_status") or ""),
        remote_id=str(entry.get("remote_id") or "").strip() or None,
    )


__all__ = [
    "DESTINATIONS_KEY",
    "CachedDestination",
    "cached_destination",
    "write_destinations",
]
