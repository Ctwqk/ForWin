"""Actual provider input-limit responses must stop every identical-input retry."""

import httpx
import pytest

from forwin.writer.chapter_writer import ChapterWriter
from forwin.writer.llm import LLMClient
from tests.test_writer_context_budget import context


@pytest.mark.parametrize("status", [200, 400, 413, 500])
@pytest.mark.parametrize("route", ["adapter", "single", "preview", "scene"])
def test_provider_limit_never_repeats_request_or_falls_back(monkeypatch, status, route):
    client = LLMClient(
        api_key="test",
        base_url="https://primary.example/v1",
        model="primary",
        retry_attempts=3,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
        fallback_profiles=[
            {
                "id": "backup",
                "api_key": "backup",
                "base_url": "https://backup.example/v1",
                "model": "backup",
            }
        ],
    )
    calls = []

    def post(url, **kwargs):
        calls.append(kwargs)
        return httpx.Response(
            status,
            request=httpx.Request("POST", url),
            json={
                "error": {
                    "code": "context_length_exceeded",
                    "type": "invalid_request_error",
                    "message": "input has 150029 tokens; maximum context length is 50000",
                }
            },
        )

    monkeypatch.setattr(client.client, "post", post)
    try:
        writer = ChapterWriter(client, writer_mode=route)
        with pytest.raises(Exception) as caught:
            if route == "adapter":
                client.chat([{"role": "user", "content": "request"}])
            elif route == "preview":
                writer.write_preview_chapter(context())
            else:
                writer.write_chapter(context())
        assert len(calls) == 1
        assert getattr(caught.value, "error_category", None) == "input_limit"
        events = client.drain_llm_attempt_events()
        assert len(events) == 1 and events[0]["error_category"] == "input_limit"
        assert events[0]["retryable"] is False
    finally:
        client.close()


def test_wrapped_numeric_counts_do_not_look_like_http_status():
    client = LLMClient(
        api_key="test", base_url="https://primary.example/v1", model="primary"
    )
    try:
        assert not client._is_fallback_retryable(
            ValueError("input contains 150029 tokens")
        )
        assert client._is_fallback_retryable(RuntimeError("HTTP 503: overloaded"))
        assert client._is_fallback_retryable(httpx.ReadTimeout("timed out"))
    finally:
        client.close()


def test_successful_model_text_mentioning_limits_is_not_a_provider_error(monkeypatch):
    client = LLMClient(
        api_key="test", base_url="https://primary.example/v1", model="primary"
    )

    def post(url, **kwargs):
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={
                "choices": [
                    {
                        "message": {
                            "content": "maximum context length exceeded is a provider error message"
                        }
                    }
                ]
            },
        )

    monkeypatch.setattr(client.client, "post", post)
    try:
        assert client.chat([{"role": "user", "content": "explain limits"}]).startswith(
            "maximum context length"
        )
    finally:
        client.close()


@pytest.mark.parametrize("successful_calls", [1, 2, 3])
def test_late_scene_provider_limit_stops_without_retry_or_single_fallback(
    monkeypatch, successful_calls
):
    client = LLMClient(
        api_key="test",
        base_url="https://primary.example/v1",
        model="primary",
        retry_attempts=3,
        retry_initial_delay_seconds=0,
        retry_max_delay_seconds=0,
    )
    sent = []

    def post(url, **kwargs):
        sent.append(kwargs)
        if len(sent) > successful_calls:
            return httpx.Response(
                200,
                request=httpx.Request("POST", url),
                json={
                    "error": {
                        "code": "context_length_exceeded",
                        "message": "input 150029 tokens",
                    }
                },
            )
        content = (
            '{"scenes":[{"scene_no":1,"objective":"查证"}]}'
            if len(sent) == 1
            else "<<FORWIN_TITLE>>\n核对\n<<FORWIN_BODY>>\n"
            + "他们逐个核实登记簿。" * 400
            + "\n<<FORWIN_SUMMARY>>\n已核对。"
        )
        return httpx.Response(
            200,
            request=httpx.Request("POST", url),
            json={"choices": [{"message": {"content": content}}]},
        )

    monkeypatch.setattr(client.client, "post", post)
    try:
        with pytest.raises(Exception) as caught:
            ChapterWriter(client).write_chapter(context())
        assert getattr(caught.value, "error_category", "") == "input_limit"
        assert len(sent) == successful_calls + 1
        assert client.drain_llm_attempt_events()[-1]["error_category"] == "input_limit"
    finally:
        client.close()
