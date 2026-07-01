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
