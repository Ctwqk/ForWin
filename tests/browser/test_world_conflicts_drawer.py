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


def test_second_decision_modal_survives_first_resolution_refresh(page, browser_test_base_url):
    conflict = {
        "project_id": "project-1", "status": "open", "impact_level": "major",
        "violated": "protection", "blocks_from_chapter": 3, "options": ["veto_world"],
    }
    backend = MockForWinBackend(world_plan_conflicts=[
        {**conflict, "id": "conflict-1", "summary": "First held change"},
        {**conflict, "id": "conflict-2", "summary": "Second held change"},
    ])
    goto_home(page, browser_test_base_url, backend)
    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    card = page.locator("#world_conflicts_card")
    card.get_by_role("button", name="决定", exact=True).first.click()
    page.locator("#project_control_action_modal_reason").fill("first review")
    page.locator("#project_control_action_field_rationale").fill("first rationale")
    page.evaluate("""() => {
        const originalSubmit = submitProjectControlActionModal;
        window.submitProjectControlActionModal = async () => {
            await originalSubmit();
            window.firstSubmitFinished = true;
        };
        window.loadBooks = () => new Promise(resolve => {
            window.finishFirstRefresh = resolve;
        });
    }""")
    page.locator("#project_control_action_modal_submit").click()
    page.wait_for_function("typeof window.finishFirstRefresh === 'function'")
    card.get_by_role("button", name="决定", exact=True).nth(1).click()
    page.locator("#project_control_action_modal_reason").fill("second review in progress")
    page.locator("#project_control_action_field_rationale").fill("second rationale in progress")
    page.evaluate("window.finishFirstRefresh()")
    page.wait_for_function("window.firstSubmitFinished === true")
    expect(page.locator("#project_control_action_modal_shell")).to_be_visible()
    expect(page.locator("#project_control_action_modal_description")).to_have_text("Second held change")
    expect(page.locator("#project_control_action_modal_reason")).to_have_value("second review in progress")
    expect(page.locator("#project_control_action_field_rationale")).to_have_value("second rationale in progress")
    assert backend.world_plan_conflicts[0]["status"] == "resolved"
    assert backend.world_plan_conflicts[1]["status"] == "open"


def test_modal_late_failure_uses_original_action_error_title(page, browser_test_base_url):
    goto_home(page, browser_test_base_url, MockForWinBackend())
    page.evaluate("""() => {
        openProjectControlActionModal({
            title: 'Original action', errorTitle: 'Original action failed', reason: 'review',
            onSubmit: async () => {
                closeProjectControlActionModal();
                throw new Error('late failure');
            },
        });
    }""")
    page.locator("#project_control_action_modal_submit").click()
    expect(page.locator("#global_status_title")).to_have_text("Original action failed")
    expect(page.locator("#global_status")).to_contain_text("late failure")


def test_drawer_status_selector_lists_each_status(page, browser_test_base_url):
    conflict = {
        "project_id": "project-1", "impact_level": "major", "violated": "protection",
        "blocks_from_chapter": 3, "options": ["veto_world"], "source": "world_tick",
    }
    backend = MockForWinBackend(world_plan_conflicts=[
        {**conflict, "id": "conflict-open", "status": "open", "summary": "Open world change"},
        {**conflict, "id": "conflict-resolved", "status": "resolved", "summary": "Resolved world change"},
        {**conflict, "id": "conflict-expired", "status": "expired", "summary": "Expired world change"},
    ])
    goto_home(page, browser_test_base_url, backend)
    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    card = page.locator("#world_conflicts_card")
    selector = card.locator("select")
    expect(selector.locator("option")).to_have_text(["待处理", "已解决", "已过期"])
    expect(card).to_contain_text("Open world change")
    expect(card.get_by_role("button", name="决定", exact=True)).to_have_count(1)

    for status, shown in (("resolved", "Resolved world change"), ("expired", "Expired world change")):
        selector.select_option(status)
        expect(card).to_contain_text(shown)
        expect(card).not_to_contain_text("Open world change")
        # Only an open conflict can be decided.
        expect(card.get_by_role("button", name="决定", exact=True)).to_have_count(0)
    selector.select_option("open")
    expect(card).to_contain_text("Open world change")
    expect(card).not_to_contain_text("Expired world change")


def test_drawer_shows_a_world_conflicts_held_change_by_visibility(page, browser_test_base_url):
    held = {
        "candidate_id": "adjudication:held", "origin": "adjudication", "summary": "The rival plots in secret.",
        "effects": {"events": [
            {"summary": "The harbour closes.", "visibility": "public", "location_id": "loc-1", "at": 10},
            {"summary": "The rival bribes the guard.", "visibility": "secret", "location_id": "loc-1", "at": 10},
        ]},
    }
    backend = MockForWinBackend(world_plan_conflicts=[{
        "id": "conflict-1", "project_id": "project-1", "status": "open", "source": "world_tick",
        "summary": "Held world change", "impact_level": "major", "violated": "impact_review",
        "blocks_from_chapter": 3, "options": ["veto_world", "amend_plan"], "proposed_change": held,
    }])
    goto_home(page, browser_test_base_url, backend)
    switch_home_tab(page, "task")
    page.get_by_role("button", name="查看详情").first.click()
    row = page.locator("#world_conflict_conflict-1")
    expect(row).to_contain_text("来源：世界推演")
    expect(row).to_contain_text("所持变化（公开）：The harbour closes.")
    expect(row).to_contain_text("所持变化（秘密，仅作者可见）：The rival plots in secret.；The rival bribes the guard.")
    # A world conflict's options keep their tokens.
    row.get_by_role("button", name="决定", exact=True).click()
    expect(page.locator("#project_control_action_field_option option")).to_have_text(["veto_world", "amend_plan"])
