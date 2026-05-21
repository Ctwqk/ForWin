from __future__ import annotations

from types import SimpleNamespace

from forwin.writer.chapter_writer import ChapterWriter


def test_extraction_retry_uses_tail_window(monkeypatch) -> None:
    seen_bodies: list[str] = []
    writer = ChapterWriter(llm_client=SimpleNamespace(chat=lambda *args, **kwargs: "{}"))
    body = "开头铺垫" * 500 + "中段推进" * 500 + "章末他当场获得三十万赔偿，敌人失去资格。"

    def fake_chat_json(prompt: str, **kwargs):
        seen_bodies.append(prompt)
        if len(seen_bodies) == 1:
            raise RuntimeError("primary failed")
        if "三十万赔偿" in prompt:
            return {"new_events": [{"summary": "获得赔偿"}]}
        raise RuntimeError("missing tail facts")

    monkeypatch.setattr(writer, "_chat_json", fake_chat_json)
    result = writer._extract_structured_part(
        label="state_event_extraction",
        prompt_builder=lambda context, title, chapter_body: chapter_body,
        context=SimpleNamespace(),
        chapter_title="第一章",
        chapter_body=body,
        primary_temperature=0.25,
        primary_max_tokens=100,
        retry_temperature=0.2,
        retry_max_tokens=100,
    )

    assert result["new_events"][0]["summary"] == "获得赔偿"
    assert any("三十万赔偿" in item for item in seen_bodies[1:])
