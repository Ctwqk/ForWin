from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, goto_home


def test_task_center_drawer_controls_and_bulk_delete(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    page.locator("#tab_task").click()
    page.get_by_role("button", name="查看详情").first.click()
    expect(page.locator("#task_drawer_overlay")).to_have_class(re.compile(r".*\bopen\b.*"))
    expect(page.locator("#drawer_body")).to_contain_text("任务主线")
    expect(page.locator("#drawer_body")).to_contain_text("治理设置")
    expect(page.locator("#drawer_body")).to_contain_text("因果回放")
    expect(page.locator("#drawer_body")).to_contain_text("治理洞察")
    expect(page.locator("#drawer_body")).to_contain_text("章节流水线")

    page.get_by_role("button", name="安全暂停").click()
    expect(page.locator("#global_status")).to_contain_text("已发送安全暂停请求")
    page.locator("#drawer_body").get_by_role("button", name="强制终止").first.click()
    expect(page.locator("#global_status")).to_contain_text("已发送终止请求")

    page.locator("#task_drawer_overlay").get_by_role("button", name="关闭").click()
    page.locator("#tab_task").click()
    page.locator("#task_select_all_btn").click()
    expect(page.locator("#task_bulk_delete_btn")).to_be_enabled()
    page.once("dialog", lambda dialog: dialog.accept())
    page.locator("#task_bulk_delete_btn").click()
    expect(page.locator("#global_status")).to_contain_text("批量删除完成")
    assert backend.captured_payloads("/api/tasks/bulk-delete")[-1]["items"]


def test_upload_task_modal_payload_combinations(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    combos = [
        {"platform": platform, "publish": publish, "create_if_missing": create, "audience": audience, "url": url}
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
    assert any(payload["upload_url"] == "https://editor.example/chapter" for payload in payloads)


def test_stale_drawer_closes_when_task_detail_404s(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)
    page.locator("#tab_task").click()
    page.get_by_role("button", name="查看详情").first.click()
    expect(page.locator("#task_drawer_overlay")).to_have_class(re.compile(r".*\bopen\b.*"))

    backend.fail["/api/task-center/items/generation/task-1"] = (404, "404 missing task")
    page.evaluate("() => window.refreshCurrentDrawerIfChanged()")
    expect(page.locator("#task_drawer_overlay")).not_to_have_class(re.compile(r".*\bopen\b.*"))
    expect(page.locator("#global_status")).to_contain_text("当前任务已不存在")


def test_stale_drawer_closes_when_task_disappears_from_list(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)
    page.locator("#tab_task").click()
    page.get_by_role("button", name="查看详情").first.click()
    expect(page.locator("#task_drawer_overlay")).to_have_class(re.compile(r".*\bopen\b.*"))

    del backend.tasks["task-1"]
    page.evaluate("() => window.loadTaskCenter()")

    expect(page.locator("#task_drawer_overlay")).not_to_have_class(re.compile(r".*\bopen\b.*"))
    expect(page.locator("#global_status")).to_contain_text("已从任务中心移除")
