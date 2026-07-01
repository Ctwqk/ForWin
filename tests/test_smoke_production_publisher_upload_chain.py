from __future__ import annotations

import json
from types import SimpleNamespace

import scripts.smoke_production_publisher_upload_chain as smoke


def args(**overrides):
    base = {
        "api_base": "http://forwin.example",
        "expect_platform_connected": ["fanqie", "qidian"],
        "extension_key_env": "FORWIN_PUBLISHER_EXTENSION_API_KEY",
        "endpoint_platform": "fanqie",
        "book_name": "ForWin Smoke Test",
        "chapter_title": "ForWin smoke chapter",
        "body": "This is a safe smoke chapter body.",
        "poll_seconds": 0.1,
        "poll_interval_seconds": 0.01,
        "run_upload_smoke": False,
        "run_project_upload_smoke": False,
        "project_id": "",
        "chapter_number": 0,
    }
    base.update(overrides)
    return SimpleNamespace(**base)


def test_redact_report_removes_nested_secret_material() -> None:
    payload = {
        "authorization": "Bearer secret",
        "cookie_names": ["AppAuthToken"],
        "cookies": [{"name": "AppAuthToken", "value": "secret-cookie"}],
        "result_payload": {"token": "secret-token", "safe": "ok"},
        "webhook_url": "https://discord.example/secret",
        "nested": {"session_secret": "secret-session", "body": "chapter body"},
    }

    redacted = smoke.redact_report(payload)
    serialized = json.dumps(redacted, ensure_ascii=False)

    assert "secret-cookie" not in serialized
    assert "secret-token" not in serialized
    assert "discord.example" not in serialized
    assert "secret-session" not in serialized
    assert redacted["cookie_names"] == ["AppAuthToken"]
    assert redacted["result_payload"]["safe"] == "ok"


def test_summarize_upload_job_omits_body_and_sensitive_payload() -> None:
    summary = smoke.summarize_upload_job(
        {
            "job_id": "job-1",
            "platform": "fanqie",
            "status": "succeeded",
            "book_name": "Book",
            "chapter_title": "Chapter",
            "body": "body must not be logged",
            "publish": False,
            "result_payload": {"token": "secret-token", "mode": "draft"},
            "current_url": "https://fanqienovel.com/main/writer/",
        }
    )

    serialized = json.dumps(summary, ensure_ascii=False)
    assert summary["job_id"] == "job-1"
    assert summary["publish"] is False
    assert "body" not in summary
    assert "body must not be logged" not in serialized
    assert "secret-token" not in serialized
    assert summary["result_payload"]["mode"] == "draft"


def test_safe_upload_payload_forces_non_publishing_flags() -> None:
    payload = smoke.safe_upload_payload(
        platform="qidian",
        book_name="ForWin Smoke Test",
        chapter_title="Smoke Chapter",
        body="Safe body",
    )

    assert payload["platform"] == "qidian"
    assert payload["publish"] is False
    assert payload["create_if_missing"] is False
    assert payload["cover_generation_enabled"] is False
    assert payload["auto_cover_upload_enabled"] is False
    assert payload["publisher_compliance_required"] is False


def test_endpoint_smoke_checks_safe_surfaces_and_cleans_api_job(monkeypatch) -> None:
    calls: list[tuple[str, str]] = []

    def fake_http_json(method, url, *, payload=None, headers=None, timeout=10.0):
        calls.append((method, url))
        if url.endswith("/api/publishers/platforms"):
            return {
                "ok": True,
                "status": 200,
                "payload": [
                    {
                        "platform_id": "fanqie",
                        "connected": True,
                        "preferred_client_state": {"connected": True},
                    },
                    {
                        "platform_id": "qidian",
                        "connected": False,
                        "preferred_client_state": {"connected": False},
                    },
                ],
            }
        if url.endswith("/api/publishers/browser-sessions/fanqie"):
            return {
                "ok": True,
                "status": 200,
                "payload": {
                    "platform": "fanqie",
                    "cookie_names": ["sessionid"],
                    "connected": True,
                },
            }
        if url.endswith("/api/publishers/browser-sessions/qidian"):
            return {
                "ok": True,
                "status": 200,
                "payload": {
                    "platform": "qidian",
                    "cookie_names": [],
                    "connected": False,
                },
            }
        if url.endswith("/api/publishers/preflight"):
            assert payload["platform"] == "fanqie"
            assert "publish" not in payload
            return {
                "ok": True,
                "status": 200,
                "payload": {"ok": True, "blocking": [], "warnings": []},
            }
        if url.endswith("/api/publishers/work-bindings"):
            return {"ok": True, "status": 200, "payload": []}
        if url.endswith("/api/publishers/chapter-bindings"):
            return {"ok": True, "status": 200, "payload": []}
        if url.endswith("/api/publishers/upload-jobs") and method == "POST":
            assert payload["publish"] is False
            return {
                "ok": True,
                "status": 200,
                "payload": {
                    "job_id": "job-1",
                    "platform": "fanqie",
                    "status": "pending",
                    "publish": False,
                },
            }
        if url.endswith("/api/publishers/upload-jobs?limit=10"):
            return {
                "ok": True,
                "status": 200,
                "payload": [
                    {
                        "job_id": "job-1",
                        "platform": "fanqie",
                        "status": "pending",
                        "publish": False,
                    }
                ],
            }
        if url.endswith("/api/publishers/upload-jobs/job-1") and method == "GET":
            return {
                "ok": True,
                "status": 200,
                "payload": {
                    "job_id": "job-1",
                    "platform": "fanqie",
                    "status": "pending",
                    "publish": False,
                },
            }
        if url.endswith("/api/publishers/upload-jobs/job-1/terminate"):
            return {
                "ok": True,
                "status": 200,
                "payload": {"ok": True, "status": "cancelled"},
            }
        if url.endswith("/api/publishers/upload-jobs/job-1") and method == "DELETE":
            return {
                "ok": True,
                "status": 200,
                "payload": {"ok": True, "status": "deleted"},
            }
        raise AssertionError(url)

    monkeypatch.setattr(smoke, "http_json", fake_http_json)
    monkeypatch.delenv("FORWIN_PUBLISHER_EXTENSION_API_KEY", raising=False)

    report = smoke.build_report(args(create_api_smoke_job=True))

    assert report["status"] == "degraded"
    assert report["publisher_api"]["platforms"]["ok"] is False
    assert report["endpoint_smoke"]["api_job"]["job_id"] == "job-1"
    assert report["endpoint_smoke"]["api_job_cleanup"]["deleted"] is True
    assert any(
        item["kind"] == "publisher_login_required" and item["platform"] == "qidian"
        for item in report["blocked_items"]
    )
    assert any(item["kind"] == "extension_key_missing" for item in report["blocked_items"])
    assert ("POST", "http://forwin.example/api/publishers/upload-jobs") in calls


