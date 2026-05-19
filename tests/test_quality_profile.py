from __future__ import annotations

from pathlib import Path

import pytest

from forwin.config import Config


PROFILE_ENV_KEYS = {
    "AUTO_BAND_CHECKPOINT",
    "EXPERIENCE_REVIEW_ENABLED",
    "FORWIN_BAND_CHECKPOINT_MODE",
    "FORWIN_BOOK_STATE_LAYERS",
    "FORWIN_CANON_QUALITY_GATE",
    "FORWIN_CANON_QUALITY_REVIEW_IN_HUB_ENABLED",
    "FORWIN_CONTEXT_RECENCY_WINDOW_CHAPTERS",
    "FORWIN_ENV_FILE",
    "FORWIN_FINAL_GATE_MODE",
    "FORWIN_HARD_FLOOR_GATE_ENABLED",
    "FORWIN_MAP_MOVEMENT_REVIEW_ENABLED",
    "FORWIN_PERSONALITY_REVIEW_ENABLED",
    "FORWIN_PLAN_PATCH_VALIDATION_MODE",
    "FORWIN_PLANNING_AUDIT_MODE",
    "FORWIN_QUALITY_PROFILE",
    "FORWIN_REVIEWER_QUALITY_MODE",
    "FORWIN_WORLD_V4_COMPAT_WRITE",
    "FREEZE_FAILED_CANDIDATES",
    "FUTURE_CONSTRAINTS_ENABLED",
    "GENERATION_AUDIT_INTERVAL_CHAPTERS",
    "GENERATION_AUDIT_PAUSE_ENABLED",
    "LINT_REVIEW_ENABLED",
    "MANUAL_CHECKPOINTS_ENABLED",
    "MAX_CHAPTER_CHARS",
    "MIN_CHAPTER_CHARS",
    "OPERATION_MODE",
    "PHASE4_USE_LLM",
    "REVIEW_FAIL_MAX_REWRITES",
    "REVIEW_INTERVAL_CHAPTERS",
    "TARGET_CHAPTER_CHARS",
    "WRITER_MODE",
}


def config_from_env(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
    values: dict[str, str],
) -> Config:
    env_file = tmp_path / "forwin.env"
    env_file.write_text("", encoding="utf-8")
    for key in PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("FORWIN_ENV_FILE", str(env_file))
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def test_pulp_profile_derives_low_cost_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = config_from_env(monkeypatch, tmp_path, {"FORWIN_QUALITY_PROFILE": "pulp"})

    assert config.quality_profile == "pulp"
    assert config.writer_mode == "single"
    assert config.operation_mode == "blackbox"
    assert config.review_interval_chapters == 0
    assert config.experience_review_enabled is False
    assert config.lint_review_enabled is True
    assert config.canon_quality_gate == "fatal_only"
    assert config.freeze_failed_candidates is False
    assert config.review_fail_max_rewrites == 0
    assert config.auto_band_checkpoint is False
    assert config.manual_checkpoints_enabled is False
    assert config.future_constraints_enabled is False
    assert config.generation_audit_interval_chapters == 0
    assert config.generation_audit_pause_enabled is False
    assert config.world_v4_compat_write_enabled is False
    assert config.phase4_use_llm is False
    assert config.reviewer_quality_mode == "deterministic"
    assert config.planning_audit_mode == "off"
    assert config.plan_patch_validation_mode == "off"
    assert config.final_gate_mode == "off"
    assert config.band_checkpoint_mode == "off"
    assert config.min_chapter_chars == 1800
    assert config.target_chapter_chars == 2400
    assert config.max_chapter_chars == 3000
    assert config.book_state_layers == ["world"]
    assert config.hard_floor_gate_enabled is True
    assert config.context_recency_window_chapters == 50
    assert config.map_movement_review_enabled is False
    assert config.personality_review_enabled is False
    assert config.canon_quality_review_in_hub_enabled is False


def test_explicit_env_wins_over_pulp_profile(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = config_from_env(
        monkeypatch,
        tmp_path,
        {
            "FORWIN_QUALITY_PROFILE": "pulp",
            "WRITER_MODE": "scene",
            "FORWIN_CANON_QUALITY_GATE": "strict",
            "FORWIN_HARD_FLOOR_GATE_ENABLED": "false",
        },
    )

    assert config.writer_mode == "scene"
    assert config.canon_quality_gate == "strict"
    assert config.hard_floor_gate_enabled is False


def test_standard_profile_keeps_representative_defaults(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = config_from_env(
        monkeypatch,
        tmp_path,
        {"FORWIN_QUALITY_PROFILE": "standard"},
    )

    assert config.quality_profile == "standard"
    assert config.writer_mode == "scene"
    assert config.canon_quality_gate == "strict"
    assert config.book_state_layers == ["world", "map", "cognition", "narrative"]
    assert config.hard_floor_gate_enabled is False
    assert config.context_recency_window_chapters == 0
    assert config.map_movement_review_enabled is True
    assert config.personality_review_enabled is True
    assert config.canon_quality_review_in_hub_enabled is True


def test_premium_profile_is_currently_identity(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    config = config_from_env(
        monkeypatch,
        tmp_path,
        {"FORWIN_QUALITY_PROFILE": "premium"},
    )

    assert config.quality_profile == "premium"
    assert config.writer_mode == "scene"
    assert config.canon_quality_gate == "strict"
    assert config.book_state_layers == ["world", "map", "cognition", "narrative"]
    assert config.hard_floor_gate_enabled is False
