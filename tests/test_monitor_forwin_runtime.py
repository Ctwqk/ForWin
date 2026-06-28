from __future__ import annotations

from scripts.monitor_forwin_runtime import (
    docker_services_snapshot,
    parse_replicas,
    publisher_platforms_snapshot,
    redact_sensitive,
)


def test_parse_replicas_requires_desired_replicas_running() -> None:
    assert parse_replicas("1/1") == (1, 1)
    assert parse_replicas("2/1") == (2, 1)
    assert parse_replicas("0/1") == (0, 1)
    assert parse_replicas("n/a") is None


def test_docker_services_snapshot_requires_runtime_swarm_services(monkeypatch) -> None:
    def fake_run_command(args, **kwargs):
        assert args == ["docker", "--context", "swarm-manager-150", "service", "ls", "--filter", "name=forwin"]
        return {
            "ok": True,
            "stdout": "\n".join(
                [
                    "ID NAME MODE REPLICAS IMAGE PORTS",
                    "aaa forwin-app-swarm replicated 1/1 forwin-forwin:deploy-abc *:8899->8899/tcp",
                    "bbb forwin-generation-worker-swarm replicated 1/1 forwin-forwin:deploy-abc",
                    "ccc forwin-mcp-swarm replicated 1/1 forwin-forwin:deploy-abc *:8896->8896/tcp",
                    "ddd forwin-outbox-worker-swarm replicated 1/1 forwin-forwin:deploy-abc",
                ]
            ),
            "stderr": "",
        }

    monkeypatch.setattr("scripts.monitor_forwin_runtime.run_command", fake_run_command)

    snapshot = docker_services_snapshot("swarm-manager-150")

    assert snapshot["ok"] is True
    assert snapshot["missing"] == []
    assert {service["name"] for service in snapshot["services"]} == {
        "forwin-app-swarm",
        "forwin-generation-worker-swarm",
        "forwin-mcp-swarm",
        "forwin-outbox-worker-swarm",
    }


def test_docker_services_snapshot_reports_missing_required_service(monkeypatch) -> None:
    def fake_run_command(args, **kwargs):
        return {
            "ok": True,
            "stdout": "\n".join(
                [
                    "ID NAME MODE REPLICAS IMAGE",
                    "aaa forwin-app-swarm replicated 1/1 forwin-forwin:deploy-abc",
                    "bbb forwin-generation-worker-swarm replicated 0/1 forwin-forwin:deploy-abc",
                    "ccc forwin-mcp-swarm replicated 1/1 forwin-forwin:deploy-abc",
                    "ddd forwin-outbox-worker-swarm replicated 1/1 forwin-forwin:deploy-abc",
                ]
            ),
            "stderr": "",
        }

    monkeypatch.setattr("scripts.monitor_forwin_runtime.run_command", fake_run_command)

    snapshot = docker_services_snapshot("swarm-manager-150")

    assert snapshot["ok"] is False
    assert snapshot["missing"] == ["forwin-generation-worker-swarm"]


def test_docker_services_snapshot_uses_colima_fallback_when_context_fails(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_command(args, **kwargs):
        calls.append(tuple(args))
        if args[:3] == ["docker", "--context", "swarm-manager-150"]:
            return {"ok": False, "stderr": "context unavailable", "stdout": ""}
        if args[:4] == ["colima", "ssh", "-p", "swarmbridged"]:
            return {
                "ok": True,
                "stdout": "\n".join(
                    [
                        "forwin-app-swarm.1.aaa image Up 5 hours (healthy)",
                        "forwin-generation-worker-swarm.1.bbb image Up 5 hours",
                        "forwin-mcp-swarm.1.ccc image Up 5 hours (healthy)",
                        "forwin-outbox-worker-swarm.1.ddd image Up 5 hours",
                    ]
                ),
                "stderr": "",
            }
        raise AssertionError(args)

    monkeypatch.setattr("scripts.monitor_forwin_runtime.run_command", fake_run_command)

    snapshot = docker_services_snapshot("swarm-manager-150", colima_profile="swarmbridged")

    assert snapshot["ok"] is True
    assert snapshot["source"] == "colima:swarmbridged"
    assert snapshot["missing"] == []
    assert ("colima", "ssh", "-p", "swarmbridged", "--", "docker", "ps", "--format", "{{.Names}} {{.Image}} {{.Status}}") in calls


def test_publisher_platforms_snapshot_requires_expected_connected_platforms(monkeypatch) -> None:
    def fake_http_json(url: str, *, timeout: float = 5.0):
        assert url == "http://127.0.0.1:8899/api/publishers/platforms"
        return {
            "ok": True,
            "payload": [
                {
                    "platform_id": "fanqie",
                    "connected": False,
                    "preferred_client_state": {"connected": False},
                    "latest_client_state": {"connected": True},
                    "browser_session_state": {"connected": True},
                    "last_heartbeat_at": "2026-06-27T20:00:00Z",
                },
                {
                    "platform_id": "qidian",
                    "connected": True,
                    "preferred_client_state": {"connected": True},
                    "latest_client_state": {"connected": True},
                    "browser_session_state": {"connected": True},
                    "last_heartbeat_at": "2026-06-27T20:00:00Z",
                },
            ],
        }

    monkeypatch.setattr("scripts.monitor_forwin_runtime.http_json", fake_http_json)

    snapshot = publisher_platforms_snapshot("http://127.0.0.1:8899", {"fanqie", "qidian"})

    assert snapshot["ok"] is False
    assert snapshot["missing_expected"] == ["fanqie"]
    assert snapshot["platforms"][0] == {
        "platform_id": "fanqie",
        "connected": False,
        "preferred_connected": False,
        "latest_connected": True,
        "session_connected": True,
        "last_heartbeat_at": "2026-06-27T20:00:00Z",
    }


def test_redact_sensitive_recursively_removes_secret_material() -> None:
    payload = {
        "ok": True,
        "token": "abc",
        "nested": {
            "FORWIN_PUBLISHER_EXTENSION_API_KEY": "secret",
            "cookies": [{"name": "sid", "value": "cookie-value"}],
            "browser_session_state": {"connected": True, "cookie_count": 4},
            "safe": "visible",
        },
        "items": ["plain", {"session_secret": "hidden"}, {"session_connected": True}],
    }

    assert redact_sensitive(payload) == {
        "ok": True,
        "token": "[redacted]",
        "nested": {
            "FORWIN_PUBLISHER_EXTENSION_API_KEY": "[redacted]",
            "cookies": "[redacted]",
            "browser_session_state": {"connected": True, "cookie_count": 4},
            "safe": "visible",
        },
        "items": ["plain", {"session_secret": "[redacted]"}, {"session_connected": True}],
    }
