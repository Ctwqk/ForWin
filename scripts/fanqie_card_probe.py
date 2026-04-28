from __future__ import annotations

import json
import subprocess
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = REPO_ROOT / "browser_extension" / "forwin-publisher"


def load_cookies() -> list[dict]:
    query = """
from sqlalchemy import text
from forwin.config import Config
from forwin.models.base import get_engine

engine = get_engine(Config.from_env().database_url)
try:
    with engine.connect() as conn:
        row = conn.execute(
            text("SELECT cookies_json FROM publisher_browser_sessions WHERE platform_id = :platform ORDER BY updated_at DESC LIMIT 1"),
            {"platform": "fanqie"},
        ).first()
        print(row[0] if row else "")
finally:
    engine.dispose()
"""
    out = subprocess.check_output(["docker", "exec", "forwin", "python", "-c", query], text=True)
    return json.loads(out)


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
