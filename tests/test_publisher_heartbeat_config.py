from __future__ import annotations

from datetime import datetime, timedelta, timezone
import os
from types import SimpleNamespace

import pytest

from forwin.config import InfrastructureConfig
from forwin.http.runtime import HttpRuntime
from forwin.publishers.manager import PublisherManager
from forwin.runtime.container import RuntimeContainer
from forwin.runtime.policy import RuntimePolicy
from scripts import check_publisher_browser_heartbeat as browser_check


@pytest.fixture
def config(monkeypatch, tmp_path):
    monkeypatch.setattr(os, "environ", {
        "FORWIN_ENV_FILE": str(tmp_path / "no-env-file"),
        "FORWIN_PUBLISHER_HEARTBEAT_STALE_SECONDS": "300",
        "FORWIN_PUBLISHER_HEARTBEAT_MONITOR_INTERVAL_SECONDS": "45",
        "FORWIN_PUBLISHER_HEARTBEAT_MONITOR_FAILURES": "4",
    })
    return InfrastructureConfig.from_env()


def test_heartbeat_monitor_settings_are_loaded(config):
    assert config.publisher_heartbeat_monitor_interval_seconds == 45
    assert config.publisher_heartbeat_monitor_failures == 4


@pytest.mark.parametrize("path", ["container", "http"])
def test_backend_and_browser_share_configured_heartbeat_window(config, monkeypatch, path):
    def no_database():
        raise AssertionError("heartbeat construction must not open a database")

    if path == "container":
        container = RuntimeContainer.for_publisher_worker(
            config, policy=RuntimePolicy.for_profile("standard")
        )
        runtime = container._build_publisher_runtime(SimpleNamespace(
            infrastructure=config, session_factory=no_database, observability=None,
        ))
    else:
        # Startup recovery requires a database; exercise the real manager/runtime constructors.
        monkeypatch.setattr(PublisherManager, "recover_interrupted_upload_attempts", lambda self: None)
        runtime = HttpRuntime(config=config, session_factory=no_database).get_publisher_manager().runtime

    monkeypatch.setattr("sys.argv", ["check_publisher_browser_heartbeat.py", "--wait-seconds", "0"])
    browser_args = browser_check.parse_args()
    assert runtime.connection_state.heartbeat_stale_seconds == browser_args.stale_seconds == 300
    assert runtime.attempts.default_lease_seconds == 300
    now = datetime.now(timezone.utc)
    assert runtime.connection_state.is_recent(now - timedelta(seconds=200))
    assert not runtime.connection_state.is_recent(now - timedelta(seconds=310))
