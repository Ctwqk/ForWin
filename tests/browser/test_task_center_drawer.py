from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.browser.fixtures import (
    MockForWinBackend,
    goto_home,
    sample_chapter_conflict,
    sample_chapter_conflict_control,
    switch_home_tab,
)


def test_task_center_drawer_controls_and_bulk_delete(
    page, browser_test_base_url: str
) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    expect(page.locator("#task_drawer_overlay")).to_have_class(
        re.compile(r".*\bopen\b.*")
    )
    expect(page.locator("#drawer_body")).to_contain_text("任务主线")
    expect(page.locator("#drawer_body")).to_contain_text("RuntimePolicy v1")
    expect(page.locator("#drawer_body")).to_contain_text("因果回放")
    expect(page.locator("#drawer_body")).to_contain_text("治理洞察")
    expect(page.locator("#drawer_body")).to_contain_text("章节流水线")
    expect(page.locator("#drawer_body")).not_to_contain_text("Replan 可触发")
    expect(page.locator("#drawer_body")).not_to_contain_text("距 replan 可触发")
    expect(page.locator("#drawer_body")).not_to_contain_text("undefined")
    expect(page.locator("#drawer_body")).not_to_contain_text("Task Contract")

    page.get_by_role("button", name="安全暂停").click()
    expect(page.locator("#global_status")).to_contain_text("已发送安全暂停请求")
    page.locator("#drawer_body").get_by_role("button", name="强制终止").first.click()
    expect(page.locator("#global_status")).to_contain_text("已发送终止请求")

    page.locator("#task_drawer_overlay").get_by_role("button", name="关闭").click()
    switch_home_tab(page, "task")
    page.locator("#task_select_all_btn").click()
    expect(page.locator("#task_bulk_delete_btn")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#task_bulk_delete_btn").click()
    expect(page.locator("#global_status")).to_contain_text("批量删除完成")
    assert backend.captured_payloads("/api/tasks/bulk-delete")[-1]["items"]


def test_task_drawer_operator_review_queue_actions(
    page, browser_test_base_url: str
) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()

    drawer = page.locator("#drawer_body")
    expect(drawer).to_contain_text("Stop Reason Distribution")
    expect(drawer).to_contain_text("Auto-continue Chain Health")
    expect(drawer).to_contain_text("needs-review queue")
    expect(drawer).to_contain_text("repair-exhausted queue")

    drawer.get_by_role("button", name="Retry Review").first.click()
    page.locator("#project_control_action_modal_reason").fill(
        "operator retries soft review blocker"
    )
    page.locator("#project_control_action_modal_submit").click()
    expect(page.locator("#global_status")).to_contain_text("review retried")
    retry_payload = backend.captured_payloads(
        "/api/projects/project-1/chapters/2/review/retry"
    )[-1]
    assert retry_payload["continue_generation"] is False
    assert retry_payload["reason"] == "operator retries soft review blocker"

    drawer.get_by_role("button", name="Register Entity").first.click()
    page.locator("#project_control_action_modal_reason").fill(
        "register named entity from review"
    )
    page.locator("#project_control_action_field_entity_name").fill("许潮")
    page.locator("#project_control_action_field_subworld_id").fill("fog-port")
    page.locator("#project_control_action_field_role_hint").fill("harbor witness")
    page.locator("#project_control_action_modal_submit").click()
    expect(page.locator("#global_status")).to_contain_text(
        "SubworldEntityRegistrationProposal"
    )
    proposal_payload = backend.captured_payloads("/api/projects/project-1/proposals")[
        -1
    ]
    assert proposal_payload["proposal_type"] == "SubworldEntityRegistrationProposal"
    assert proposal_payload["proposed_patch"]["action"] == "register_entity"
    assert proposal_payload["proposed_patch"]["entity_name"] == "许潮"


def test_upload_task_modal_payload_combinations(
    page, browser_test_base_url: str
) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    combos = [
        {
            "platform": platform,
            "publish": publish,
            "create_if_missing": create,
            "audience": audience,
            "url": url,
        }
        for platform in ["fanqie", "qidian"]
        for publish in [False, True]
        for create in [False, True]
        for audience in ["", "male", "female"]
        for url in ["", "https://editor.example/chapter"]
    ]

    page.evaluate(
        """
        async (combos) => {
          window.openTaskDrawer = async () => {};
          window.loadTaskCenter = async () => {};
          window.loadBooks = async () => {};
          for (const combo of combos) {
            await window.openTaskModal('upload');
            document.getElementById('task_upload_platform').value = combo.platform;
            document.getElementById('task_upload_book_name').value = `作品-${combo.platform}`;
            document.getElementById('task_upload_chapter_title').value = '浏览器组合章';
            document.getElementById('task_upload_body').value = '正文'.repeat(40);
            document.getElementById('task_upload_upload_url').value = combo.url;
            document.getElementById('task_upload_publish').checked = combo.publish;
            document.getElementById('task_upload_create_if_missing').checked = combo.create_if_missing;
            document.getElementById('task_upload_audience').value = combo.audience;
            document.getElementById('task_upload_primary_category').value = '都市日常';
            document.getElementById('task_upload_protagonist_names').value = '林夜, 周澜';
            document.getElementById('task_upload_intro').value = '一本测试简介。';
            await window.submitTaskModal();
          }
        }
        """,
        combos,
    )

    payloads = backend.captured_payloads("/api/publishers/upload-jobs")
    assert len(payloads) == 48
    assert {payload["platform"] for payload in payloads} == {"fanqie", "qidian"}
    assert {payload["publish"] for payload in payloads} == {False, True}
    assert {payload["create_if_missing"] for payload in payloads} == {False, True}
    assert any(payload["upload_url"] is None for payload in payloads)
    assert any(
        payload["upload_url"] == "https://editor.example/chapter"
        for payload in payloads
    )


def test_stale_drawer_closes_when_task_detail_404s(
    page, browser_test_base_url: str
) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)
    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    expect(page.locator("#task_drawer_overlay")).to_have_class(
        re.compile(r".*\bopen\b.*")
    )

    backend.fail["/api/task-center/items/generation/task-1"] = (404, "404 missing task")
    page.evaluate("() => window.refreshCurrentDrawerIfChanged()")
    expect(page.locator("#task_drawer_overlay")).not_to_have_class(
        re.compile(r".*\bopen\b.*")
    )
    expect(page.locator("#global_status")).to_contain_text("当前任务已不存在")


