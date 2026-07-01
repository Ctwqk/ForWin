#!/usr/bin/env python3
from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


REPO_ROOT = Path(__file__).resolve().parents[1]
SETTINGS_KEY = "forwinPublisherSettings"
DEFAULT_WEBHOOK_ENV = "FORWIN_PUBLISHER_LOGIN_QR_ONE_SHOT_WEBHOOK_URL"
DEFAULT_PLATFORM_PAGE_URLS = {
    "fanqie": "http://forwin-app-swarm:8899/publishers",
    "qidian": "http://forwin-app-swarm:8899/publishers",
}
SENSITIVE_KEY_PARTS = (
    "api_key",
    "authorization",
    "cookie",
    "image_data_url",
    "password",
    "qr",
    "secret",
    "token",
    "webhook",
)


def redact_sensitive(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key).lower()
            if any(part in normalized for part in SENSITIVE_KEY_PARTS):
                result[str(key)] = "[redacted]"
            else:
                result[str(key)] = redact_sensitive(item)
        return result
    if isinstance(value, list):
        return [redact_sensitive(item) for item in value]
    return value


def read_secret(*, env_name: str, file_path: str = "") -> str:
    if env_name:
        value = os.environ.get(env_name, "").strip()
        if value:
            return value
    if file_path:
        path = Path(file_path).expanduser()
        if path.exists():
            return path.read_text(encoding="utf-8").strip()
    return ""


def parse_publisher_browser_container(output: str) -> str:
    for line in str(output or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 2 and "forwin-publisher-browser-swarm" in parts[1]:
            return parts[0].strip()
    return ""


def run_command(args: list[str], *, timeout: float = 30.0, input_text: str = "") -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(REPO_ROOT),
        input=input_text or None,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=timeout,
        check=False,
    )


def find_publisher_browser_container(colima_profile: str) -> str:
    proc = run_command(
        [
            "colima",
            "ssh",
            "-p",
            colima_profile,
            "--",
            "docker",
            "ps",
            "--format",
            "{{.ID}}\t{{.Names}}\t{{.Status}}",
        ],
        timeout=20,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "docker ps failed").strip())
    container = parse_publisher_browser_container(proc.stdout)
    if not container:
        raise RuntimeError("forwin-publisher-browser-swarm container not found")
    return container


