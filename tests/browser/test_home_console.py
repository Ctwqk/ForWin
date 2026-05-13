from __future__ import annotations

from itertools import product
import re

from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, goto_home


def test_home_console_navigation_books_and_config(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    expect(page.locator("#book_list")).to_contain_text("雾港潮生录")
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("世界档案")
    expect(page.get_by_role("navigation", name="ForWin primary navigation")).to_contain_text("发布")
    expect(page.get_by_text("V4 世界")).to_have_count(0)
    page.locator("#tab_task").click()
    expect(page.locator("#panel_task")).to_contain_text("统一任务中心")
    expect(page.locator("#task_list")).to_contain_text("等待人工 review")

    page.locator("#tab_config").click()
    expect(page.locator("#profile_list")).to_contain_text("测试 MiniMax")
    expect(page.get_by_role("heading", name="浏览器扩展")).to_be_visible()
    expect(page.get_by_role("button", name="打开扩展设置")).to_be_visible()
    expect(page.get_by_role("link", name="下载扩展包（Chrome/Edge）")).to_have_attribute(
        "href", "/api/publishers/extension-package"
    )
    expect(page.get_by_role("link", name="下载 Firefox 扩展包")).to_have_attribute(
        "href", "/api/publishers/extension-package/firefox"
    )
    page.get_by_role("button", name="添加模型").click()
    expect(page.locator("#model_modal_shell")).to_have_class(re.compile(r".*\bopen\b.*"))
    page.locator("#model_form_name").fill("Kimi 测试")
    page.locator("#model_form_api_key").fill("secret-key-for-test")
    page.locator("#model_form_set_default").check()
    page.locator("#model_modal_shell").get_by_role("button", name="保存").click()
    expect(page.locator("#global_status")).to_contain_text("模型配置已保存")
    assert backend.captured_payloads("/api/settings/llm/profiles")[-1]["set_as_default"] is True

    page.locator("#config_generation_min_chapter_chars").fill("3100")
    page.locator("#config_generation_operation_mode").select_option("copilot")
    page.get_by_role("button", name="保存").click()
    expect(page.locator("#global_status")).to_contain_text("运行偏好已保存")
    prefs = backend.captured_payloads("/api/settings/llm/preferences")[-1]
    assert prefs["min_chapter_chars"] == 3100
    assert prefs["operation_mode"] == "copilot"

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


def test_generation_modal_serializes_all_operation_combinations(page, browser_test_base_url: str) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    combos = [
        {
            "operation_mode": operation_mode,
            "progression_mode": progression_mode,
            "freeze_failed_candidates": freeze_failed_candidates,
            "auto_band_checkpoint": auto_band_checkpoint,
            "manual_checkpoints_enabled": manual_checkpoints_enabled,
            "future_constraints_enabled": future_constraints_enabled,
        }
        for operation_mode, progression_mode, freeze_failed_candidates, auto_band_checkpoint, manual_checkpoints_enabled, future_constraints_enabled in product(
            ["blackbox", "copilot", "checkpoint"],
            ["", "legacy_relaxed", "serial_canon", "serial_canon_band_guard"],
            [False, True],
            [False, True],
            [False, True],
            [False, True],
        )
    ]

    page.evaluate(
        """
        async (combos) => {
          window.openTaskDrawer = async () => {};
          window.loadTaskCenter = async () => {};
          window.loadBooks = async () => {};
          for (const combo of combos) {
            await window.openTaskModal('generation');
            document.getElementById('task_generation_premise').value = `组合 ${combo.operation_mode} ${combo.progression_mode}`;
            document.getElementById('task_generation_genre').value = '玄幻';
            document.getElementById('task_generation_num_chapters').value = '4';
            document.getElementById('task_generation_min_chapter_chars').value = '2800';
            document.getElementById('task_generation_operation_mode').value = combo.operation_mode;
            document.getElementById('task_generation_progression_mode').value = combo.progression_mode;
            document.getElementById('task_generation_freeze_failed_candidates').checked = combo.freeze_failed_candidates;
            document.getElementById('task_generation_auto_band_checkpoint').checked = combo.auto_band_checkpoint;
            document.getElementById('task_generation_manual_checkpoints_enabled').checked = combo.manual_checkpoints_enabled;
            document.getElementById('task_generation_future_constraints_enabled').checked = combo.future_constraints_enabled;
            await window.submitTaskModal();
          }
        }
        """,
        combos,
    )

    payloads = backend.captured_payloads("/api/generate")
    assert len(payloads) == 192
    observed = {
        (
            payload["operation_mode"],
            payload["progression_mode"],
            payload["freeze_failed_candidates"],
            payload["auto_band_checkpoint"],
            payload["manual_checkpoints_enabled"],
            payload["future_constraints_enabled"],
        )
        for payload in payloads
    }
    expected = {
        (
            combo["operation_mode"],
            combo["progression_mode"],
            combo["freeze_failed_candidates"],
            combo["auto_band_checkpoint"],
            combo["manual_checkpoints_enabled"],
            combo["future_constraints_enabled"],
        )
        for combo in combos
    }
    assert observed == expected
