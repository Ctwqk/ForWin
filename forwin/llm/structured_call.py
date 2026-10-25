from __future__ import annotations

import json
from collections.abc import Callable
from dataclasses import dataclass, replace
from typing import Any, Literal

from pydantic import BaseModel

from forwin.audit.events import DecisionEventInfo, DecisionEventType
from forwin.observability.llm_trace import mark_latest_attempt_parse_failure


_SENSITIVE_LOG_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "browser_session",
    "access_token",
    "bearer_token",
    "client_secret",
    "cookie",
    "cookies",
    "minimax_api_key",
    "password",
    "private_key",
    "publisher_session_secret",
    "raw_browser_session",
    "refresh_token",
    "secret_key",
    "session_secret",
    "session_token",
    "token",
}


class GateAuditWriter:
    """The only persistence capability available to delegated gate code."""

    __slots__ = ("_save_decision_event", "_save_prompt_trace")

    def __init__(
        self,
        *,
        save_decision_event: Callable[[DecisionEventInfo], Any],
        save_prompt_trace: Callable[..., Any],
    ) -> None:
        self._save_decision_event = save_decision_event
        self._save_prompt_trace = save_prompt_trace

    def save_decision_event(self, event: DecisionEventInfo) -> Any:
        return self._save_decision_event(event)

    def save_prompt_trace(self, **payload: Any) -> Any:
        return self._save_prompt_trace(**payload)


def _sanitize_complete_log(value: Any) -> Any:
    if isinstance(value, dict):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized = str(key or "").strip().lower()
            is_sensitive = normalized in _SENSITIVE_LOG_KEYS or normalized.endswith(
                ("_api_key", "_password", "_cookie", "_token")
            )
            result[str(key)] = (
                "[REDACTED]" if is_sensitive else _sanitize_complete_log(item)
            )
        return result
    if isinstance(value, (list, tuple)):
        return [_sanitize_complete_log(item) for item in value]
    return value


