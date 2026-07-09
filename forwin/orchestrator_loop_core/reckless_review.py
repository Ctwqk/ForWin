from __future__ import annotations

from typing import Any

from forwin.governance import DecisionEventType
from forwin.reckless_review import (
    RECKLESS_REVIEW_MODEL,
    RecklessReviewAgent,
    RecklessReviewOutcome,
    RecklessReviewRequest,
)
from forwin.state.updater import StateUpdater


def _delegate_reckless_review(
    self,
    *,
    updater: StateUpdater,
    project_id: str,
    governance,
    gate_kind: str,
    input_snapshot: dict[str, Any],
    scope: str = "project",
    band_id: str = "",
    chapter_number: int = 0,
    related_object_type: str = "",
    related_object_id: str = "",
    parent_event_id: str = "",
) -> RecklessReviewOutcome | None:
    if str(getattr(governance, "review_delegation_mode", "human") or "human") != "reckless":
        return None
    outcome = RecklessReviewAgent(llm_client=self.llm_client).review_and_record(
        updater=updater,
        request=RecklessReviewRequest(
            project_id=project_id,
            task_id=self._governance_task_id,
            causal_root_id=self._governance_root_event_id,
            parent_event_id=parent_event_id,
            gate_kind=gate_kind,
            scope=scope,
            band_id=band_id,
            chapter_number=chapter_number,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            input_snapshot=input_snapshot,
        ),
    )
    if outcome.approved:
        self._record_decision_event(
            updater=updater,
            project_id=project_id,
            band_id=band_id,
            chapter_number=chapter_number,
            event_family="audit_action",
            event_type=DecisionEventType.RECKLESS_GATE_OVERRIDDEN,
            scope=scope,
            actor_type="worker",
            actor_id=outcome.actual_model or RECKLESS_REVIEW_MODEL,
            summary=f"Reckless review approved {gate_kind}.",
            reason=outcome.reason,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            parent_event_id=outcome.decision_event_id,
            payload={
                "gate_kind": gate_kind,
                "decision": outcome.decision,
                "risk_level": outcome.risk_level,
                "trace_id": outcome.trace_id,
                "requested_model": RECKLESS_REVIEW_MODEL,
                "actual_model": outcome.actual_model,
                "backend": outcome.backend,
            },
        )
    return outcome


__all__ = ["_delegate_reckless_review"]
