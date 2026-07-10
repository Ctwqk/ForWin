from __future__ import annotations

import pytest

from forwin.config import InfrastructureConfig
from forwin.runtime.policy import RuntimePolicy


def test_infrastructure_config_has_no_generation_policy_fields() -> None:
    config = InfrastructureConfig()
    for removed in (
        "quality_profile",
        "operation_mode",
        "writer_mode",
        "progression_mode",
        "review_delegation_mode",
        "freeze_failed_candidates",
        "review_interval_chapters",
        "review_engine_repair_v2_enabled",
        "review_engine_arc_patcher_enabled",
        "canon_quality_gate",
        "chapter_review_form_mode",
        "reviewer_quality_mode",
        "planning_audit_mode",
        "final_gate_mode",
        "band_checkpoint_mode",
        "provisional_preview_enabled",
        "final_completion_gate",
        "style_telemetry_mode",
    ):
        assert not hasattr(config, removed)


def test_model_profile_resolution_is_environment_only(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KIMI_API_KEY", "secret")
    config = InfrastructureConfig.from_env()

    profile = config.resolve_model_profile("env-kimi")

    assert profile.model
    assert profile.api_key == "secret"
    with pytest.raises(ValueError, match="Unknown model profile"):
        config.resolve_model_profile("saved-browser-profile")


def test_writer_profile_requires_explicit_runtime_policy() -> None:
    config = InfrastructureConfig(
        temperature=0.6,
        default_scene_count=2,
        max_scene_count=5,
        prompt_budget_chars=9000,
    )
    profile = config.writer_profile(
        RuntimePolicy.for_profile("standard").with_user_settings(
            min_chars=1000,
            target_chars=1500,
            max_chars=2000,
        )
    )

    assert profile.temperature == 0.6
    assert profile.default_scene_count == 2
    assert profile.max_scene_count == 5
    assert profile.min_chapter_chars == 1000
    assert profile.target_chapter_chars == 1500
    assert profile.max_chapter_chars == 2000
    assert profile.prompt_budget_chars == 9000


def test_lan_bind_requires_basic_auth_or_explicit_unauthenticated_override() -> None:
    with pytest.raises(ValueError, match="FORWIN_HTTP_BASIC_USER/PASSWORD"):
        InfrastructureConfig(http_bind="192.168.1.10")

    assert InfrastructureConfig(
        http_bind="192.168.1.10",
        http_basic_user="alice",
        http_basic_password="secret",
    ).http_bind == "192.168.1.10"
    assert InfrastructureConfig(
        http_bind="192.168.1.10",
        allow_unauthenticated_lan=True,
    ).allow_unauthenticated_lan is True


def test_bind_all_interfaces_requires_explicit_confirmation() -> None:
    with pytest.raises(ValueError, match="FORWIN_ALLOW_BIND_ALL_INTERFACES=true"):
        InfrastructureConfig(
            http_bind="0.0.0.0",
            http_basic_user="alice",
            http_basic_password="secret",
        )


def test_publisher_profile_requires_session_secret_and_encryption() -> None:
    with pytest.raises(ValueError, match="FORWIN_PUBLISHER_SESSION_SECRET"):
        InfrastructureConfig(publisher_extension_api_key="extension-secret")

    with pytest.raises(ValueError, match="placeholder"):
        InfrastructureConfig(
            publisher_extension_api_key="extension-secret",
            publisher_session_secret="change-me-session-secret",
            publisher_session_encryption_required=True,
        )