def test_upload_smoke_skips_create_when_platform_not_connected(monkeypatch) -> None:
    monkeypatch.setattr(
        smoke,
        "http_json",
        lambda method, url, **kwargs: {"ok": True, "status": 200, "payload": []},
    )
    report = {
        "platforms": [{"platform_id": "fanqie", "connected": False}],
        "blocked_items": [],
        "actions_taken": [],
    }

    smoke.run_upload_smoke(args(run_upload_smoke=True, upload_platform=["fanqie"]), report)

    assert report["upload_jobs"] == []
    assert report["blocked_items"][0]["kind"] == "publisher_login_required"
    assert report["blocked_items"][0]["platform"] == "fanqie"


def test_upload_smoke_polls_until_terminal_and_redacts(monkeypatch) -> None:
    responses = [
        {
            "ok": True,
            "status": 200,
            "payload": {
                "job_id": "job-2",
                "platform": "fanqie",
                "status": "pending",
                "publish": False,
                "body": "secret body",
            },
        },
        {
            "ok": True,
            "status": 200,
            "payload": {
                "job_id": "job-2",
                "platform": "fanqie",
                "status": "running",
                "publish": False,
                "body": "secret body",
            },
        },
        {
            "ok": True,
            "status": 200,
            "payload": {
                "job_id": "job-2",
                "platform": "fanqie",
                "status": "succeeded",
                "publish": False,
                "result_payload": {"mode": "draft", "token": "secret-token"},
            },
        },
    ]

    def fake_http_json(method, url, *, payload=None, headers=None, timeout=10.0):
        return responses.pop(0)

    monkeypatch.setattr(smoke, "http_json", fake_http_json)
    report = {
        "platforms": [{"platform_id": "fanqie", "connected": True}],
        "blocked_items": [],
        "actions_taken": [],
    }

    smoke.run_upload_smoke(args(run_upload_smoke=True, upload_platform=["fanqie"]), report)

    assert report["upload_jobs"][0]["job_id"] == "job-2"
    assert report["upload_jobs"][0]["terminal_state"] == "succeeded"
    serialized = json.dumps(report, ensure_ascii=False)
    assert "secret body" not in serialized
    assert "secret-token" not in serialized


def test_project_upload_smoke_requires_explicit_project_and_chapter() -> None:
    report = {"blocked_items": [], "actions_taken": []}

    smoke.run_project_upload_smoke(args(run_project_upload_smoke=True), report)

    assert report["project_chapter_path"]["ok"] is False
    assert report["blocked_items"][0]["kind"] == "project_chapter_not_specified"


def test_project_upload_smoke_posts_safe_project_payload(monkeypatch) -> None:
    calls: list[tuple[str, str, dict | None]] = []

    def fake_http_json(method, url, *, payload=None, headers=None, timeout=10.0):
        calls.append((method, url, payload))
        if method == "GET" and url.endswith("/api/projects/project-1/chapters/7"):
            return {
                "ok": True,
                "status": 200,
                "payload": {
                    "chapter_number": 7,
                    "title": "Chapter 7",
                    "status": "accepted",
                },
            }
        if method == "POST" and url.endswith("/api/projects/project-1/publishers/upload-jobs"):
            assert payload["chapter_number"] == 7
            assert payload["publish"] is False
            assert payload["create_if_missing"] is False
            return {
                "ok": True,
                "status": 200,
                "payload": {
                    "job_id": "job-project",
                    "platform": "qidian",
                    "status": "pending",
                    "publish": False,
                },
            }
        if method == "GET" and url.endswith("/api/publishers/upload-jobs/job-project"):
            return {
                "ok": True,
                "status": 200,
                "payload": {
                    "job_id": "job-project",
                    "platform": "qidian",
                    "status": "succeeded",
                    "publish": False,
                },
            }
        raise AssertionError(url)

    monkeypatch.setattr(smoke, "http_json", fake_http_json)
    report = {"blocked_items": [], "actions_taken": []}

    smoke.run_project_upload_smoke(
        args(
            run_project_upload_smoke=True,
            project_id="project-1",
            chapter_number=7,
            project_platform="qidian",
        ),
        report,
    )

    assert report["project_chapter_path"]["ok"] is True
    assert report["project_chapter_path"]["job"]["job_id"] == "job-project"
