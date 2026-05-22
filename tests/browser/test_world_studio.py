from __future__ import annotations

from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, goto_world_studio


def test_world_studio_pages_conflicts_proposals_export_import(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_world_studio(page, browser_test_base_url, backend)

    expect(page.locator(".project-strip")).to_contain_text("雾港潮生录")
    expect(page.locator(".status-grid")).to_contain_text("页面")
    expect(page.locator(".status-grid")).to_contain_text("Proposals")
    expect(page.locator(".main-panel")).to_contain_text("林夜")

    page.get_by_placeholder("搜索页面").fill("雾港")
    expect(page.locator(".list-scroll")).to_contain_text("雾港")
    page.locator(".sidebar select").select_option("location")
    expect(page.locator(".list-scroll")).to_contain_text("雾港")
    expect(page.locator(".list-scroll")).not_to_contain_text("林夜")

    page.get_by_role("tab", name="图谱").click()
    expect(page.locator(".main-panel")).to_contain_text("Object Graph")
    expect(page.locator(".main-panel")).to_contain_text("located_in")
    page.get_by_role("tab", name="搜索").click()
    page.get_by_placeholder("搜索 canon / notes / LLM KB / skill").fill("林夜")
    page.get_by_role("button", name="搜索").click()
    expect(page.locator(".main-panel")).to_contain_text("检索结果")
    expect(page.locator(".main-panel")).to_contain_text("human_unreviewed")
    page.get_by_role("tab", name="Proposal", exact=True).click()
    expect(page.locator(".main-panel")).to_contain_text("Proposal Review")
    expect(page.locator(".sidebar")).to_contain_text("entity/linye")

    page.locator("#vault_root").fill("/tmp/forwin-browser-vault")
    page.get_by_role("button", name="导出").click()
    expect(page.locator(".notice.success")).to_contain_text("已导出")
    page.get_by_role("button", name="导入 proposal").click()
    expect(page.locator(".notice.success")).to_contain_text("已导入")

    page.get_by_title("接受 proposal").click()
    expect(page.locator(".notice.success")).to_contain_text("Proposal 已接受")
    assert backend.captured_payloads("/api/projects/project-1/proposals/proposal-1/approve")[-1]["status"] == "accepted"


def test_world_studio_empty_and_error_states(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    backend.projects = []
    backend.world_pages = []
    backend.world_snapshots = []
    backend.world_conflicts = []
    backend.world_proposals = []
    backend.install(page)
    page.goto(f"{browser_test_base_url}/world-studio", wait_until="domcontentloaded")
    expect(page.locator(".project-picker select")).to_have_value("")
    expect(page.locator(".main-panel")).to_contain_text("还没有页面")

    backend.fail["/api/projects"] = (500, "项目列表加载失败")
    page.reload(wait_until="domcontentloaded")
    expect(page.locator(".notice.error")).to_contain_text("项目列表加载失败")
