from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import time
import tempfile
from pathlib import Path

from playwright.sync_api import sync_playwright


REPO_ROOT = Path(__file__).resolve().parents[1]
EXTENSION_DIR = REPO_ROOT / "browser_extension" / "forwin-publisher"
DB_PATH = REPO_ROOT / "data" / "novel.db"
BACKEND_URL = "http://127.0.0.1:8899"


def find_browser_executable() -> str | None:
    candidates = ["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome"]
    for candidate in candidates:
        if Path(candidate).exists():
            return candidate
    return None


def load_api_key() -> str:
    env_path = REPO_ROOT / ".env"
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        if key.strip() == "FORWIN_PUBLISHER_EXTENSION_API_KEY":
            return value.strip()
    raise RuntimeError("FORWIN_PUBLISHER_EXTENSION_API_KEY not found in .env")


def load_probe_body() -> str:
    body_file = os.environ.get("FORWIN_PROBE_BODY_FILE", "").strip()
    if body_file:
        return Path(body_file).read_text(encoding="utf-8")
    body_inline = os.environ.get("FORWIN_PROBE_BODY", "").strip()
    if body_inline:
        return body_inline
    return (
        "夜里起风的时候，旧城区总会先听见雨棚敲打铁门的回音。"
        "\n\n巷口的路灯坏了一半，昏黄的光斑落在湿漉漉的石板路上，"
        "像被人随手丢下的一串铜钱。"
        "\n\n她把伞骨撑开，沿着门牌一块块找过去，直到那扇写着旧姓氏的木门"
        "在风里轻轻晃了一下。"
        "\n\n门后的人没有立刻应声，只传来一阵细碎的脚步，像在迟疑是否要让"
        "这场夜雨真的进门。"
        "\n\n她低头看了一眼怀里的纸包，封口还整整齐齐，墨迹却已经被潮气洇开。"
        "那上面只写了七个字：今夜之前，务必送到。"
    )


