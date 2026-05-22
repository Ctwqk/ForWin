from __future__ import annotations

from playwright.sync_api import expect

from tests.browser.fixtures import (
    GENESIS_STAGES,
    MockForWinBackend,
    goto_home,
    sample_chapter,
    sample_generation_task,
    sample_project,
    switch_home_tab,
)


STAGE_LABELS = {
    "brief": "创意简报",
    "world": "世界观与背景",
    "map": "地图与空间拓扑",
    "story_engine": "角色势力与叙事引擎",
    "book_blueprint": "整本书多 Arc 路线图",
    "bootstrap": "执行契约与启动交接",
}


def test_mock_regression_create_book_start_generation_and_persist_after_refresh(page, browser_test_base_url: str) -> None:
    """CI regression only: mocked backend/provider, not a replacement for live LLM E2E."""
    backend = MockForWinBackend(
        projects=[
            sample_project("project-seed-a", title="Seed A"),
            sample_project("project-seed-b", title="Seed B"),
        ]
    )
    goto_home(page, browser_test_base_url, backend)

    page.get_by_role("button", name="新建书本").click()
    page.locator("#book_form_title").fill("CI 回归：创建并启动生成")
    page.locator("#book_form_genre").fill("架空悬疑")
    page.locator("#book_form_target_total_chapters").fill("4")
    page.locator("#book_form_premise").fill("陆明追查被核心系统抹除的家族档案。")
    page.locator("#book_form_setting_summary").fill("旧城每十年遗失一段历史。")

    with page.expect_response(lambda response: response.url.endswith("/api/projects") and response.request.method == "POST") as create_response:
        page.get_by_role("button", name="创建并进入创世").click()

    project_id = create_response.value.json()["project_id"]
    created_payload = backend.captured_payloads("/api/projects")[-1]
    assert created_payload["target_total_chapters"] == 4
    expect(page.locator("#genesis_modal_shell")).to_have_class(__import__("re").compile(r".*\bopen\b.*"))

    for stage in GENESIS_STAGES:
        page.locator("#genesis_stage_board button").filter(has_text=STAGE_LABELS[stage]).click()
        with page.expect_response(lambda response, stage=stage: f"/api/projects/{project_id}/genesis/stages/{stage}/generate" in response.url):
            page.locator("#genesis_generate_stage_btn").click()
        with page.expect_response(lambda response, stage=stage: f"/api/projects/{project_id}/genesis/stages/{stage}/lock" in response.url):
            page.locator("#genesis_lock_stage_btn").click()

    expect(page.locator("#genesis_start_writing_btn")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    with page.expect_response(lambda response: response.url.endswith(f"/api/projects/{project_id}/start-writing") and response.request.method == "POST") as start_response:
        page.locator("#genesis_start_writing_btn").click()

    task_id = start_response.value.json()["task_id"]
    assert backend.captured_payloads(f"/api/projects/{project_id}/start-writing")[-1] == {}
    expect(page.locator("#drawer_body")).to_contain_text("项目章节")
    expect(page.locator("#drawer_body")).to_contain_text("潮声第1章")

    page.get_by_role("button", name="查看正文").first.click()
    expect(page.locator("#drawer_body")).to_contain_text("第1章正文")

    page.reload(wait_until="domcontentloaded")
    expect(page.locator("#global_status")).to_contain_text("首页已加载")
    switch_home_tab(page, "task")
    page.locator(".task-item").filter(has_text=task_id).get_by_role("button", name="查看详情").click()
    expect(page.locator("#drawer_body")).to_contain_text("潮声第1章")
    page.get_by_role("button", name="查看正文").first.click()
    expect(page.locator("#drawer_body")).to_contain_text("第1章正文")


def test_large_project_chapter_list_renders_in_batches(page, browser_test_base_url: str) -> None:
    """CI regression only: mocked frontend data, verifies large projects avoid full chapter DOM upfront."""
    project = sample_project("project-many", title="长篇分页书")
    project["chapters"] = [sample_chapter(number) for number in range(1, 76)]
    project["chapter_count"] = 75
    project["generated_chapter_count"] = 75
    backend = MockForWinBackend(
        projects=[project],
        tasks={"task-many": sample_generation_task("task-many", project_id="project-many")},
    )
    goto_home(page, browser_test_base_url, backend)

    switch_home_tab(page, "book")
    page.locator(".task-item").filter(has_text="长篇分页书").get_by_role("button", name="查看书本").click()
    expect(page.locator("#drawer_body")).to_contain_text("项目章节")

    page_requests = [
        item
        for item in backend.captured
        if item["method"] == "GET" and item["path"] == "/api/projects/project-many/chapters/page"
    ]
    assert page_requests
    assert page_requests[0]["query"]["offset"] == ["0"]
    assert page_requests[0]["query"]["limit"] == ["60"]

    section = page.locator("#drawer_body section.detail-card").filter(
        has=page.locator(".task-id", has_text="项目章节")
    ).first
    rows = section.locator(".chapter-row")
    expect(rows).to_have_count(60)
    expect(section).to_contain_text("已显示 60 / 75 章")
    expect(section).not_to_contain_text("潮声第75章")

    section.get_by_role("button", name="加载更多章节").click()

    page_requests = [
        item
        for item in backend.captured
        if item["method"] == "GET" and item["path"] == "/api/projects/project-many/chapters/page"
    ]
    assert page_requests[-1]["query"]["offset"] == ["60"]
    assert page_requests[-1]["query"]["limit"] == ["60"]
    expect(rows).to_have_count(75)
    expect(section).to_contain_text("潮声第75章")
