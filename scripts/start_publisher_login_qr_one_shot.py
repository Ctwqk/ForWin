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
PLATFORM_LOGIN_URLS = {
    "fanqie": "https://fanqienovel.com/main/writer/login",
    "qidian": "https://write.qq.com/portal/login",
}
DEFAULT_PLATFORM_PAGE_URLS = PLATFORM_LOGIN_URLS
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
import base64
import json
import re
import secrets
import sys
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

payload = json.loads(sys.stdin.read())
settings_key = payload["settings_key"]
platform = payload["platform"]
login_url = payload["login_url"]
webhook_url = payload["webhook_url"]
max_wait_ms = int(payload.get("max_wait_ms") or 30000)
discord_content = payload.get("discord_content") or (
    f"ForWin {platform} publisher login QR "
    "(one-shot direct production-browser extraction; no screenshot fallback)."
)

def decode_image_data_url(value):
    match = re.match(r"^data:(image/[A-Za-z0-9.+-]+);base64,(.+)$", str(value or ""), re.S)
    if not match:
        raise RuntimeError("login QR extraction did not return an image data URL")
    mime_type = match.group(1).lower()
    try:
        image_bytes = base64.b64decode(match.group(2), validate=True)
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError("login QR extraction returned invalid base64 image data") from exc
    if len(image_bytes) < 128:
        raise RuntimeError("login QR extraction returned an unexpectedly small image")
    return mime_type, image_bytes

def extension_for_mime_type(mime_type):
    return {
        "image/jpeg": "jpg",
        "image/jpg": "jpg",
        "image/png": "png",
        "image/webp": "webp",
        "image/gif": "gif",
    }.get(mime_type, "png")

def multipart_body(fields, files):
    boundary = "----forwin-login-qr-" + secrets.token_hex(12)
    chunks = []
    for name, value in fields.items():
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(f'Content-Disposition: form-data; name="{name}"\r\n\r\n'.encode("utf-8"))
        chunks.append(str(value).encode("utf-8"))
        chunks.append(b"\r\n")
    for name, file_info in files.items():
        filename, content_type, data = file_info
        chunks.append(f"--{boundary}\r\n".encode("utf-8"))
        chunks.append(
            (
                f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
                f"Content-Type: {content_type}\r\n\r\n"
            ).encode("utf-8")
        )
        chunks.append(data)
        chunks.append(b"\r\n")
    chunks.append(f"--{boundary}--\r\n".encode("utf-8"))
    return boundary, b"".join(chunks)

