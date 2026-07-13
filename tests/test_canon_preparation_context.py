from __future__ import annotations

from typing import Any

from forwin.canon.preparation import CanonPreparationContext
from forwin.generation.pipeline_core.quality_gates import (
    _persist_canon_quality_attempt_trace,
)
from forwin.runtime.policy import RuntimePolicy


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

    context = CanonPreparationContext(
        policy=RuntimePolicy.for_profile("standard"),
        llm_client=client,  # type: ignore[arg-type]
        artifact_store=object(),  # type: ignore[arg-type]
        _record_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
        _record_rule_decision_event=lambda **_kwargs: None,  # type: ignore[arg-type]
        save_prompt_trace=save_prompt_trace,
    )

    trace_id = _persist_canon_quality_attempt_trace(
        context,
        session=object(),  # type: ignore[arg-type]
        updater=object(),  # type: ignore[arg-type]
        project_id="project-1",
        chapter_number=1,
        candidate_id="candidate-1",
    )

    assert trace_id == "canon-quality-trace"
    assert client.events == []
    assert saved[0]["prompt_trace"]["attempts"][0]["attempt_no"] == 1
