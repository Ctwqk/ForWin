from __future__ import annotations

import json
import os
import re
from itertools import product
import time
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
from playwright.sync_api import expect


REAL_API_PREFIX = "ForWin Browser Real E2E"
TERMINAL_GENERATION_STATUSES = {"completed", "partial_failed", "failed", "needs_review", "cancelled", "paused"}


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


def _settings_have_llm_key(base_url: str) -> bool:
    try:
        settings = _api_json(base_url, "/api/settings/llm")
    except Exception:
        return False
    return bool(settings.get("has_api_key"))


def _wait_for_generation_terminal(base_url: str, task_id: str, *, timeout_seconds: int) -> dict:
    deadline = time.monotonic() + timeout_seconds
    last_task: dict = {}
    while time.monotonic() < deadline:
        last_task = _api_json(base_url, f"/api/tasks/{task_id}")
        if str(last_task.get("status") or "") in TERMINAL_GENERATION_STATUSES:
            return last_task
        time.sleep(5)
    raise AssertionError(f"generation task {task_id} did not finish in {timeout_seconds}s; last={last_task}")


def _assert_live_generation_outputs(base_url: str, task: dict) -> None:
    status = str(task.get("status") or "")
    assert status not in {"failed", "partial_failed", "cancelled"}, task
    project_id = str(task.get("project_id") or "")
    assert project_id, task

    if status == "needs_review":
        paused = [int(value) for value in task.get("paused_chapters", []) if int(value or 0)]
        assert paused, task
        _api_json(
            base_url,
            f"/api/projects/{project_id}/chapters/{paused[0]}/review/approve",
            method="POST",
            payload={"continue_generation": False, "reason": "live browser e2e accepts generated review checkpoint"},
        )

    chapters = _api_json(base_url, f"/api/projects/{project_id}/chapters")
    assert chapters, task
    first_chapter = chapters[0]
    chapter_number = int(first_chapter.get("chapter_number") or 1)
    chapter = _api_json(base_url, f"/api/projects/{project_id}/chapters/{chapter_number}")
    body = str(chapter.get("body") or "")
    assert len(body) >= 300, {"chapter": chapter, "task": task}

    timeline = _api_json(base_url, f"/api/tasks/{task['task_id']}/timeline")
    assert timeline.get("events"), timeline
    decision_events = _api_json(base_url, f"/api/projects/{project_id}/decision-events")
    assert decision_events.get("events") or decision_events.get("items"), decision_events
    ledger = _api_json(base_url, f"/api/projects/{project_id}/chapters/{chapter_number}/ledger")
    assert ledger.get("chapter_number") == chapter_number


def _live_llm_combo_limit(default_count: int) -> int:
    raw = str(os.environ.get("FORWIN_E2E_LIVE_LLM_MATRIX_LIMIT", "")).strip()
    if not raw:
        return default_count
    return max(1, min(default_count, int(raw)))


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


@pytest.mark.skipif(
    os.environ.get("FORWIN_E2E_LIVE_LLM", "").strip().lower() not in {"1", "true", "yes"},
    reason="set FORWIN_E2E_LIVE_LLM=1 to run a live LLM generation through the browser",
)
def test_live_llm_browser_generation_reaches_backend_artifacts(page) -> None:
    base_url = _real_base_url()
    assert _settings_have_llm_key(base_url), "live LLM E2E requires a saved LLM API key"
    _cleanup_stale_real_e2e_projects(base_url)
    title_marker = f"{REAL_API_PREFIX} Live LLM {int(time.time())}"
    task_id = ""
    project_id = ""

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#global_status")).to_contain_text("首页已加载")
    page.get_by_role("button", name=re.compile(r"^(Tasks|任务)$")).click()
    page.get_by_role("button", name="新建任务").click()
    page.locator("#task_generation_premise").fill(
        f"{title_marker}。写一个完整第一章：雾港记录员林夜在退潮时发现旧航线档案异常。"
        "要求情节闭合、正文紧凑、不要调用外部资料。"
    )
    page.locator("#task_generation_genre").fill("浏览器实测玄幻")
    page.locator("#task_generation_num_chapters").fill("1")
    page.locator("#task_generation_min_chapter_chars").fill("500")
    page.locator("#task_generation_operation_mode").select_option(os.environ.get("FORWIN_E2E_LIVE_OPERATION_MODE", "blackbox"))
    page.locator("#task_generation_progression_mode").select_option(
        os.environ.get("FORWIN_E2E_LIVE_PROGRESSION_MODE", "serial_canon_band_guard")
    )

    with page.expect_response(lambda response: response.url.endswith("/api/generate") and response.request.method == "POST") as created_response:
        page.get_by_role("button", name="创建任务").click()
    created = created_response.value.json()
    task_id = str(created["task_id"])

    try:
        timeout_seconds = int(os.environ.get("FORWIN_E2E_LIVE_LLM_TIMEOUT_SECONDS", "2400"))
        task = _wait_for_generation_terminal(base_url, task_id, timeout_seconds=timeout_seconds)
        project_id = str(task.get("project_id") or "")
        _assert_live_generation_outputs(base_url, task)
    finally:
        _delete_project_if_present(base_url, project_id)


@pytest.mark.skipif(
    os.environ.get("FORWIN_E2E_LIVE_LLM_MATRIX", "").strip().lower() not in {"1", "true", "yes"},
    reason="set FORWIN_E2E_LIVE_LLM_MATRIX=1 to run the full live LLM operation matrix",
)
def test_live_llm_all_generation_operation_combinations(page) -> None:
    base_url = _real_base_url()
    assert _settings_have_llm_key(base_url), "live LLM matrix requires a saved LLM API key"
    _cleanup_stale_real_e2e_projects(base_url)
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
            ["", "serial_canon", "serial_canon_band_guard"],
            [False, True],
            [False, True],
            [False, True],
            [False, True],
        )
    ]
    combos = combos[: _live_llm_combo_limit(len(combos))]
    timeout_seconds = int(os.environ.get("FORWIN_E2E_LIVE_LLM_TIMEOUT_SECONDS", "2400"))
    created_project_ids: list[str] = []

    page.goto(f"{base_url}/", wait_until="domcontentloaded")
    expect(page.locator("#global_status")).to_contain_text("首页已加载")

    try:
        for index, combo in enumerate(combos, start=1):
            title_marker = f"{REAL_API_PREFIX} Live Matrix {index:03d}"
            created = page.evaluate(
                """
                async ({ combo, titleMarker }) => {
                  const response = await fetch('/api/generate', {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify({
                      premise: `${titleMarker}。写第一章：林夜在雾港检查潮汐档案，发现一条不该存在的旧航线。`,
                      genre: '浏览器实测玄幻',
                      num_chapters: 1,
                      min_chapter_chars: 500,
                      review_interval_chapters: 0,
                      ...combo,
                    }),
                  });
                  const data = await response.json();
                  if (!response.ok) throw new Error(data.detail || JSON.stringify(data));
                  return data;
                }
                """,
                {"combo": combo, "titleMarker": title_marker},
            )
            task_id = str(created["task_id"])
            task = _wait_for_generation_terminal(base_url, task_id, timeout_seconds=timeout_seconds)
            if task.get("project_id"):
                created_project_ids.append(str(task["project_id"]))
            _assert_live_generation_outputs(base_url, task)
    finally:
        for project_id in created_project_ids:
            _delete_project_if_present(base_url, project_id)
