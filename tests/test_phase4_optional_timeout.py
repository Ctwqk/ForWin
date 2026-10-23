from __future__ import annotations

from forwin.audience.comment_analysis import _read_optional_phase4_llm_timeout_seconds


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


def test_comment_analysis_timeout_falls_back_to_language_cues() -> None:
    from types import SimpleNamespace
    from forwin.audience.comment_analysis import CommentAnalyzer, _keyword_fallback
    calls = []

    def chat(messages, **kwargs):
        calls.append(kwargs)
        raise TimeoutError("optional analysis timed out")

    analyzer = CommentAnalyzer(llm_client=SimpleNamespace(chat=chat))
    comment = SimpleNamespace(id="comment-1", body_text="逻辑矛盾")
    assert analyzer._analyze_comments_with_llm([comment]) is None
    assert calls[0]["stage_key"] == "comment_analysis"
    assert calls[0]["timeout_seconds"] > 0
    assert calls[0]["retry_on_timeout"] is False
    assert _keyword_fallback(comment.body_text)[0].signal_type == "risk"
