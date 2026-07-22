from __future__ import annotations

from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, goto_publishers, sample_upload_job


def test_publishers_page_extension_platforms_and_upload_flow(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_publishers(page, browser_test_base_url, backend, bridge=True)

    expect(page.locator("#extension_summary")).to_contain_text("浏览器扩展已连接")
    expect(page.locator("#platforms")).to_contain_text("番茄小说")
    expect(page.locator("#upload_jobs_list")).to_contain_text("潮声第1章")

    page.locator("#platform").select_option("qidian")
    page.locator("#book_name").fill("发布页测试书")
    page.locator("#create_if_missing").check()
    page.locator("#book_audience").select_option("female")
    page.locator("#book_primary_category").fill("东方仙侠")
    page.locator("#book_protagonist_1").fill("林夜")
    page.locator("#book_protagonist_2").fill("周澜")
    page.locator("#book_intro").fill("这是用于浏览器测试的作品简介。")
    page.locator("#chapter_title").fill("第一章 浏览器测试")
    page.locator("#body").fill("测试正文。" * 80)
    page.locator("#upload_url").fill("https://qidian.example/editor")
    page.get_by_role("button", name="保存草稿").click()
    expect(page.locator("#upload_status")).to_contain_text("任务状态：succeeded")

    payload = backend.captured_payloads("/api/publishers/upload-jobs")[-1]
    assert payload["platform"] == "qidian"
    assert payload["publish"] is False
    assert payload["create_if_missing"] is True
    assert payload["book_meta"]["audience"] == "female"
    assert payload["book_meta"]["protagonist_names"] == ["林夜", "周澜"]

    page.get_by_role("button", name="重新连接").first.click()
    expect(page.locator("#status_fanqie")).to_contain_text("handled by browser test bridge")
    page.get_by_role("button", name="仅打开官网").first.click()
    expect(page.locator("#status_fanqie")).to_contain_text("已在浏览器里打开平台官网")


def test_publishers_page_surfaces_extension_absence_and_upload_api_failure(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_publishers(page, browser_test_base_url, backend, bridge=False)
    expect(page.locator("#extension_summary")).to_contain_text("未检测到浏览器扩展")

    backend.fail["/api/publishers/upload-jobs"] = (400, "缺少正文")
    page.locator("#book_name").fill("失败测试书")
    page.locator("#chapter_title").fill("失败测试章")
    page.locator("#body").fill("")
    page.get_by_role("button", name="直接发布").click()
    expect(page.locator("#upload_status")).to_contain_text("缺少正文")


def test_publishers_page_resumes_a_typed_risk_pause(
    page, browser_test_base_url: str
) -> None:
    paused = sample_upload_job("upload-paused")
    paused.update(
        {
            "status": "paused",
            "message": "平台风险状态已暂停，等待操作员手工处理。",
            "finished_at": "",
            "paused_at": "2026-04-24T12:01:30Z",
            "pause_reason": "captcha",
            "pause_token": "attempt-paused",
            "resumable": True,
            "deletable": False,
            "terminable": True,
        }
    )
    backend = MockForWinBackend(upload_jobs={"upload-paused": paused})
    goto_publishers(page, browser_test_base_url, backend, bridge=True)

    expect(page.locator("#upload_jobs_list")).to_contain_text("CAPTCHA / 人机验证")
    page.get_by_role("button", name="恢复任务").click()
    expect(page.locator("#upload_resume_dialog")).to_be_visible()
    page.locator("#upload_resume_reason").fill("人工完成验证码并确认账号安全")
    page.get_by_role("button", name="确认恢复").click()

    expect(page.locator("#upload_resume_dialog")).not_to_be_visible()
    expect(page.locator("#upload_jobs_list")).to_contain_text("pending")
    payload = backend.captured_payloads(
        "/api/publishers/upload-jobs/upload-paused/resume"
    )[-1]
    assert payload == {
        "expected_pause_reason": "captcha",
        "expected_pause_token": "attempt-paused",
        "operator_reason": "人工完成验证码并确认账号安全",
    }
