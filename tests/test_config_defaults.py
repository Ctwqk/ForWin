from __future__ import annotations

from forwin.config import Config


def test_world_v4_compat_write_is_disabled_by_default() -> None:
    assert Config().world_v4_compat_write_enabled is False


def test_lan_bind_requires_basic_auth_or_explicit_unauthenticated_override() -> None:
    try:
        Config(http_bind="192.168.1.10")
    except ValueError as exc:
        assert "FORWIN_HTTP_BASIC_USER/PASSWORD" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("LAN bind without Basic Auth should be rejected")

    assert Config(
        http_bind="192.168.1.10",
        http_basic_user="alice",
        http_basic_password="secret",
    ).http_bind == "192.168.1.10"
    assert Config(
        http_bind="192.168.1.10",
        allow_unauthenticated_lan=True,
    ).allow_unauthenticated_lan is True


def test_bind_all_interfaces_requires_explicit_confirmation() -> None:
    try:
        Config(
            http_bind="0.0.0.0",
            http_basic_user="alice",
            http_basic_password="secret",
        )
    except ValueError as exc:
        assert "FORWIN_ALLOW_BIND_ALL_INTERFACES=true" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("0.0.0.0 bind should require explicit confirmation")


def test_publisher_profile_requires_session_secret_and_encryption() -> None:
    try:
        Config(publisher_extension_api_key="extension-secret")
    except ValueError as exc:
        assert "FORWIN_PUBLISHER_SESSION_SECRET" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("publisher extension profile should require session secret")

    try:
        Config(
            publisher_extension_api_key="extension-secret",
            publisher_session_secret="change-me-session-secret",
            publisher_session_encryption_required=True,
        )
    except ValueError as exc:
        assert "placeholder" in str(exc)
    else:  # pragma: no cover
        raise AssertionError("placeholder publisher session secret should be rejected")