def env_bool(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_book_meta() -> dict:
    protagonist_names = []
    for key in ("FORWIN_BOOK_PROTAGONIST_1", "FORWIN_BOOK_PROTAGONIST_2"):
        value = os.environ.get(key, "").strip()
        if value:
            protagonist_names.append(value)
    return {
        "audience": os.environ.get("FORWIN_BOOK_AUDIENCE", "male").strip() or "male",
        "primary_category": os.environ.get("FORWIN_BOOK_PRIMARY_CATEGORY", "").strip(),
        "protagonist_names": protagonist_names,
        "intro": os.environ.get("FORWIN_BOOK_INTRO", "").strip(),
    }


def load_platform_session(platform: str) -> list[dict]:
    row = _load_platform_session_from_sqlite(DB_PATH, platform)
    if row:
        return row
    row = _load_platform_session_from_container(platform)
    if row:
        return row
    raise RuntimeError(f"no synced session found for platform={platform}")


def _load_platform_session_from_sqlite(path: Path, platform: str) -> list[dict] | None:
    if not path.exists():
        return None
    conn = sqlite3.connect(path)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            SELECT cookies_json
            FROM publisher_browser_sessions
            WHERE platform_id = ?
            ORDER BY updated_at DESC
            LIMIT 1
            """,
            (platform,),
        )
        row = cur.fetchone()
    except sqlite3.Error:
        row = None
    finally:
        conn.close()
    if not row:
        return None
    return json.loads(row[0])


def _load_platform_session_from_container(platform: str) -> list[dict] | None:
    query = (
        "import sqlite3; conn=sqlite3.connect('/app/data/novel.db'); "
        "cur=conn.cursor(); "
        f"cur.execute(\"SELECT cookies_json FROM publisher_browser_sessions WHERE platform_id='{platform}' ORDER BY updated_at DESC LIMIT 1\"); "
        "row=cur.fetchone(); "
        "print(row[0] if row else '')"
    )
    result = subprocess.run(
        ["docker", "exec", "forwin", "python", "-c", query],
        capture_output=True,
        text=True,
        check=False,
    )
    payload = (result.stdout or "").strip()
    if result.returncode != 0 or not payload:
        return None
    return json.loads(payload)


def load_extension_id(context) -> str:
    deadline = time.time() + 15
    while time.time() < deadline:
        workers = context.service_workers
        if workers:
            worker = workers[0]
            url = worker.url
            if url.startswith("chrome-extension://"):
                return url.split("/")[2]
        time.sleep(0.5)
    raise RuntimeError("extension service worker did not appear")


def ensure_extension_configured(context, extension_id: str, api_key: str) -> None:
    page = context.new_page()
    try:
        page.goto(f"chrome-extension://{extension_id}/options.html", wait_until="domcontentloaded", timeout=30000)
        page.locator("#backend_base_url").fill(BACKEND_URL)
        page.locator("#api_key").fill(api_key)
        page.get_by_role("button", name="保存设置").click(timeout=10000)
        page.wait_for_timeout(1500)
    finally:
        page.close()


def import_cookies(context, cookies: list[dict]) -> None:
    browser_cookies = []
    for item in cookies:
        name = str(item.get("name", "")).strip()
        if not name:
            continue
        payload = {
            "name": name,
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
        except (TypeError, ValueError):
            expires = -1
        if expires > 0:
            payload["expires"] = expires
        browser_cookies.append(payload)
    context.add_cookies(browser_cookies)


def wait_for_upload_terminal(page, timeout_s: int = 120) -> str:
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        status = page.locator("#upload_status").inner_text(timeout=10000)
        if "任务状态：succeeded" in status or "任务状态：failed" in status:
            return status
        time.sleep(1.5)
    raise TimeoutError("upload job did not reach terminal state in time")


def main() -> int:
    if len(sys.argv) != 5:
        print(
            "usage: plugin_publish_probe.py <platform> <book_name> <chapter_title> <publish:true|false>",
            file=sys.stderr,
        )
        return 2

    platform = sys.argv[1]
    book_name = sys.argv[2]
    chapter_title = sys.argv[3]
    publish = sys.argv[4].lower() == "true"
    api_key = load_api_key()
    cookies = load_platform_session(platform)
    body = load_probe_body()
    create_if_missing = env_bool("FORWIN_CREATE_IF_MISSING", False)
    book_meta = load_book_meta()

    with sync_playwright() as playwright:
        executable = find_browser_executable()
        profile_dir = tempfile.mkdtemp(prefix="forwin-extension-probe-")
        context = playwright.chromium.launch_persistent_context(
            profile_dir,
            headless=not bool(os.environ.get("DISPLAY")),
            ignore_default_args=["--disable-extensions"],
            executable_path=executable,
            args=[
                f"--disable-extensions-except={EXTENSION_DIR}",
                f"--load-extension={EXTENSION_DIR}",
                "--no-first-run",
                "--no-default-browser-check",
            ],
        )
        try:
            extension_id = load_extension_id(context)
            ensure_extension_configured(context, extension_id, api_key)
            import_cookies(context, cookies)

            page = context.new_page()
            try:
                page.goto(f"{BACKEND_URL}/publishers", wait_until="domcontentloaded", timeout=45000)
                page.evaluate("window.__FORWIN_PREFER_EXTENSION_DEBUG__ = true;")
                page.wait_for_timeout(3000)
                page.select_option("#platform", platform)
                page.locator("#book_name").fill(book_name)
                if create_if_missing:
                    page.locator("#create_if_missing").check()
                page.select_option("#book_audience", book_meta["audience"])
                page.locator("#book_primary_category").fill(book_meta["primary_category"])
                protagonist_names = book_meta["protagonist_names"]
                page.locator("#book_protagonist_1").fill(protagonist_names[0] if protagonist_names else "")
                page.locator("#book_protagonist_2").fill(protagonist_names[1] if len(protagonist_names) > 1 else "")
                page.locator("#book_intro").fill(book_meta["intro"])
                page.locator("#chapter_title").fill(chapter_title)
                page.locator("#body").fill(body)
                button_name = "直接发布" if publish else "保存草稿"
                page.get_by_role("button", name=button_name).click(timeout=10000)
                final_status = wait_for_upload_terminal(page)
                print(final_status)
            finally:
                page.close()
        finally:
            context.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
