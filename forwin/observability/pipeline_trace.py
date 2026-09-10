"""Transaction-bound pipeline event and prompt-trace recording.

Only task/root identity is shared with execution owners; no pipeline callbacks.
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
    ensure_decision_event_type,
)
from forwin.models import new_id
from forwin.models.audit import DecisionEvent
from forwin.models.project import Project
from forwin.observability.context import OperationContext
from forwin.observability.llm_trace import (
    build_llm_decision_event_payloads,
    prepare_prompt_trace_payload,
)
from forwin.observability.redaction import redact_payload
from forwin.observability.spans import SpanRecord, current_span
from forwin.review.decision.audit import (
    build_decision_event_payload,
    digest_decision_input,
)
from forwin.review.decision.types import Decision, DecisionInput
from forwin.state.updater import StateUpdater

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class PipelineAuditContext:
    task_id: str = ""
    root_event_id: str = ""

    @property
    def operation_id(self) -> str:
        return str(self.task_id or self.root_event_id or "").strip()


class PipelineTraceRecorder:
    def __init__(self, *, audit: PipelineAuditContext, artifact_store, observability):
        self.audit = audit
        self.artifact_store = artifact_store
        self.observability = observability

    def record_event(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        event_family: str,
        event_type: str,
        summary: str,
        reason: str = "",
        scope: str = "project",
        actor_type: str = "system",
        actor_id: str = "",
        band_id: str = "",
        chapter_number: int = 0,
        task_id: str = "",
        related_object_type: str = "",
        related_object_id: str = "",
        payload: dict[str, Any] | None = None,
        parent_event_id: str = "",
        causal_root_id: str = "",
    ):
        row = updater.save_decision_event(
            DecisionEventInfo(
                project_id=project_id,
                task_id=task_id or self.audit.task_id,
                band_id=band_id,
                chapter_number=chapter_number,
                scope=scope,
                event_family=event_family,
                event_type=ensure_decision_event_type(event_type),
                actor_type=actor_type,
                actor_id=actor_id,
                summary=summary,
                reason=reason,
                payload=payload or {},
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                parent_event_id=parent_event_id,
                causal_root_id=causal_root_id or self.audit.root_event_id,
            )
        )
        if not self.audit.root_event_id:
            self.audit.root_event_id = str(row.causal_root_id or row.id or "")
        return row

    def save_prompt_trace(
        self,
        *,
        session: Session,
        updater: StateUpdater,
        project_id: str,
        prompt_trace: dict[str, object] | None,
        parent_trace_id: str = "",
        decision_event_id: str = "",
    ) -> str:
        payload = prompt_trace if isinstance(prompt_trace, dict) else {}
        if not payload:
            return ""
        input_snapshot = (
            payload.get("input_snapshot")
            if isinstance(payload.get("input_snapshot"), dict)
            else {}
        )
        output_summary = (
            payload.get("output_summary")
            if isinstance(payload.get("output_summary"), dict)
            else {}
        )
        trace_chapter_number = int(
            (input_snapshot or {}).get("chapter_number")
            or (output_summary or {}).get("chapter_number")
            or 0
        )
        payload = prepare_prompt_trace_payload(
            payload,
            artifact_store=self.artifact_store,
            project_id=project_id,
            chapter_number=trace_chapter_number,
        )
        project = session.get(Project, project_id)
        row = updater.save_prompt_trace(
            project_id=project_id,
            genesis_revision_id=str(
                getattr(project, "active_genesis_revision_id", "") or ""
            ),
            decision_event_id=str(decision_event_id or "").strip(),
            parent_trace_id=str(parent_trace_id or "").strip(),
            trace_scope=str(payload.get("trace_scope", "writer") or "writer"),
            stage_key=str(payload.get("stage_key", "") or ""),
            template_id=str(payload.get("template_id", "") or ""),
            template_version=str(payload.get("template_version", "v1") or "v1"),
            effective_system_prompt=str(
                payload.get("effective_system_prompt", "") or ""
            ),
            prompt_layers_json=json.dumps(
                payload.get("prompt_layers", []), ensure_ascii=False
            ),
            input_snapshot_json=json.dumps(
                payload.get("input_snapshot", {}), ensure_ascii=False
            ),
            model_profile_json=json.dumps(
                payload.get("model_profile", {}), ensure_ascii=False
            ),
            attempts_json=json.dumps(payload.get("attempts", []), ensure_ascii=False),
            output_summary_json=json.dumps(
                payload.get("output_summary", {}), ensure_ascii=False
            ),
            backend=str(payload.get("backend", "") or ""),
            codex_job_id=str(payload.get("codex_job_id", "") or ""),
            permission_profile=str(payload.get("permission_profile", "") or ""),
            fallback_used=bool(payload.get("fallback_used", False)),
        )
        for event_payload in build_llm_decision_event_payloads(
            payload, prompt_trace_id=row.id
        ):
            self.record_event(
                updater=updater,
                project_id=project_id,
                chapter_number=trace_chapter_number,
                event_family=str(
                    event_payload.get("event_family") or "runtime_observation"
                ),
                event_type=str(
                    event_payload.get("event_type")
                    or DecisionEventType.LLM_REQUEST_FAILED
                ),
                scope="chapter" if trace_chapter_number else "project",
                summary=str(event_payload.get("summary") or "LLM trace event."),
                payload=event_payload.get("payload")
                if isinstance(event_payload.get("payload"), dict)
                else {},
                related_object_type="prompt_trace",
                related_object_id=row.id,
                parent_event_id=str(decision_event_id or "").strip(),
            )
        self.record_performance_spans(
            project_id=project_id,
            chapter_number=trace_chapter_number,
            prompt_trace_id=row.id,
            trace_payload=payload,
        )
        return row.id

    def record_performance_spans(
        self,
        *,
        project_id: str,
        chapter_number: int,
        prompt_trace_id: str,
        trace_payload: dict[str, object],
    ) -> None:
        attempts = (
            trace_payload.get("attempts") if isinstance(trace_payload, dict) else []
        )
        if not isinstance(attempts, list):
            return
        trace_scope = str(trace_payload.get("trace_scope") or "llm").strip() or "llm"
        fallback_stage = str(trace_payload.get("stage_key") or "").strip()
        for attempt in attempts:
            if not isinstance(attempt, dict):
                continue
            stage_key = str(attempt.get("stage_key") or fallback_stage or "").strip()
            try:
                duration_ms = max(0, int(attempt.get("duration_ms") or 0))
            except (TypeError, ValueError):
                duration_ms = 0
            tags = redact_payload(
                {
                    "prompt_trace_id": prompt_trace_id,
                    "trace_scope": trace_scope,
                    "stage_key": stage_key,
                    "profile_id": str(attempt.get("profile_id") or ""),
                    "profile_name": str(attempt.get("profile_name") or ""),
                    "model": str(attempt.get("model") or ""),
                    "llm_task_route": str(attempt.get("llm_task_route") or ""),
                    "http_status": int(attempt.get("http_status") or 0),
                    "attempt_no": int(attempt.get("attempt_no") or 0),
                    "attempt_group_id": str(attempt.get("attempt_group_id") or ""),
                    "retryable": bool(attempt.get("retryable", False)),
                    "fallback_eligible": bool(attempt.get("fallback_eligible", False)),
                    "final_failure": bool(attempt.get("final_failure", False)),
                    "parse_ok": bool(attempt.get("parse_ok", True)),
                    "schema_ok": bool(attempt.get("schema_ok", True)),
                }
            )
            metrics = {
                "input_chars": int(attempt.get("input_chars") or 0),
                "output_chars": int(attempt.get("output_chars") or 0),
                "sleep_ms": int(attempt.get("sleep_ms") or 0),
            }
            failed = bool(
                attempt.get("error_class")
                or attempt.get("final_failure")
                or attempt.get("parse_error")
            )
            error = {}
            if failed:
                error = redact_payload(
                    {
                        "error_class": str(attempt.get("error_class") or ""),
                        "error_message": str(
                            attempt.get("error_message")
                            or attempt.get("parse_error")
                            or attempt.get("error_category")
                            or ""
                        ),
                        "error_category": str(attempt.get("error_category") or ""),
                    }
                )
            context = OperationContext(
                project_id=project_id,
                task_id=self.audit.task_id,
                chapter_number=int(chapter_number or 0),
                stage=stage_key,
                operation_id=self.audit.operation_id,
            )
            parent_span = current_span()
            record = SpanRecord(
                context=context,
                span_name="llm.request",
                span_kind="llm",
                component=trace_scope,
                tags=tags,
                metrics=metrics,
                status="failed" if failed else "ok",
                error=error,
                trace_id=str(getattr(parent_span, "trace_id", "") or prompt_trace_id),
                span_id=new_id(),
                parent_span_id=str(getattr(parent_span, "span_id", "") or ""),
                start_time_unix_ms=int(time.time() * 1000),
                duration_ms=duration_ms,
                self_duration_ms=duration_ms,
            )
            try:
                self.observability._record_span(record)
            except Exception:
                logger.debug(
                    "Ignoring prompt trace performance span failure.", exc_info=True
                )

    def record_rule_decision(
        self,
        *,
        updater: StateUpdater,
        decision: Decision,
        decision_input: DecisionInput,
        related_object_type: str = "",
        related_object_id: str = "",
        parent_event_id: str = "",
    ) -> DecisionEvent | None:
        try:
            payload = build_decision_event_payload(
                decision=decision,
                input_digest=digest_decision_input(decision_input),
            )
            return self.record_event(
                updater=updater,
                project_id=decision_input.project_id,
                chapter_number=decision_input.chapter_number,
                event_family="evaluation_verdict",
                event_type=DecisionEventType.RULE_DECISION_EVALUATED,
                scope="chapter",
                summary=f"engine decided {decision.outcome} via {decision.rule_id}",
                reason=decision.reason,
                related_object_type=related_object_type,
                related_object_id=related_object_id,
                payload=payload,
                parent_event_id=parent_event_id,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "Failed to record rule decision event project=%s chapter=%s rule=%s: %s",
                decision_input.project_id,
                decision_input.chapter_number,
                decision.rule_id,
                exc,
            )
            return None
