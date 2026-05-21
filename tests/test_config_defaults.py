from __future__ import annotations

from forwin.config import Config


def test_world_v4_projection_flags_are_removed() -> None:
    config = Config()

    assert not hasattr(config, "world_v4_compat_write_enabled")
    assert not hasattr(config, "enable_world_v4_debug_api")


def test_review_engine_repair_v2_is_disabled_by_default() -> None:
    assert Config().review_engine_repair_v2_enabled is False
    assert Config().review_engine_arc_patcher_enabled is False
    assert Config().review_engine_book_patcher_enabled is False
    assert Config().review_engine_obligation_verifier_enabled is False
    assert Config().review_engine_auto_approve_enabled is False


def test_review_engine_repair_v2_can_be_enabled_from_env(monkeypatch) -> None:
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_REPAIR_V2_ENABLED", "true")
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_ARC_PATCHER_ENABLED", "true")
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_BOOK_PATCHER_ENABLED", "true")
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_OBLIGATION_VERIFIER_ENABLED", "true")
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_AUTO_APPROVE_ENABLED", "true")

    assert Config.from_env().review_engine_repair_v2_enabled is True
    assert Config.from_env().review_engine_arc_patcher_enabled is True
    assert Config.from_env().review_engine_book_patcher_enabled is True
    assert Config.from_env().review_engine_obligation_verifier_enabled is True
    assert Config.from_env().review_engine_auto_approve_enabled is True


def test_config_exposes_domain_writer_profile_without_removing_flat_fields() -> None:
    config = Config(
        temperature=0.6,
        default_scene_count=2,
        max_scene_count=5,
        min_chapter_chars=1000,
        target_chapter_chars=1500,
        max_chapter_chars=2000,
        prompt_budget_chars=9000,
    )

    assert config.temperature == 0.6
    assert config.writer.temperature == 0.6
    assert config.writer.default_scene_count == 2
    assert config.writer.max_scene_count == 5
    assert config.writer.target_chapter_chars == 1500
    assert config.writer.prompt_budget_chars == 9000
    assert config.llm.max_tokens == config.max_tokens
    assert config.storage.database_url == config.database_url


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
