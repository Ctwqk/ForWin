from __future__ import annotations

from forwin.orchestrator.phase4 import _read_optional_phase4_llm_timeout_seconds


def test_phase4_optional_llm_timeout_defaults_to_runtime_safe_budget() -> None:
    assert _read_optional_phase4_llm_timeout_seconds({}) == 20.0


def test_phase4_optional_llm_timeout_can_be_overridden() -> None:
    assert (
        _read_optional_phase4_llm_timeout_seconds(
            {"FORWIN_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS": "12.5"}
        )
        == 12.5
    )


def test_phase4_optional_llm_timeout_ignores_invalid_values() -> None:
    assert (
        _read_optional_phase4_llm_timeout_seconds(
            {"FORWIN_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS": "invalid"}
        )
        == 20.0
    )
    assert (
        _read_optional_phase4_llm_timeout_seconds(
            {"FORWIN_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS": "-1"}
        )
        == 20.0
    )
