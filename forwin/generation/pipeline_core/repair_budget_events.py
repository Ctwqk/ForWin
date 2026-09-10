from __future__ import annotations

from typing import Any

from forwin.generation.pipeline_core.repair_budget import evaluate_repair_body_budget
from forwin.observability.pipeline_trace import PipelineTraceRecorder


def record_repair_body_budget_event(
    recorder: PipelineTraceRecorder,
    *,
    updater: Any,
    project_id: str,
    chapter_number: int,
    attempt_no: int,
    repair_scope: str,
    current_output: Any,
    rewritten_output: Any,
    design_patch: dict[str, object],
    attempt_row: Any,
    parent_event_id: str,
) -> None:
    budget_decision = evaluate_repair_body_budget(
        source_char_count=_writer_output_char_count(current_output),
        result_char_count=_writer_output_char_count(rewritten_output),
        design_patch=design_patch,
    )
    if budget_decision is None:
        return
    recorder.record_event(
        updater=updater,
        project_id=project_id,
        chapter_number=chapter_number,
        event_family="runtime_observation",
        event_type=budget_decision.event_type,
        scope="chapter",
        summary=f"第{chapter_number}章第 {attempt_no} 次 repair 正文超出预算护栏。",
        reason=budget_decision.reason,
        related_object_type="chapter_rewrite_attempt",
        related_object_id=attempt_row.id,
        payload={
            **budget_decision.payload,
            "attempt_no": attempt_no,
            "repair_scope": repair_scope,
        },
        parent_event_id=parent_event_id,
    )


def _writer_output_char_count(output: Any) -> int:
    try:
        char_count = int(output.char_count or 0)
    except (AttributeError, TypeError, ValueError):
        char_count = 0
    if char_count > 0:
        return char_count
    return len(str(getattr(output, "body", "") or ""))


__all__ = ["record_repair_body_budget_event"]
