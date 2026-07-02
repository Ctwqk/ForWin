from __future__ import annotations

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from types import SimpleNamespace

import scripts.supervise_forwin_interventions as supervisor


REPO_ROOT = Path(__file__).resolve().parents[1]


def make_args(**overrides):
    args = {
        "api_base": "http://forwin.example",
        "mcp_url": "http://mcp.example/mcp",
        "github_repo": "Ctwqk/ForWin",
        "github_limit": 5,
        "upload_job_limit": 5,
        "expect_platform_connected": ["fanqie", "qidian"],
        "skip_github": False,
    }
    args.update(overrides)
    return SimpleNamespace(**args)


def test_build_report_includes_required_fields_and_summarizes_sensitive_payloads(monkeypatch) -> None:
    def fake_http_json(url: str, *, timeout: float = 5.0):
        if url == "http://forwin.example/api/publishers/upload-jobs?limit=5":
            return {
                "ok": True,
                "payload": [
                    {
                        "job_id": "job-failed",
                        "task_kind": "chapter_upload",
                        "project_id": "project-1",
                        "platform": "fanqie",
                        "status": "failed",
                        "book_name": "Book",
                        "chapter_title": "Chapter 1",
                        "body": "secret chapter body must not be logged",
                        "publish": False,
                        "message": "登录已过期",
                        "error": "login required",
                        "updated_at": "2026-06-29T16:00:00Z",
                        "result_payload": {"token": "secret-token", "codex_intervention": {"status": "request_failed"}},
                    }
                ],
            }
        if url == "http://forwin.example/api/publishers/platforms":
            return {
                "ok": True,
                "payload": [
                    {
                        "platform_id": "fanqie",
                        "connected": False,
                        "last_heartbeat_at": "2026-06-29 09:30:51 PDT",
                        "preferred_client_state": {"connected": False, "cookies": [{"value": "cookie"}]},
                        "latest_client_state": {"connected": False},
                        "browser_session_state": {"connected": True, "session_secret": "hidden"},
                    },
                    {
                        "platform_id": "qidian",
                        "connected": True,
                        "last_heartbeat_at": "2026-06-29 09:30:51 PDT",
                        "preferred_client_state": {"connected": True},
                        "latest_client_state": {"connected": True},
                        "browser_session_state": {"connected": True},
                    },
                ],
            }
        if url == "http://forwin.example/api/settings/codex/health":
            return {
                "ok": True,
                "payload": {
                    "enabled": False,
                    "healthy": False,
                    "status": "disabled",
                    "bridge_url": "http://host.docker.internal:8897",
                    "token": "secret-token",
                },
            }
        raise AssertionError(url)

    def fake_run_command(args, **kwargs):
        if args[:2] == ["gh", "pr"]:
            return {
                "ok": True,
                "stdout": json.dumps(
                    [
                        {
                            "number": 7,
                            "title": "Review me",
                            "url": "https://github.example/pr/7",
                            "isDraft": False,
                            "reviewDecision": "CHANGES_REQUESTED",
                            "mergeStateStatus": "BLOCKED",
                            "updatedAt": "2026-06-29T16:00:00Z",
                        }
                    ]
                ),
                "stderr": "",
            }
        if args[:2] == ["gh", "issue"]:
            return {"ok": True, "stdout": "[]", "stderr": ""}
        raise AssertionError(args)

    monkeypatch.setattr(supervisor, "http_json", fake_http_json)
    monkeypatch.setattr(supervisor, "http_json_full", fake_http_json, raising=False)
    monkeypatch.setattr(supervisor, "run_command", fake_run_command)
    monkeypatch.setattr(supervisor, "utc_now", lambda: "2026-06-29T16:30:00Z")
    monkeypatch.setattr(
        supervisor,
        "generation_tasks_snapshot",
        lambda mcp_url: {"ok": True, "has_active_generation_task": False, "tasks": []},
    )

    report = supervisor.build_report(make_args())

    required = {
        "checked_at",
        "github_prs_checked",
        "issues_checked",
        "upload_jobs_checked",
        "generation_tasks_checked",
        "publisher_browser_heartbeat",
        "actions_taken",
        "blocked_items",
    }
    assert required <= report.keys()
    assert report["upload_jobs_checked"]["jobs"][0]["job_id"] == "job-failed"
    assert "body" not in report["upload_jobs_checked"]["jobs"][0]
    assert report["publisher_browser_heartbeat"]["missing_expected"] == ["fanqie"]
    blocked_kinds = {item["kind"] for item in report["blocked_items"]}
    assert {"publisher_login_required", "upload_job_failed", "codex_bridge_unhealthy", "github_pr_needs_attention"} <= blocked_kinds
    serialized = json.dumps(report, ensure_ascii=False)
    assert "secret chapter body" not in serialized
    assert "secret-token" not in serialized
    assert "cookie" not in serialized