def test_stale_drawer_closes_when_task_disappears_from_list(
    page, browser_test_base_url: str
) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)
    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    expect(page.locator("#task_drawer_overlay")).to_have_class(
        re.compile(r".*\bopen\b.*")
    )

    del backend.tasks["task-1"]
    page.evaluate("() => window.loadTaskCenter()")

    expect(page.locator("#task_drawer_overlay")).not_to_have_class(
        re.compile(r".*\bopen\b.*")
    )
    expect(page.locator("#global_status")).to_contain_text("已从任务中心移除")


def test_drawer_shows_chapter_conflict_with_review_chapters(
    page, browser_test_base_url: str
) -> None:
    conflict = sample_chapter_conflict(chapter_number=2)
    backend = MockForWinBackend(world_plan_conflicts=[conflict])
    control = sample_chapter_conflict_control(conflict)
    backend.tasks["task-1"]["generation_control"] = control
    backend.projects[0]["generation_control"] = control
    backend.projects[0]["blocking_reason"] = control["blocking_reason"]
    goto_home(page, browser_test_base_url, backend)

    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    drawer = page.locator("#drawer_body")
    # The copy names the chapter conflict, and the paused chapter stays in the review queue beside it.
    expect(drawer).to_contain_text("第 2 章正文与规划保护冲突")
    expect(drawer).to_contain_text("退回修复正文")
    expect(drawer).to_contain_text("第2章 · 待人工检查")
    expect(drawer).to_contain_text("正文冲突待处理：接受会被拒绝")
    expect(drawer).to_contain_text("needs-review queue：1")
    expect(drawer.get_by_role("button", name="接受", exact=True).first).to_be_visible()

    card = page.locator("#world_conflicts_card")
    row = card.locator("#world_conflict_chapter-conflict-1")
    expect(row).to_contain_text("来源：章节正文")
    expect(row).to_contain_text("保护类型：must_be_alive")
    expect(row).to_contain_text("引用名：林夜")
    expect(row).to_contain_text("until_chapter：第 5 章")
    expect(row).to_contain_text("证据引文：「林夜倒在雾港码头。」")
    expect(row).not_to_contain_text("hero")
    row.get_by_role("button", name="决定", exact=True).click()
    select = page.locator("#project_control_action_field_option")
    expect(select.locator("option")).to_have_text(["退回修复正文", "接受正文并解除保护"])
    assert select.locator("option").evaluate_all("options => options.map(option => option.value)") == [
        "rewrite_chapter", "amend_plan",
    ]
    page.locator("#project_control_action_modal_reason").fill("operator review")
    page.locator("#project_control_action_field_rationale").fill("repair the text")
    page.locator("#project_control_action_modal_submit").click()
    expect(row).to_have_count(0)
    payload = backend.captured_payloads(
        "/api/projects/project-1/world-conflicts/chapter-conflict-1/resolve"
    )[-1]
    assert payload == {"option": "rewrite_chapter", "reason": "operator review", "rationale": "repair the text"}


def test_world_conflict_without_chapter_source_keeps_its_copy(
    page, browser_test_base_url: str
) -> None:
    backend = MockForWinBackend()
    control = {
        **backend.tasks["task-1"]["generation_control"],
        "blocking_reason": {"code": "world_conflict_pending", "message": "存在待处理的世界冲突", "chapter_number": 2,
                            "conflict_id": "conflict-1", "conflict_source": "world_tick"},
    }
    backend.tasks["task-1"]["generation_control"] = control
    goto_home(page, browser_test_base_url, backend)

    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    drawer = page.locator("#drawer_body")
    expect(drawer).to_contain_text("世界与计划冲突需要处理")
    expect(drawer).not_to_contain_text("正文与规划保护冲突")
    expect(drawer).to_contain_text("needs-review queue：0")
