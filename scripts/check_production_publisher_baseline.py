#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.monitor_forwin_runtime import (
    discord_login_webhook_env_snapshot,
    docker_services_snapshot,
    http_json,
    publisher_platforms_snapshot,
    redact_sensitive,
    run_command,
    utc_now,
)


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


def classify_page_evidence(
    *,
    platform: str,
    final_url: str,
    title: str,
    text: str,
    navigation_error: str = "",
) -> dict[str, Any]:
    final_url = str(final_url or "")
    title = str(title or "")
    text = str(text or "")
    navigation_error = str(navigation_error or "")
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
            or "工作台" in title
        )
    )
    payload = {
        "platform_id": platform,
        "ok": bool(login_visible or dashboard_visible or not navigation_error),
        "final_url": final_url,
        "title": title,
        "login_visible": login_visible,
        "dashboard_visible": dashboard_visible,
    }
    if navigation_error:
        payload["navigation_error"] = navigation_error[:240]
        if not payload["ok"]:
            payload["error"] = navigation_error[:240]
    return payload


def classify_platform(api_state: dict[str, Any], page_state: dict[str, Any]) -> dict[str, Any]:
    platform_id = str(api_state.get("platform_id") or page_state.get("platform_id") or "")
    final_url = str(page_state.get("final_url") or "")
    login_visible = bool(page_state.get("login_visible"))
    dashboard_visible = bool(page_state.get("dashboard_visible"))
    api_connected = bool(api_state.get("connected"))
    preferred_connected = bool(api_state.get("preferred_connected"))
    base: dict[str, Any] = {
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
    if page_state.get("navigation_error"):
        base["page"]["navigation_error"] = str(page_state.get("navigation_error") or "")
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
                    "human_action": (
                        f"Log in to {platform_id} in the production publisher browser "
                        "profile, then rerun the baseline verifier."
                    ),
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


def rollup_status(checks: dict[str, Any], platforms: list[dict[str, Any]]) -> str:
    required = ("services", "api_health", "mcp_health", "discord_env")
    if any(not bool(checks.get(name, {}).get("ok")) for name in required):
        return "failed"
    for name in ("publisher_browser_container", "publisher_browser_pages"):
        if name in checks and not bool(checks.get(name, {}).get("ok")):
            return "failed"
    platform_statuses = {str(item.get("status") or "") for item in platforms}
    if platform_statuses and platform_statuses <= {"connected"}:
        return "ok"
    if platform_statuses & {
        "human_login_required",
        "state_sync_mismatch",
        "unknown",
        "browser_unreachable",
    }:
        return "degraded"
    return "failed"


def publisher_browser_container_snapshot(args: argparse.Namespace) -> dict[str, Any]:
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
        return {
            "ok": False,
            "error": str(proc.get("stderr") or proc.get("error") or proc.get("stdout") or "")[:500],
        }
    for line in str(proc.get("stdout") or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4 and "forwin-publisher-browser-swarm" in parts[1]:
            return {
                "ok": True,
                "container_id": parts[0],
                "name": parts[1],
                "image": parts[2],
                "status": parts[3],
            }
    return {"ok": False, "error": "forwin-publisher-browser-swarm container not found"}


def browser_pages_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    if bool(getattr(args, "skip_browser", False)):
        return {"ok": True, "skipped": True, "pages": {}}
    script = r'''
from playwright.sync_api import sync_playwright
import json

def classify_page_evidence(platform, final_url, title, text, navigation_error=""):
    final_url = str(final_url or "")
    title = str(title or "")
    text = str(text or "")
    navigation_error = str(navigation_error or "")
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
            or "工作台" in title
        )
    )
    payload = {
        "platform_id": platform,
        "ok": bool(login_visible or dashboard_visible or not navigation_error),
        "final_url": final_url,
        "title": title,
        "login_visible": login_visible,
        "dashboard_visible": dashboard_visible,
    }
    if navigation_error:
        payload["navigation_error"] = navigation_error[:240]
        if not payload["ok"]:
            payload["error"] = navigation_error[:240]
    return payload

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
            navigation_error = ""
            try:
                try:
                    page.goto(url, wait_until="domcontentloaded", timeout=30000)
                    page.wait_for_timeout(5000)
                except Exception as exc:
                    navigation_error = f"{type(exc).__name__}: {str(exc)[:240]}"
                    page.wait_for_timeout(2000)
                text = page.locator("body").inner_text(timeout=5000)[:1200]
                final_url = page.url
                title = page.title()
                results[platform] = classify_page_evidence(
                    platform=platform,
                    final_url=final_url,
                    title=title,
                    text=text,
                    navigation_error=navigation_error,
                )
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
    container = str(getattr(args, "publisher_browser_container", "") or "")
    if not container:
        return {"ok": False, "error": "publisher browser container id is not configured", "pages": {}}
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
            container,
            "python",
            "-c",
            script,
        ],
        timeout=90,
    )
    if not proc.get("ok"):
        return {
            "ok": False,
            "error": str(proc.get("stderr") or proc.get("error") or proc.get("stdout") or "browser inspection failed")[:500],
            "pages": {},
        }
    try:
        payload = json.loads(str(proc.get("stdout") or "{}"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid browser JSON: {exc}", "pages": {}}
    return payload if isinstance(payload, dict) else {
        "ok": False,
        "error": "browser returned non-object JSON",
        "pages": {},
    }


def _platform_by_id(items: list[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    return {str(item.get("platform_id") or ""): item for item in items if isinstance(item, dict)}


def _classify_platforms(
    platform_api: dict[str, Any],
    browser: dict[str, Any],
    expected: set[str],
) -> list[dict[str, Any]]:
    api_by_id = _platform_by_id(platform_api.get("platforms", []))
    page_by_id = browser.get("pages", {}) if isinstance(browser.get("pages"), dict) else {}
    platform_ids = sorted(expected or (set(api_by_id) | set(page_by_id)))
    return [
        classify_platform(
            api_by_id.get(platform_id, {"platform_id": platform_id}),
            page_by_id.get(platform_id, {"platform_id": platform_id, "ok": False}),
        )
        for platform_id in platform_ids
    ]


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
    browser = (
        browser_pages_snapshot(args)
        if container.get("ok")
        else {"ok": False, "error": container.get("error"), "pages": {}}
    )

    platforms = _classify_platforms(platform_api, browser, expected)
    if any(item.get("status") == "state_sync_mismatch" for item in platforms):
        wait_seconds = max(float(getattr(args, "wait_heartbeat_seconds", 0.0) or 0.0), 0.0)
        if wait_seconds > 0:
            time.sleep(wait_seconds)
            platform_api = publisher_platforms_snapshot(args.api_base, expected)
            platforms = _classify_platforms(platform_api, browser, expected)
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
        item["blocked_item"] for item in platforms if isinstance(item.get("blocked_item"), dict)
    ]
    result = {
        "status": rollup_status(checks, platforms),
        "checked_at": checked_at,
        "services": services,
        "health": {"api": api_health, "mcp": mcp_health},
        "discord_env": discord_env,
        "publisher_browser": {
            "container": container,
            "pages_ok": bool(browser.get("ok")),
            "error": browser.get("error", ""),
        },
        "publisher_api": platform_api,
        "platforms": platforms,
        "blocked_items": blocked_items,
        "actions_taken": [{"kind": "checked_production_publisher_baseline"}],
    }
    return redact_sensitive(result)


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


def main(argv: list[str] | None = None) -> int:
    result = build_baseline(parse_args(argv))
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0 if result.get("status") == "ok" else 1


if __name__ == "__main__":
    raise SystemExit(main())
