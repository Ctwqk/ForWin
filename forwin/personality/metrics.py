from __future__ import annotations

import json
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state import BookStateRepository
from forwin.governance import DecisionEventType
from forwin.models import DecisionEvent
from forwin.personality.models import PersonalityLoadout


def build_character_personality_metrics(session: Session, project_id: str) -> dict[str, Any]:
    nodes = [
        node for node in BookStateRepository(session).list_world_nodes(project_id)
        if str(node.node_type) == "character"
    ]
    events = (
        session.execute(
            select(DecisionEvent)
            .where(DecisionEvent.project_id == project_id)
            .order_by(DecisionEvent.created_at.asc(), DecisionEvent.id.asc())
        )
        .scalars()
        .all()
    )
    event_counts: dict[str, int] = {}
    for event in events:
        event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1

    missing_loadout = 0
    fallback_used = 0
    manual_override = 0
    low_confidence = 0
    confidence_values: list[float] = []
    dominant_skills: dict[str, int] = {}
    for node in nodes:
        profile = node.profile if isinstance(node.profile, dict) else {}
        metadata = node.metadata if isinstance(node.metadata, dict) else {}
        loadout_raw = profile.get("personality_loadout")
        assignment = metadata.get("personality_assignment") if isinstance(metadata, dict) else {}
        if not isinstance(loadout_raw, dict) or not loadout_raw:
            missing_loadout += 1
        else:
            try:
                loadout = PersonalityLoadout.model_validate(loadout_raw)
                if loadout.dominant is not None:
                    dominant_skills[loadout.dominant.skill] = dominant_skills.get(loadout.dominant.skill, 0) + 1
            except Exception:
                missing_loadout += 1
        if isinstance(assignment, dict):
            if assignment.get("manual_override"):
                manual_override += 1
            if assignment.get("assignment_mode") == "fallback_minimal" or assignment.get("status") == "fallback_used":
                fallback_used += 1
            confidence = assignment.get("confidence")
            if isinstance(confidence, (int, float)):
                confidence_values.append(float(confidence))
                if float(confidence) < 0.60:
                    low_confidence += 1

    ooc_issue_counts = _personality_issue_counts(events)
    return {
        "schema_version": "character.personality_metrics.v1",
        "project_id": project_id,
        "character_count": len(nodes),
        "character_creation_total": event_counts.get(DecisionEventType.CHARACTER_CREATED, 0),
        "character_creation_auto_personality_assigned_total": event_counts.get(DecisionEventType.PERSONALITY_LOADOUT_AUTO_ASSIGNED, 0),
        "character_creation_manual_override_total": max(
            manual_override,
            event_counts.get(DecisionEventType.PERSONALITY_LOADOUT_MANUAL_OVERRIDE, 0),
        ),
        "character_creation_fallback_used_total": fallback_used,
        "character_creation_low_confidence_total": low_confidence,
        "character_integrity_missing_loadout_total": missing_loadout,
        "personality_assignment_confidence_avg": (
            0.0 if not confidence_values else round(sum(confidence_values) / len(confidence_values), 4)
        ),
        "personality_ooc_issue_total_by_assignment_mode": ooc_issue_counts,
        "most_used_dominant_skills": [
            {"skill": skill, "count": count}
            for skill, count in sorted(dominant_skills.items(), key=lambda item: (-item[1], item[0]))
        ],
        "event_counts": event_counts,
    }


def _personality_issue_counts(events: list[DecisionEvent]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for event in events:
        payload = _json_object(event.payload_json)
        for issue in _walk_issues(payload):
            issue_type = str(issue.get("issue_type") or issue.get("code") or "")
            if issue_type.startswith("personality_") or issue_type in {
                "reference_model_override",
                "stress_mode_without_trigger",
                "skill_invented_canon",
                "writer_ignored_active_context",
            }:
                counts[issue_type] = counts.get(issue_type, 0) + 1
    return counts


def _walk_issues(value: Any) -> list[dict[str, Any]]:
    issues: list[dict[str, Any]] = []
    if isinstance(value, dict):
        if "issue_type" in value or "code" in value:
            issues.append(value)
        for child in value.values():
            issues.extend(_walk_issues(child))
    elif isinstance(value, list):
        for child in value:
            issues.extend(_walk_issues(child))
    return issues


def _json_object(raw: str | None) -> dict[str, Any]:
    try:
        payload = json.loads(raw or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}