def post_discord_webhook(*, image_bytes, mime_type):
    filename = f"{platform}-login-qr.{extension_for_mime_type(mime_type)}"
    payload_json = json.dumps({"content": discord_content}, ensure_ascii=False)
    boundary, body = multipart_body(
        {"payload_json": payload_json},
        {"files[0]": (filename, mime_type, image_bytes)},
    )
    request = Request(
        webhook_url,
        data=body,
        headers={
            "Content-Type": f"multipart/form-data; boundary={boundary}",
            "User-Agent": "ForWin-operator-login-qr/1.0",
        },
        method="POST",
    )
    try:
        with urlopen(request, timeout=20) as response:
            response.read(1024)
            status = int(getattr(response, "status", 0) or 0)
    except HTTPError as exc:
        exc.read(1024)
        raise RuntimeError(f"Discord webhook upload failed: HTTP {exc.code}") from exc
    except URLError as exc:
        reason = getattr(exc, "reason", None)
        raise RuntimeError(f"Discord webhook upload failed: {reason or exc.__class__.__name__}") from exc
    return {"ok": 200 <= status < 300, "status": status, "filename": filename}

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
        cdp_result = worker.evaluate(
            """async ({ settingsKey, platform, loginUrl, maxWaitMs }) => {
              const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));
              const callChrome = (target, method, ...args) => (
                new Promise((resolve, reject) => {
                  target[method].call(target, ...args, (result) => {
                    const error = chrome.runtime.lastError;
                    if (error) {
                      reject(new Error(error.message));
                      return;
                    }
                    resolve(result);
                  });
                })
              );
              const sanitizeUrl = (value) => {
                try {
                  const url = new URL(String(value || ''));
                  return `${url.origin}${url.pathname}`;
                } catch (_error) {
                  return '';
                }
              };
              const platformScore = (value) => {
                const url = String(value || '').toLowerCase();
                if (!url) {
                  return 0;
                }
                if (platform === 'fanqie') {
                  if (url.includes('fanqienovel.com')) {
                    return url.includes('/login') ? 80 : 50;
                  }
                  return 0;
                }
                if (platform === 'qidian') {
                  if (url.includes('open.weixin.qq.com')) {
                    return 100;
                  }
                  if (url.includes('write.qq.com') || url.includes('yuewen.com')) {
                    return url.includes('/login') ? 80 : 50;
                  }
                }
                return 0;
              };
              const message = {
                channel: 'forwin-publisher-platform-agent',
                action: 'extract-login-qr-image',
              };
              const sendExtract = async (tabId, target) => {
                try {
                  const response = await callChrome(
                    chrome.tabs,
                    'sendMessage',
                    tabId,
                    message,
                    { frameId: target.frameId },
                  );
                  if (response?.imageDataUrl && String(response.imageDataUrl).startsWith('data:image/')) {
                    return {
                      ok: true,
                      imageDataUrl: response.imageDataUrl,
                      source: response.source ? `platform-agent:${response.source}` : 'platform-agent',
                      frameId: target.frameId,
                      frameUrl: sanitizeUrl(target.url),
                      currentUrl: sanitizeUrl(response.currentUrl || target.url),
                    };
                  }
                  return {
                    ok: false,
                    error: String(response?.error || 'login-qr-not-found'),
                    frameId: target.frameId,
                    frameUrl: sanitizeUrl(target.url),
                  };
                } catch (error) {
                  return {
                    ok: false,
                    error: error instanceof Error ? error.message : String(error),
                    frameId: target.frameId,
                    frameUrl: sanitizeUrl(target.url),
                  };
                }
              };
              const getFrameTargets = async (tabId, currentTabUrl) => {
                let frames = [];
                try {
                  frames = await new Promise((resolve, reject) => {
                    chrome.webNavigation.getAllFrames({ tabId }, (items) => {
                      const error = chrome.runtime.lastError;
                      if (error) {
                        reject(new Error(error.message));
                        return;
                      }
                      resolve(Array.isArray(items) ? items : []);
                    });
                  });
                } catch (_error) {
                  frames = [];
                }
                if (!frames.some((frame) => Number(frame?.frameId) === 0)) {
                  frames.push({ frameId: 0, url: currentTabUrl || '' });
                }
                const seen = new Set();
                return frames
                  .map((frame) => ({
                    frameId: Number(frame?.frameId ?? 0),
                    url: String(frame?.url || ''),
                    priority: platformScore(frame?.url) + (Number(frame?.frameId ?? 0) === 0 ? 1 : 5),
                  }))
                  .filter((target) => Number.isInteger(target.frameId) && !seen.has(target.frameId) && seen.add(target.frameId))
                  .sort((left, right) => right.priority - left.priority || left.frameId - right.frameId);
              };
              const existing = (await chrome.storage.local.get(settingsKey))[settingsKey] || {};
              const next = {
                ...existing,
                loginQrNotificationsEnabled: false,
                loginQrNotificationsAllowed: false,
                loginQrNotificationsAllowedUntilMs: 0,
              };
              await chrome.storage.local.set({ [settingsKey]: next });
              const storageResult = {
                hasBackendBaseUrl: Boolean(next.backendBaseUrl),
                hasApiKey: Boolean(next.apiKey),
                loginQrNotificationsEnabled: next.loginQrNotificationsEnabled === true,
                loginQrNotificationsAllowed: next.loginQrNotificationsAllowed === true,
                loginQrNotificationsAllowedUntilMs: Number(next.loginQrNotificationsAllowedUntilMs || 0),
              };
              const existingTabs = await callChrome(chrome.tabs, 'query', {});
              const matchingTabs = existingTabs
                .filter((tab) => tab?.id && platformScore(tab.url) > 0)
                .sort((left, right) => platformScore(right.url) - platformScore(left.url));
              let tab = matchingTabs[0];
              if (tab?.id) {
                tab = await callChrome(chrome.tabs, 'update', tab.id, { url: loginUrl, active: true });
              } else {
                tab = await callChrome(chrome.tabs, 'create', { url: loginUrl, active: true });
              }
              const tabId = Number(tab?.id || 0);
              if (!tabId) {
                return { ok: false, settings: storageResult, error: 'login-tab-open-failed' };
              }
              const deadline = Date.now() + maxWaitMs;
              let lastResult = { ok: false, error: 'login-qr-not-found' };
              while (Date.now() < deadline) {
                let currentTab = null;
                try {
                  currentTab = await callChrome(chrome.tabs, 'get', tabId);
                } catch (_error) {
                  currentTab = tab;
                }
                const targets = await getFrameTargets(tabId, currentTab?.url || loginUrl);
                for (const target of targets) {
                  lastResult = await sendExtract(tabId, target);
                  if (lastResult?.ok && lastResult.imageDataUrl) {
                    return {
                      ...lastResult,
                      ok: true,
                      settings: storageResult,
                      platform,
                      tabId,
                      currentUrl: sanitizeUrl(lastResult.currentUrl || currentTab?.url || loginUrl),
                    };
                  }
                }
                await sleep(1000);
              }
              return {
                ok: false,
                settings: storageResult,
                platform,
                tabId,
                currentUrl: sanitizeUrl(loginUrl),
                error: 'login-qr-direct-extraction-failed',
                lastError: String(lastResult?.error || ''),
                lastFrameId: Number(lastResult?.frameId ?? -1),
                lastFrameUrl: sanitizeUrl(lastResult?.frameUrl || ''),
              };
            }""",
            {
                "settingsKey": settings_key,
                "platform": platform,
                "loginUrl": login_url,
                "maxWaitMs": max_wait_ms,
            },
        )
        storage_result = cdp_result.get("settings") if isinstance(cdp_result, dict) else {}
        if not storage_result.get("hasBackendBaseUrl") or not storage_result.get("hasApiKey"):
            raise RuntimeError("extension settings are missing backendBaseUrl or apiKey")
        if not isinstance(cdp_result, dict) or not cdp_result.get("ok"):
            raise RuntimeError(str((cdp_result or {}).get("error") or "login QR direct extraction failed"))
        image_data_url = str(cdp_result.pop("imageDataUrl", ""))
        mime_type, image_bytes = decode_image_data_url(image_data_url)
        discord_result = post_discord_webhook(image_bytes=image_bytes, mime_type=mime_type)
        output = {
            "ok": bool(discord_result.get("ok")),
            "settings": storage_result,
            "extraction": {
                "ok": True,
                "platform": platform,
                "source": cdp_result.get("source", ""),
                "tab_id": cdp_result.get("tabId"),
                "frame_id": cdp_result.get("frameId"),
                "current_url": cdp_result.get("currentUrl", ""),
                "frame_url": cdp_result.get("frameUrl", ""),
                "mime_type": mime_type,
                "byte_count": len(image_bytes),
            },
            "discord": discord_result,
        }
    finally:
        browser.close()

