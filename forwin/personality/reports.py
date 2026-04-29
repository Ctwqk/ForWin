from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state import BookStateRepository
from forwin.models import DecisionEvent


class PersonalityAssignmentReportStore:
    def __init__(self, session: Session) -> None:
        self.session = session

    def explain(self, project_id: str, assignment_id: str) -> dict[str, Any] | None:
        normalized = str(assignment_id or "").strip()
        if not normalized:
            return None
        node_result = self._find_node_report(project_id, normalized)
        if node_result is not None:
            node_result["decision_events"] = self._matching_events(project_id, normalized)
            return node_result
        events = self._matching_events(project_id, normalized)
        if not events:
            return None
        assignment = events[-1].get("personality_assignment") or {}
        return {
            "character_id": str(events[-1].get("character_id") or ""),
            "character_name": str(events[-1].get("character_name") or ""),
            "personality_assignment": assignment if isinstance(assignment, dict) else {},
            "decision_events": events,
        }

    def _find_node_report(self, project_id: str, assignment_id: str) -> dict[str, Any] | None:
        for node in BookStateRepository(self.session).list_world_nodes(project_id):
            if str(node.node_type) != "character":
                continue
            assignment = node.metadata.get("personality_assignment") if isinstance(node.metadata, dict) else {}
            if not isinstance(assignment, dict):
                continue
            if str(assignment.get("assignment_id") or "") != assignment_id:
                continue
            return {
                "character_id": node.id,
                "character_name": node.name,
                "personality_assignment": dict(assignment),
            }
        return None

    def _matching_events(self, project_id: str, assignment_id: str) -> list[dict[str, Any]]:
        rows = (
            self.session.execute(
                select(DecisionEvent)
                .where(DecisionEvent.project_id == project_id)
                .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
            )
            .scalars()
            .all()
        )
        events: list[dict[str, Any]] = []
        for row in rows:
            payload = _json_object(row.payload_json)
            if _payload_assignment_id(payload) != assignment_id:
                continue
            event_payload = dict(payload)
            event_payload.update(
                {
                    "decision_event_id": row.id,
                    "event_type": row.event_type,
                    "summary": row.summary,
                    "reason": row.reason,
                    "created_at": row.created_at.isoformat() if row.created_at else "",
                }
            )
            events.append(event_payload)
        return events


def _payload_assignment_id(payload: dict[str, Any]) -> str:
    assignment = payload.get("personality_assignment")
    if isinstance(assignment, dict):
        assignment_id = str(assignment.get("assignment_id") or "").strip()
        if assignment_id:
            return assignment_id
    return str(payload.get("assignment_id") or "").strip()


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
