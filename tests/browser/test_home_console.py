from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, goto_home, sample_project, switch_home_tab


def test_home_console_navigation_books_and_config(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    expect(page.locator("#book_list")).to_contain_text("雾港潮生录")
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("世界档案")
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("发布")
    expect(page.get_by_text("V4 世界")).to_have_count(0)
    switch_home_tab(page, "task")
    expect(page.locator("#panel_task")).to_contain_text("任务")
    expect(page.locator("#task_list")).to_contain_text("等待人工 review")

    switch_home_tab(page, "config")
    expect(page.locator("#profile_list")).to_contain_text("测试 MiniMax")
    expect(page.get_by_role("heading", name="浏览器扩展")).to_be_visible()
    expect(page.get_by_role("button", name="打开扩展设置")).to_be_visible()
    expect(page.get_by_role("link", name="下载扩展包（Chrome/Edge）")).to_have_attribute(
        "href", "/api/publishers/extension-package"
    )
    expect(page.get_by_role("link", name="下载 Firefox 扩展包")).to_have_attribute(
        "href", "/api/publishers/extension-package/firefox"
    )
    expect(page.locator("#profile_list")).to_contain_text("凭据可用")
    expect(page.get_by_role("button", name="添加模型")).to_have_count(0)
    expect(page.locator("#model_form_api_key")).to_have_count(0)
    expect(page.locator("#config_generation_operation_mode")).to_have_count(0)

    expect(page.get_by_role("link", name="发布")).to_have_attribute("href", "/publishers")
    expect(page.get_by_role("link", name="世界档案")).to_have_attribute("href", "/world-studio")


def test_book_modal_validates_bindings_and_opens_genesis(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    page.get_by_role("button", name="新建书本").click()
    expect(page.locator("#book_modal_shell")).to_have_class(re.compile(r".*\bopen\b.*"))
    page.locator("#book_form_title").fill("潮雾测试书")
    page.locator("#book_form_premise").fill("长文本 premise：主角在潮雾里发现被抹掉的航线。")
    page.locator("#book_form_content_guardrails").fill("不提前揭示密钥\n不跳过人工 checkpoint")
    page.locator("#book_form_publish_platform_1").select_option("fanqie")
    page.locator("#book_form_publish_mode_1").select_option("create_book")
    page.locator("#book_form_publish_book_name_1").fill("番茄潮雾")
    page.locator("#book_form_publish_upload_url_1").fill("https://fanqie.example/editor")
    page.locator("#book_form_publish_platform_2").select_option("fanqie")
    page.locator("#book_form_publish_mode_2").select_option("chapter_only")
    page.get_by_role("button", name="创建并进入创世").click()
    expect(page.locator("#global_status")).to_contain_text("两个绑定平台不能重复")

    page.locator("#book_form_publish_platform_2").select_option("qidian")
    page.locator("#book_form_publish_book_name_2").fill("起点潮雾")
    page.get_by_role("button", name="创建并进入创世").click()
    expect(page.locator("#genesis_modal_shell")).to_have_class(re.compile(r".*\bopen\b.*"))
    payload = backend.captured_payloads("/api/projects")[-1]
    assert payload["title"] == "潮雾测试书"
    assert payload["content_guardrails"] == ["不提前揭示密钥", "不跳过人工 checkpoint"]
    assert len(payload["publish_bindings"]) == 2
    assert payload["publish_bindings"][0]["create_if_missing"] is True


def test_generation_modal_only_submits_project_run_boundary(page, browser_test_base_url: str) -> None:
    project = sample_project()
    project["needs_review_chapter_count"] = 0
    project["generation_control"]["pending_review_chapters"] = []
    backend = MockForWinBackend(projects=[project])
    goto_home(page, browser_test_base_url, backend)

    page.get_by_role("button", name="继续生成剩余章节").click()
    expect(page.locator("#task_modal_shell")).to_have_class(re.compile(r".*\bopen\b.*"))
    page.locator("#task_generation_num_chapters").fill("4")
    page.locator("#task_generation_run_until_chapter").fill("6")
    page.locator("#task_generation_auto_continue").uncheck()
    page.locator("#task_modal_submit").click()

    payload = backend.captured_payloads("/api/projects/project-1/continue-generation")[-1]
    assert payload == {"max_chapters": 4, "run_until_chapter": 6, "auto_continue": False}
    expect(page.locator("#task_generation_operation_mode")).to_have_count(0)
    expect(page.locator("#task_generation_model_profile_id")).to_have_count(0)
