from __future__ import annotations

from forwin.config import Config


def test_canon_quality_config_defaults() -> None:
    config = Config()

    assert config.canon_quality_gate == "strict"
    assert config.chapter_review_form_mode == "primary"
    assert config.chapter_review_form_min_blocking_confidence == 0.8
    assert config.chapter_review_form_max_llm_retries == 1
    assert config.chapter_review_form_token_budget_chars == 8000
    assert config.reviewer_quality_mode == "hybrid"
    assert config.planning_audit_mode == "hybrid"
    assert config.final_gate_mode == "hybrid"
    assert config.band_checkpoint_mode == "hybrid"
    assert config.final_completion_gate == "strict"
    assert config.style_telemetry_mode == "warn"


def test_canon_quality_config_reads_env(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("FORWIN_CANON_QUALITY_GATE", "shadow")
    monkeypatch.setenv("FORWIN_CHAPTER_REVIEW_FORM_MODE", "off")
    monkeypatch.setenv("FORWIN_CHAPTER_REVIEW_FORM_MIN_BLOCKING_CONFIDENCE", "0.9")
    monkeypatch.setenv("FORWIN_CHAPTER_REVIEW_FORM_MAX_LLM_RETRIES", "2")
    monkeypatch.setenv("FORWIN_CHAPTER_REVIEW_FORM_TOKEN_BUDGET_CHARS", "5000")
    monkeypatch.setenv("FORWIN_REVIEWER_QUALITY_MODE", "shadow")
    monkeypatch.setenv("FORWIN_PLANNING_AUDIT_MODE", "deterministic")
    monkeypatch.setenv("FORWIN_FINAL_GATE_MODE", "chapter_review_form")
    monkeypatch.setenv("FORWIN_BAND_CHECKPOINT_MODE", "shadow")
    monkeypatch.setenv("FORWIN_FINAL_COMPLETION_GATE", "off")
    monkeypatch.setenv("FORWIN_STYLE_TELEMETRY_MODE", "shadow")

    config = Config.from_env()

    assert config.canon_quality_gate == "shadow"
    assert config.chapter_review_form_mode == "off"
    assert config.chapter_review_form_min_blocking_confidence == 0.9
    assert config.chapter_review_form_max_llm_retries == 2
    assert config.chapter_review_form_token_budget_chars == 5000
    assert config.reviewer_quality_mode == "shadow"
    assert config.planning_audit_mode == "deterministic"
    assert config.final_gate_mode == "chapter_review_form"
    assert config.band_checkpoint_mode == "shadow"
    assert config.final_completion_gate == "off"
    assert config.style_telemetry_mode == "shadow"
