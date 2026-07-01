from __future__ import annotations

import json

from scripts.qualify_linux_extension_profile import profile_extension_is_active, qualified_profile_settings


def _write_profile_fixture(tmp_path, *, marker: dict[str, object], preferences: dict[str, object]) -> None:
    profile_dir = tmp_path / "profile"
    (profile_dir / "Default").mkdir(parents=True)
    (profile_dir / ".forwin-extension-profile.json").write_text(
        json.dumps(marker, ensure_ascii=False),
        encoding="utf-8",
    )
    (profile_dir / "Default" / "Preferences").write_text(
        json.dumps(preferences, ensure_ascii=False),
        encoding="utf-8",
    )


def test_profile_extension_is_active_accepts_registered_enabled_extension(tmp_path):
    _write_profile_fixture(
        tmp_path,
        marker={
            "extensionId": "ext-123",
            "extensionDir": "/app/browser_extension/forwin-publisher",
        },
        preferences={
            "extensions": {
                "settings": {
                    "ext-123": {
                        "path": "/app/browser_extension/forwin-publisher",
                        "disable_reasons": [],
                    }
                }
            }
        },
    )

    ok, message = profile_extension_is_active(tmp_path / "profile")

    assert ok is True
    assert message == "profile extension is active"


def test_qualified_profile_settings_disables_login_qr_notifications(monkeypatch):
    monkeypatch.delenv("FORWIN_EXTENSION_SYNC_SESSION_TO_BACKEND", raising=False)

    settings = qualified_profile_settings("http://forwin:8899", "secret")

    assert settings["backendBaseUrl"] == "http://forwin:8899"
    assert settings["apiKey"] == "secret"
    assert settings["syncSessionToBackend"] is True
    assert settings["loginQrNotificationsEnabled"] is False
    assert settings["loginQrNotificationsAllowed"] is False
    assert settings["loginQrNotificationsAllowedUntilMs"] == 0


def test_profile_extension_is_active_rejects_disabled_extension(tmp_path):
    _write_profile_fixture(
        tmp_path,
        marker={
            "extensionId": "ext-123",
            "extensionDir": "/app/browser_extension/forwin-publisher",
        },
        preferences={
            "extensions": {
                "settings": {
                    "ext-123": {
                        "path": "/app/browser_extension/forwin-publisher",
                        "disable_reasons": [1 << 24],
                    }
                }
            }
        },
    )

    ok, message = profile_extension_is_active(tmp_path / "profile")

    assert ok is False
    assert "qualified extension is disabled" in message


def test_profile_extension_is_active_rejects_missing_registered_extension(tmp_path):
    _write_profile_fixture(
        tmp_path,
        marker={
            "extensionId": "ext-123",
            "extensionDir": "/app/browser_extension/forwin-publisher",
        },
        preferences={
            "extensions": {
                "settings": {}
            }
        },
    )

    ok, message = profile_extension_is_active(tmp_path / "profile")

    assert ok is False
    assert "not registered" in message
