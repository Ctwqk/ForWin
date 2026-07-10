from __future__ import annotations

import json
import os
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from playwright.sync_api import expect


REAL_API_PREFIX = "ForWin Browser Real E2E"


def _real_base_url() -> str:
    return os.environ.get("FORWIN_E2E_BASE_URL", "http://127.0.0.1:8899").rstrip("/")


def _api_json(
    base_url: str,
    path: str,
    *,
    method: str = "GET",
    payload: dict | None = None,
    timeout_seconds: float = 8,
):
    body = json.dumps(payload or {}, ensure_ascii=False).encode("utf-8") if payload is not None else None
    request = Request(
        f"{base_url}{path}",
        data=body,
        method=method,
        headers={"Content-Type": "application/json"},
    )
    with urlopen(request, timeout=timeout_seconds) as response:
        text = response.read().decode("utf-8")
    return json.loads(text) if text else {}


def _delete_project_if_present(base_url: str, project_id: str) -> None:
    if not project_id:
        return
    try:
        _api_json(base_url, f"/api/projects/{project_id}", method="DELETE", payload={}, timeout_seconds=60)
    except HTTPError as exc:
        if exc.code != 404:
            raise
    except TimeoutError:
        return


def _cleanup_stale_real_e2e_projects(base_url: str) -> None:
    try:
        projects = _api_json(base_url, "/api/projects")
    except Exception:
        return
    for project in projects if isinstance(projects, list) else []:
        title = str(project.get("title") or "")
        if title.startswith(REAL_API_PREFIX):
            _delete_project_if_present(base_url, str(project.get("id") or ""))


@pytest.mark.skipif(
    os.environ.get("FORWIN_E2E_REAL_API", "").strip().lower() not in {"1", "true", "yes"},
    reason="set FORWIN_E2E_REAL_API=1 to run against a real ForWin backend",
)
def test_real_backend_create_patch_and_delete_project_from_browser(page) -> None:
    base_url = _real_base_url()
    _cleanup_stale_real_e2e_projects(base_url)
    title = f"{REAL_API_PREFIX} {int(time.time())}"
    project_id = ""

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#global_status")).to_contain_text("首页已加载")

    page.get_by_role("button", name="新建书本").click()
    page.locator("#book_form_title").fill(title)
    page.locator("#book_form_genre").fill("浏览器实测")
    page.locator("#book_form_target_total_chapters").fill("2")
    page.locator("#book_form_premise").fill("真实后端浏览器测试：创建书本、保存 Genesis，然后删除。")
    page.locator("#book_form_content_guardrails").fill("测试创建\n测试清理")

    with page.expect_response(lambda response: response.url.endswith("/api/projects") and response.request.method == "POST") as created_response:
        page.get_by_role("button", name="创建并进入创世").click()
    created = created_response.value.json()
    project_id = str(created["project_id"])

    try:
        expect(page.locator("#genesis_modal_shell")).to_have_class(__import__("re").compile(r".*\bopen\b.*"))
        projects_after_create = _api_json(base_url, "/api/projects")
        created_project = next((project for project in projects_after_create if project.get("id") == project_id), None)
        assert created_project is not None
        assert created_project["title"] == title

        brief = {
            "title": title,
            "one_line": "真实后端已保存的 Genesis brief。",
            "audience": "测试读者",
            "core_emotion": "验证感",
            "core_delight": "看到真实 DB 状态变化",
            "promise": "测试完成后清理。",
            "guardrails": ["测试创建", "测试清理"],
        }
        page.locator("#genesis_stage_editor").fill(json.dumps(brief, ensure_ascii=False, indent=2))
        with page.expect_response(lambda response: f"/api/projects/{project_id}/genesis" in response.url and response.request.method == "PATCH"):
            page.locator("#genesis_save_stage_btn").click()
        expect(page.locator("#global_status")).to_contain_text("已保存")

        genesis = _api_json(base_url, f"/api/projects/{project_id}/genesis")
        assert genesis["pack"]["book_brief"]["one_line"] == brief["one_line"]
        assert genesis["pack"]["book_brief"]["guardrails"] == ["测试创建", "测试清理"]
    finally:
        _delete_project_if_present(base_url, project_id)

    projects_after_delete = _api_json(base_url, "/api/projects")
    assert all(project.get("id") != project_id for project in projects_after_delete)