def test_github_cli_failure_falls_back_to_github_rest(monkeypatch) -> None:
    def fake_http_json(url: str, *, timeout: float = 5.0):
        if url.endswith("/api/publishers/upload-jobs?limit=5"):
            return {"ok": True, "payload": []}
        if url.endswith("/api/publishers/platforms"):
            return {"ok": True, "payload": []}
        if url.endswith("/api/settings/codex/health"):
            return {"ok": True, "payload": {"enabled": True, "healthy": True, "status": "ok"}}
        raise AssertionError(url)

    monkeypatch.setattr(supervisor, "http_json", fake_http_json)
    monkeypatch.setattr(supervisor, "http_json_full", fake_http_json, raising=False)
    monkeypatch.setattr(supervisor, "run_command", lambda args, **kwargs: {"ok": False, "error": "gh not found"})
    monkeypatch.setattr(
        supervisor,
        "github_rest_pr_items",
        lambda repo, limit: {
            "ok": True,
            "items": [
                {
                    "number": 17,
                    "title": "Fallback PR",
                    "url": "https://github.example/pr/17",
                    "isDraft": False,
                    "reviewDecision": "",
                    "mergeStateStatus": "",
                    "updatedAt": "2026-07-02T04:00:00Z",
                    "statusCheckRollup": [],
                }
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(
        supervisor,
        "github_rest_issue_items",
        lambda repo, limit: {
            "ok": True,
            "items": [
                {
                    "number": 23,
                    "title": "Fallback issue",
                    "url": "https://github.example/issues/23",
                    "labels": [{"name": "codex"}],
                    "assignees": [{"login": "magi1"}],
                    "updatedAt": "2026-07-02T04:01:00Z",
                }
            ],
        },
        raising=False,
    )
    monkeypatch.setattr(
        supervisor,
        "generation_tasks_snapshot",
        lambda mcp_url: {"ok": True, "has_active_generation_task": False, "tasks": []},
    )

    report = supervisor.build_report(make_args(github_repo="Ctwqk/ForWin"))

    assert report["github_prs_checked"]["ok"] is True
    assert report["github_prs_checked"]["prs"][0]["number"] == 17
    assert report["issues_checked"]["ok"] is True
    assert report["issues_checked"]["issues"][0]["number"] == 23
    assert {"github_prs_unavailable", "github_issues_unavailable"}.isdisjoint(
        {item["kind"] for item in report["blocked_items"]}
    )


def test_github_cli_and_rest_failure_is_reported_as_blocked_without_crashing(monkeypatch) -> None:
    def fake_http_json(url: str, *, timeout: float = 5.0):
        if url.endswith("/api/publishers/upload-jobs?limit=5"):
            return {"ok": True, "payload": []}
        if url.endswith("/api/publishers/platforms"):
            return {"ok": True, "payload": []}
        if url.endswith("/api/settings/codex/health"):
            return {"ok": True, "payload": {"enabled": True, "healthy": True, "status": "ok"}}
        raise AssertionError(url)

    monkeypatch.setattr(supervisor, "http_json", fake_http_json)
    monkeypatch.setattr(supervisor, "http_json_full", fake_http_json, raising=False)
    monkeypatch.setattr(supervisor, "run_command", lambda args, **kwargs: {"ok": False, "error": "gh not found"})
    monkeypatch.setattr(
        supervisor,
        "github_rest_pr_items",
        lambda repo, limit: {"ok": False, "error": "REST unavailable"},
        raising=False,
    )
    monkeypatch.setattr(
        supervisor,
        "github_rest_issue_items",
        lambda repo, limit: {"ok": False, "error": "REST unavailable"},
        raising=False,
    )
    monkeypatch.setattr(
        supervisor,
        "generation_tasks_snapshot",
        lambda mcp_url: {"ok": True, "has_active_generation_task": False, "tasks": []},
    )

    report = supervisor.build_report(make_args(github_repo="Ctwqk/ForWin"))

    assert report["github_prs_checked"]["ok"] is False
    assert report["issues_checked"]["ok"] is False
    assert {"github_prs_unavailable", "github_issues_unavailable"} <= {
        item["kind"] for item in report["blocked_items"]
    }


def test_upload_jobs_snapshot_reads_large_api_responses_without_logging_body() -> None:
    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802
            assert self.path == "/api/publishers/upload-jobs?limit=5"
            payload = [
                {
                    "job_id": "job-large",
                    "task_kind": "chapter_upload",
                    "platform": "qidian",
                    "status": "succeeded",
                    "book_name": "Book",
                    "chapter_title": "Chapter",
                    "body": "x" * 70000,
                    "publish": False,
                    "message": "ok",
                    "error": "",
                    "result_payload": {},
                }
            ]
            body = json.dumps(payload).encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format, *args):  # noqa: A002
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    blocked: list[dict] = []
    actions: list[dict] = []
    try:
        snapshot = supervisor.upload_jobs_snapshot(
            f"http://127.0.0.1:{server.server_port}",
            5,
            blocked,
            actions,
        )
    finally:
        server.shutdown()
        thread.join(timeout=2)

    assert snapshot["ok"] is True
    assert snapshot["jobs"][0]["job_id"] == "job-large"
    serialized = json.dumps(snapshot)
    assert "x" * 1000 not in serialized


def test_upload_jobs_snapshot_ignores_failed_jobs_superseded_by_success(monkeypatch) -> None:
    blocked: list[dict] = []
    actions: list[dict] = []

    def fake_http_json(url: str, *, timeout: float = 15.0, max_bytes: int = 8_000_000):
        assert url == "http://forwin.example/api/publishers/upload-jobs?limit=5"
        return {
            "ok": True,
            "payload": [
                {
                    "job_id": "job-new-success",
                    "platform": "qidian",
                    "status": "succeeded",
                    "book_name": "Bound Book",
                    "chapter_title": "Smoke 2",
                    "publish": False,
                },
                {
                    "job_id": "job-old-failed",
                    "platform": "qidian",
                    "status": "failed",
                    "book_name": "Placeholder",
                    "chapter_title": "Smoke 1",
                    "publish": False,
                    "error": "book not found",
                },
            ],
        }

    monkeypatch.setattr(supervisor, "http_json_full", fake_http_json, raising=False)
    snapshot = supervisor.upload_jobs_snapshot("http://forwin.example", 5, blocked, actions)

    assert snapshot["ok"] is True
    assert {job["job_id"] for job in snapshot["jobs"]} == {"job-new-success", "job-old-failed"}
    assert [item["kind"] for item in blocked] == []


def test_classify_generation_tasks_ignores_project_tasks_superseded_by_completed() -> None:
    blocked: list[dict] = []
    actions: list[dict] = []
    snapshot = {
        "ok": True,
        "has_active_generation_task": False,
        "tasks": [
            {
                "task_id": "task-new-completed",
                "project_id": "project-1",
                "status": "completed",
                "current_chapter": 10,
            },
            {
                "task_id": "task-old-failed",
                "project_id": "project-1",
                "status": "failed",
                "current_chapter": 10,
            },
            {
                "task_id": "task-old-paused",
                "project_id": "project-1",
                "status": "paused",
                "current_chapter": 9,
            },
        ],
    }

    supervisor.classify_generation_tasks(snapshot, blocked, actions)

    assert blocked == []
    assert {"kind": "checked_generation_tasks"} in actions


def test_codex_bridge_disabled_payload_is_classified_as_unhealthy(monkeypatch) -> None:
    monkeypatch.setattr(
        supervisor,
        "http_json",
        lambda url: {
            "ok": False,
            "status": 200,
            "payload": {
                "enabled": False,
                "healthy": False,
                "status": "disabled",
                "message": "Codex Bridge 未启用。",
            },
        },
    )
    blocked: list[dict] = []
    actions: list[dict] = []

    snapshot = supervisor.codex_bridge_health_snapshot("http://forwin.example", blocked, actions)

    assert snapshot["ok"] is False
    assert snapshot["status"] == "disabled"
    assert [item["kind"] for item in blocked] == ["codex_bridge_unhealthy"]


def test_main_writes_one_jsonl_record_and_exit_code_reflects_blocks(monkeypatch, tmp_path) -> None:
    output = tmp_path / "supervisor.jsonl"
    report = {
        "checked_at": "2026-06-29T16:30:00Z",
        "github_prs_checked": {"ok": True},
        "issues_checked": {"ok": True},
        "upload_jobs_checked": {"ok": True},
        "generation_tasks_checked": {"ok": True},
        "publisher_browser_heartbeat": {"ok": False},
        "codex_bridge_health": {"ok": True},
        "actions_taken": [],
        "blocked_items": [{"kind": "publisher_login_required", "message": "fanqie login required"}],
    }
    monkeypatch.setattr(supervisor, "build_report", lambda args: report)

    code = supervisor.main(["--output-jsonl", str(output)])

    assert code == 1
    assert json.loads(output.read_text(encoding="utf-8").strip()) == report

    code = supervisor.main(["--output-jsonl", str(output), "--no-fail-on-blocked"])

    assert code == 0
    assert len(output.read_text(encoding="utf-8").splitlines()) == 2


def test_script_can_run_directly_from_repo_root() -> None:
    proc = subprocess.run(
        [sys.executable, "scripts/supervise_forwin_interventions.py", "--help"],
        cwd=REPO_ROOT,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=False,
    )

    assert proc.returncode == 0, proc.stderr
    assert "two-hour ForWin intervention supervisor" in proc.stdout
