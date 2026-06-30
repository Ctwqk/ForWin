# Production Publisher Baseline Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Build a repeatable, redacted production baseline verifier for ForWin services and shared publisher-browser login state.

**Architecture:** Extend existing monitor helpers where they already match the problem, then add a focused `scripts/check_production_publisher_baseline.py` entrypoint for production one-shot checks. The verifier composes Swarm service state, app/MCP health, publisher API summaries, Discord webhook env checks, production browser page evidence, and heartbeat convergence into one JSON result.

**Tech Stack:** Python 3.13, standard library `argparse`/`json`/`subprocess`/`urllib`, Playwright inside the publisher-browser container, pytest, existing `scripts.monitor_forwin_runtime` helpers.

---

## File Structure

- Modify `scripts/monitor_forwin_runtime.py`
  - Add `forwin-publisher-browser-swarm` to required service checks.
  - Add a helper that inspects service environment for disabled Discord login webhook settings.
- Modify `tests/test_monitor_forwin_runtime.py`
  - Update service snapshot tests for the publisher browser service.
  - Add tests for Discord webhook env detection.
- Create `scripts/check_production_publisher_baseline.py`
  - One-shot production baseline verifier.
  - Owns page-evidence classification and heartbeat convergence.
  - Emits redacted JSON and exit code.
- Create `tests/test_check_production_publisher_baseline.py`
  - Unit tests for redaction, classification, status rollup, and failure isolation.
- Modify `README.md`
  - Link to the production baseline verifier from the production deployment section.
- Modify `docs/operations/forwin-production-processes.md`
  - Add operator command, output meaning, and human-login rerun path.

## Task 1: Extend Runtime Monitor Service Coverage

**Files:**
- Modify: `scripts/monitor_forwin_runtime.py`
- Modify: `tests/test_monitor_forwin_runtime.py`

- [ ] **Step 1: Update failing service tests for publisher browser**

Change `tests/test_monitor_forwin_runtime.py::test_docker_services_snapshot_requires_runtime_swarm_services` so the fake Docker output includes `forwin-publisher-browser-swarm`, and assert the service is required:

```python
"fff forwin-publisher-browser-swarm replicated 1/1 forwin-publisher-browser:deploy-abc",
```

Expected set:

```python
assert {service["name"] for service in snapshot["services"]} == {
    "forwin-app-swarm",
    "forwin-generation-worker-swarm",
    "forwin-mcp-swarm",
    "forwin-publisher-worker-swarm",
    "forwin-outbox-worker-swarm",
    "forwin-publisher-browser-swarm",
}
```

- [ ] **Step 2: Add failing test for missing publisher browser**

Add this test to `tests/test_monitor_forwin_runtime.py`:

```python
def test_docker_services_snapshot_requires_publisher_browser(monkeypatch) -> None:
    def fake_run_command(args, **kwargs):
        return {
            "ok": True,
            "stdout": "\n".join(
                [
                    "ID NAME MODE REPLICAS IMAGE",
                    "aaa forwin-app-swarm replicated 1/1 forwin-forwin:deploy-abc",
                    "bbb forwin-generation-worker-swarm replicated 1/1 forwin-forwin:deploy-abc",
                    "ccc forwin-mcp-swarm replicated 1/1 forwin-forwin:deploy-abc",
                    "ddd forwin-publisher-worker-swarm replicated 1/1 forwin-forwin:deploy-abc",
                    "eee forwin-outbox-worker-swarm replicated 1/1 forwin-forwin:deploy-abc",
                ]
            ),
            "stderr": "",
        }

    monkeypatch.setattr("scripts.monitor_forwin_runtime.run_command", fake_run_command)

    snapshot = docker_services_snapshot("swarm-manager-150")

    assert snapshot["ok"] is False
    assert snapshot["missing"] == ["forwin-publisher-browser-swarm"]
```

- [ ] **Step 3: Run the focused test and verify it fails**

Run:

```bash
.venv/bin/pytest tests/test_monitor_forwin_runtime.py::test_docker_services_snapshot_requires_publisher_browser -q
```

Expected: `FAILED` because `forwin-publisher-browser-swarm` is not yet in the required service set.

- [ ] **Step 4: Update `docker_services_snapshot` required set**

