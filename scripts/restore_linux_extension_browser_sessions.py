from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[1]
SUPPORTED_PLATFORMS = ("fanqie", "qidian")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Restore the latest backend-synced publisher sessions into the Linux extension browser profile."
    )
    parser.add_argument(
        "--cdp-url",
        default="http://127.0.0.1:9222",
        help="CDP endpoint for the running Chromium instance.",
    )
    parser.add_argument(
        "--api-base-url",
        default=(
            os.environ.get("FORWIN_BACKEND_URL")
            or os.environ.get("FORWIN_API_BASE_URL")
            or "http://127.0.0.1:8899"
        ),
        help="ForWin backend API base URL.",
    )
    parser.add_argument(
        "--api-key",
        default=os.environ.get("FORWIN_PUBLISHER_EXTENSION_API_KEY", ""),
        help="Publisher extension API key.",
    )
    parser.add_argument(
        "--wait-seconds",
        type=float,
        default=30.0,
        help="How long to wait for the CDP endpoint before failing.",
    )
    return parser.parse_args()


def wait_for_cdp(cdp_url: str, timeout_seconds: float) -> None:
    deadline = time.time() + max(timeout_seconds, 1.0)
    version_url = cdp_url.rstrip("/") + "/json/version"
    last_error: Exception | None = None
    while time.time() < deadline:
        try:
            with urllib.request.urlopen(version_url, timeout=3) as response:
                payload = json.loads(response.read().decode("utf-8"))
            if payload.get("Browser"):
                return
        except (OSError, urllib.error.URLError, json.JSONDecodeError) as error:
            last_error = error
        time.sleep(0.5)
    if last_error:
        raise RuntimeError(f"CDP endpoint not ready at {cdp_url}: {last_error}") from last_error
    raise RuntimeError(f"CDP endpoint not ready at {cdp_url}")


def load_latest_sessions(api_base_url: str, api_key: str) -> dict[str, list[dict]]:
    sessions: dict[str, list[dict]] = {}
    headers = {"x-forwin-extension-key": api_key} if api_key else {}
    for platform_id in SUPPORTED_PLATFORMS:
        try:
            with urllib.request.urlopen(
                urllib.request.Request(
                    f"{api_base_url.rstrip('/')}/api/publishers/extension/browser-sessions/{platform_id}",
                    headers=headers,
                ),
                timeout=10,
            ) as response:
                payload = json.loads(response.read().decode("utf-8") or "null")
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            continue
        if not isinstance(payload, dict):
            continue
        cookies = payload.get("cookies") or []
        if not isinstance(cookies, list):
            continue
        sessions[str(platform_id)] = [
            item for item in cookies if isinstance(item, dict) and str(item.get("name", "")).strip()
        ]
    return sessions


def to_browser_cookie(cookie: dict) -> dict | None:
    name = str(cookie.get("name", "")).strip()
    domain = str(cookie.get("domain", "")).strip()
    if not name or not domain:
        return None
    payload = {
        "name": name,
        "value": str(cookie.get("value", "")),
        "domain": domain,
        "path": str(cookie.get("path", "/") or "/"),
        "secure": bool(cookie.get("secure")),
        "httpOnly": bool(cookie.get("httpOnly")),
    }
    same_site = str(cookie.get("sameSite", "Lax") or "Lax")
    if same_site in {"Strict", "Lax", "None"}:
        payload["sameSite"] = same_site
    try:
        expires = float(cookie.get("expires", cookie.get("expirationDate", -1)))
    except (TypeError, ValueError):
        expires = -1
    if expires > 0:
        payload["expires"] = expires
    return payload


def main() -> int:
    args = parse_args()
    sessions = load_latest_sessions(args.api_base_url, args.api_key)
    cookies_to_add = [
        browser_cookie
        for platform in SUPPORTED_PLATFORMS
        for browser_cookie in (
            to_browser_cookie(item) for item in sessions.get(platform, [])
        )
        if browser_cookie is not None
    ]
    if not cookies_to_add:
        print("No backend browser sessions found to restore.")
        return 0

    wait_for_cdp(args.cdp_url, args.wait_seconds)
    with sync_playwright() as playwright:
        browser = playwright.chromium.connect_over_cdp(args.cdp_url)
        try:
            if not browser.contexts:
                raise RuntimeError("no browser context available over CDP")
            context = browser.contexts[0]
            context.add_cookies(cookies_to_add)
        finally:
            browser.close()

    summary = {
        platform: len(sessions.get(platform, []))
        for platform in SUPPORTED_PLATFORMS
        if sessions.get(platform)
    }
    print(json.dumps({"restored": summary, "cookie_count": len(cookies_to_add)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
