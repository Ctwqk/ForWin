from __future__ import annotations

import os
import socket
import subprocess
import sys
import time
from pathlib import Path
from typing import Iterator
from urllib.error import URLError
from urllib.request import urlopen

import pytest
from playwright.sync_api import Browser, BrowserContext, Page, sync_playwright

from tests.postgres import postgres_test_url


REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_BASE_URL = "http://127.0.0.1:8899"


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _wait_for_health(base_url: str, timeout: float = 20.0) -> None:
    deadline = time.monotonic() + timeout
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        try:
            with urlopen(f"{base_url}/health", timeout=1.0) as response:
                if response.status == 200:
                    return
        except (OSError, URLError) as exc:
            last_error = exc
        time.sleep(0.15)
    raise RuntimeError(f"ForWin browser test server did not become healthy at {base_url}: {last_error}")


@pytest.fixture(scope="session")
def browser_test_base_url(tmp_path_factory: pytest.TempPathFactory) -> Iterator[str]:
    configured = os.environ.get("FORWIN_E2E_BASE_URL", "").strip()
    if configured:
        yield configured.rstrip("/")
        return

    port = _free_port()
    base_url = f"http://127.0.0.1:{port}"
    run_root = tmp_path_factory.mktemp("forwin_browser_e2e")
    env = os.environ.copy()
    env.update(
        {
            "FORWIN_DATABASE_URL": postgres_test_url("browser_e2e"),
            "FORWIN_ARTIFACT_ROOT": str(run_root / "artifacts"),
            "FORWIN_RUNTIME_SETTINGS_PATH": str(run_root / "runtime_settings.json"),
            "FORWIN_RETRIEVAL_ROOT": str(run_root / "retrieval"),
            "FORWIN_QDRANT_URL": "http://127.0.0.1:6335",
            "FORWIN_HTTP_BIND": "127.0.0.1",
            "FORWIN_PUBLISHER_EXTENSION_API_KEY": "browser-test-extension-key",
            "FORWIN_PUBLISHER_SESSION_SECRET": "browser-test-session-secret",
            "FORWIN_PUBLISHER_SESSION_ENCRYPTION_REQUIRED": "true",
            "FORWIN_CODEX_ENABLED": "false",
            "MINIMAX_API_KEY": env.get("MINIMAX_API_KEY", "browser-test-key"),
        }
    )
    proc = subprocess.Popen(
        [
            sys.executable,
            "-m",
            "uvicorn",
            "forwin.api:app",
            "--host",
            "127.0.0.1",
            "--port",
            str(port),
            "--log-level",
            "warning",
        ],
        cwd=REPO_ROOT,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
    )
    try:
        _wait_for_health(base_url)
        yield base_url
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait(timeout=8)


@pytest.fixture
def playwright_driver():
    with sync_playwright() as driver:
        yield driver


@pytest.fixture
def browser(playwright_driver) -> Iterator[Browser]:
    headed = os.environ.get("FORWIN_E2E_HEADED", "").strip().lower() in {"1", "true", "yes"}
    browser = playwright_driver.chromium.launch(headless=not headed, args=["--no-sandbox"])
    try:
        yield browser
    finally:
        browser.close()


@pytest.fixture
def browser_context(browser: Browser) -> Iterator[BrowserContext]:
    context = browser.new_context(
        viewport={"width": 1440, "height": 1000},
        locale="zh-CN",
        timezone_id="America/Los_Angeles",
    )
    context.set_default_timeout(7000)
    try:
        yield context
    finally:
        context.close()


@pytest.fixture
def page(browser_context: BrowserContext) -> Iterator[Page]:
    page = browser_context.new_page()
    failures: list[str] = []

    def on_console(msg) -> None:
        if msg.type == "error":
            if msg.text.startswith("Failed to load resource: the server responded with a status of"):
                return
            failures.append(f"console error: {msg.text}")

    def on_page_error(exc) -> None:
        failures.append(f"page error: {exc}")

    def on_request_failed(request) -> None:
        failure = request.failure
        reason = failure or "unknown"
        if request.url.startswith("data:"):
            return
        failures.append(f"request failed: {request.method} {request.url} {reason}")

    page.on("console", on_console)
    page.on("pageerror", on_page_error)
    page.on("requestfailed", on_request_failed)
    try:
        yield page
    finally:
        if failures:
            pytest.fail("\n".join(failures))
