from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, goto_home, sample_genesis_detail, sample_project


STAGE_LABELS = [
    "创意简报",
    "世界观与背景",
    "地图与空间拓扑",
    "角色势力与叙事引擎",
    "整本书多 Arc 路线图",
    "执行契约与启动交接",
]


def test_genesis_workspace_stage_actions_json_and_items(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    backend.projects = [sample_project(creation_status="creating")]
    backend.genesis = {"project-1": sample_genesis_detail("project-1")}
    goto_home(page, browser_test_base_url, backend)

    page.get_by_role("button", name="创世工作台").first.click()
    expect(page.locator("#genesis_modal_shell")).to_have_class(re.compile(r".*\bopen\b.*"))

    for label in STAGE_LABELS:
        page.get_by_role("button", name=re.compile(label)).click()
        expect(page.locator("#genesis_stage_editor")).not_to_be_empty()
        page.locator("#genesis_stage_editor").fill("{invalid json")
        page.locator("#genesis_save_stage_btn").click()
        expect(page.locator("#global_status_title")).to_contain_text("Genesis 保存失败")
        page.get_by_role("button", name=re.compile(label)).click()
        page.locator("#genesis_generate_stage_btn").click()
        expect(page.locator("#global_status")).to_contain_text("已生成")
        page.locator("#genesis_rerun_stage_btn").click()
        expect(page.locator("#global_status")).to_contain_text("已重生")

    page.get_by_role("button", name=re.compile("世界观与背景")).click()
    page.locator("#genesis_item_collection_select").select_option("world_bible.culture_profiles")
    page.locator("#genesis_create_item_btn").click()
    expect(page.locator("#genesis_item_meta")).to_contain_text("文化背景")
    expect(page.locator("#genesis_refine_item_btn")).to_be_enabled()
    page.locator("#genesis_refine_instruction").fill("把文化背景改得更阴冷。")
    page.locator("#genesis_refine_item_btn").click()
    expect(page.locator("#global_status")).to_contain_text("选中子项已按指令改写")
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#genesis_delete_item_btn").click()
    expect(page.locator("#global_status")).to_contain_text("文化背景 已删除")

    detail = backend.genesis["project-1"]
    for state in detail["pack"]["stage_states"].values():
        state["locked"] = True
    detail["can_start_writing"] = True
    page.evaluate("() => window.refreshGenesisWorkspace('project-1')")
    expect(page.locator("#genesis_start_writing_btn")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#genesis_start_writing_btn").click()
    expect(page.locator("#global_status")).to_contain_text("已启动写作")
