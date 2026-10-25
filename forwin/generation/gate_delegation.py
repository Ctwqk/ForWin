from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from forwin.audit.events import (
    DecisionEventInfo,
    DecisionEventType,
)
from forwin.audit.gate_outcome import GateOutcome, attach_gate_outcome
from forwin.runtime.policy import GateDelegate, RuntimePolicy
from forwin.llm.structured_call import (
    GateAuditWriter,
    StructuredCallEnvelope,
    StructuredCallEventTypes,
    StructuredCallOutcome,
    StructuredCallVerdict,
    _json_dump,
    _sanitize_complete_log,
    run_structured_llm_call,
)


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


class LLMGateDecision(BaseModel):
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
    profile_id: str = ""
    model: str = ""
    backend: str = ""
    trace_id: str = ""
    decision_event_id: str = ""


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


_LLM_EVENT_TYPES = StructuredCallEventTypes(
    requested=DecisionEventType.GATE_DELEGATION_REQUESTED,
    completed=DecisionEventType.GATE_DELEGATION_DECIDED,
    failed=DecisionEventType.GATE_DELEGATION_FAILED,
)


def _llm_decision(outcome: StructuredCallOutcome) -> LLMGateDecision | None:
    return LLMGateDecision.model_validate(outcome.payload) if outcome.payload is not None else None


class LLMGateDelegate:
    def __init__(self, *, llm_client: Any) -> None:
        self.llm_client = llm_client

    def resolve(
        self,
        *,
        audit_writer: GateAuditWriter,
        request: GateDelegationRequest,
    ) -> GateResolution:
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
        outcome = run_structured_llm_call(
            llm_client=self.llm_client,
            recorder=audit_writer,
            gate_kind=request.gate_kind,
            stage_key="pause_gate",
            messages=messages,
            output_model=LLMGateDecision,
            output_schema=_OUTPUT_SCHEMA,
            event_types=_LLM_EVENT_TYPES,
            request_payload=attach_gate_outcome(
                {
                    "gate_kind": request.gate_kind,
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
            envelope=StructuredCallEnvelope(
                project_id=request.project_id,
                task_id=request.task_id,
                band_id=request.band_id,
                chapter_number=request.chapter_number,
                scope=request.scope,
                related_object_type=request.related_object_type,
                related_object_id=request.related_object_id,
                parent_event_id=request.parent_event_id,
                causal_root_id=request.causal_root_id,
            ),
            input_snapshot=sanitized_snapshot,
            verdict=lambda result: self._verdict(request, result),
            trace_scope="gate_delegation",
            template_id="pause_gate",
        )
        parsed = _llm_decision(outcome)
        failure_reason = outcome.failure_reason
        decision_text = parsed.decision if parsed is not None else "reject"
        reason = parsed.reason if parsed is not None else outcome.failure_detail
        approved = not bool(failure_reason) and decision_text == "approve"
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
                    actor_id=outcome.model,
                    summary=f"Gate delegation approved {request.gate_kind}.",
                    reason=reason,
                    payload=attach_gate_outcome(
                        {
                            "gate_kind": request.gate_kind,
                            "trace_id": outcome.trace_id,
                            "profile_id": outcome.profile_id,
                            "model": outcome.model,
                            "backend": outcome.backend,
                        },
                        self._final_gate_outcome(request, outcome),
                    ),
                    related_object_type=request.related_object_type,
                    related_object_id=request.related_object_id,
                    parent_event_id=outcome.decision_event_id,
                    causal_root_id=outcome.causal_root_id,
                )
            )
        return GateResolution(
            resolved=not bool(failure_reason),
            approved=approved,
            decision=decision_text,
            delegate="llm",
            reason=reason,
            risk_level=parsed.risk_level if parsed is not None else "",
            findings=parsed.findings if parsed is not None else [],
            evidence=parsed.evidence if parsed is not None else [],
            failure_reason=failure_reason or "",
            profile_id=outcome.profile_id,
            model=outcome.model,
            backend=outcome.backend,
            trace_id=outcome.trace_id,
            decision_event_id=outcome.decision_event_id,
        )

    @staticmethod
    def _final_gate_outcome(
        request: GateDelegationRequest,
        outcome: StructuredCallOutcome,
    ) -> GateOutcome:
        parsed = _llm_decision(outcome)
        failure_reason = outcome.failure_reason
        decision_text = parsed.decision if parsed is not None else "reject"
        approved = not bool(failure_reason) and decision_text == "approve"
        return _delegation_gate_outcome(
            request,
            evaluated=True,
            decision=("error" if failure_reason else decision_text),
            blocked=not approved,
            overridden_by="llm" if approved else "",
            issue_keys=[
                *([failure_reason] if failure_reason else []),
                *(parsed.findings if parsed is not None else []),
            ],
            evidence_refs=parsed.evidence if parsed is not None else [],
            trace_id=outcome.trace_id,
        )

    def _verdict(
        self,
        request: GateDelegationRequest,
        outcome: StructuredCallOutcome,
    ) -> StructuredCallVerdict:
        parsed = _llm_decision(outcome)
        failure_reason = outcome.failure_reason
        decision_text = parsed.decision if parsed is not None else "reject"
        return StructuredCallVerdict(
            summary=(
                f"Gate delegation failed for {request.gate_kind}: {failure_reason}."
                if failure_reason
                else f"Gate delegation decided {decision_text} for "
                f"{request.gate_kind}."
            ),
            reason=parsed.reason if parsed is not None else outcome.failure_detail,
            actor_id=outcome.model or "llm-gate-router",
            payload=attach_gate_outcome(
                {
                    "gate_kind": request.gate_kind,
                    "decision": decision_text,
                    "failure_reason": failure_reason or "",
                    "trace_id": outcome.trace_id,
                    "profile_id": outcome.profile_id,
                    "model": outcome.model,
                    "backend": outcome.backend,
                    "risk_level": parsed.risk_level if parsed is not None else "",
                    "findings": parsed.findings if parsed is not None else [],
                    "evidence": parsed.evidence if parsed is not None else [],
                    "gate_related_object_type": request.related_object_type,
                    "gate_related_object_id": request.related_object_id,
                },
                self._final_gate_outcome(request, outcome),
            ),
        )


class GateDelegationService:
    def __init__(self, *, llm_delegate: LLMGateDelegate) -> None:
        self.llm_delegate = llm_delegate

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
            raise ValueError("LLM gate delegation requires an audit writer")
        return self.llm_delegate.resolve(
            audit_writer=audit_writer,
            request=request,
        )


__all__ = [
    "GateDelegationRequest",
    "GateDelegationService",
    "GateAuditWriter",
    "GateResolution",
    "LLMGateDecision",
    "LLMGateDelegate",
]