def _json_dump(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


@dataclass(frozen=True)
class StructuredCallEventTypes:
    """The decision event types one LLM call records; an empty type records no event for that step."""

    requested: str
    completed: str
    failed: str


@dataclass(frozen=True)
class StructuredCallEnvelope:
    """Where an LLM call's decision events attach."""

    project_id: str
    task_id: str = ""
    band_id: str = ""
    chapter_number: int = 0
    scope: str = "project"
    related_object_type: str = ""
    related_object_id: str = ""
    parent_event_id: str = ""
    causal_root_id: str = ""


@dataclass(frozen=True)
class StructuredCallVerdict:
    """The caller's wording of a call's completed or failed event."""

    summary: str
    reason: str
    actor_id: str
    payload: dict[str, Any]


@dataclass(frozen=True)
class StructuredCallOutcome:
    ok: bool
    payload: dict[str, Any] | None
    failure_reason: Literal["llm_call_failed", "parse_or_schema"] | None
    profile_id: str
    model: str
    trace_id: str
    # The completed or failed event, or the trace event when neither is recorded.
    decision_event_id: str
    failure_detail: str = ""
    backend: str = ""
    causal_root_id: str = ""


def _drain_attempts(llm_client: Any) -> list[dict[str, Any]]:
    drain = getattr(llm_client, "drain_llm_attempt_events", None)
    if not callable(drain):
        return []
    try:
        attempts = drain()
    except Exception:  # noqa: BLE001
        return []
    if not isinstance(attempts, list):
        return []
    return [dict(item) for item in attempts if isinstance(item, dict)]


def run_structured_llm_call(
    *,
    llm_client: Any,
    recorder: GateAuditWriter,
    gate_kind: str,
    stage_key: str,
    messages: list[dict[str, Any]],
    output_model: type[BaseModel],
    output_schema: dict[str, Any],
    event_types: StructuredCallEventTypes,
    request_payload: dict[str, Any],
    envelope: StructuredCallEnvelope,
    input_snapshot: dict[str, Any],
    verdict: Callable[[StructuredCallOutcome], StructuredCallVerdict],
    trace_scope: str,
    template_id: str,
    trace_stage_key: str = "",
    task_family: str = "review",
    label: str = "Gate delegation",
) -> StructuredCallOutcome:
    """Validate one chain response and preserve its trace and decision events.

    Provider failures follow the client's route chain. Parsing and schema failures
    stay with the caller, which decides whether to retry or hand control to a human.
    """
    requested_event = None
    if event_types.requested:
        requested_event = recorder.save_decision_event(
            DecisionEventInfo(
                project_id=envelope.project_id,
                task_id=envelope.task_id,
                band_id=envelope.band_id,
                chapter_number=envelope.chapter_number,
                scope=envelope.scope,
                event_family="audit_action",
                event_type=event_types.requested,
                actor_type="system",
                summary=f"{label} requested for {gate_kind}.",
                payload=request_payload,
                related_object_type=envelope.related_object_type,
                related_object_id=envelope.related_object_id,
                parent_event_id=envelope.parent_event_id,
                causal_root_id=envelope.causal_root_id,
            )
        )
        causal_root_id = str(requested_event.causal_root_id or requested_event.id or "")
    else:
        causal_root_id = envelope.causal_root_id
    sanitized_snapshot = _sanitize_complete_log(input_snapshot)
    _drain_attempts(llm_client)

    raw_output = ""
    profile_id = ""
    model = ""
    backend = str(getattr(llm_client, "provider", "openai_compatible"))
    failure_reason: Literal["llm_call_failed", "parse_or_schema"] | None = None
    failure_detail = ""
    payload: dict[str, Any] | None = None
    try:
        result = llm_client.chat_with_meta(
            messages,
            temperature=0.1,
            max_tokens=2500,
            response_format={"type": "json_object"},
            output_schema=output_schema,
            task_family=task_family,
            stage_key=stage_key,
        )
        raw_output = result.content
        profile_id = result.profile_id
        model = result.model
    except Exception as exc:  # noqa: BLE001
        failure_reason = "llm_call_failed"
        failure_detail = f"{exc.__class__.__name__}: {exc}"
    else:
        try:
            payload = output_model.model_validate_json(raw_output).model_dump(mode="json")
        except Exception as exc:  # noqa: BLE001
            failure_reason = "parse_or_schema"
            failure_detail = f"{exc.__class__.__name__}: {exc}"
            mark_latest_attempt_parse_failure(
                llm_client, parser_name="structured_llm_call", stage_key=stage_key,
                schema_name=output_model.__name__, raw_output=raw_output, error=exc,
            )

    attempts = _drain_attempts(llm_client)
    fallback_used = len({item.get("profile_id") for item in attempts if item.get("profile_id")}) > 1
    logged_attempts = [_sanitize_complete_log(item) for item in attempts]
    output_summary = {
        "gate_kind": gate_kind,
        "raw_model_output": raw_output,
        "parsed_decision": payload,
        "failure_reason": failure_reason,
        "failure_detail": failure_detail,
        "profile_id": profile_id,
        "model": model,
        "backend": backend,
    }
    trace = recorder.save_prompt_trace(
        project_id=envelope.project_id,
        decision_event_id=requested_event.id if requested_event is not None else "",
        trace_scope=trace_scope,
        stage_key=trace_stage_key or stage_key,
        template_id=template_id,
        template_version="v1",
        effective_system_prompt="\n\n".join(
            str(message.get("content") or "")
            for message in messages
            if message.get("role") == "system"
        ),
        prompt_layers_json=_json_dump(_sanitize_complete_log(messages)),
        input_snapshot_json=_json_dump(sanitized_snapshot),
        model_profile_json=_json_dump(
            {
                "profile_id": profile_id,
                "model": model,
                "backend": backend,
                "fallback_used": fallback_used,
            }
        ),
        attempts_json=_json_dump(logged_attempts),
        output_summary_json=_json_dump(output_summary),
        backend=backend,
        fallback_used=fallback_used,
    )
    trace_event = recorder.save_decision_event(
        DecisionEventInfo(
            project_id=envelope.project_id,
            task_id=envelope.task_id,
            band_id=envelope.band_id,
            chapter_number=envelope.chapter_number,
            scope=envelope.scope,
            event_family="runtime_observation",
            event_type=DecisionEventType.PROMPT_TRACE_RECORDED,
            actor_type="system",
            summary=f"{label} trace recorded for {gate_kind}.",
            payload={
                "gate_kind": gate_kind,
                "trace_id": trace.id,
                "profile_id": profile_id,
                "model": model,
                "backend": backend,
            },
            related_object_type="prompt_trace",
            related_object_id=trace.id,
            parent_event_id=(
                requested_event.id
                if requested_event is not None
                else envelope.parent_event_id
            ),
            causal_root_id=causal_root_id,
        )
    )
    causal_root_id = causal_root_id or str(
        trace_event.causal_root_id or trace_event.id or ""
    )
    outcome = StructuredCallOutcome(
        ok=failure_reason is None,
        payload=payload,
        failure_reason=failure_reason,
        trace_id=trace.id,
        decision_event_id=trace_event.id,
        failure_detail=failure_detail,
        profile_id=profile_id,
        model=model,
        backend=backend,
        causal_root_id=causal_root_id,
    )
    event_type = event_types.failed if failure_reason else event_types.completed
    if not event_type:
        return outcome
    wording = verdict(outcome)
    final_event = recorder.save_decision_event(
        DecisionEventInfo(
            project_id=envelope.project_id,
            task_id=envelope.task_id,
            band_id=envelope.band_id,
            chapter_number=envelope.chapter_number,
            scope=envelope.scope,
            event_family="evaluation_verdict",
            event_type=event_type,
            actor_type="worker",
            actor_id=wording.actor_id,
            summary=wording.summary,
            reason=wording.reason,
            payload=wording.payload,
            related_object_type="prompt_trace",
            related_object_id=trace.id,
            parent_event_id=trace_event.id,
            causal_root_id=causal_root_id,
        )
    )
    return replace(outcome, decision_event_id=final_event.id)
