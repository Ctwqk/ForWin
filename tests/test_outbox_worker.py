from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace

from forwin.outbox import store as outbox_store


ROOT = Path(__file__).resolve().parents[1]


class _AddOnlySession:
    def __init__(self) -> None:
        self.added: list[object] = []

    def add(self, row: object) -> None:
        self.added.append(row)


def test_enqueue_outbox_event_serializes_payload() -> None:
    session = _AddOnlySession()

    event = outbox_store.enqueue_outbox_event(
        session,
        aggregate_type="project",
        aggregate_id="project-1",
        event_type="knowledge.rebuild.requested",
        payload={"chapter": 3},
    )

    assert session.added == [event]
    assert event.status == "pending"
    assert event.attempts == 0
    assert event.event_id
    assert json.loads(event.payload_json) == {"chapter": 3}


def test_outbox_worker_cli_help_exposes_fenced_lease_controls() -> None:
    result = subprocess.run(
        [sys.executable, "-m", "forwin.cli", "outbox-worker", "--help"],
        check=False,
        capture_output=True,
        text=True,
    )

    assert result.returncode == 0, result.stderr
    assert "--once" in result.stdout
    assert "--worker-id" in result.stdout
    assert "--lease-seconds" in result.stdout
    assert "--heartbeat-interval-seconds" in result.stdout
    assert "--base-delay-seconds" in result.stdout
    assert "--max-delay-seconds" in result.stdout
    assert "--max-attempts" not in result.stdout
    assert "--retry-delay-seconds" not in result.stdout


def test_outbox_compose_uses_lease_and_bounded_backoff_configuration() -> None:
    compose = (ROOT / "docker-compose.yml").read_text(encoding="utf-8")
    outbox_service = compose.split("  outbox-worker:", 1)[1].split(
        "\n  postgres:", 1
    )[0]

    for option, environment_name in (
        ("--lease-seconds", "FORWIN_OUTBOX_WORKER_LEASE_SECONDS"),
        (
            "--heartbeat-interval-seconds",
            "FORWIN_OUTBOX_WORKER_HEARTBEAT_INTERVAL_SECONDS",
        ),
        ("--base-delay-seconds", "FORWIN_OUTBOX_WORKER_BASE_DELAY_SECONDS"),
        ("--max-delay-seconds", "FORWIN_OUTBOX_WORKER_MAX_DELAY_SECONDS"),
    ):
        assert option in outbox_service
        assert environment_name in outbox_service

    assert "FORWIN_OUTBOX_WORKER_MAX_ATTEMPTS" not in compose
    assert "--max-attempts" not in outbox_service
    assert "--retry-delay-seconds" not in outbox_service


def test_cli_forwards_fenced_worker_configuration(monkeypatch) -> None:
    from forwin import cli

    captured: dict[str, object] = {}

    class _Runtime:
        session_factory = "session-factory"
        handlers = {"event": lambda _claim: None}

        def close(self) -> None:
            captured["closed"] = True

    monkeypatch.setattr(
        "forwin.runtime.workers.build_outbox_worker_runtime",
        lambda _config: _Runtime(),
    )

    def run_loop(**kwargs):
        captured.update(kwargs)
        return 0

    monkeypatch.setattr("forwin.outbox.worker.run_outbox_worker_loop", run_loop)
    monkeypatch.setattr(
        cli,
        "_get_config",
        lambda _args: SimpleNamespace(database_url="postgresql://unused"),
    )
    args = SimpleNamespace(
        worker_id="worker-1",
        poll_interval=2.5,
        once=True,
        lease_seconds=60.0,
        heartbeat_interval_seconds=15.0,
        base_delay_seconds=10.0,
        max_delay_seconds=300.0,
    )

    cli.cmd_outbox_worker(args)

    assert captured == {
        "session_factory": "session-factory",
        "worker_id": "worker-1",
        "handlers": {"event": captured["handlers"]["event"]},
        "poll_interval": 2.5,
        "once": True,
        "lease_seconds": 60.0,
        "heartbeat_interval_seconds": 15.0,
        "base_delay_seconds": 10.0,
        "max_delay_seconds": 300.0,
        "closed": True,
    }
