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
from forwin.models.project import Project
from forwin.models.world_model import WorldEditProposalRow
from forwin.obsidian import ObsidianExporter, ObsidianImporter
from forwin.obsidian.proposal_review import approve_world_edit_proposal
from forwin.retrieval.obsidian_human_index import ObsidianHumanVectorIndex
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
            _rebuild_human_index(project_id, Path(result.vault_root))
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
            _rebuild_human_index(project_id, Path(result.vault_root))
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
                result = approve_world_edit_proposal(
                    session,
                    project_id=project_id,
                    proposal_id=proposal_id,
                    reason=request.reason,
                    trigger="obsidian_proposal_approve",
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

    def _rebuild_human_index(project_id: str, vault_root: Path) -> None:
        try:
            ObsidianHumanVectorIndex(
                qdrant_url=_qdrant_url(),
                qdrant_client=qdrant_client,
                qdrant_models=qdrant_models,
            ).rebuild_project(project_id, vault_root=vault_root)
        except Exception:
            return

    return {
        "export_obsidian": export_obsidian,
        "import_obsidian": import_obsidian,
        "list_obsidian_proposals": list_proposals,
        "approve_obsidian_proposal": approve_proposal,
        "reject_obsidian_proposal": reject_proposal,
    }
