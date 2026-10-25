from __future__ import annotations

import re

from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, goto_home, switch_home_tab


def _submit_project_control_reason(page, reason: str = "浏览器测试审计原因") -> None:
    page.locator("#project_control_action_modal_reason").fill(reason)
    page.locator("#project_control_action_modal_submit").click()


def test_runtime_policy_world_conflict_switch_saves(
    page, browser_test_base_url: str
) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)
    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    expect(page.locator("#runtime_policy_world_conflict_llm")).to_have_attribute(
        "aria-pressed", "true"
    )
    page.locator("#runtime_policy_world_conflict_human").click()
    expect(page.locator("#runtime_policy_world_conflict_llm")).to_have_attribute(
        "aria-pressed", "false"
    )
    expect(page.locator("#runtime_policy_world_conflict_human")).to_have_attribute(
        "aria-pressed", "true"
    )
    page.get_by_role("button", name="保存运行策略").click()
    _submit_project_control_reason(page)
    expect(page.locator("#global_status")).to_contain_text("项目运行策略已保存")
    payload = backend.captured_payloads("/api/projects/project-1/policy", method="PUT")[-1]
    assert payload["world_conflict_delegate"] == "human"
    assert payload["gate_delegate"] == "human"
    expect(page.locator("#drawer_body")).to_contain_text("RuntimePolicy v2")
    expect(page.locator("#runtime_policy_world_conflict_human")).to_have_attribute(
        "aria-pressed", "true"
    )


def test_project_control_actions_review_and_chapter_operations(
    page, browser_test_base_url: str
) -> None:
    backend = MockForWinBackend()
    goto_home(page, browser_test_base_url, backend)

    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    expect(page.locator("#drawer_body")).to_contain_text("RuntimePolicy v1")
    expect(page.get_by_role("button", name="新增 Constraint")).to_have_count(0)
    expect(page.locator("#drawer_body")).not_to_contain_text("Narrative Constraint")
    expect(
        page.locator("#runtime_policy_generation_audit_interval")
    ).to_have_count(0)
    expect(
        page.locator("#runtime_policy_generation_audit_pauses")
    ).to_have_count(0)
    page.locator("#runtime_policy_quality_profile").select_option("pulp")
    page.locator("#runtime_policy_target_chapter_chars").fill("2900")
    page.locator("#runtime_policy_gate_llm").click()

    page.get_by_role("button", name="保存运行策略").click()
    page.locator("#project_control_action_modal_submit").click()
    expect(page.locator("#global_status")).to_contain_text(
        "项目控制动作必须填写 reason"
    )
    _submit_project_control_reason(page)
    expect(page.locator("#global_status")).to_contain_text("项目运行策略已保存")
    policy_payload = backend.captured_payloads(
        "/api/projects/project-1/policy", method="PUT"
    )[-1]
    assert policy_payload["reason"]
    assert policy_payload["expected_version"] == 1
    assert policy_payload["quality_profile"] == "pulp"
    assert policy_payload["target_chapter_chars"] == 2900
    assert policy_payload["gate_delegate"] == "llm"
    assert "generation_audit_" + "interval" not in policy_payload
    assert "generation_audit_" + "pauses" not in policy_payload

    page.get_by_role("button", name="插入 Manual Checkpoint").click()
    _submit_project_control_reason(page)
    assert backend.captured_payloads("/api/projects/project-1/manual-checkpoints")[-1][
        "boundary_kind"
    ]

    page.get_by_role("button", name="Override Checkpoint").click()
    _submit_project_control_reason(page)
    assert (
        backend.captured_payloads(
            "/api/projects/project-1/bands/band-1/checkpoint/approve"
        )[-1]["status"]
        == "overridden"
    )

    page.get_by_role("button", name="查看 Review").first.click()
    expect(page.locator("#review_modal_shell")).to_have_class(re.compile(r"\bopen\b"))
    expect(page.locator("#review_layer_flow .review-layer-panel")).to_have_count(5)
    expect(page.locator("#review_layer_flow")).to_contain_text("残余资格")
    expect(page.locator("#review_repair_list")).to_contain_text(
        "arc_patcher_disabled"
    )
    expect(page.locator("#review_modal_shell")).to_contain_text("arc_patch")
    page.locator("#review_modal_shell .icon-button").click()

    page.get_by_role("button", name="Review 决策链").first.click()
    expect(page.locator("#global_status")).to_contain_text("已跳到")

    page.get_by_role("button", name="接受").first.click()
    _submit_project_control_reason(page)
    assert (
        backend.captured_payloads("/api/projects/project-1/chapters/2/review/approve")[
            -1
        ]["continue_generation"]
        is False
    )

    page.get_by_role("button", name="接受并继续").first.click()
    _submit_project_control_reason(page)
    assert (
        backend.captured_payloads("/api/projects/project-1/chapters/2/review/approve")[
            -1
        ]["continue_generation"]
        is True
    )
