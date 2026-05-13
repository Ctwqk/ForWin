from __future__ import annotations

from forwin.config import Config


def test_canon_quality_config_defaults() -> None:
    config = Config()

    assert config.canon_quality_gate == "strict"
    assert config.final_completion_gate == "strict"
    assert config.style_telemetry_mode == "warn"


def test_canon_quality_config_reads_env(monkeypatch) -> None:  # noqa: ANN001
    monkeypatch.setenv("FORWIN_CANON_QUALITY_GATE", "shadow")
    monkeypatch.setenv("FORWIN_FINAL_COMPLETION_GATE", "off")
    monkeypatch.setenv("FORWIN_STYLE_TELEMETRY_MODE", "shadow")

    config = Config.from_env()

    assert config.canon_quality_gate == "shadow"
    assert config.final_completion_gate == "off"
    assert config.style_telemetry_mode == "shadow"
