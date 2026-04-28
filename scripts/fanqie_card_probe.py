from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = REPO_ROOT / "browser_extension" / "forwin-publisher"
BACKEND_URL = os.environ.get("FORWIN_BACKEND_URL", "http://127.0.0.1:8899")


def load_api_key() -> str:
    env_value = os.environ.get("FORWIN_PUBLISHER_EXTENSION_API_KEY", "").strip()
    if env_value:
        return env_value
    env_path = REPO_ROOT / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "FORWIN_PUBLISHER_EXTENSION_API_KEY":
            return value.strip()
    return ""


def load_cookies() -> list[dict]:
    req = urllib.request.Request(
        f"{BACKEND_URL.rstrip('/')}/api/publishers/extension/browser-sessions/fanqie",
        headers={"x-forwin-extension-key": load_api_key()},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = json.loads(response.read().decode("utf-8") or "null")
    except (OSError, urllib.error.URLError, json.JSONDecodeError) as exc:
        raise RuntimeError(f"failed to load Fanqie session from backend API: {exc}") from exc
    if not isinstance(payload, dict):
        return []
    cookies = payload.get("cookies") or []
    return [item for item in cookies if isinstance(item, dict)]


def main() -> int:
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="forwin-fanqie-card-"),
            headless=False,
            executable_path="/usr/bin/chromium",
            ignore_default_args=["--disable-extensions"],
            args=[
                "--enable-unsafe-extension-debugging",
                f"--disable-extensions-except={EXTENSION_DIR}",
                f"--load-extension={EXTENSION_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        try:
            cookie_payload = []
            for item in load_cookies():
                if not item.get("name"):
                    continue
                payload = {
                    "name": str(item.get("name", "")),
                    "value": str(item.get("value", "")),
                    "domain": str(item.get("domain", "")),
                    "path": str(item.get("path", "/") or "/"),
                    "secure": bool(item.get("secure")),
                    "httpOnly": bool(item.get("httpOnly")),
                }
                same_site = str(item.get("sameSite", "Lax") or "Lax")
                if same_site in {"Strict", "Lax", "None"}:
                    payload["sameSite"] = same_site
                cookie_payload.append(payload)
            context.add_cookies(cookie_payload)

            page = context.new_page()
            page.goto("https://fanqienovel.com/main/writer/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            result = page.evaluate(
                """
                () => {
                  const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
                  const items = Array.from(document.querySelectorAll('.home-book-item, .home-book-item-home'));
                  const matchedItem = items.find((item) => textOf(item).includes('拍阑干的新书')) || items[0] || null;
                  if (!matchedItem) {
                    return {
                      itemCount: items.length,
                      bodyPreview: textOf(document.body).slice(0, 1000),
                    };
                  }
                  const anchors = Array.from(matchedItem.querySelectorAll('a[href]')).map((a) => ({
                    text: textOf(a),
                    href: a.getAttribute('href') || '',
                  }));
                  const buttons = Array.from(matchedItem.querySelectorAll('button, [role="button"], .button, .btn')).map((node) => ({
                    text: textOf(node),
                    tag: node.tagName,
                    cls: String(node.className || ''),
                  }));
                  return {
                    itemCount: items.length,
                    bookText: textOf(matchedItem),
                    anchors,
                    buttons,
                    html: matchedItem.outerHTML.slice(0, 4000),
                  };
                }
                """
            )
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
