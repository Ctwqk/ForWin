"""Writer execution telemetry using the caller's transaction and causal root."""

from __future__ import annotations

import logging
from typing import Any

from forwin.audit.events import DecisionEventType
from forwin.observability.llm_trace import safe_prompt_trace_attempts
from forwin.observability.payloads import (
    attempt_group_ids,
    audit_payload,
    safe_error_summary,
)
from forwin.observability.pipeline_trace import PipelineTraceRecorder
from forwin.protocol.writer import WriterOutput
from forwin.skills import summarize_skill_layers
from forwin.state.updater import StateUpdater

from .execution_errors import diagnostic_kind_for_failure, error_category_from_attempts

logger = logging.getLogger(__name__)


class WriterExecutionTelemetry:
    def __init__(self, *, recorder: PipelineTraceRecorder, writer, model_client):
        self.recorder = recorder
        self.writer = writer
        self.model_client = model_client
        self.artifact_store = recorder.artifact_store

    def operation_id(self) -> str:
        return self.recorder.audit.operation_id

    def record_event(self, **kwargs):
        return self.recorder.record_event(**kwargs)

    def save_prompt_trace(self, **kwargs):
        return self.recorder.save_prompt_trace(**kwargs)

    def model_identity(self) -> tuple[str, str]:
        return (
            str(getattr(self.model_client, "profile_id", "") or "").strip(),
            str(getattr(self.model_client, "model", "") or "").strip(),
        )

    def drain_attempts(self) -> list[dict[str, object]]:
        drain = getattr(
            getattr(self.writer, "llm_client", None), "drain_llm_attempt_events", None
        )
        if not callable(drain):
            return []
        events = drain()
        return (
            [dict(item) for item in events if isinstance(item, dict)]
            if isinstance(events, list)
            else []
        )

    def record_failure_trace(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        context,
        stage_key: str,
        template_id: str,
        source_event_id: str,
        exc: BaseException,
        duration_ms: int,
        attempts: list[dict[str, object]],
        skill_layers: list[object] | None,
        fallback_stage: str = "",
    ) -> str:
        if not isinstance(updater, StateUpdater):
            return ""
        fallback_attempt_no = 0
        if attempts:
            try:
                fallback_attempt_no = int(attempts[-1].get("attempt_no") or 0)
            except (TypeError, ValueError):
                fallback_attempt_no = 0
        safe_attempts = safe_prompt_trace_attempts(
            attempts,
            fallback_attempt_no=fallback_attempt_no,
            exc=exc,
            duration_ms=duration_ms,
        )
        error_category = error_category_from_attempts(safe_attempts, exc)
        selected_skills = summarize_skill_layers(skill_layers)
        operation_id = self.operation_id()
        model_profile_id, model_name = self.model_identity()
        drain_feedback = getattr(self.writer, "drain_feedback_inputs", None)
        feedback_inputs = drain_feedback() if callable(drain_feedback) else []
        trace_payload = {
            "trace_scope": "writer",
            "stage_key": stage_key,
            "template_id": template_id,
            "template_version": "v1",
            "effective_system_prompt": "",
            "prompt_layers": [],
            "input_snapshot": audit_payload(
                stage=stage_key,
                status="failed",
                operation_id=operation_id,
                chapter_number=chapter_number,
                writer_mode=str(getattr(self.writer, "writer_mode", "") or ""),
                selected_skills=selected_skills,
                feedback_inputs=feedback_inputs,
            ),
            "model_profile": {
                "profile_id": model_profile_id,
                "model": model_name,
                "base_url": str(getattr(self.model_client, "base_url", "") or ""),
            },
            "attempts": safe_attempts,
            "output_summary": audit_payload(
                stage=stage_key,
                status="failed",
                operation_id=operation_id,
                duration_ms=duration_ms,
                error_category=error_category,
                chapter_number=chapter_number,
                context_chapter_number=int(
                    getattr(context, "chapter_number", chapter_number) or chapter_number
                ),
                error_class=exc.__class__.__name__,
                error_summary=safe_error_summary(exc),
                fallback_stage=fallback_stage,
                attempt_count=len(safe_attempts),
                attempt_group_ids=attempt_group_ids(safe_attempts),
            ),
        }
        trace_id = self.save_prompt_trace(
            session=updater.session,
            updater=updater,
            project_id=project_id,
            prompt_trace=trace_payload,
            decision_event_id=source_event_id,
        )
        artifact_manifest: list[dict[str, object]] = []
        try:
            manifest = self.artifact_store.save_observability_diagnostic(
                project_id=project_id,
                chapter_number=chapter_number,
                kind=diagnostic_kind_for_failure(exc, error_category),
                source_event_id=source_event_id,
                trace_id=trace_id,
                payload={
                    "schema_version": "v4.5.1-audit",
                    "project_id": project_id,
                    "chapter_number": chapter_number,
                    "stage": stage_key,
                    "status": "failed",
                    "operation_id": operation_id,
                    "error_class": exc.__class__.__name__,
                    "error_summary": safe_error_summary(exc),
                    "error_category": error_category,
                    "attempts": safe_attempts,
                    "selected_skills": selected_skills,
                },
            )
            artifact_manifest.append(manifest)
        except Exception:
            logger.warning(
                "Failed to persist observability diagnostic artifact.", exc_info=True
            )
        self.record_event(
            updater=updater,
            project_id=project_id,
            chapter_number=chapter_number,
            event_family="runtime_observation",
            event_type=DecisionEventType.PROMPT_TRACE_RECORDED,
            scope="chapter",
            summary=f"第{chapter_number}章失败 prompt trace 已落盘。",
            parent_event_id=source_event_id,
            related_object_type="prompt_trace",
            related_object_id=trace_id,
            payload=audit_payload(
                stage=stage_key,
                status="failed",
                operation_id=operation_id,
                duration_ms=duration_ms,
                error_category=error_category,
                trace_id=trace_id,
                source_event_id=source_event_id,
                artifact_manifest=artifact_manifest,
            ),
        )
        return trace_id

    def record_model_fallbacks(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        chapter_number: int,
        parent_stage: str,
        events: list[dict[str, Any]],
    ) -> None:
        for item in events:
            if not isinstance(item, dict):
                continue
            self.record_event(
                updater=updater,
                project_id=project_id,
                chapter_number=chapter_number,
                event_family="runtime_observation",
                event_type=DecisionEventType.FALLBACK_PROFILE_SWITCHED,
                scope="chapter",
                summary=(
                    f"writer fallback: {item.get('from_model') or '-'!s} -> "
                    f"{item.get('to_model') or '-'!s}"
                ),
                payload=audit_payload(
                    stage=parent_stage,
                    status="profile_switched",
                    operation_id=self.operation_id(),
                    model_profile_id=str(item.get("to_profile_id") or ""),
                    model=str(item.get("to_model") or ""),
                    error_summary=safe_error_summary(str(item.get("reason") or "")),
                    from_model_profile_id=str(item.get("from_profile_id") or ""),
                    from_model=str(item.get("from_model") or ""),
                ),
            )


