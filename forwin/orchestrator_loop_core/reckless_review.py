from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from forwin.governance import DecisionEventType
from forwin.reckless_review import (
    RECKLESS_REVIEW_MODEL,
    RecklessReviewAgent,
    RecklessReviewOutcome,
    RecklessReviewRequest,
)
from forwin.state.updater import StateUpdater

logger = logging.getLogger(__name__)


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
    savepoint = None
    try:
        savepoint = updater.session.begin_nested()
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
        savepoint.commit()
        return outcome
    except Exception as exc:  # noqa: BLE001
        if savepoint is not None and savepoint.is_active:
            savepoint.rollback()
        logger.exception("Reckless review transaction failed for %s.", gate_kind)
        return RecklessReviewOutcome(
            completed=False,
            approved=False,
            decision="reject",
            reason=f"{exc.__class__.__name__}: {exc}",
            failure_reason="review_transaction_failed",
        )


def _delegate_checkpoint_if_reckless(
    self,
    *,
    updater: StateUpdater,
    governance,
    checkpoint,
    gate_kind: str,
    chapter_number: int = 0,
) -> bool:
    if str(getattr(governance, "review_delegation_mode", "human") or "human") != "reckless":
        return False
    try:
        issues = json.loads(str(getattr(checkpoint, "issues_json", "[]") or "[]"))
    except (json.JSONDecodeError, TypeError):
        issues = []
    if not isinstance(issues, list):
        issues = []
    savepoint = None
    try:
        savepoint = updater.session.begin_nested()
        outcome = self._delegate_reckless_review(
            updater=updater,
            project_id=str(getattr(checkpoint, "project_id", "") or ""),
            governance=governance,
            gate_kind=gate_kind,
            scope=(
                "band"
                if str(getattr(checkpoint, "boundary_kind", "") or "") == "band_end"
                else "chapter"
            ),
            band_id=str(getattr(checkpoint, "band_id", "") or ""),
            chapter_number=(
                int(chapter_number or 0)
                or int(getattr(checkpoint, "boundary_chapter", 0) or 0)
            ),
            related_object_type="band_checkpoint",
            related_object_id=str(getattr(checkpoint, "id", "") or ""),
            input_snapshot={
                "checkpoint": {
                    "id": str(getattr(checkpoint, "id", "") or ""),
                    "project_id": str(getattr(checkpoint, "project_id", "") or ""),
                    "arc_id": str(getattr(checkpoint, "arc_id", "") or ""),
                    "band_id": str(getattr(checkpoint, "band_id", "") or ""),
                    "chapter_start": int(getattr(checkpoint, "chapter_start", 0) or 0),
                    "chapter_end": int(getattr(checkpoint, "chapter_end", 0) or 0),
                    "trigger_source": str(getattr(checkpoint, "trigger_source", "") or ""),
                    "boundary_kind": str(getattr(checkpoint, "boundary_kind", "") or ""),
                    "boundary_chapter": int(getattr(checkpoint, "boundary_chapter", 0) or 0),
                    "status": str(getattr(checkpoint, "status", "") or ""),
                    "summary": str(getattr(checkpoint, "summary", "") or ""),
                    "reason": str(getattr(checkpoint, "reason", "") or ""),
                    "issues": issues,
                },
                "governance": governance.model_dump(mode="json"),
            },
        )
        if outcome is None or not outcome.approved:
            savepoint.commit()
            return False
        checkpoint.status = "overridden"
        checkpoint.reason = outcome.reason
        checkpoint.related_task_id = self._governance_task_id
        checkpoint.resolved_at = datetime.now(timezone.utc)
        updater.session.add(checkpoint)
        updater.session.flush()
        savepoint.commit()
        return True
    except Exception:  # noqa: BLE001
        if savepoint is not None and savepoint.is_active:
            savepoint.rollback()
        logger.exception("Reckless checkpoint override failed for %s.", gate_kind)
        return False


__all__ = ["_delegate_checkpoint_if_reckless", "_delegate_reckless_review"]
