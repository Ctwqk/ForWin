from __future__ import annotations

from types import SimpleNamespace
from typing import Any

from forwin.canon.quality_preparation import (
    persist_canon_quality_attempt_trace,
)


def test_canon_preparation_context_persists_quality_attempt_telemetry() -> None:
    class AttemptClient:
        def __init__(self) -> None:
            self.events = [
                {
                    "attempt_no": 1,
                    "status": "success",
                    "model": "test-model",
                    "provider": "test-provider",
                }
            ]

        def drain_llm_attempt_events(self) -> list[dict[str, object]]:
            events = list(self.events)
            self.events.clear()
            return events

    client = AttemptClient()
    saved: list[dict[str, Any]] = []

    def save_prompt_trace(**kwargs: Any) -> str:
        saved.append(kwargs)
        return "canon-quality-trace"

    trace_id = persist_canon_quality_attempt_trace(
        llm_client=client,
        recorder=SimpleNamespace(save_prompt_trace=save_prompt_trace),
        session=object(),  # type: ignore[arg-type]
        updater=object(),  # type: ignore[arg-type]
        project_id="project-1",
        chapter_number=1,
        candidate_id="candidate-1",
    )

    assert trace_id == "canon-quality-trace"
    assert client.events == []
    assert saved[0]["prompt_trace"]["attempts"][0]["attempt_no"] == 1
