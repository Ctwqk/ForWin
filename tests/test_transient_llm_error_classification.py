from __future__ import annotations

import pytest

from forwin.generation.pipeline import ChapterPipeline
from forwin.writer.llm.errors import ErrorsMixin


def _wrapped(message: str) -> Exception:
    try:
        try:
            raise RuntimeError(message)
        except RuntimeError as inner:
            raise ValueError("ChapterWriter generation failed after retries") from inner
    except ValueError as outer:
        return outer


@pytest.mark.parametrize(
    "message",
    [
        "Server error '529 Unknown Status Code' for url 'https://api.example/v1/chat'",
        "[Errno 61] Connection refused",
        "RemoteProtocolError: peer closed connection without sending complete message body",
        "HTTP 529 overloaded",
        "429 Too Many Requests",
        "The read operation timed out",
    ],
)
def test_writer_and_pipeline_agree_on_transient_llm_errors(message: str) -> None:
    exc = _wrapped(message)

    assert ErrorsMixin._is_fallback_retryable(exc) is True
    assert ChapterPipeline._is_transient_llm_like(exc) is True


def test_writer_and_pipeline_agree_on_non_transient_llm_errors() -> None:
    exc = _wrapped("invalid api key")

    assert ErrorsMixin._is_fallback_retryable(exc) is False
    assert ChapterPipeline._is_transient_llm_like(exc) is False