print(json.dumps(output, ensure_ascii=False))
'''


def send_login_qr_via_cdp(
    *,
    colima_profile: str,
    container: str,
    platform: str,
    login_url: str,
    webhook_url: str,
    max_wait_ms: int = 30000,
) -> dict[str, Any]:
    payload = {
        "settings_key": SETTINGS_KEY,
        "platform": platform,
        "login_url": login_url,
        "webhook_url": webhook_url,
        "max_wait_ms": max_wait_ms,
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
        timeout=max(75, int(max_wait_ms / 1000) + 45),
    )
    if proc.returncode != 0:
        raise RuntimeError((proc.stderr or proc.stdout or "CDP login QR handoff failed").strip()[:800])
    try:
        parsed: Any = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"CDP login QR handoff returned non-JSON output: {proc.stdout[:300]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("CDP login QR handoff returned non-object JSON")
    return parsed


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Send one fresh publisher login QR from the production browser to an operator Discord webhook.",
    )
    parser.add_argument("--platform", required=True, choices=sorted(PLATFORM_LOGIN_URLS))
    parser.add_argument("--api-base", default=os.environ.get("FORWIN_API_BASE", "http://10.0.0.126:8899"))
    parser.add_argument("--ttl-seconds", type=int, default=300)
    parser.add_argument("--max-dispatches", type=int, default=1)
    parser.add_argument("--webhook-url-env", default=DEFAULT_WEBHOOK_ENV)
    parser.add_argument("--webhook-url-file", default="")
    parser.add_argument("--basic-user-env", default="FORWIN_HTTP_BASIC_USER")
    parser.add_argument("--basic-password-env", default="FORWIN_HTTP_BASIC_PASSWORD")
    parser.add_argument("--colima-profile", default="swarmbridged")
    parser.add_argument("--publisher-browser-container", default="")
    parser.add_argument("--login-url", default="")
    parser.add_argument("--page-url", default="", help=argparse.SUPPRESS)
    parser.add_argument("--max-wait-ms", type=int, default=30000)
    parser.add_argument("--enable-backend-one-shot", action="store_true")
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
        backend_result: dict[str, Any] = {"skipped": True}
        if args.enable_backend_one_shot:
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
            browser_result = send_login_qr_via_cdp(
                colima_profile=args.colima_profile,
                container=container,
                platform=args.platform,
                login_url=args.login_url or args.page_url or PLATFORM_LOGIN_URLS[args.platform],
                webhook_url=webhook_url,
                max_wait_ms=args.max_wait_ms,
            )
        output = {
            "ok": bool(backend_result.get("ok", True)) and bool(browser_result.get("ok", True)),
            "platform": args.platform,
            "backend": backend_result,
            "browser": browser_result,
            "actions_taken": [
                *(["enabled_backend_login_qr_one_shot"] if args.enable_backend_one_shot else []),
                *([] if args.skip_browser_trigger else ["sent_direct_cdp_discord_login_qr"]),
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
