from __future__ import annotations

from datetime import datetime, timezone
import json
import logging
from typing import Any

from forwin.generation.gate_delegation import (
    GateDelegationRequest,
    GateResolution,
)
from forwin.state.updater import StateUpdater


logger = logging.getLogger(__name__)


class GateDelegationStage:
    """Owns the gate delegation stage behavior."""

    def _resolve_gate_delegation(
        self,
        *,
        updater: StateUpdater,
        project_id: str,
        gate_kind: str,
        input_snapshot: dict[str, Any],
        scope: str = "project",
        band_id: str = "",
        chapter_number: int = 0,
        related_object_type: str = "",
        related_object_id: str = "",
        parent_event_id: str = "",
    ) -> GateResolution:
        request = GateDelegationRequest(
            project_id=project_id,
            task_id=self._audit_task_id,
            causal_root_id=self._audit_root_event_id,
            parent_event_id=parent_event_id,
            gate_kind=gate_kind,
            scope=scope,
            band_id=band_id,
            chapter_number=chapter_number,
            related_object_type=related_object_type,
            related_object_id=related_object_id,
            input_snapshot=input_snapshot,
        )
        if self.policy.pause.gate_delegate == "human":
            return self.gate_delegation.resolve(
                request,
                policy=self.policy,
                updater=updater,
            )

        savepoint = None
        try:
            savepoint = updater.session.begin_nested()
            outcome = self.gate_delegation.resolve(
                request,
                policy=self.policy,
                updater=updater,
            )
            savepoint.commit()
            return outcome
        except Exception as exc:  # noqa: BLE001
            if savepoint is not None and savepoint.is_active:
                savepoint.rollback()
            logger.exception("Gate delegation transaction failed for %s.", gate_kind)
            return GateResolution(
                delegate="spark",
                reason=f"{exc.__class__.__name__}: {exc}",
                failure_reason="delegation_transaction_failed",
            )

    def _resolve_checkpoint_gate(
        self,
        *,
        updater: StateUpdater,
        checkpoint,
        gate_kind: str,
        chapter_number: int = 0,
    ) -> bool:
        if str(getattr(checkpoint, "status", "") or "") in {"fail", "error"}:
            return False
        try:
            issues = json.loads(str(getattr(checkpoint, "issues_json", "[]") or "[]"))
        except (json.JSONDecodeError, TypeError):
            issues = []
        if not isinstance(issues, list):
            issues = []
        outcome = self._resolve_gate_delegation(
            updater=updater,
            project_id=str(getattr(checkpoint, "project_id", "") or ""),
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
                    "trigger_source": str(
                        getattr(checkpoint, "trigger_source", "") or ""
                    ),
                    "boundary_kind": str(
                        getattr(checkpoint, "boundary_kind", "") or ""
                    ),
                    "boundary_chapter": int(
                        getattr(checkpoint, "boundary_chapter", 0) or 0
                    ),
                    "status": str(getattr(checkpoint, "status", "") or ""),
                    "summary": str(getattr(checkpoint, "summary", "") or ""),
                    "reason": str(getattr(checkpoint, "reason", "") or ""),
                    "issues": issues,
                },
                "pause_policy": self.policy.pause.model_dump(mode="json"),
            },
        )
        if not outcome.approved:
            return False
        checkpoint.status = "overridden"
        checkpoint.reason = outcome.reason
        checkpoint.related_task_id = self._audit_task_id
        checkpoint.resolved_at = datetime.now(timezone.utc)
        updater.session.add(checkpoint)
        updater.session.flush()
        return True


__all__ = ["GateDelegationStage"]