In `scripts/monitor_forwin_runtime.py`, change the required set to:

```python
required = {
    "forwin-app-swarm",
    "forwin-generation-worker-swarm",
    "forwin-mcp-swarm",
    "forwin-publisher-worker-swarm",
    "forwin-outbox-worker-swarm",
    "forwin-publisher-browser-swarm",
}
```

- [ ] **Step 5: Update Colima fallback test data**

In `tests/test_monitor_forwin_runtime.py::test_docker_services_snapshot_uses_colima_fallback_when_context_fails`, add:

```python
"forwin-publisher-browser-swarm.1.fff image Up 5 hours (healthy)",
```

- [ ] **Step 6: Run monitor tests**

Run:

```bash
.venv/bin/pytest tests/test_monitor_forwin_runtime.py -q
```

Expected: all tests pass.

- [ ] **Step 7: Commit service coverage**

Run:

```bash
git add scripts/monitor_forwin_runtime.py tests/test_monitor_forwin_runtime.py
git commit -m "Require publisher browser in runtime monitor"
```

## Task 2: Add Discord Login Webhook Env Detection

**Files:**
- Modify: `scripts/monitor_forwin_runtime.py`
- Modify: `tests/test_monitor_forwin_runtime.py`

- [ ] **Step 1: Add failing env checker tests**

Add these imports to `tests/test_monitor_forwin_runtime.py`:

```python
from scripts.monitor_forwin_runtime import discord_login_webhook_env_snapshot
```

Add the passing disabled-state test:

```python
def test_discord_login_webhook_env_snapshot_passes_when_unset(monkeypatch) -> None:
    calls: list[tuple[str, ...]] = []

    def fake_run_command(args, **kwargs):
        calls.append(tuple(args))
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr("scripts.monitor_forwin_runtime.run_command", fake_run_command)

    snapshot = discord_login_webhook_env_snapshot(
        ["forwin-app-swarm", "forwin-publisher-browser-swarm"],
        docker_context="swarm-manager-150",
    )

    assert snapshot == {
        "ok": True,
        "source": "docker-context:swarm-manager-150",
        "configured": [],
    }
    assert len(calls) == 2
```

Add the failing enabled-state test:

```python
def test_discord_login_webhook_env_snapshot_fails_when_set(monkeypatch) -> None:
    def fake_run_command(args, **kwargs):
        service_name = args[-1]
        if service_name == "forwin-app-swarm":
            return {
                "ok": True,
                "stdout": "FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE=SET\n",
                "stderr": "",
            }
        return {"ok": True, "stdout": "", "stderr": ""}

    monkeypatch.setattr("scripts.monitor_forwin_runtime.run_command", fake_run_command)

    snapshot = discord_login_webhook_env_snapshot(
        ["forwin-app-swarm", "forwin-publisher-browser-swarm"],
        docker_context="swarm-manager-150",
    )

    assert snapshot["ok"] is False
    assert snapshot["configured"] == [
        {
            "service": "forwin-app-swarm",
            "env": "FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE",
        }
    ]
```

- [ ] **Step 2: Run the new tests and verify they fail**

Run:

```bash
.venv/bin/pytest tests/test_monitor_forwin_runtime.py -q
```

Expected: import failure for `discord_login_webhook_env_snapshot`.

- [ ] **Step 3: Implement `discord_login_webhook_env_snapshot`**

Add this function to `scripts/monitor_forwin_runtime.py` after `docker_services_snapshot`:

```python
def discord_login_webhook_env_snapshot(
    service_names: list[str],
    *,
    docker_context: str,
) -> dict[str, Any]:
    configured: list[dict[str, str]] = []
    errors: list[dict[str, str]] = []
    for service_name in service_names:
        proc = run_command(
            [
                "docker",
                "--context",
                docker_context,
                "service",
                "inspect",
                service_name,
                "--format",
                "{{range .Spec.TaskTemplate.ContainerSpec.Env}}{{println .}}{{end}}",
            ],
            timeout=15,
        )
        if not proc.get("ok"):
            errors.append(
                {
                    "service": service_name,
                    "error": str(proc.get("stderr") or proc.get("error") or proc.get("stdout") or "inspect failed")[:320],
                }
            )
            continue
        for line in str(proc.get("stdout") or "").splitlines():
            name = line.split("=", 1)[0]
            if name in {
                "FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_URL",
                "FORWIN_PUBLISHER_LOGIN_DISCORD_WEBHOOK_FILE",
            }:
                configured.append({"service": service_name, "env": name})
    return {
        "ok": not configured and not errors,
        "source": f"docker-context:{docker_context}",
        "configured": configured,
        "errors": errors,
    }
```

