"""Context assembler - builds ChapterContextPack from current state."""
from __future__ import annotations
import json
import logging
import re
from typing import Any

from sqlalchemy import func, select

from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ChapterPlan
from forwin.protocol.context import (
    ArcEnvelopeView,
    AudienceHintView,
    ChapterContextPack,
    NPCIntentView,
    TimelineSnapshot,
    WorldPressureView,
)
from forwin.characters.events import CHARACTER_INTEGRITY_CHECK_FAILED
from forwin.canon_names import extract_candidate_character_names
from forwin.canon_quality.rule_profile import CanonGlossary
from forwin.governance import DecisionEventInfo
from forwin.observability.context import OperationContext
from forwin.observability.ports import NullObservability
from forwin.planning.world_contracts import WorldContractRepository
from forwin.state.updater import StateUpdater

logger = logging.getLogger(__name__)

_MAP_CONTEXT_NEIGHBOR_LIMIT = 8
_MAP_CONTEXT_REVIEW_GRAPH_NODE_LIMIT = 256
_MAP_CONTEXT_REVIEW_GRAPH_EDGE_LIMIT = 512


def _project_personality_integrity_strict(project) -> bool:
    try:
        automation = json.loads(getattr(project, "automation_json", "{}") or "{}") or {}
    except (TypeError, ValueError, json.JSONDecodeError):
        automation = {}
    personality_policy = automation.get("character_personality") if isinstance(automation, dict) else {}
    if isinstance(personality_policy, dict) and "strict_integrity" in personality_policy:
        return bool(personality_policy.get("strict_integrity"))
    return str(getattr(project, "creation_status", "") or "legacy") != "legacy"


def _personality_integrity_issues(
    *,
    book_state_overlay: dict,
    allowed_entities: list[str],
    active_entities: list,
    library: CharacterPersonalityLibrary,
) -> list[dict[str, Any]]:
    from forwin.personality import PersonalityLoadoutAssigner

    allowed_names = {str(item or "").strip() for item in allowed_entities if str(item or "").strip()}
    allowed_ids = {
        str(getattr(item, "entity_id", "") or "").strip()
        for item in active_entities
        if str(getattr(item, "kind", "") or "") == "character" and str(getattr(item, "entity_id", "") or "").strip()
    }
    assigner = PersonalityLoadoutAssigner(library)
    issues: list[dict[str, Any]] = []
    for character in book_state_overlay.get("character_nodes", []):
        if not isinstance(character, dict):
            continue
        character_id = str(character.get("character_id") or "").strip()
        character_name = str(character.get("character_name") or "").strip()
        legacy_entity_id = str(character.get("legacy_entity_id") or "").strip()
        if not (
            character_id in allowed_ids
            or legacy_entity_id in allowed_ids
            or character_name in allowed_names
            or character_id in allowed_names
        ):
            continue
        loadout = character.get("personality_loadout") if isinstance(character.get("personality_loadout"), dict) else {}
        if not loadout:
            issues.append(
                {
                    "code": "personality_missing_loadout",
                    "severity": "error",
                    "character_id": character_id,
                    "character_name": character_name,
                    "message": "named character is missing personality_loadout",
                }
            )
            continue
        validation = assigner.validate(loadout)
        for error in validation.errors:
            issues.append(
                {
                    "code": error,
                    "severity": "error",
                    "character_id": character_id,
                    "character_name": character_name,
                    "message": error,
                }
            )
        for warning in validation.warnings:
            issues.append(
                {
                    "code": warning,
                    "severity": "warning",
                    "character_id": character_id,
                    "character_name": character_name,
                    "message": warning,
                }
            )
    return issues


def _save_personality_integrity_failure(repo_session, project_id: str, chapter_number: int, issues: list[dict[str, Any]]) -> None:
    if repo_session is None:
        return
    StateUpdater(repo_session).save_decision_event(
        DecisionEventInfo(
            project_id=project_id,
            chapter_number=int(chapter_number or 0),
            scope="character_creation",
            event_family="audit_action",
            event_type=CHARACTER_INTEGRITY_CHECK_FAILED,
            actor_type="system",
            summary="人物 personality_loadout integrity gate failed before writer context assembly.",
            reason="writer context assembly",
            payload={"issues": issues},
            related_object_type="project",
            related_object_id=project_id,
        )
    )


__all__ = [
    '_project_personality_integrity_strict',
    '_personality_integrity_issues',
    '_save_personality_integrity_failure',
]
