from __future__ import annotations

import json
from collections.abc import Callable
from typing import Any

from fastapi import HTTPException
from pydantic import ValidationError

from forwin.api_schema.world_conflicts import WorldConflictInfo, WorldConflictResolveRequest
from forwin.canon.admission import CanonAdmissionService
from forwin.http.request_support import _utcnow, require_project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.runtime.policy_store import ProjectPolicyMissing, ProjectPolicyStore
from forwin.world_engine.conflict_repository import ConflictNotOpen, WorldConflictRepository
from forwin.world_engine.conflict_resolution import (
    ConflictOptionUnavailable,
    ConflictResolutionService,
    ConflictStale,
)
from forwin.world_engine.conflict_types import ConflictDecision


def _conflict_info(row: WorldPlanConflictRow) -> WorldConflictInfo:
    return WorldConflictInfo(
        id=row.id, project_id=row.project_id, source=row.source,
        detected_after_chapter=row.detected_after_chapter, story_time=row.story_time,
        involved_refs=json.loads(row.involved_refs_json), violated=row.violated,
        proposed_change=json.loads(row.proposed_change_json), impact_level=row.impact_level,
        options=json.loads(row.options_json), required_decider=row.required_decider,
        blocks_from_chapter=row.blocks_from_chapter, status=row.status, summary=row.summary,
        decision=json.loads(row.decision_json), result_refs=json.loads(row.result_refs_json),
        intent_ref=row.intent_ref, actor_ref=row.actor_ref,
        created_at=row.created_at, updated_at=row.updated_at,
    )


def build_handlers(*, get_session: Callable[[], Any]) -> dict[str, Callable[..., Any]]:
    def list_project_world_conflicts(project_id: str, status: str | None = None) -> list[WorldConflictInfo]:
        with get_session() as session:
            require_project(session, project_id)
            if status is not None and status not in {"open", "resolved"}:
                raise HTTPException(status_code=400, detail="status must be open or resolved")
            return [_conflict_info(row) for row in WorldConflictRepository(session).list(project_id, status=status)]

    def get_project_world_conflict(project_id: str, conflict_id: str) -> WorldConflictInfo:
        with get_session() as session:
            require_project(session, project_id)
            row = WorldConflictRepository(session).get(project_id, conflict_id)
            if row is None:
                raise HTTPException(status_code=404, detail="world conflict not found")
            return _conflict_info(row)

    def resolve_project_world_conflict(
        project_id: str, conflict_id: str, request: WorldConflictResolveRequest,
    ) -> WorldConflictInfo:
        with get_session() as session:
            project = require_project(session, project_id)
            repo = WorldConflictRepository(session)
            row = repo.get(project_id, conflict_id)
            if row is None:
                raise HTTPException(status_code=404, detail="world conflict not found")
            try:
                policy = ProjectPolicyStore(session).load(project).policy
            except ProjectPolicyMissing as exc:
                raise HTTPException(status_code=500, detail="project runtime policy is missing") from exc
            try:
                decision = ConflictDecision(
                    option=request.option, decided_by="human", actor_id="human",
                    rationale=request.rationale, reason=request.reason, decided_at=_utcnow(),
                )
            except (ValidationError, ValueError) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            try:
                ConflictResolutionService(
                    session, admission=CanonAdmissionService(), policy=policy,
                ).resolve(project_id=project_id, conflict_id=conflict_id, decision=decision)
            except (ConflictNotOpen, ConflictStale) as exc:
                raise HTTPException(status_code=409, detail=str(exc)) from exc
            except (ValidationError, json.JSONDecodeError):
                raise
            except (ValueError, ConflictOptionUnavailable) as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            session.commit()
            return _conflict_info(row)

    return {
        "list_project_world_conflicts": list_project_world_conflicts,
        "get_project_world_conflict": get_project_world_conflict,
        "resolve_project_world_conflict": resolve_project_world_conflict,
    }


__all__ = ["build_handlers"]