def prompt_trace_success_summary(
    writer_output: WriterOutput,
) -> dict[str, object]:
    generation_meta = getattr(writer_output, "generation_meta", {}) or {}
    prompt_trace = (
        generation_meta.get("prompt_trace") if isinstance(generation_meta, dict) else {}
    )
    attempts = (
        prompt_trace.get("attempts", []) if isinstance(prompt_trace, dict) else []
    )
    if not isinstance(attempts, list):
        attempts = []
    successful = None
    for item in attempts:
        if not isinstance(item, dict):
            continue
        if int(item.get("output_chars") or 0) > 0 and not str(
            item.get("error_class") or ""
        ):
            successful = item
    if successful is None and attempts:
        successful = next(
            (item for item in reversed(attempts) if isinstance(item, dict)),
            None,
        )
    if not isinstance(successful, dict):
        return {
            "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
            "effective_model": "",
            "effective_profile_id": "",
            "successful_attempt_no": 0,
            "attempt_group_id": "",
            "output_chars": int(getattr(writer_output, "char_count", 0) or 0),
            "fallback_chain": generation_meta.get("model_fallbacks", []),
        }
    return {
        "prompt_trace_id": str(generation_meta.get("prompt_trace_id", "") or ""),
        "effective_model": str(successful.get("model") or ""),
        "effective_profile_id": str(successful.get("profile_id") or ""),
        "effective_profile_name": str(successful.get("profile_name") or ""),
        "successful_attempt_no": int(successful.get("attempt_no") or 0),
        "attempt_group_id": str(successful.get("attempt_group_id") or ""),
        "output_chars": int(
            successful.get("output_chars")
            or getattr(writer_output, "char_count", 0)
            or 0
        ),
        "fallback_chain": generation_meta.get("model_fallbacks", []),
    }
