from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schemas import (
    WorldEditProposalCreateRequest,
    WorldEditProposalInfo,
    WorldEditProposalReviewRequest,
)
from forwin.models.project import Project
from forwin.models.world_model import WorldEditProposalRow
from forwin.obsidian.proposal_review import approve_world_edit_proposal
from forwin.world_model.store import load_json


def build_handlers(
    *,
    get_session: Callable[[], Any],
    get_config: Callable[[], Any] | None = None,
    qdrant_url: str | None = None,
    llm_kb_qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Callable[..., Any]]:
    def _qdrant_url() -> str | None:
        if qdrant_url is not None:
            return qdrant_url
        config = get_config() if get_config is not None else None
        return getattr(config, "qdrant_url", None)

    def _llm_kb_qdrant_collection() -> str | None:
        if llm_kb_qdrant_collection is not None:
            return llm_kb_qdrant_collection
        config = get_config() if get_config is not None else None
        return getattr(config, "llm_kb_qdrant_collection", None)

    def list_project_proposals(project_id: str) -> list[WorldEditProposalInfo]:
        with get_session() as session:
            _require_project(session, project_id)
            rows = session.execute(
                select(WorldEditProposalRow)
                .where(WorldEditProposalRow.project_id == project_id)
                .order_by(WorldEditProposalRow.created_at.desc(), WorldEditProposalRow.id.desc())
            ).scalars().all()
            return [_proposal_info(row) for row in rows]

    def get_project_proposal(project_id: str, proposal_id: str) -> WorldEditProposalInfo:
        with get_session() as session:
            _require_project(session, project_id)
            return _proposal_info(_get_proposal(session, project_id, proposal_id))

    def create_project_proposal(
        project_id: str,
        req: WorldEditProposalCreateRequest,
    ) -> WorldEditProposalInfo:
        with get_session() as session:
            _require_project(session, project_id)
            row = WorldEditProposalRow(
                project_id=project_id,
                source=req.source or "world_studio",
                target_page_key=req.target_page_key,
                target_node_id=req.target_node_id,
                target_field=req.target_field,
                proposal_type=req.proposal_type or "CanonCorrectionProposal",
                proposed_patch_json=json.dumps(req.proposed_patch or {}, ensure_ascii=False, sort_keys=True),
                reason=req.reason,
                human_notes=req.human_notes,
                status="pending",
                created_by=req.created_by or "world_studio",
            )
            session.add(row)
            session.commit()
            session.refresh(row)
            return _proposal_info(row)

    def approve_project_proposal(
        project_id: str,
        proposal_id: str,
        req: WorldEditProposalReviewRequest | None = None,
    ) -> WorldEditProposalInfo:
        request = req or WorldEditProposalReviewRequest(status="accepted", reason="")
        with get_session() as session:
            _require_project(session, project_id)
            try:
                result = approve_world_edit_proposal(
                    session,
                    project_id=project_id,
                    proposal_id=proposal_id,
                    reason=request.reason,
                    forced_accept_reason=request.forced_accept_reason,
                    trigger="proposal_api_approve",
                    qdrant_url=_qdrant_url(),
                    qdrant_collection=_llm_kb_qdrant_collection(),
                    qdrant_client=qdrant_client,
                    qdrant_models=qdrant_models,
                )
                session.commit()
                return _proposal_info(result.row, projection_refresh=result.projection_refresh)
            except Exception:
                session.rollback()
                raise

    def reject_project_proposal(
        project_id: str,
        proposal_id: str,
        req: WorldEditProposalReviewRequest | None = None,
    ) -> WorldEditProposalInfo:
        request = req or WorldEditProposalReviewRequest(status="rejected", reason="")
        with get_session() as session:
            _require_project(session, project_id)
            row = _get_proposal(session, project_id, proposal_id)
            if row.status not in {"pending", "proposed"}:
                raise HTTPException(status_code=409, detail=f"proposal already {row.status}")
            row.status = "rejected"
            row.reviewed_at = datetime.now(UTC)
            row.review_reason = request.reason
            session.add(row)
            session.commit()
            session.refresh(row)
            return _proposal_info(row)

    return {
        "list_project_proposals": list_project_proposals,
        "get_project_proposal": get_project_proposal,
        "create_project_proposal": create_project_proposal,
        "approve_project_proposal": approve_project_proposal,
        "reject_project_proposal": reject_project_proposal,
    }


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _get_proposal(session, project_id: str, proposal_id: str) -> WorldEditProposalRow:
    row = session.get(WorldEditProposalRow, proposal_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="proposal not found")
    return row


def _proposal_info(
    row: WorldEditProposalRow,
    *,
    projection_refresh: dict[str, Any] | None = None,
) -> WorldEditProposalInfo:
    return WorldEditProposalInfo(
        id=row.id,
        project_id=row.project_id,
        source=row.source,
        target_page_key=row.target_page_key,
        target_node_id=getattr(row, "target_node_id", "") or "",
        target_field=row.target_field,
        proposal_type=getattr(row, "proposal_type", "") or "",
        proposed_patch=load_json(row.proposed_patch_json, {}),
        reason=row.reason,
        human_notes=getattr(row, "human_notes", "") or "",
        status=row.status,
        created_by=row.created_by,
        created_at=_dt(row.created_at),
        reviewed_at=_dt(row.reviewed_at),
        review_reason=getattr(row, "review_reason", "") or "",
        graph_delta_id=getattr(row, "graph_delta_id", "") or "",
        projection_refresh=projection_refresh or {},
    )


def _dt(value) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else ""
