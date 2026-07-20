from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schema import (
    WorldEditProposalCreateRequest,
    WorldEditProposalInfo,
    WorldEditProposalReviewRequest,
)
from forwin.http.request_support import require_project
from forwin.models.knowledge import KnowledgeEditProposalRow
from forwin.knowledge_system.store import load_json
from forwin.proposals.proposal_review import approve_world_edit_proposal


def build_handlers(
    *,
    get_session: Callable[[], Any],
) -> dict[str, Callable[..., Any]]:
    def list_project_proposals(project_id: str) -> list[WorldEditProposalInfo]:
        with get_session() as session:
            require_project(session, project_id)
            rows = (
                session.execute(
                    select(KnowledgeEditProposalRow)
                    .where(KnowledgeEditProposalRow.project_id == project_id)
                    .order_by(
                        KnowledgeEditProposalRow.created_at.desc(),
                        KnowledgeEditProposalRow.id.desc(),
                    )
                )
                .scalars()
                .all()
            )
            return [_proposal_info(row) for row in rows]

    def get_project_proposal(
        project_id: str, proposal_id: str
    ) -> WorldEditProposalInfo:
        with get_session() as session:
            require_project(session, project_id)
            return _proposal_info(_get_proposal(session, project_id, proposal_id))

    def create_project_proposal(
        project_id: str,
        req: WorldEditProposalCreateRequest,
    ) -> WorldEditProposalInfo:
        with get_session() as session:
            require_project(session, project_id)
            row = KnowledgeEditProposalRow(
                project_id=project_id,
                source=req.source or "world_studio",
                target_page_key=req.target_page_key,
                target_node_id=req.target_node_id,
                target_field=req.target_field,
                proposal_type=req.proposal_type or "CanonCorrectionProposal",
                proposed_patch_json=json.dumps(
                    req.proposed_patch or {}, ensure_ascii=False, sort_keys=True
                ),
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
            require_project(session, project_id)
            try:
                result = approve_world_edit_proposal(
                    session,
                    project_id=project_id,
                    proposal_id=proposal_id,
                    reason=request.reason,
                    forced_accept_reason=request.forced_accept_reason,
                    trigger="proposal_api_approve",
                )
                session.commit()
                return _proposal_info(
                    result.row, projection_refresh=result.projection_refresh
                )
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
            require_project(session, project_id)
            row = _get_proposal(session, project_id, proposal_id)
            if row.status not in {"pending", "proposed"}:
                raise HTTPException(
                    status_code=409, detail=f"proposal already {row.status}"
                )
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


def _get_proposal(
    session, project_id: str, proposal_id: str
) -> KnowledgeEditProposalRow:
    row = session.get(KnowledgeEditProposalRow, proposal_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="proposal not found")
    return row


def _proposal_info(
    row: KnowledgeEditProposalRow,
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
