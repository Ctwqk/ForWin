from __future__ import annotations

import json
import logging
from typing import Any

from forwin.governance import DecisionEventInfo, ensure_decision_event_type
from forwin.models.observability import PerformanceSpan
from forwin.models.project import Project

from .context import OperationContext
from .llm_trace import prepare_prompt_trace_payload
from .redaction import redact_payload
from .spans import SpanRecord

logger = logging.getLogger(__name__)


class DecisionEventSink:
    def __init__(self, *, session_factory: Any | None = None, updater: Any | None = None) -> None:
        self.session_factory = session_factory
        self.updater = updater

    def record_event(
        self,
        context: OperationContext,
        *,
        event_family: str,
        event_type: str,
        summary: str,
        reason: str = "",
        scope: str = "project",
        actor_type: str = "",
        actor_id: str = "",
        payload: dict[str, Any] | None = None,
        related_object_type: str = "",
        related_object_id: str = "",
        parent_event_id: str = "",
        causal_root_id: str = "",
        band_id: str = "",
        chapter_number: int | None = None,
        commit: bool | None = None,
    ):
        updater = self.updater
        session = None
        owns_session = updater is None
        if updater is None:
            if self.session_factory is None:
                return None
            from forwin.state.updater import StateUpdater

            session = self.session_factory()
            updater = StateUpdater(session)
        try:
            normalized_payload = {
                **context.payload_fields(),
                **(payload or {}),
            }
            row = updater.save_decision_event(
                DecisionEventInfo(
                    project_id=context.project_id,
                    task_id=context.task_id,
                    band_id=band_id or context.band_id,
                    chapter_number=(
                        int(chapter_number)
                        if chapter_number is not None
                        else int(context.chapter_number or 0)
                    ),
                    scope=scope,
                    event_family=event_family,
                    event_type=ensure_decision_event_type(event_type),
                    actor_type=actor_type or context.actor_type,
                    actor_id=actor_id or context.actor_id,
                    summary=summary,
                    reason=reason,
                    payload=redact_payload(normalized_payload),
                    related_object_type=related_object_type,
                    related_object_id=related_object_id,
                    parent_event_id=parent_event_id or context.parent_event_id,
                    causal_root_id=causal_root_id or context.causal_root_id,
                )
            )
            if owns_session and (commit is not False):
                session.commit()
            return row
        except Exception:
            if owns_session and session is not None:
                session.rollback()
            raise
        finally:
            if owns_session and session is not None:
                session.close()


class PerformanceSpanSink:
    def __init__(self, *, session_factory: Any | None) -> None:
        self.session_factory = session_factory

    def record_span(self, record: SpanRecord) -> PerformanceSpan | None:
        if self.session_factory is None or not str(record.context.project_id or "").strip():
            return None
        session = self.session_factory()
        try:
            row = PerformanceSpan(
                project_id=record.context.project_id,
                task_id=record.context.task_id,
                operation_id=record.context.operation_id or record.context.task_id,
                trace_id=record.trace_id,
                span_id=record.span_id,
                parent_span_id=record.parent_span_id,
                span_name=record.span_name,
                span_kind=record.span_kind,
                component=record.component,
                stage=record.context.stage,
                chapter_number=int(record.context.chapter_number or 0),
                arc_id=record.context.arc_id,
                band_id=record.context.band_id,
                status=record.status,
                start_time_unix_ms=int(record.start_time_unix_ms or 0),
                duration_ms=max(0, int(record.duration_ms or 0)),
                self_duration_ms=max(0, int(record.self_duration_ms or 0)),
                tags_json=json.dumps(redact_payload(record.tags), ensure_ascii=False),
                metrics_json=json.dumps(record.metrics, ensure_ascii=False),
                error_json=json.dumps(redact_payload(record.error), ensure_ascii=False),
            )
            session.add(row)
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class PromptTraceSink:
    def __init__(self, *, session_factory: Any | None, artifact_store: Any | None = None) -> None:
        self.session_factory = session_factory
        self.artifact_store = artifact_store

    def record_prompt_trace(
        self,
        context: OperationContext,
        prompt_trace: dict[str, Any],
        *,
        artifact_store: Any | None = None,
        decision_event_id: str = "",
        parent_trace_id: str = "",
    ):
        if self.session_factory is None or not str(context.project_id or "").strip():
            return None
        session = self.session_factory()
        try:
            from forwin.state.updater import StateUpdater

            updater = StateUpdater(session)
            payload = prepare_prompt_trace_payload(
                prompt_trace,
                artifact_store=artifact_store or self.artifact_store,
                project_id=context.project_id,
                chapter_number=int(context.chapter_number or 0),
            )
            project = session.get(Project, context.project_id)
            row = updater.save_prompt_trace(
                project_id=context.project_id,
                genesis_revision_id=str(getattr(project, "active_genesis_revision_id", "") or ""),
                decision_event_id=str(decision_event_id or "").strip(),
                parent_trace_id=str(parent_trace_id or "").strip(),
                trace_scope=str(payload.get("trace_scope", "llm") or "llm"),
                stage_key=str(payload.get("stage_key", "") or ""),
                template_id=str(payload.get("template_id", "") or ""),
                template_version=str(payload.get("template_version", "v1") or "v1"),
                effective_system_prompt=str(payload.get("effective_system_prompt", "") or ""),
                prompt_layers_json=json.dumps(payload.get("prompt_layers", []), ensure_ascii=False),
                input_snapshot_json=json.dumps(payload.get("input_snapshot", {}), ensure_ascii=False),
                model_profile_json=json.dumps(payload.get("model_profile", {}), ensure_ascii=False),
                attempts_json=json.dumps(payload.get("attempts", []), ensure_ascii=False),
                output_summary_json=json.dumps(payload.get("output_summary", {}), ensure_ascii=False),
                backend=str(payload.get("backend", "") or ""),
                codex_job_id=str(payload.get("codex_job_id", "") or ""),
                permission_profile=str(payload.get("permission_profile", "") or ""),
                fallback_used=bool(payload.get("fallback_used", False)),
            )
            session.commit()
            return row
        except Exception:
            session.rollback()
            raise
        finally:
            session.close()


class StdlibLogSink:
    def record(self, message: str, **fields: Any) -> None:
        logger.info("%s %s", message, " ".join(f"{key}={value}" for key, value in fields.items()))
