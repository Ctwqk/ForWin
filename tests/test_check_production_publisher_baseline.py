from __future__ import annotations

import json
from types import SimpleNamespace

import scripts.check_production_publisher_baseline as baseline


def test_classify_platform_connected_when_api_and_page_agree() -> None:
    result = baseline.classify_platform(
        {"platform_id": "qidian", "connected": True, "preferred_connected": True},
        {
            "platform_id": "qidian",
            "ok": True,
            "dashboard_visible": True,
            "login_visible": False,
            "final_url": "https://write.qq.com/portal/dashboard",
            "title": "工作台-阅文作家专区",
        },
    )

    assert result["status"] == "connected"
    assert result["connected"] is True


def test_classify_platform_login_page_as_human_login_required() -> None:
    result = baseline.classify_platform(
        {"platform_id": "fanqie", "connected": False, "preferred_connected": False},
        {
            "platform_id": "fanqie",
            "ok": True,
            "dashboard_visible": False,
            "login_visible": True,
            "final_url": "https://fanqienovel.com/main/writer/login",
            "title": "作家专区-番茄小说网-番茄小说旗下原创文学平台",
        },
    )

    assert result["status"] == "human_login_required"
    assert result["blocked_item"] == {
        "kind": "publisher_login_required",
        "platform": "fanqie",
        "current_url": "https://fanqienovel.com/main/writer/login",
        "page_state": "login_visible",
        "human_action": "Log in to fanqie in the production publisher browser profile, then rerun the baseline verifier.",
    }


def test_classify_platform_dashboard_api_mismatch() -> None:
    result = baseline.classify_platform(
        {"platform_id": "qidian", "connected": False, "preferred_connected": False},
        {
            "platform_id": "qidian",
            "ok": True,
            "dashboard_visible": True,
            "login_visible": False,
            "final_url": "https://write.qq.com/portal/dashboard",
            "title": "工作台-阅文作家专区",
        },
    )

    assert result["status"] == "state_sync_mismatch"
    assert result["connected"] is False


def test_browser_failure_classifies_platform_browser_unreachable() -> None:
    result = baseline.classify_platform(
        {"platform_id": "fanqie", "connected": False, "preferred_connected": False},
        {"platform_id": "fanqie", "ok": False, "error": "cdp unavailable"},
    )

    assert result["status"] == "browser_unreachable"
    assert result["connected"] is False


def test_page_evidence_accepts_dashboard_after_navigation_timeout() -> None:
    result = baseline.classify_page_evidence(
        platform="qidian",
        final_url="https://write.qq.com/portal/dashboard",
        title="工作台-阅文作家专区",
        text="工作台\n作品管理\n数据中心",
        navigation_error="TimeoutError: Page.goto timeout",
    )

    assert result["ok"] is True
    assert result["dashboard_visible"] is True
    assert result["login_visible"] is False
    assert result["navigation_error"] == "TimeoutError: Page.goto timeout"


def test_rollup_status_marks_degraded_for_human_login_required() -> None:
    assert (
        baseline.rollup_status(
            {
                "services": {"ok": True},
                "api_health": {"ok": True},
                "mcp_health": {"ok": True},
                "discord_env": {"ok": True},
            },
            [{"platform_id": "fanqie", "status": "human_login_required"}],
        )
        == "degraded"
    )


def test_rollup_status_fails_for_discord_env_enabled() -> None:
    assert (
        baseline.rollup_status(
            {
                "services": {"ok": True},
                "api_health": {"ok": True},
                "mcp_health": {"ok": True},
                "discord_env": {"ok": False},
            },
            [{"platform_id": "qidian", "status": "connected"}],
        )
        == "failed"
    )


