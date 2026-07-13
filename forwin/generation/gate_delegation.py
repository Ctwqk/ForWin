from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
)
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.runtime.policy import GateDelegate, RuntimePolicy


SPARK_GATE_MODEL = "gpt-5.3-codex-spark"
SPARK_GATE_PERMISSION_PROFILE = "prompt_only_readonly"

_SYSTEM_PROMPT = """You are ForWin's delegated reviewer for one pause gate.
You replace a human approval decision for exactly one supplied gate.
Evaluate only the supplied snapshot. Do not invent patches, hidden facts, or missing evidence.
Return approve only when the supplied evidence supports continuing with the gate's existing semantics.
Return reject when material canon, continuity, integrity, or execution risk remains.
Never ask for human input. Never claim that deterministic hard gates can be bypassed.
Return one JSON object matching the requested schema and no other text."""

_OUTPUT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "decision": {"type": "string", "enum": ["approve", "reject"]},
        "reason": {"type": "string"},
        "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
        "findings": {"type": "array", "items": {"type": "string"}},
        "evidence": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["decision", "reason", "risk_level", "findings", "evidence"],
    "additionalProperties": False,
}

_SENSITIVE_LOG_KEYS = {
    "api_key",
    "apikey",
    "authorization",
    "browser_session",
    "access_token",
    "bearer_token",
    "client_secret",
    "codex_bridge_token",
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


class GateDelegationRequest(BaseModel):
    project_id: str
    task_id: str = ""
    causal_root_id: str = ""
    parent_event_id: str = ""
    gate_kind: str
    scope: str = "project"
    band_id: str = ""
    chapter_number: int = 0
    related_object_type: str = ""
    related_object_id: str = ""
    input_snapshot: dict[str, Any] = Field(default_factory=dict)


class SparkGateDecision(BaseModel):
    model_config = ConfigDict(extra="forbid")

    decision: Literal["approve", "reject"]
    reason: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high"]
    findings: list[str]
    evidence: list[str]


class GateResolution(BaseModel):
    resolved: bool = False
    approved: bool = False
    decision: Literal["approve", "reject"] = "reject"
    delegate: GateDelegate
    reason: str = ""
    risk_level: Literal["", "low", "medium", "high"] = ""
    findings: list[str] = Field(default_factory=list)
    evidence: list[str] = Field(default_factory=list)
    failure_reason: str = ""
    requested_model: str = SPARK_GATE_MODEL
    actual_model: str = ""
    backend: str = ""
    trace_id: str = ""
    decision_event_id: str = ""


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


def _delegation_gate_outcome(
    request: GateDelegationRequest,
    *,
    evaluated: bool,
    decision: str,
    blocked: bool,
    overridden_by: str = "",
    issue_keys: list[str] | None = None,
    evidence_refs: list[str] | None = None,
    trace_id: str = "",
) -> GateOutcome:
    scope = request.scope if request.scope in {"chapter", "band", "arc"} else "project"
    return GateOutcome(
        gate_id="delegation",
        responsibility_domain=str(request.gate_kind or "delegated_gate_resolution"),
        scope=scope,
        candidate_id=str(request.related_object_id or ""),
        chapter_number=int(request.chapter_number or 0),
        band_id=str(request.band_id or ""),
        evaluated=evaluated,
        fired=True,
        decision=decision,
        blocked=blocked,
        overridden_by=overridden_by,
        issue_keys=[str(item) for item in issue_keys or [] if str(item)],
        evidence_refs=[str(item) for item in evidence_refs or [] if str(item)],
        trace_ids=[trace_id] if trace_id else [],
    )


class SparkGateDelegate:
    def __init__(
        self,
        *,
        llm_client: Any,
        requested_model: str = SPARK_GATE_MODEL,
    ) -> None:
        self.llm_client = llm_client
        self.requested_model = str(requested_model or SPARK_GATE_MODEL).strip()

    def resolve(
        self,
        *,
        audit_writer: GateAuditWriter,
        request: GateDelegationRequest,
    ) -> GateResolution:
        requested_event = audit_writer.save_decision_event(
            DecisionEventInfo(
                project_id=request.project_id,
                task_id=request.task_id,
                band_id=request.band_id,
                chapter_number=request.chapter_number,
                scope=request.scope,
                event_family="audit_action",
                event_type=DecisionEventType.GATE_DELEGATION_REQUESTED,
                actor_type="system",
                summary=f"Gate delegation requested for {request.gate_kind}.",
                payload=attach_gate_outcome(
                    {
                        "gate_kind": request.gate_kind,
                        "requested_model": self.requested_model,
                        "related_object_type": request.related_object_type,
                        "related_object_id": request.related_object_id,
                    },
                    _delegation_gate_outcome(
                        request,
                        evaluated=False,
                        decision="reject",
                        blocked=False,
                    ),
                ),
                related_object_type=request.related_object_type,
                related_object_id=request.related_object_id,
                parent_event_id=request.parent_event_id,
                causal_root_id=request.causal_root_id,
            )
        )
        causal_root_id = str(requested_event.causal_root_id or requested_event.id or "")
        sanitized_snapshot = _sanitize_complete_log(request.input_snapshot)
        user_payload = {
            "gate_kind": request.gate_kind,
            "scope": request.scope,
            "project_id": request.project_id,
            "task_id": request.task_id,
            "band_id": request.band_id,
            "chapter_number": request.chapter_number,
            "related_object_type": request.related_object_type,
            "related_object_id": request.related_object_id,
            "gate_snapshot": sanitized_snapshot,
            "allowed_decisions": ["approve", "reject"],
        }
        messages = [
            {"role": "system", "content": _SYSTEM_PROMPT},
            {"role": "user", "content": _json_dump(user_payload)},
        ]
        self._drain_attempts()

        raw_output = ""
        call_error: Exception | None = None
        try:
            raw_output = str(
                self.llm_client.chat(
                    messages,
                    temperature=0.1,
                    max_tokens=2500,
                    response_format={"type": "json_object"},
                    output_schema=_OUTPUT_SCHEMA,
                    task_family="review",
                    stage_key="spark_pause_gate",
                    latency_class="sync",
                    codex_allowed=True,
                    permission_profile=SPARK_GATE_PERMISSION_PROFILE,
                    preferred_provider_kind="spark",
                    preferred_model=self.requested_model,
                )
                or ""
            )
        except Exception as exc:  # noqa: BLE001
            call_error = exc

        attempts = self._drain_attempts()
        call_result = getattr(self.llm_client, "last_call_result", None)
        backend = str(getattr(call_result, "backend", "") or "")
        router_trace = dict(getattr(call_result, "trace", {}) or {})
        fallback_used = bool(getattr(call_result, "fallback_used", False))
        actual_model = self._actual_model(
            backend=backend,
            router_trace=router_trace,
        )

        parsed: SparkGateDecision | None = None
        failure_reason = ""
        failure_detail = ""
        if call_error is not None:
            failure_reason = "llm_call_failed"
            failure_detail = f"{call_error.__class__.__name__}: {call_error}"
        elif actual_model != self.requested_model:
            failure_reason = "model_mismatch"
            failure_detail = (
                f"Expected {self.requested_model}, got {actual_model or '<unknown>'}."
            )
        else:
            try:
                parsed = SparkGateDecision.model_validate(json.loads(raw_output))
            except Exception as exc:  # noqa: BLE001
                failure_reason = "parse_or_schema"
                failure_detail = f"{exc.__class__.__name__}: {exc}"

        logged_attempts: list[dict[str, Any]] = [
            _sanitize_complete_log(dict(item))
            for item in attempts
            if isinstance(item, dict)
        ]
        if router_trace:
            logged_attempts.append(
                {
                    "source": "router_trace",
                    "backend": backend,
                    "model": actual_model,
                    "trace": _sanitize_complete_log(router_trace),
                }
            )
        output_summary = {
            "gate_kind": request.gate_kind,
            "raw_model_output": raw_output,
            "parsed_decision": (
                parsed.model_dump(mode="json") if parsed is not None else None
            ),
            "failure_reason": failure_reason,
            "failure_detail": failure_detail,
            "requested_model": self.requested_model,
            "actual_model": actual_model,
            "backend": backend,
        }
        trace = audit_writer.save_prompt_trace(
            project_id=request.project_id,
            decision_event_id=requested_event.id,
            trace_scope="gate_delegation",
            stage_key=f"gate_{request.gate_kind}",
            template_id="spark_pause_gate",
            template_version="v1",
            effective_system_prompt=_SYSTEM_PROMPT,
            prompt_layers_json=_json_dump(_sanitize_complete_log(messages)),
            input_snapshot_json=_json_dump(sanitized_snapshot),
            model_profile_json=_json_dump(
                {
                    "requested_model": self.requested_model,
                    "actual_model": actual_model,
                    "backend": backend,
                    "permission_profile": SPARK_GATE_PERMISSION_PROFILE,
                    "fallback_used": fallback_used,
                }
            ),
            attempts_json=_json_dump(logged_attempts),
            output_summary_json=_json_dump(output_summary),
            backend=backend,
            codex_job_id=str(
                router_trace.get("job_id") or router_trace.get("codex_job_id") or ""
            ),
            permission_profile=SPARK_GATE_PERMISSION_PROFILE,
            fallback_used=fallback_used,
        )
        trace_event = audit_writer.save_decision_event(
            DecisionEventInfo(
                project_id=request.project_id,
                task_id=request.task_id,
                band_id=request.band_id,
                chapter_number=request.chapter_number,
                scope=request.scope,
                event_family="runtime_observation",
                event_type=DecisionEventType.PROMPT_TRACE_RECORDED,
                actor_type="system",
                summary=f"Gate delegation trace recorded for {request.gate_kind}.",
                payload={
                    "gate_kind": request.gate_kind,
                    "trace_id": trace.id,
                    "requested_model": self.requested_model,
                    "actual_model": actual_model,
                    "backend": backend,
                },
                related_object_type="prompt_trace",
                related_object_id=trace.id,
                parent_event_id=requested_event.id,
                causal_root_id=causal_root_id,
            )
        )

        event_type = (
            DecisionEventType.GATE_DELEGATION_FAILED
            if failure_reason
            else DecisionEventType.GATE_DELEGATION_DECIDED
        )
        decision_text = parsed.decision if parsed is not None else "reject"
        reason = parsed.reason if parsed is not None else failure_detail
        approved = not bool(failure_reason) and decision_text == "approve"
        final_gate_outcome = _delegation_gate_outcome(
            request,
            evaluated=True,
            decision=("error" if failure_reason else decision_text),
            blocked=not approved,
            overridden_by="spark" if approved else "",
            issue_keys=[
                *([failure_reason] if failure_reason else []),
                *(parsed.findings if parsed is not None else []),
            ],
            evidence_refs=parsed.evidence if parsed is not None else [],
            trace_id=trace.id,
        )
        final_event = audit_writer.save_decision_event(
            DecisionEventInfo(
                project_id=request.project_id,
                task_id=request.task_id,
                band_id=request.band_id,
                chapter_number=request.chapter_number,
                scope=request.scope,
                event_family="evaluation_verdict",
                event_type=event_type,
                actor_type="worker",
                actor_id=actual_model or "spark-gate-router",
                summary=(
                    f"Gate delegation failed for {request.gate_kind}: {failure_reason}."
                    if failure_reason
                    else f"Gate delegation decided {decision_text} for "
                    f"{request.gate_kind}."
                ),
                reason=reason,
                payload=attach_gate_outcome(
                    {
                        "gate_kind": request.gate_kind,
                        "decision": decision_text,
                        "failure_reason": failure_reason,
                        "trace_id": trace.id,
                        "requested_model": self.requested_model,
                        "actual_model": actual_model,
                        "backend": backend,
                        "risk_level": parsed.risk_level if parsed is not None else "",
                        "findings": parsed.findings if parsed is not None else [],
                        "evidence": parsed.evidence if parsed is not None else [],
                        "gate_related_object_type": request.related_object_type,
                        "gate_related_object_id": request.related_object_id,
                    },
                    final_gate_outcome,
                ),
                related_object_type="prompt_trace",
                related_object_id=trace.id,
                parent_event_id=trace_event.id,
                causal_root_id=causal_root_id,
            )
        )
        if approved:
            audit_writer.save_decision_event(
                DecisionEventInfo(
                    project_id=request.project_id,
                    task_id=request.task_id,
                    band_id=request.band_id,
                    chapter_number=request.chapter_number,
                    scope=request.scope,
                    event_family="audit_action",
                    event_type=DecisionEventType.GATE_DELEGATION_APPROVED,
                    actor_type="worker",
                    actor_id=actual_model,
                    summary=f"Gate delegation approved {request.gate_kind}.",
                    reason=reason,
                    payload=attach_gate_outcome(
                        {
                            "gate_kind": request.gate_kind,
                            "trace_id": trace.id,
                            "requested_model": self.requested_model,
                            "actual_model": actual_model,
                            "backend": backend,
                        },
                        final_gate_outcome,
                    ),
                    related_object_type=request.related_object_type,
                    related_object_id=request.related_object_id,
                    parent_event_id=final_event.id,
                    causal_root_id=causal_root_id,
                )
            )
        return GateResolution(
            resolved=not bool(failure_reason),
            approved=approved,
            decision=decision_text,
            delegate="spark",
            reason=reason,
            risk_level=parsed.risk_level if parsed is not None else "",
            findings=parsed.findings if parsed is not None else [],
            evidence=parsed.evidence if parsed is not None else [],
            failure_reason=failure_reason,
            requested_model=self.requested_model,
            actual_model=actual_model,
            backend=backend,
            trace_id=trace.id,
            decision_event_id=final_event.id,
        )

    def _drain_attempts(self) -> list[dict[str, Any]]:
        drain = getattr(self.llm_client, "drain_llm_attempt_events", None)
        if not callable(drain):
            return []
        try:
            attempts = drain()
        except Exception:  # noqa: BLE001
            return []
        if not isinstance(attempts, list):
            return []
        return [dict(item) for item in attempts if isinstance(item, dict)]

    @staticmethod
    def _actual_model(
        *,
        backend: str,
        router_trace: dict[str, Any],
    ) -> str:
        if backend != "codex_bridge":
            return ""
        return str(router_trace.get("actual_model") or "").strip()


class GateDelegationService:
    def __init__(self, *, spark_delegate: SparkGateDelegate) -> None:
        self.spark_delegate = spark_delegate

    def resolve(
        self,
        request: GateDelegationRequest,
        *,
        policy: RuntimePolicy,
        audit_writer: GateAuditWriter | None = None,
    ) -> GateResolution:
        if policy.pause.gate_delegate == "human":
            return GateResolution(
                delegate="human",
                reason="human_pause_required",
            )
        if audit_writer is None:
            raise ValueError("Spark gate delegation requires an audit writer")
        return self.spark_delegate.resolve(
            audit_writer=audit_writer,
            request=request,
        )


__all__ = [
    "GateDelegationRequest",
    "GateDelegationService",
    "GateAuditWriter",
    "GateResolution",
    "SPARK_GATE_MODEL",
    "SPARK_GATE_PERMISSION_PROFILE",
    "SparkGateDecision",
    "SparkGateDelegate",
]
