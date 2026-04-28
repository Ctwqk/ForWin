from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schemas import (
    WorldEditProposalInfo,
    WorldEditProposalReviewRequest,
    WorldModelExportRequest,
    WorldModelExportResponse,
    WorldModelImportRequest,
    WorldModelImportResponse,
)
from forwin.book_state.compiler import BookStateCompiler
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.knowledge_system import KnowledgeProjectionRefresher
from forwin.models.project import Project
from forwin.models.world_model import WorldEditProposalRow
from forwin.obsidian import ObsidianExporter, ObsidianImporter
from forwin.obsidian.structured_patch import proposal_to_graph_delta
from forwin.protocol.book_state import ApprovedGraphDeltaSet
from forwin.world_model.store import load_json


def _dt(value) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else ""


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _proposal_info(row: WorldEditProposalRow, *, projection_refresh: dict[str, Any] | None = None) -> WorldEditProposalInfo:
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

    def export_obsidian(project_id: str, req: WorldModelExportRequest) -> WorldModelExportResponse:
        with get_session() as session:
            _require_project(session, project_id)
            vault_root = Path(req.vault_root) if str(req.vault_root or "").strip() else None
            with session.begin_nested():
                result = ObsidianExporter(session).export_project(project_id, vault_root=vault_root)
            session.commit()
            return WorldModelExportResponse(
                ok=True,
                project_id=project_id,
                vault_root=result.vault_root,
                exported_count=result.exported_count,
                message=f"exported BookState-backed Obsidian vault as of chapter {result.as_of_chapter}",
            )

    def import_obsidian(project_id: str, req: WorldModelImportRequest) -> WorldModelImportResponse:
        with get_session() as session:
            _require_project(session, project_id)
            vault_root = Path(req.vault_root) if str(req.vault_root or "").strip() else None
            with session.begin_nested():
                result = ObsidianImporter(session).import_project(project_id, vault_root=vault_root)
            session.commit()
            return WorldModelImportResponse(
                ok=True,
                project_id=project_id,
                vault_root=result.vault_root,
                proposal_count=result.proposal_count,
                changed_paths=result.changed_paths,
                message=f"created {result.proposal_count} Obsidian proposal(s)",
            )

    def list_proposals(project_id: str) -> list[WorldEditProposalInfo]:
        with get_session() as session:
            _require_project(session, project_id)
            rows = session.execute(
                select(WorldEditProposalRow)
                .where(WorldEditProposalRow.project_id == project_id)
                .order_by(WorldEditProposalRow.created_at.desc(), WorldEditProposalRow.id.desc())
            ).scalars().all()
            return [_proposal_info(row) for row in rows]

    def approve_proposal(
        project_id: str,
        proposal_id: str,
        req: WorldEditProposalReviewRequest | None = None,
    ) -> WorldEditProposalInfo:
        request = req or WorldEditProposalReviewRequest(status="accepted", reason="")
        with get_session() as session:
            _require_project(session, project_id)
            try:
                row = _load_pending_proposal(session, project_id, proposal_id)
                try:
                    delta = proposal_to_graph_delta(session, row, reason=request.reason)
                except ValueError as exc:
                    raise HTTPException(status_code=400, detail=str(exc)) from exc
                changes = ApprovedGraphDeltaSet(
                    project_id=project_id,
                    chapter_number=_proposal_chapter(row),
                    graph_deltas=[delta],
                    approved_by=["obsidian_proposal_approval"],
                    review_verdict_id=f"obsidian_proposal_review_{proposal_id}",
                )
                verdict = BookStateReviewGate(session).review(changes)
                if not verdict.accepted or verdict.approved_changes is None:
                    message = "; ".join(f"{issue.code}: {issue.message}" for issue in verdict.issues)
                    raise HTTPException(status_code=409, detail=message or "proposal rejected by BookStateReviewGate")
                result = BookStateCompiler(session).compile(verdict.approved_changes)
                if not result.committed:
                    raise HTTPException(status_code=409, detail="; ".join(result.blocked_reasons) or "BookState compile blocked")
                row.status = "accepted"
                row.reviewed_at = datetime.now(UTC)
                row.review_reason = request.reason
                row.graph_delta_id = delta.id
                session.add(row)
                session.flush()
                projection_refresh = KnowledgeProjectionRefresher(
                    session,
                    qdrant_url=_qdrant_url(),
                    qdrant_collection=_llm_kb_qdrant_collection(),
                    qdrant_client=qdrant_client,
                    qdrant_models=qdrant_models,
                ).refresh(
                    project_id,
                    as_of_chapter=result.chapter_number,
                    trigger="obsidian_proposal_approve",
                )
                session.commit()
                return _proposal_info(row, projection_refresh=projection_refresh.as_dict())
            except Exception:
                session.rollback()
                raise

    def reject_proposal(
        project_id: str,
        proposal_id: str,
        req: WorldEditProposalReviewRequest | None = None,
    ) -> WorldEditProposalInfo:
        request = req or WorldEditProposalReviewRequest(status="rejected", reason="")
        with get_session() as session:
            _require_project(session, project_id)
            row = session.get(WorldEditProposalRow, proposal_id)
            if row is None or row.project_id != project_id:
                raise HTTPException(status_code=404, detail="proposal not found")
            row.status = "rejected"
            row.reviewed_at = datetime.now(UTC)
            row.review_reason = request.reason
            session.add(row)
            session.commit()
            return _proposal_info(row)

    return {
        "export_obsidian": export_obsidian,
        "import_obsidian": import_obsidian,
        "list_obsidian_proposals": list_proposals,
        "approve_obsidian_proposal": approve_proposal,
        "reject_obsidian_proposal": reject_proposal,
    }


def _load_pending_proposal(session, project_id: str, proposal_id: str) -> WorldEditProposalRow:
    row = session.get(WorldEditProposalRow, proposal_id)
    if row is None or row.project_id != project_id:
        raise HTTPException(status_code=404, detail="proposal not found")
    if row.status not in {"pending", "proposed"}:
        raise HTTPException(status_code=409, detail=f"proposal is not pending: {row.status}")
    return row


def _proposal_chapter(row: WorldEditProposalRow) -> int:
    payload = load_json(row.proposed_patch_json, {})
    frontmatter = payload.get("frontmatter") if isinstance(payload.get("frontmatter"), dict) else {}
    try:
        return int(frontmatter.get("as_of_chapter") or 0)
    except (TypeError, ValueError):
        return 0