- [ ] **Step 4: Run env checker tests**

Run:

```bash
.venv/bin/pytest tests/test_monitor_forwin_runtime.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit env checker**

Run:

```bash
git add scripts/monitor_forwin_runtime.py tests/test_monitor_forwin_runtime.py
git commit -m "Detect publisher Discord login webhook env"
```

## Task 3: Add Production Baseline Verifier Tests

**Files:**
- Create: `tests/test_check_production_publisher_baseline.py`
- Create: `scripts/check_production_publisher_baseline.py`

- [ ] **Step 1: Create a minimal script module for imports**

Create `scripts/check_production_publisher_baseline.py` with:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from typing import Any


def classify_platform(api_state: dict[str, Any], page_state: dict[str, Any]) -> dict[str, Any]:
    return {"platform_id": api_state.get("platform_id") or page_state.get("platform_id") or "", "status": "unknown"}


def rollup_status(checks: dict[str, Any], platforms: list[dict[str, Any]]) -> str:
    return "unknown"


def build_baseline(args: argparse.Namespace) -> dict[str, Any]:
    return {"status": "unknown", "checked_at": "", "platforms": []}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ForWin production publisher baseline verifier.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    result = build_baseline(parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 2: Add classifier tests**

Create `tests/test_check_production_publisher_baseline.py`:

```python
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


def test_rollup_status_marks_degraded_for_human_login_required() -> None:
    assert baseline.rollup_status(
        {"services": {"ok": True}, "api_health": {"ok": True}, "mcp_health": {"ok": True}, "discord_env": {"ok": True}},
        [{"platform_id": "fanqie", "status": "human_login_required"}],
    ) == "degraded"


def test_rollup_status_fails_for_discord_env_enabled() -> None:
    assert baseline.rollup_status(
        {"services": {"ok": True}, "api_health": {"ok": True}, "mcp_health": {"ok": True}, "discord_env": {"ok": False}},
        [{"platform_id": "qidian", "status": "connected"}],
    ) == "failed"


