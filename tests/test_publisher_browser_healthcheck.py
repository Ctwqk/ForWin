from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone

from sqlalchemy import create_engine

from forwin.publishers.healthcheck import (
    get_preferred_client_heartbeat,
    resolve_target_client_id,
)


def _seed_db(path, *, client_id: str, heartbeat_at: datetime, backend_base_url: str = "http://forwin:8899") -> None:
    conn = sqlite3.connect(path)
    cur = conn.cursor()
    cur.execute(
        """
        CREATE TABLE publisher_extension_clients (
            client_id TEXT PRIMARY KEY,
            extension_version TEXT NOT NULL DEFAULT '',
            browser_name TEXT NOT NULL DEFAULT '',
            browser_version TEXT NOT NULL DEFAULT '',
            backend_base_url TEXT NOT NULL DEFAULT '',
            created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    cur.execute(
        """
        CREATE TABLE publisher_extension_platform_states (
            client_id TEXT NOT NULL,
            platform_id TEXT NOT NULL,
            connected INTEGER NOT NULL DEFAULT 0,
            login_method TEXT NOT NULL DEFAULT '',
            status_json TEXT NOT NULL DEFAULT '{}',
            last_error TEXT NOT NULL DEFAULT '',
            updated_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            last_heartbeat_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (client_id, platform_id)
        )
        """
    )
    stamp = heartbeat_at.replace(tzinfo=None).isoformat(sep=" ")
    cur.execute(
        """
        INSERT INTO publisher_extension_clients (
            client_id, backend_base_url, last_heartbeat_at
        ) VALUES (?, ?, ?)
        """,
        (client_id, backend_base_url, stamp),
    )
    cur.execute(
        """
        INSERT INTO publisher_extension_platform_states (
            client_id, platform_id, connected, last_heartbeat_at
        ) VALUES (?, 'fanqie', 1, ?)
        """,
        (client_id, stamp),
    )
    conn.commit()
    conn.close()


def _patch_healthcheck_engine(monkeypatch, db_path) -> str:
    engine = create_engine(f"sqlite:///{db_path}")
    monkeypatch.setattr("forwin.publishers.healthcheck.get_engine", lambda _database_url: engine)
    return "postgresql+psycopg://forwin:forwin@localhost:5432/forwin"


def test_resolve_target_client_id_falls_back_to_profile_marker(tmp_path):
    profile_dir = tmp_path / "profile"
    profile_dir.mkdir()
    (profile_dir / ".forwin-extension-profile.json").write_text(
        json.dumps({"clientId": "marker-client"}, ensure_ascii=False),
        encoding="utf-8",
    )

    assert resolve_target_client_id("", profile_dir=profile_dir) == "marker-client"


def test_get_preferred_client_heartbeat_accepts_recent_client(tmp_path, monkeypatch):
    db_path = tmp_path / "novel.db"
    now = datetime.now(timezone.utc)
    _seed_db(db_path, client_id="preferred-client", heartbeat_at=now)
    database_url = _patch_healthcheck_engine(monkeypatch, db_path)

    result = get_preferred_client_heartbeat(
        database_url,
        preferred_client_id="preferred-client",
        stale_seconds=90,
    )

    assert result.ok is True
    assert result.client_id == "preferred-client"
    assert result.backend_base_url == "http://forwin:8899"
    assert result.recent_platforms == ("fanqie",)


def test_get_preferred_client_heartbeat_rejects_stale_client(tmp_path, monkeypatch):
    db_path = tmp_path / "novel.db"
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    _seed_db(db_path, client_id="preferred-client", heartbeat_at=stale_at)
    database_url = _patch_healthcheck_engine(monkeypatch, db_path)

    result = get_preferred_client_heartbeat(
        database_url,
        preferred_client_id="preferred-client",
        stale_seconds=90,
    )

    assert result.ok is False
    assert result.client_id == "preferred-client"
    assert result.message == "preferred publisher client heartbeat is stale"


def test_get_preferred_client_heartbeat_falls_back_to_latest_recent_client(tmp_path, monkeypatch):
    db_path = tmp_path / "novel.db"
    now = datetime.now(timezone.utc)
    _seed_db(db_path, client_id="recent-client", heartbeat_at=now)
    database_url = _patch_healthcheck_engine(monkeypatch, db_path)

    result = get_preferred_client_heartbeat(
        database_url,
        preferred_client_id="",
        stale_seconds=90,
        allow_latest_recent_fallback=True,
    )

    assert result.ok is True
    assert result.client_id == "recent-client"
    assert result.message == "latest publisher client heartbeat is recent"


def test_get_preferred_client_heartbeat_rejects_stale_latest_recent_fallback(tmp_path, monkeypatch):
    db_path = tmp_path / "novel.db"
    stale_at = datetime.now(timezone.utc) - timedelta(minutes=5)
    _seed_db(db_path, client_id="recent-client", heartbeat_at=stale_at)
    database_url = _patch_healthcheck_engine(monkeypatch, db_path)

    result = get_preferred_client_heartbeat(
        database_url,
        preferred_client_id="",
        stale_seconds=90,
        allow_latest_recent_fallback=True,
    )

    assert result.ok is False
    assert result.client_id == "recent-client"
    assert result.message == "latest publisher client heartbeat is stale"
