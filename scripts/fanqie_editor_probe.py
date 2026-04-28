from __future__ import annotations

import json
import os
import tempfile
import urllib.error
import urllib.request
from pathlib import Path

from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
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


def add_cookies(context, cookies: list[dict]) -> None:
    payloads = []
    for item in cookies:
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
        try:
            expires = float(item.get("expires", -1))
        except Exception:
            expires = -1
        if expires > 0:
            payload["expires"] = expires
        payloads.append(payload)
    context.add_cookies(payloads)


def collect_editor_state(page) -> dict:
    return page.evaluate(
        """
        () => {
          const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
          const buttons = Array.from(document.querySelectorAll('button, a, [role="button"]'))
            .map((node) => ({
              text: textOf(node),
              tag: node.tagName,
              href: node.getAttribute && node.getAttribute('href') || '',
              cls: String(node.className || ''),
              disabled: Boolean(node.disabled),
            }))
            .filter((item) => item.text)
            .slice(0, 80);
          const titleSelectors = [
            'input[placeholder*="标题"]',
            'textarea[placeholder*="标题"]',
            'input[name*="title"]',
            'input[id*="title"]',
          ];
          const bodySelectors = [
            '.ProseMirror[contenteditable="true"]',
            'textarea[placeholder*="正文"]',
            'textarea[placeholder*="内容"]',
            'div[role="textbox"]',
          ];
          const titleMatches = titleSelectors
            .map((selector) => {
              const node = document.querySelector(selector);
              return node ? { selector, value: node.value || textOf(node) } : null;
            })
            .filter(Boolean);
          const bodyMatches = bodySelectors
            .map((selector) => {
              const nodes = Array.from(document.querySelectorAll(selector));
              return nodes.map((node, index) => ({
                selector,
                index,
                text: textOf(node).slice(0, 400),
                html: String(node.outerHTML || '').slice(0, 800),
                rect: (() => {
                  const rect = node.getBoundingClientRect();
                  return {
                    x: Math.round(rect.x),
                    y: Math.round(rect.y),
                    width: Math.round(rect.width),
                    height: Math.round(rect.height),
                  };
                })(),
              }));
            })
            .flat();
          const proseMirrors = Array.from(document.querySelectorAll('.ProseMirror'))
            .map((node, index) => ({
              index,
              contenteditable: node.getAttribute('contenteditable') || '',
              text: textOf(node).slice(0, 400),
              html: String(node.outerHTML || '').slice(0, 1200),
              parentHtml: String(node.parentElement?.outerHTML || '').slice(0, 1200),
              rect: (() => {
                const rect = node.getBoundingClientRect();
                return {
                  x: Math.round(rect.x),
                  y: Math.round(rect.y),
                  width: Math.round(rect.width),
                  height: Math.round(rect.height),
                };
              })(),
            }));
          const bodyText = textOf(document.body);
          const wordCountMatch = bodyText.match(/正文字数\\s*(\\d+)/);
          return {
            url: location.href,
            bodyPreview: textOf(document.body).slice(0, 1600),
            wordCount: wordCountMatch ? Number(wordCountMatch[1]) : null,
            buttons,
            titleMatches,
            bodyMatches,
            proseMirrors,
          };
        }
        """
    )


def read_word_count(page) -> dict:
    return page.evaluate(
        """
        () => {
          const bodyText = String(document.body?.innerText || '').replace(/\\s+/g, ' ').trim();
          const match = bodyText.match(/正文字数\\s*(\\d+)/);
          return {
            wordCount: match ? Number(match[1]) : null,
            bodyPreview: bodyText.slice(0, 1200),
            activeElement: document.activeElement ? {
              tag: document.activeElement.tagName,
              className: String(document.activeElement.className || ''),
            } : null,
          };
        }
        """
    )


def open_publish_popup(page):
    try:
        with page.expect_popup(timeout=5000) as popup_info:
            page.evaluate(
                """
                () => {
                  const textOf = (node) => String(node?.innerText || node?.textContent || '').trim();
                  const items = Array.from(document.querySelectorAll('.home-book-item, .home-book-item-home'));
                  const matched = items.find((item) => textOf(item).includes('拍阑干的新书')) || items[0] || null;
                  if (!matched) {
                    return;
                  }
                  const anchor = Array.from(matched.querySelectorAll('a[href]')).find((a) => {
                    const href = String(a.getAttribute('href') || '');
                    return href.includes('/publish/');
                  });
                  if (anchor) {
                    anchor.click();
                  }
                }
                """
            )
        popup = popup_info.value
    except PlaywrightTimeoutError:
        popup = page
    popup.wait_for_load_state("domcontentloaded", timeout=45000)
    popup.wait_for_timeout(6000)
    return popup


def run_input_strategies(context) -> list[dict]:
    results = []

    def open_editor():
        page = context.new_page()
        page.goto("https://fanqienovel.com/main/writer/", wait_until="domcontentloaded", timeout=45000)
        page.wait_for_timeout(5000)
        popup = open_publish_popup(page)
        return page, popup

    page, popup = open_editor()
    try:
        popup.locator('.ProseMirror[contenteditable="true"]').first.click()
        popup.keyboard.type("策略一 第一段测试正文")
        popup.keyboard.press("Enter")
        popup.keyboard.type("第二段正文")
        popup.wait_for_timeout(1500)
        results.append({"strategy": "click-editor-type", **read_word_count(popup)})
    finally:
        popup.close()
        page.close()

    page, popup = open_editor()
    try:
        box = popup.locator('.ProseMirror[contenteditable="true"]').first.bounding_box()
        popup.mouse.click(box["x"] + 30, box["y"] + max(200, box["height"] - 40))
        popup.keyboard.type("策略二 第一段测试正文")
        popup.keyboard.press("Enter")
        popup.keyboard.type("第二段正文")
        popup.wait_for_timeout(1500)
        results.append({"strategy": "click-deep-position-type", **read_word_count(popup)})
    finally:
        popup.close()
        page.close()

    page, popup = open_editor()
    try:
        popup.locator('.ProseMirror[contenteditable="true"] p').first.click()
        popup.keyboard.press("End")
        popup.keyboard.type("策略三 第一段测试正文")
        popup.keyboard.press("Enter")
        popup.keyboard.type("第二段正文")
        popup.wait_for_timeout(1500)
        results.append({"strategy": "click-first-p-type", **read_word_count(popup)})
    finally:
        popup.close()
        page.close()

    return results


def main() -> int:
    cookies = load_cookies()
    with sync_playwright() as p:
        context = p.chromium.launch_persistent_context(
            tempfile.mkdtemp(prefix="fanqie-editor-probe-"),
            headless=True,
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
            add_cookies(context, cookies)
            strategy_page = context.new_page()
            try:
                strategy_page.goto("https://fanqienovel.com/main/writer/", wait_until="domcontentloaded", timeout=45000)
                strategy_page.wait_for_timeout(3000)
            finally:
                strategy_page.close()
            strategy_results = run_input_strategies(context)
            page = context.new_page()
            page.goto("https://fanqienovel.com/main/writer/", wait_until="domcontentloaded", timeout=45000)
            page.wait_for_timeout(5000)
            popup = open_publish_popup(page)
            result = collect_editor_state(popup)
            result["inputStrategies"] = strategy_results
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
