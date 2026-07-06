from __future__ import annotations

from scripts import restore_linux_extension_browser_sessions as restore


class FakeContext:
    def __init__(self) -> None:
        self.cookies: list[dict] = []

    def add_cookies(self, cookies: list[dict]) -> None:
        self.cookies.extend(cookies)


class FakeBrowser:
    def __init__(self) -> None:
        self.contexts = [FakeContext()]
        self.closed = False

    def close(self) -> None:
        self.closed = True


class FakeChromium:
    def __init__(self, browser: FakeBrowser) -> None:
        self.browser = browser
        self.cdp_url = ""

    def connect_over_cdp(self, cdp_url: str) -> FakeBrowser:
        self.cdp_url = cdp_url
        return self.browser


class FakePlaywright:
    def __init__(self, browser: FakeBrowser) -> None:
        self.chromium = FakeChromium(browser)

    def __enter__(self) -> "FakePlaywright":
        return self

    def __exit__(self, *_args: object) -> None:
        return None


def test_restore_cookies_to_running_browser_leaves_remote_browser_open(monkeypatch) -> None:
    browser = FakeBrowser()
    playwright = FakePlaywright(browser)
    cookies = [
        {
            "name": "session",
            "value": "value",
            "domain": ".example.test",
            "path": "/",
        }
    ]

    monkeypatch.setattr(restore, "sync_playwright", lambda: playwright)

    restore.restore_cookies_to_running_browser("http://127.0.0.1:9222", cookies)

    assert playwright.chromium.cdp_url == "http://127.0.0.1:9222"
    assert browser.contexts[0].cookies == cookies
    assert browser.closed is False
