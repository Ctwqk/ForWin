from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from forwin.governance import DecisionEventInfo
from forwin.state.updater import StateUpdater
from forwin.world_model.store import WorldModelStore


CodexGovernedActionType = Literal[
    "world_edit_proposal_create",
    "review_finding_create",
    "repair_suggestion_create",
    "conflict_explanation_create",
]


class CodexGovernedActionRequest(BaseModel):
    action_type: str
    target_page_key: str = ""
    target_field: str = ""
    reason: str = ""
    payload: dict[str, Any] = Field(default_factory=dict)
    created_by: str = "codex"


class CodexGovernedActionResult(BaseModel):
    ok: bool
    action_type: str
    created_object_type: str = ""
    created_object_id: str = ""
    message: str = ""


class CodexGovernedActionProcessor:
    _allowed = {
        "world_edit_proposal_create",
        "review_finding_create",
        "repair_suggestion_create",
        "conflict_explanation_create",
    }

    def __init__(self, session: Session) -> None:
        self.session = session
        self.store = WorldModelStore(session)

    def apply(self, *, project_id: str, request: CodexGovernedActionRequest) -> CodexGovernedActionResult:
        action_type = str(request.action_type or "").strip()
        if action_type not in self._allowed:
            raise ValueError(f"Codex governed action is not allowed: {action_type}")
        if action_type == "world_edit_proposal_create":
            proposal = self.store.create_proposal(
                project_id=project_id,
                source="codex",
                target_page_key=request.target_page_key,
                target_field=request.target_field or "markdown",
                proposed_patch={
                    "action_type": action_type,
                    **request.payload,
                },
                reason=request.reason,
                created_by=request.created_by or "codex",
            )
            self._record_event(
                project_id=project_id,
                action_type=action_type,
                object_type="world_edit_proposal",
                object_id=proposal.id,
                reason=request.reason,
                payload=request.payload,
            )
            return CodexGovernedActionResult(
                ok=True,
                action_type=action_type,
                created_object_type="world_edit_proposal",
                created_object_id=proposal.id,
                message="Codex proposal created.",
            )
        proposal = self.store.create_proposal(
            project_id=project_id,
            source="codex",
            target_page_key=request.target_page_key,
            target_field=action_type,
            proposed_patch={
                "action_type": action_type,
                "managed_payload": request.payload,
            },
            reason=request.reason,
            created_by=request.created_by or "codex",
        )
        self._record_event(
            project_id=project_id,
            action_type=action_type,
            object_type="world_edit_proposal",
            object_id=proposal.id,
            reason=request.reason,
            payload=request.payload,
        )
        return CodexGovernedActionResult(
            ok=True,
            action_type=action_type,
            created_object_type="world_edit_proposal",
            created_object_id=proposal.id,
            message="Codex governed management proposal created.",
        )

    def _record_event(
        self,
        *,
        project_id: str,
        action_type: str,
        object_type: str,
        object_id: str,
        reason: str,
        payload: dict[str, Any],
    ) -> None:
        StateUpdater(self.session).save_decision_event(
            DecisionEventInfo(
                project_id=project_id,
                scope="project",
                event_family="audit_action",
                event_type="codex_governed_action_recorded",
                actor_type="system",
                summary=f"Codex governed action recorded: {action_type}",
                reason=reason,
                payload={"action_type": action_type, "payload": payload},
                related_object_type=object_type,
                related_object_id=object_id,
            )
        )
