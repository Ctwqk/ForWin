import pytest
from playwright.sync_api import expect

from tests.browser.fixtures import MockForWinBackend, goto_home, switch_home_tab


def test_drawer_lists_open_conflicts_and_submits_resolution(page, browser_test_base_url):
    conflict = {
        "id": "conflict-1", "project_id": "project-1", "status": "open",
        "summary": "Held world change", "impact_level": "major", "violated": "protection",
        "blocks_from_chapter": 3, "options": ["veto_world", "amend_plan"],
    }
    backend = MockForWinBackend(world_plan_conflicts=[
        conflict, {**conflict, "id": "conflict-2", "status": "resolved", "summary": "Closed world change"},
    ])
    goto_home(page, browser_test_base_url, backend)
    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    card = page.locator("#world_conflicts_card")
    expect(card).to_contain_text("Held world change")
    expect(card).not_to_contain_text("Closed world change")
    expect(card).to_contain_text("major")
    expect(card).to_contain_text("protection")
    expect(card).to_contain_text("3")
    card.get_by_role("button", name="决定", exact=True).click()
    select = page.locator("#project_control_action_field_option")
    expect(select.locator("option")).to_have_text(["veto_world", "amend_plan"])
    select.select_option("amend_plan")
    page.locator("#project_control_action_modal_reason").fill("operator review")
    page.locator("#project_control_action_field_rationale").fill("accept evolution")
    page.locator("#project_control_action_modal_submit").click()
    expect(card).not_to_contain_text("Held world change")
    payload = backend.captured_payloads("/api/projects/project-1/world-conflicts/conflict-1/resolve")[-1]
    assert payload == {"option": "amend_plan", "reason": "operator review", "rationale": "accept evolution"}


@pytest.mark.parametrize("refresh_function", ["loadBooks", "openTaskDrawer"])
def test_resolution_closes_modal_before_refresh_failure(page, browser_test_base_url, refresh_function):
    backend = MockForWinBackend(world_plan_conflicts=[{
        "id": "conflict-1", "project_id": "project-1", "status": "open",
        "summary": "Held world change", "impact_level": "major", "violated": "protection",
        "blocks_from_chapter": 3, "options": ["veto_world"],
    }])
    goto_home(page, browser_test_base_url, backend)
    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    page.locator("#world_conflicts_card").get_by_role("button", name="决定", exact=True).click()
    page.locator("#project_control_action_modal_reason").fill("operator review")
    page.locator("#project_control_action_field_rationale").fill("preserve plan")
    page.evaluate("""(name) => {
        window[name] = async () => {
            window.modalOpenDuringRefresh = document.getElementById('project_control_action_modal_shell').classList.contains('open');
            throw new Error('refresh failed');
        };
    }""", refresh_function)
    page.locator("#project_control_action_modal_submit").click()
    expect(page.locator("#global_status")).to_contain_text("refresh failed")
    expect(page.locator("#global_status_title")).to_contain_text("世界冲突已处理")
    expect(page.locator("#global_status_title")).not_to_contain_text("世界冲突处理失败")
    expect(page.locator("#project_control_action_modal_shell")).not_to_be_visible()
    assert page.evaluate("window.modalOpenDuringRefresh") is False
    assert backend.world_plan_conflicts[0]["status"] == "resolved"
    assert len(backend.captured_payloads("/api/projects/project-1/world-conflicts/conflict-1/resolve")) == 1