def test_baseline_output_is_redacted(monkeypatch) -> None:
    monkeypatch.setattr(baseline, "utc_now", lambda: "2026-06-30T12:00:00Z")
    monkeypatch.setattr(
        baseline,
        "docker_services_snapshot",
        lambda context, colima_profile="": {"ok": True, "services": []},
        raising=False,
    )
    monkeypatch.setattr(
        baseline,
        "discord_login_webhook_env_snapshot",
        lambda services, docker_context: {"ok": True, "configured": []},
        raising=False,
    )
    monkeypatch.setattr(
        baseline,
        "http_json",
        lambda url: {"ok": True, "payload": {"status": "ok"}},
        raising=False,
    )
    monkeypatch.setattr(
        baseline,
        "publisher_platforms_snapshot",
        lambda api_base, expected: {
            "ok": True,
            "platforms": [
                {"platform_id": "qidian", "connected": True, "preferred_connected": True}
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(
        baseline,
        "publisher_browser_container_snapshot",
        lambda args: {"ok": True, "container_id": "container-1"},
        raising=False,
    )
    monkeypatch.setattr(
        baseline,
        "browser_pages_snapshot",
        lambda args: {
            "ok": True,
            "pages": {
                "qidian": {
                    "platform_id": "qidian",
                    "ok": True,
                    "dashboard_visible": True,
                    "login_visible": False,
                    "final_url": "https://write.qq.com/portal/dashboard",
                    "title": "工作台-阅文作家专区",
                    "cookies": [{"value": "secret-cookie"}],
                }
            },
        },
        raising=False,
    )

    result = baseline.build_baseline(
        SimpleNamespace(
            api_base="http://127.0.0.1:8899",
            mcp_health_url="http://127.0.0.1:8896/health",
            docker_context="swarm-manager-150",
            colima_profile="swarmbridged",
            expect_platform_connected=["qidian"],
            skip_browser=False,
            wait_heartbeat_seconds=0,
        )
    )

    serialized = json.dumps(result, ensure_ascii=False)
    assert "secret-cookie" not in serialized
    assert result["status"] == "ok"


def test_build_baseline_rereads_api_after_state_sync_mismatch(monkeypatch) -> None:
    platform_calls: list[int] = []
    sleeps: list[float] = []

    monkeypatch.setattr(baseline, "utc_now", lambda: "2026-06-30T12:00:00Z")
    monkeypatch.setattr(
        baseline,
        "docker_services_snapshot",
        lambda context, colima_profile="": {"ok": True, "services": []},
    )
    monkeypatch.setattr(
        baseline,
        "discord_login_webhook_env_snapshot",
        lambda services, docker_context: {"ok": True, "configured": []},
    )
    monkeypatch.setattr(
        baseline,
        "http_json",
        lambda url: {"ok": True, "payload": {"status": "ok"}},
    )

    def fake_platforms(api_base, expected):
        platform_calls.append(len(platform_calls))
        connected = len(platform_calls) > 1
        return {
            "ok": connected,
            "missing_expected": [] if connected else ["qidian"],
            "platforms": [
                {
                    "platform_id": "qidian",
                    "connected": connected,
                    "preferred_connected": connected,
                }
            ],
        }

    monkeypatch.setattr(baseline, "publisher_platforms_snapshot", fake_platforms)
    monkeypatch.setattr(
        baseline,
        "publisher_browser_container_snapshot",
        lambda args: {"ok": True, "container_id": "container-1"},
    )
    monkeypatch.setattr(
        baseline,
        "browser_pages_snapshot",
        lambda args: {
            "ok": True,
            "pages": {
                "qidian": {
                    "platform_id": "qidian",
                    "ok": True,
                    "dashboard_visible": True,
                    "login_visible": False,
                    "final_url": "https://write.qq.com/portal/dashboard",
                    "title": "工作台-阅文作家专区",
                }
            },
        },
    )
    monkeypatch.setattr(baseline.time, "sleep", lambda seconds: sleeps.append(seconds))

    result = baseline.build_baseline(
        SimpleNamespace(
            api_base="http://127.0.0.1:8899",
            mcp_health_url="http://127.0.0.1:8896/health",
            docker_context="swarm-manager-150",
            colima_profile="swarmbridged",
            expect_platform_connected=["qidian"],
            skip_browser=False,
            wait_heartbeat_seconds=3,
        )
    )

    assert len(platform_calls) == 2
    assert sleeps == [3.0]
    assert result["platforms"][0]["status"] == "connected"
    assert result["status"] == "ok"
