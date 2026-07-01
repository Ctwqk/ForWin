from __future__ import annotations

from pathlib import Path

from scripts import start_publisher_login_qr_one_shot as one_shot


def test_parse_publisher_browser_container_finds_swarm_browser() -> None:
    output = "\n".join(
        [
            "abc123\tforwin-app-swarm.1.worker\tUp 2 hours",
            "def456\tforwin-publisher-browser-swarm.1.worker\tUp 2 hours",
        ]
    )

    assert one_shot.parse_publisher_browser_container(output) == "def456"


def test_read_secret_prefers_environment(monkeypatch, tmp_path: Path) -> None:
    secret_file = tmp_path / "webhook.txt"
    secret_file.write_text("https://discord.invalid/from-file\n", encoding="utf-8")
    monkeypatch.setenv("FORWIN_TEST_WEBHOOK", "https://discord.invalid/from-env")

    assert one_shot.read_secret(
        env_name="FORWIN_TEST_WEBHOOK",
        file_path=str(secret_file),
    ) == "https://discord.invalid/from-env"


def test_redact_sensitive_removes_webhook_and_qr_payloads() -> None:
    payload = {
        "webhook_url": "https://discord.invalid/secret",
        "image_data_url": "data:image/png;base64,secret",
        "nested": {"api_key": "secret", "message": "kept"},
    }

    redacted = one_shot.redact_sensitive(payload)

    assert redacted == {
        "webhook_url": "[redacted]",
        "image_data_url": "[redacted]",
        "nested": {"api_key": "[redacted]", "message": "kept"},
    }


def test_browser_cdp_script_uses_direct_platform_agent_qr_extraction() -> None:
    script = one_shot.browser_cdp_script()

    assert "forwin-publisher-platform-agent" in script
    assert "extract-login-qr-image" in script
    assert "chrome.webNavigation.getAllFrames" in script
    assert "multipart/form-data" in script
    assert "loginQrNotificationsEnabled: false" in script
    assert "loginQrNotificationsAllowed: false" in script
    assert "open-login" not in script
    assert "page-to-extension" not in script


def test_login_url_defaults_target_platform_login_pages() -> None:
    assert one_shot.PLATFORM_LOGIN_URLS == {
        "fanqie": "https://fanqienovel.com/main/writer/login",
        "qidian": "https://write.qq.com/portal/login",
    }