def request_one_shot(
    *,
    api_base: str,
    platform: str,
    webhook_url: str,
    ttl_seconds: int,
    max_dispatches: int,
    basic_user: str = "",
    basic_password: str = "",
    timeout: float = 10.0,
) -> dict[str, Any]:
    payload = {
        "platform": platform,
        "webhook_url": webhook_url,
        "ttl_seconds": ttl_seconds,
        "max_dispatches": max_dispatches,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    if basic_user or basic_password:
        token = base64.b64encode(f"{basic_user}:{basic_password}".encode("utf-8")).decode("ascii")
        headers["Authorization"] = f"Basic {token}"
    request = Request(
        f"{api_base.rstrip('/')}/api/publishers/login-qr-one-shot",
        data=data,
        headers=headers,
        method="POST",
    )
    try:
        with urlopen(request, timeout=timeout) as response:
            body = response.read(65536).decode("utf-8", errors="replace")
            status = int(getattr(response, "status", 0) or 0)
    except HTTPError as exc:
        body = exc.read(65536).decode("utf-8", errors="replace")
        raise RuntimeError(f"one-shot backend request failed: HTTP {exc.code} {body[:300]}") from exc
    except URLError as exc:
        raise RuntimeError(f"one-shot backend request failed: {exc}") from exc
    try:
        parsed: Any = json.loads(body)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"one-shot backend returned non-JSON response: HTTP {status}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("one-shot backend returned non-object JSON")
    if not (200 <= status < 300) or parsed.get("ok") is False:
        raise RuntimeError(f"one-shot backend rejected request: HTTP {status}")
    return parsed


def browser_cdp_script() -> str:
    return r'''
from playwright.sync_api import sync_playwright
import json
import sys

payload = json.loads(sys.stdin.read())
settings_key = payload["settings_key"]
allowed_until_ms = int(payload["allowed_until_ms"])
platform = payload["platform"]
page_url = payload["page_url"]
bridge_timeout_ms = int(payload.get("bridge_timeout_ms") or 5000)

def first_extension_worker(context):
    for worker in context.service_workers:
        if str(worker.url).startswith("chrome-extension://"):
            return worker
    return context.wait_for_event("serviceworker", timeout=15000)

with sync_playwright() as playwright:
    browser = playwright.chromium.connect_over_cdp("http://127.0.0.1:9222")
    try:
        if not browser.contexts:
            raise RuntimeError("production browser has no CDP contexts")
        context = browser.contexts[0]
        worker = first_extension_worker(context)
        storage_result = worker.evaluate(
            """async ({ settingsKey, allowedUntilMs }) => {
              const existing = (await chrome.storage.local.get(settingsKey))[settingsKey] || {};
              const next = {
                ...existing,
                loginQrNotificationsEnabled: true,
                loginQrNotificationsAllowed: true,
                loginQrNotificationsAllowedUntilMs: allowedUntilMs,
              };
              await chrome.storage.local.set({ [settingsKey]: next });
              return {
                hasBackendBaseUrl: Boolean(next.backendBaseUrl),
                hasApiKey: Boolean(next.apiKey),
                loginQrNotificationsEnabled: next.loginQrNotificationsEnabled === true,
                loginQrNotificationsAllowed: next.loginQrNotificationsAllowed === true,
                loginQrNotificationsAllowedUntilMs: Number(next.loginQrNotificationsAllowedUntilMs || 0),
              };
            }""",
            {"settingsKey": settings_key, "allowedUntilMs": allowed_until_ms},
        )
        if not storage_result.get("hasBackendBaseUrl") or not storage_result.get("hasApiKey"):
            raise RuntimeError("extension settings are missing backendBaseUrl or apiKey")

        page = context.new_page()
        try:
            page.goto(page_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(1000)
            bridge_result = page.evaluate(
                """async ({ platform, timeoutMs }) => {
                  const channel = 'forwin-publisher-extension';
                  const correlationId = globalThis.crypto?.randomUUID
                    ? globalThis.crypto.randomUUID()
                    : `forwin-${Date.now()}-${Math.random().toString(16).slice(2)}`;
                  return await new Promise((resolve) => {
                    const timer = setTimeout(() => {
                      window.removeEventListener('message', onMessage);
                      resolve({ ok: false, error: 'extension bridge timed out' });
                    }, timeoutMs);
                    function onMessage(event) {
                      if (event.source !== window || event.origin !== window.location.origin) {
                        return;
                      }
                      const data = event.data || {};
                      if (
                        data.channel === channel
                        && data.direction === 'extension-to-page'
                        && data.kind === 'response'
                        && data.correlationId === correlationId
                      ) {
                        clearTimeout(timer);
                        window.removeEventListener('message', onMessage);
                        resolve({
                          ok: Boolean(data.ok),
                          message: String(data.payload?.message || ''),
                          error: String(data.error || ''),
                        });
                      }
                    }
                    window.addEventListener('message', onMessage);
                    window.postMessage({
                      channel,
                      direction: 'page-to-extension',
                      kind: 'request',
                      correlationId,
                      action: 'open-login',
                      payload: { platform },
                    }, window.location.origin);
                  });
                }""",
                {"platform": platform, "timeoutMs": bridge_timeout_ms},
            )
        finally:
            page.close()
    finally:
        browser.close()

print(json.dumps({
    "ok": bool(bridge_result.get("ok")),
    "settings": storage_result,
    "bridge": bridge_result,
}, ensure_ascii=False))
'''


def enable_extension_and_open_login(
    *,
    colima_profile: str,
    container: str,
    platform: str,
    page_url: str,
    allowed_until_ms: int,
    bridge_timeout_ms: int = 5000,
) -> dict[str, Any]:
    payload = {
        "settings_key": SETTINGS_KEY,
        "allowed_until_ms": allowed_until_ms,
        "platform": platform,
        "page_url": page_url,
        "bridge_timeout_ms": bridge_timeout_ms,
    }
    proc = run_command(
        [
            "colima",
            "ssh",
            "-p",
            colima_profile,
            "--",
            "docker",
            "exec",
            "-i",
            container,
            "python",
            "-c",
            browser_cdp_script(),
        ],
        input_text=json.dumps(payload),
        timeout=75,
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "CDP login trigger failed").strip()[:800])
    try:
        parsed: Any = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CDP login trigger returned non-JSON output: {proc.stdout[:300]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("CDP login trigger returned non-object JSON")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Enable a temporary one-shot publisher login QR delivery window and trigger the production browser login flow.",
    )
    parser.add_argument("--platform", required=True, choices=sorted(DEFAULT_PLATFORM_PAGE_URLS))
    parser.add_argument("--api-base", default=os.environ.get("FORWIN_API_BASE", "http://10.0.0.126:8899"))
    parser.add_argument("--ttl-seconds", type=int, default=300)
    parser.add_argument("--max-dispatches", type=int, default=1)
    parser.add_argument("--webhook-url-env", default=DEFAULT_WEBHOOK_ENV)
    parser.add_argument("--webhook-url-file", default="")
    parser.add_argument("--basic-user-env", default="FORWIN_HTTP_BASIC_USER")
    parser.add_argument("--basic-password-env", default="FORWIN_HTTP_BASIC_PASSWORD")
    parser.add_argument("--colima-profile", default="swarmbridged")
    parser.add_argument("--publisher-browser-container", default="")
    parser.add_argument("--page-url", default="")
    parser.add_argument("--skip-browser-trigger", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    webhook_url = read_secret(env_name=args.webhook_url_env, file_path=args.webhook_url_file)
    if not webhook_url:
        print(
            json.dumps(
                {
                    "ok": False,
                    "error": (
                        f"missing one-shot Discord webhook; set {args.webhook_url_env} "
                        "or pass --webhook-url-file"
                    ),
                },
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 2

    try:
        backend_result = request_one_shot(
            api_base=args.api_base,
            platform=args.platform,
            webhook_url=webhook_url,
            ttl_seconds=args.ttl_seconds,
            max_dispatches=args.max_dispatches,
            basic_user=read_secret(env_name=args.basic_user_env),
            basic_password=read_secret(env_name=args.basic_password_env),
        )
        browser_result: dict[str, Any] = {"skipped": True}
        container = args.publisher_browser_container
        if not args.skip_browser_trigger:
            if not container:
                container = find_publisher_browser_container(args.colima_profile)
            browser_result = enable_extension_and_open_login(
                colima_profile=args.colima_profile,
                container=container,
                platform=args.platform,
                page_url=args.page_url or DEFAULT_PLATFORM_PAGE_URLS[args.platform],
                allowed_until_ms=int(backend_result.get("allowed_until_ms") or 0),
            )
        output = {
            "ok": bool(backend_result.get("ok")) and bool(browser_result.get("ok", True)),
            "platform": args.platform,
            "backend": backend_result,
            "browser": browser_result,
            "actions_taken": [
                "enabled_backend_login_qr_one_shot",
                *([] if args.skip_browser_trigger else ["enabled_extension_window_and_opened_login"]),
            ],
        }
        print(json.dumps(redact_sensitive(output), ensure_ascii=False, sort_keys=True))
        return 0 if output["ok"] else 1
    except Exception as exc:  # noqa: BLE001
        print(
            json.dumps(
                redact_sensitive({"ok": False, "platform": args.platform, "error": str(exc)}),
                ensure_ascii=False,
                sort_keys=True,
            ),
            file=sys.stderr,
        )
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
