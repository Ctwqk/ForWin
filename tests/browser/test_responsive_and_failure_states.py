from __future__ import annotations

import pytest
from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, switch_home_tab


VIEWPORTS = [
    ("desktop", {"width": 1440, "height": 1000}),
    ("tablet", {"width": 1024, "height": 768}),
    ("mobile", {"width": 390, "height": 844}),
]


@pytest.mark.parametrize("label,viewport", VIEWPORTS)
@pytest.mark.parametrize("path,marker", [("/", "ForWin Workspace"), ("/publishers", "ForWin Publisher"), ("/world-studio", "世界档案")])
def test_primary_pages_render_without_console_errors_or_horizontal_overflow(
    browser,
    browser_test_base_url: str,
    label: str,
    viewport: dict[str, int],
    path: str,
    marker: str,
) -> None:
    context = browser.new_context(viewport=viewport, locale="zh-CN", timezone_id="America/Los_Angeles")
    page = context.new_page()
    backend = MockForWinBackend()
    backend.install(page)
    page.goto(f"{browser_test_base_url}{path}", wait_until="domcontentloaded")
    expect(page.get_by_text(marker).first).to_be_visible()
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("书本")
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("任务")
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("世界档案")
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("发布")
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("配置")
    page.wait_for_timeout(250)
    overflow = page.evaluate(
        """
        () => ({
          label: window.innerWidth,
          scrollWidth: document.documentElement.scrollWidth,
          bodyScrollWidth: document.body.scrollWidth,
          overflowing: document.documentElement.scrollWidth > window.innerWidth + 4,
        })
        """
    )
    assert not overflow["overflowing"], f"{label} {path} horizontal overflow: {overflow}"
    context.close()


def test_home_surfaces_api_failures_and_clears_modal_state(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    backend.install(page)
    page.goto(f"{browser_test_base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#global_status")).to_contain_text("首页已加载")
    backend.fail["/api/projects"] = (500, "book list exploded")
    page.get_by_role("button", name="刷新").first.click()
    expect(page.locator("#global_status")).to_contain_text("book list exploded")

    switch_home_tab(page, "task")
    page.get_by_role("button", name="新建任务").click()
    page.locator("#task_generation_premise").fill("临时输入")
    page.get_by_role("button", name="取消").first.click()
    page.get_by_role("button", name="新建任务").click()
    expect(page.locator("#task_generation_premise")).to_have_value("")