def test_baseline_output_is_redacted(monkeypatch) -> None:
    monkeypatch.setattr(baseline, "utc_now", lambda: "2026-06-30T12:00:00Z")
    monkeypatch.setattr(baseline, "docker_services_snapshot", lambda context, colima_profile="": {"ok": True, "services": []})
    monkeypatch.setattr(baseline, "discord_login_webhook_env_snapshot", lambda services, docker_context: {"ok": True, "configured": []})
    monkeypatch.setattr(baseline, "http_json", lambda url: {"ok": True, "payload": {"status": "ok"}})
    monkeypatch.setattr(
        baseline,
        "publisher_platforms_snapshot",
        lambda api_base, expected: {
            "ok": True,
            "platforms": [{"platform_id": "qidian", "connected": True, "preferred_connected": True}],
        },
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
```

- [ ] **Step 3: Run new tests and verify failures**

Run:

```bash
.venv/bin/pytest tests/test_check_production_publisher_baseline.py -q
```

Expected: classifier and rollup tests fail because the functions still return stub unknown values.

## Task 4: Implement Baseline Classifiers and Rollup

**Files:**
- Modify: `scripts/check_production_publisher_baseline.py`

- [ ] **Step 1: Add imports from monitor helpers**

Replace imports in `scripts/check_production_publisher_baseline.py` with:

```python
#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from typing import Any

from scripts.monitor_forwin_runtime import (
    discord_login_webhook_env_snapshot,
    docker_services_snapshot,
    http_json,
    publisher_platforms_snapshot,
    redact_sensitive,
    utc_now,
)
```

- [ ] **Step 2: Implement `classify_platform`**

Replace `classify_platform` with:

```python
def classify_platform(api_state: dict[str, Any], page_state: dict[str, Any]) -> dict[str, Any]:
    platform_id = str(api_state.get("platform_id") or page_state.get("platform_id") or "")
    final_url = str(page_state.get("final_url") or "")
    login_visible = bool(page_state.get("login_visible"))
    dashboard_visible = bool(page_state.get("dashboard_visible"))
    api_connected = bool(api_state.get("connected"))
    preferred_connected = bool(api_state.get("preferred_connected"))
    base = {
        "platform_id": platform_id,
        "api_connected": api_connected,
        "preferred_connected": preferred_connected,
        "page": {
            "ok": bool(page_state.get("ok")),
            "final_url": final_url,
            "title": str(page_state.get("title") or ""),
            "dashboard_visible": dashboard_visible,
            "login_visible": login_visible,
        },
    }
    if login_visible:
        base.update(
            {
                "status": "human_login_required",
                "connected": False,
                "blocked_item": {
                    "kind": "publisher_login_required",
                    "platform": platform_id,
                    "current_url": final_url,
                    "page_state": "login_visible",
                    "human_action": f"Log in to {platform_id} in the production publisher browser profile, then rerun the baseline verifier.",
                },
            }
        )
        return base
    if api_connected and preferred_connected and dashboard_visible:
        base.update({"status": "connected", "connected": True})
        return base
    if dashboard_visible and not api_connected:
        base.update({"status": "state_sync_mismatch", "connected": False})
        return base
    if not page_state.get("ok"):
        base.update({"status": "browser_unreachable", "connected": False})
        return base
    base.update({"status": "unknown", "connected": False})
    return base
```

- [ ] **Step 3: Implement `rollup_status`**

Replace `rollup_status` with:

```python
def rollup_status(checks: dict[str, Any], platforms: list[dict[str, Any]]) -> str:
    required = ("services", "api_health", "mcp_health", "discord_env")
    if any(not bool(checks.get(name, {}).get("ok")) for name in required):
        return "failed"
    platform_statuses = {str(item.get("status") or "") for item in platforms}
    if platform_statuses and platform_statuses <= {"connected"}:
        return "ok"
    if platform_statuses & {"human_login_required", "state_sync_mismatch", "unknown", "browser_unreachable"}:
        return "degraded"
    return "failed"
```

- [ ] **Step 4: Run classifier tests**

Run:

```bash
.venv/bin/pytest tests/test_check_production_publisher_baseline.py::test_classify_platform_connected_when_api_and_page_agree tests/test_check_production_publisher_baseline.py::test_classify_platform_login_page_as_human_login_required tests/test_check_production_publisher_baseline.py::test_classify_platform_dashboard_api_mismatch tests/test_check_production_publisher_baseline.py::test_rollup_status_marks_degraded_for_human_login_required tests/test_check_production_publisher_baseline.py::test_rollup_status_fails_for_discord_env_enabled -q
```

Expected: all selected tests pass.

## Task 5: Implement Browser Page Evidence Collection

**Files:**
- Modify: `scripts/check_production_publisher_baseline.py`
- Modify: `tests/test_check_production_publisher_baseline.py`

- [ ] **Step 1: Add a test that browser inspection failures are isolated**

Add this test:

```python
def test_browser_failure_classifies_platform_browser_unreachable() -> None:
    result = baseline.classify_platform(
        {"platform_id": "fanqie", "connected": False, "preferred_connected": False},
        {"platform_id": "fanqie", "ok": False, "error": "cdp unavailable"},
    )

    assert result["status"] == "browser_unreachable"
    assert result["connected"] is False
```

- [ ] **Step 2: Add browser helper constants**

Add these constants near the top of `scripts/check_production_publisher_baseline.py`:

```python
REQUIRED_SERVICES = [
    "forwin-app-swarm",
    "forwin-generation-worker-swarm",
    "forwin-mcp-swarm",
    "forwin-publisher-worker-swarm",
    "forwin-outbox-worker-swarm",
    "forwin-publisher-browser-swarm",
]

PLATFORM_URLS = {
    "qidian": "https://write.qq.com/portal/dashboard",
    "fanqie": "https://fanqienovel.com/main/writer/",
}
```

- [ ] **Step 3: Implement `browser_pages_snapshot`**

Add this function:

```python
def browser_pages_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    if bool(getattr(args, "skip_browser", False)):
        return {"ok": True, "skipped": True, "pages": {}}
    script = r'''
from playwright.sync_api import sync_playwright
import json
urls = {
    "qidian": "https://write.qq.com/portal/dashboard",
    "fanqie": "https://fanqienovel.com/main/writer/",
}
results = {}
with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    try:
        ctx = browser.contexts[0]
        for platform, url in urls.items():
            page = ctx.new_page()
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(5000)
                text = page.locator("body").inner_text(timeout=5000)[:1200]
                final_url = page.url
                title = page.title()
                login_visible = (
                    "/login" in final_url
                    or "扫码登录" in text
                    or "验证码登录" in text
                    or "登录/注册" in text
                    or "密码登录" in text
                )
                dashboard_visible = (
                    not login_visible
                    and (
                        "工作台" in text
                        or "作品管理" in text
                        or "新建作品" in text
                        or "稿酬" in text
                    )
                )
                results[platform] = {
                    "platform_id": platform,
                    "ok": True,
                    "final_url": final_url,
                    "title": title,
                    "login_visible": login_visible,
                    "dashboard_visible": dashboard_visible,
                }
            except Exception as exc:
                results[platform] = {
                    "platform_id": platform,
                    "ok": False,
                    "error": f"{type(exc).__name__}: {str(exc)[:240]}",
                }
    finally:
        browser.close()
print(json.dumps({"ok": True, "pages": results}, ensure_ascii=False))
'''
    from scripts.monitor_forwin_runtime import run_command

    proc = run_command(
        [
            "colima",
            "ssh",
            "-p",
            str(getattr(args, "colima_profile", "swarmbridged")),
            "--",
            "docker",
            "exec",
            "-i",
            str(getattr(args, "publisher_browser_container", "")) or "forwin-publisher-browser-swarm",
            "python",
            "-c",
            script,
        ],
        timeout=90,
    )
    if not proc.get("ok"):
        return {"ok": False, "error": str(proc.get("stderr") or proc.get("error") or proc.get("stdout") or "browser inspection failed")[:500], "pages": {}}
    try:
        payload = json.loads(str(proc.get("stdout") or "{}"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid browser JSON: {exc}", "pages": {}}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "browser returned non-object JSON", "pages": {}}
```

- [ ] **Step 4: Add container discovery before browser exec**

If production container names cannot be used directly with `docker exec`, add:

```python
def publisher_browser_container_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    from scripts.monitor_forwin_runtime import run_command

    proc = run_command(
        [
            "colima",
            "ssh",
            "-p",
            str(getattr(args, "colima_profile", "swarmbridged")),
            "--",
            "docker",
            "ps",
            "--format",
            "{{.ID}}\t{{.Names}}\t{{.Image}}\t{{.Status}}",
        ],
        timeout=15,
    )
    if not proc.get("ok"):
        return {"ok": False, "error": str(proc.get("stderr") or proc.get("error") or proc.get("stdout") or "")[:500]}
    for line in str(proc.get("stdout") or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and "forwin-publisher-browser-swarm" in parts[1]:
            return {"ok": True, "container_id": parts[0], "name": parts[1], "image": parts[2], "status": parts[3]}
    return {"ok": False, "error": "forwin-publisher-browser-swarm container not found"}
```

Then have `build_baseline` set `args.publisher_browser_container` from this snapshot before calling `browser_pages_snapshot`.

- [ ] **Step 5: Run browser failure test**

Run:

```bash
.venv/bin/pytest tests/test_check_production_publisher_baseline.py::test_browser_failure_classifies_platform_browser_unreachable -q
```

Expected: pass.

## Task 6: Implement Baseline Build and CLI

**Files:**
- Modify: `scripts/check_production_publisher_baseline.py`
- Modify: `tests/test_check_production_publisher_baseline.py`

- [ ] **Step 1: Implement `build_baseline`**

Replace `build_baseline` with:

```python
def _platform_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("platform_id") or ""): item for item in items if isinstance(item, dict)}


def build_baseline(args: argparse.Namespace) -> dict[str, Any]:
    checked_at = utc_now()
    expected = set(getattr(args, "expect_platform_connected", []) or [])
    services = docker_services_snapshot(args.docker_context, colima_profile=args.colima_profile)
    discord_env = discord_login_webhook_env_snapshot(REQUIRED_SERVICES, docker_context=args.docker_context)
    api_health = http_json(f"{args.api_base.rstrip('/')}/health")
    mcp_health = http_json(args.mcp_health_url)
    platform_api = publisher_platforms_snapshot(args.api_base, expected)
    container = publisher_browser_container_snapshot(args)
    if container.get("ok"):
        setattr(args, "publisher_browser_container", container.get("container_id"))
    browser = browser_pages_snapshot(args) if container.get("ok") else {"ok": False, "error": container.get("error"), "pages": {}}

    api_by_id = _platform_by_id(platform_api.get("platforms", []))
    page_by_id = browser.get("pages", {}) if isinstance(browser.get("pages"), dict) else {}
    platform_ids = sorted(expected or (set(api_by_id) | set(page_by_id)))
    platforms = [
        classify_platform(api_by_id.get(platform_id, {"platform_id": platform_id}), page_by_id.get(platform_id, {"platform_id": platform_id, "ok": False}))
        for platform_id in platform_ids
    ]
    checks = {
        "services": services,
        "api_health": api_health,
        "mcp_health": mcp_health,
        "discord_env": discord_env,
        "publisher_api": platform_api,
        "publisher_browser_container": container,
        "publisher_browser_pages": browser,
    }
    blocked_items = [
        item["blocked_item"]
        for item in platforms
        if isinstance(item.get("blocked_item"), dict)
    ]
    result = {
        "status": rollup_status(checks, platforms),
        "checked_at": checked_at,
        "services": services,
        "health": {"api": api_health, "mcp": mcp_health},
        "discord_env": discord_env,
        "publisher_browser": {"container": container, "pages_ok": bool(browser.get("ok"))},
        "publisher_api": platform_api,
        "platforms": platforms,
        "blocked_items": blocked_items,
        "actions_taken": [{"kind": "checked_production_publisher_baseline"}],
    }
    return redact_sensitive(result)
```

- [ ] **Step 2: Implement CLI args**

Replace `parse_args` with:

```python
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="ForWin production publisher baseline verifier.")
    parser.add_argument("--api-base", default="http://10.0.0.126:8899")
    parser.add_argument("--mcp-health-url", default="http://10.0.0.126:8896/health")
    parser.add_argument("--docker-context", default="swarm-manager-150")
    parser.add_argument("--colima-profile", default="swarmbridged")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument("--wait-heartbeat-seconds", type=float, default=75.0)
    parser.add_argument(
        "--expect-platform-connected",
        action="append",
        default=["fanqie", "qidian"],
        help="Platform id expected to be connected.",
    )
    return parser.parse_args(argv)
```

- [ ] **Step 3: Implement `main` exit codes**

Replace `main` with:

```python
def main(argv: list[str] | None = None) -> int:
    result = build_baseline(parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1
```

- [ ] **Step 4: Run baseline tests**

Run:

```bash
.venv/bin/pytest tests/test_check_production_publisher_baseline.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Run combined monitor/baseline tests**

Run:

```bash
.venv/bin/pytest tests/test_monitor_forwin_runtime.py tests/test_check_production_publisher_baseline.py -q
```

Expected: all tests pass.

- [ ] **Step 6: Commit verifier**

Run:

```bash
git add scripts/check_production_publisher_baseline.py tests/test_check_production_publisher_baseline.py
git commit -m "Add production publisher baseline verifier"
```

## Task 7: Document Operator Usage

**Files:**
- Modify: `README.md`
- Modify: `docs/operations/forwin-production-processes.md`

- [ ] **Step 1: Add README command**

In `README.md`, under `Current production deployment`, add:

```markdown
Production publisher baseline can be checked without publishing content:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

The command emits one redacted JSON object. `status=ok` means service health,
MCP health, publisher browser, and Fanqie/Qidian page/API login evidence agree.
`status=degraded` with `publisher_login_required` means the operator must log in
to the named platform in the shared production publisher browser profile, then
rerun the command. The command must not publish content or send Discord login
messages.
```

- [ ] **Step 2: Add operations doc command and interpretation**

In `docs/operations/forwin-production-processes.md`, near the publisher browser checks, add:

```markdown
For a one-shot production publisher baseline check:

```bash
python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

Interpretation:

- `ok`: six ForWin Swarm services are healthy, app/MCP health checks pass, the
  Discord publisher login webhook env is absent, and Fanqie/Qidian are connected
  by both API and browser page evidence.
- `degraded`: runtime is up, but a platform needs human login or page/API state
  has not converged. Follow `blocked_items[*].human_action` and rerun the same
  command.
- `failed`: a required service, health endpoint, publisher browser, or Discord
  env policy check failed.

The verifier is read-only for ForWin business state. It must not create books,
upload chapters, publish content, or record secrets.
```

- [ ] **Step 3: Run direct help command**

Run:

```bash
.venv/bin/python scripts/check_production_publisher_baseline.py --help
```

Expected: exit 0 and help text containing `ForWin production publisher baseline verifier`.

- [ ] **Step 4: Run docs-related tests**

Run:

```bash
.venv/bin/pytest tests/test_monitor_forwin_runtime.py tests/test_check_production_publisher_baseline.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit docs**

Run:

```bash
git add README.md docs/operations/forwin-production-processes.md
git commit -m "Document production publisher baseline check"
```

## Task 8: Production Verification

**Files:**
- No code edits unless verification exposes a defect.

- [ ] **Step 1: Run focused tests**

Run:

```bash
.venv/bin/pytest tests/test_monitor_forwin_runtime.py tests/test_check_production_publisher_baseline.py -q
```

Expected: all tests pass.

- [ ] **Step 2: Run production baseline**

Run:

```bash
.venv/bin/python scripts/check_production_publisher_baseline.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged
```

Expected with current known state:

- Exit code `1` is acceptable if Fanqie remains logged out.
- JSON `status` should be `degraded`, not `failed`, if services are healthy.
- Qidian platform entry should be `connected`.
- Fanqie platform entry should be `human_login_required` when it redirects to `/main/writer/login`.
- `discord_env.ok` should be `true`.
- Output must not contain cookie values, tokens, session secrets, QR image data, API keys, or Discord webhook URLs.

- [ ] **Step 3: If production verification finds a code defect, fix with TDD**

Create a failing test in `tests/test_check_production_publisher_baseline.py` for the exact defect, run it to fail, implement the smallest fix, rerun focused tests, then rerun production verification.

- [ ] **Step 4: Commit verification fixes if any**

If Step 3 changed code:

```bash
git add scripts/check_production_publisher_baseline.py tests/test_check_production_publisher_baseline.py README.md docs/operations/forwin-production-processes.md
git commit -m "Fix production publisher baseline verification"
```

- [ ] **Step 5: Push and deploy**

Run:

```bash
git status --short
git push origin master
ssh infra-150-via-colima '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --force --project forwin'
```

Expected:

- Git status contains only unrelated untracked `.codex-monitor/`, if present.
- Deploy completes successfully.
- `docker service ls` on the Swarm manager shows all six ForWin services on the new commit image and `1/1`.

- [ ] **Step 6: Run production baseline after deploy**

Run the same command from Step 2 after deployment completes.

Expected:

- `status=ok` only if both Fanqie and Qidian are logged in by page/API evidence.
- `status=degraded` with a Fanqie `publisher_login_required` blocked item is acceptable if Fanqie still requires human login.
- `status=failed` is not acceptable unless it reflects a real service, MCP, publisher-browser, or Discord-env policy failure that must be fixed before proceeding to publishing-chain validation.

## Self-Review Checklist

- Spec coverage:
  - Service health: Task 1, Task 6, Task 8.
  - Discord login alert disabled policy: Task 2, Task 6, Task 8.
  - Shared publisher browser page evidence: Task 5, Task 6, Task 8.
  - API and page evidence classification: Task 3, Task 4.
  - Human login handoff: Task 3, Task 4, Task 7.
  - Redacted output: Task 3, Task 6, Task 8.
  - Docs: Task 7.
- Completion-marker scan: no unfinished markers and no undefined implementation step.
- Type consistency: `classify_platform`, `rollup_status`, `browser_pages_snapshot`, and `build_baseline` signatures are defined before later tasks use them.
