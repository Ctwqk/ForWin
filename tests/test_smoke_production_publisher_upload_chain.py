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
