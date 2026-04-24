from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from fastapi import HTTPException
from sqlalchemy import select

from forwin.api_schemas import (
    WorldEditProposalInfo,
    WorldEditProposalReviewRequest,
    WorldModelConflictInfo,
    WorldModelExportRequest,
    WorldModelExportResponse,
    WorldModelImportRequest,
    WorldModelImportResponse,
    WorldModelPageInfo,
    WorldModelSnapshotInfo,
)
from forwin.models.project import Project
from forwin.models.world_model import (
    WorldEditProposalRow,
    WorldModelConflictRow,
    WorldModelPageRow,
    WorldModelSnapshotRow,
)

from .compiler import WorldModelCompiler
from .exporter_obsidian import ObsidianWorldExporter
from .importer_obsidian import ObsidianWorldImporter
from .store import load_json


def _dt(value) -> str:
    return value.isoformat(sep=" ", timespec="seconds") if value else ""


def _ensure_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "项目不存在")
    return project


def _snapshot_info(row: WorldModelSnapshotRow) -> WorldModelSnapshotInfo:
    return WorldModelSnapshotInfo(
        id=row.id,
        project_id=row.project_id,
        as_of_chapter=row.as_of_chapter,
        version=row.version,
        status=row.status,
        source_digest=row.source_digest,
        snapshot=load_json(row.snapshot_json, {}),
        created_at=_dt(row.created_at),
        updated_at=_dt(row.updated_at),
    )


def _page_info(row: WorldModelPageRow) -> WorldModelPageInfo:
    return WorldModelPageInfo(
        id=row.id,
        project_id=row.project_id,
        page_key=row.page_key,
        page_type=row.page_type,
        title=row.title,
        vault_path=row.vault_path,
        markdown=row.markdown,
        frontmatter=load_json(row.frontmatter_json, {}),
        content_hash=row.content_hash,
        revision=row.revision,
        status=row.status,
        as_of_chapter=row.as_of_chapter,
        updated_at=_dt(row.updated_at),
    )


def _conflict_info(row: WorldModelConflictRow) -> WorldModelConflictInfo:
    return WorldModelConflictInfo(
        id=row.id,
        project_id=row.project_id,
        conflict_type=row.conflict_type,
        severity=row.severity,
        subject_key=row.subject_key,
        description=row.description,
        evidence_refs=load_json(row.evidence_refs_json, []),
        status=row.status,
        created_at=_dt(row.created_at),
        resolved_at=_dt(row.resolved_at),
    )


def _proposal_info(row: WorldEditProposalRow) -> WorldEditProposalInfo:
    return WorldEditProposalInfo(
        id=row.id,
        project_id=row.project_id,
        source=row.source,
        target_page_key=row.target_page_key,
        target_field=row.target_field,
        proposed_patch=load_json(row.proposed_patch_json, {}),
        reason=row.reason,
        status=row.status,
        created_by=row.created_by,
        created_at=_dt(row.created_at),
        reviewed_at=_dt(row.reviewed_at),
    )


def list_snapshots(project_id: str, *, get_session) -> list[WorldModelSnapshotInfo]:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        rows = session.execute(
            select(WorldModelSnapshotRow)
            .where(WorldModelSnapshotRow.project_id == project_id)
            .order_by(WorldModelSnapshotRow.as_of_chapter.desc(), WorldModelSnapshotRow.version.desc())
        ).scalars().all()
        return [_snapshot_info(row) for row in rows]
    finally:
        session.close()


def latest_snapshot(project_id: str, *, as_of_chapter: int | None = None, get_session) -> WorldModelSnapshotInfo:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        query = (
            select(WorldModelSnapshotRow)
            .where(WorldModelSnapshotRow.project_id == project_id, WorldModelSnapshotRow.status == "live")
            .order_by(WorldModelSnapshotRow.as_of_chapter.desc(), WorldModelSnapshotRow.version.desc())
            .limit(1)
        )
        if as_of_chapter is not None:
            query = query.where(WorldModelSnapshotRow.as_of_chapter <= int(as_of_chapter))
        row = session.execute(query).scalar_one_or_none()
        if row is None:
            row = _bootstrap_if_possible(session, project_id)
            session.commit()
        if row is None:
            raise HTTPException(404, "WorldModel snapshot 不存在")
        return _snapshot_info(row)
    finally:
        session.close()


def list_pages(project_id: str, *, get_session) -> list[WorldModelPageInfo]:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        _bootstrap_if_needed(session, project_id)
        rows = session.execute(
            select(WorldModelPageRow)
            .where(WorldModelPageRow.project_id == project_id)
            .order_by(WorldModelPageRow.page_type.asc(), WorldModelPageRow.title.asc())
        ).scalars().all()
        session.commit()
        return [_page_info(row) for row in rows]
    finally:
        session.close()


def get_page(project_id: str, page_key: str, *, get_session) -> WorldModelPageInfo:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        row = session.execute(
            select(WorldModelPageRow).where(
                WorldModelPageRow.project_id == project_id,
                WorldModelPageRow.page_key == page_key,
            )
        ).scalar_one_or_none()
        if row is None:
            raise HTTPException(404, "WorldModel page 不存在")
        return _page_info(row)
    finally:
        session.close()


def list_conflicts(project_id: str, *, get_session) -> list[WorldModelConflictInfo]:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        rows = session.execute(
            select(WorldModelConflictRow)
            .where(WorldModelConflictRow.project_id == project_id)
            .order_by(WorldModelConflictRow.status.asc(), WorldModelConflictRow.created_at.desc())
        ).scalars().all()
        return [_conflict_info(row) for row in rows]
    finally:
        session.close()


def export_obsidian(
    project_id: str,
    req: WorldModelExportRequest,
    *,
    get_session,
) -> WorldModelExportResponse:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        _bootstrap_if_needed(session, project_id)
        vault_root = Path(req.vault_root) if str(req.vault_root or "").strip() else None
        result = ObsidianWorldExporter(session).export_project(project_id, vault_root=vault_root)
        session.commit()
        return WorldModelExportResponse.model_validate(result.model_dump(mode="json"))
    finally:
        session.close()


def import_obsidian(
    project_id: str,
    req: WorldModelImportRequest,
    *,
    get_session,
) -> WorldModelImportResponse:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        vault_root = Path(req.vault_root) if str(req.vault_root or "").strip() else None
        result = ObsidianWorldImporter(session).import_project(project_id, vault_root=vault_root)
        session.commit()
        return WorldModelImportResponse.model_validate(result.model_dump(mode="json"))
    finally:
        session.close()


def list_proposals(project_id: str, *, get_session) -> list[WorldEditProposalInfo]:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        rows = session.execute(
            select(WorldEditProposalRow)
            .where(WorldEditProposalRow.project_id == project_id)
            .order_by(WorldEditProposalRow.created_at.desc())
        ).scalars().all()
        return [_proposal_info(row) for row in rows]
    finally:
        session.close()


def review_proposal(
    project_id: str,
    proposal_id: str,
    req: WorldEditProposalReviewRequest,
    *,
    get_session,
) -> WorldEditProposalInfo:
    session = get_session()
    try:
        _ensure_project(session, project_id)
        status = str(req.status or "").strip()
        if status not in {"accepted", "rejected", "superseded"}:
            raise HTTPException(400, "proposal status 只能是 accepted / rejected / superseded")
        row = session.get(WorldEditProposalRow, proposal_id)
        if row is None or row.project_id != project_id:
            raise HTTPException(404, "WorldEditProposal 不存在")
        row.status = status
        if req.reason:
            row.reason = req.reason
        row.reviewed_at = datetime.now(UTC)
        session.add(row)
        if status == "accepted":
            latest = session.execute(
                select(WorldModelSnapshotRow)
                .where(WorldModelSnapshotRow.project_id == project_id, WorldModelSnapshotRow.status == "live")
                .order_by(WorldModelSnapshotRow.as_of_chapter.desc())
                .limit(1)
            ).scalar_one_or_none()
            if latest is not None:
                WorldModelCompiler(session).compile_after_chapter(project_id, latest.as_of_chapter)
        session.commit()
        return _proposal_info(row)
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


def _bootstrap_if_needed(session, project_id: str) -> WorldModelSnapshotRow | None:
    row = session.execute(
        select(WorldModelSnapshotRow)
        .where(WorldModelSnapshotRow.project_id == project_id, WorldModelSnapshotRow.status == "live")
        .limit(1)
    ).scalar_one_or_none()
    if row is not None:
        return row
    return _bootstrap_if_possible(session, project_id)


def _bootstrap_if_possible(session, project_id: str) -> WorldModelSnapshotRow | None:
    try:
        snapshot = WorldModelCompiler(session).bootstrap_from_genesis(project_id)
    except ValueError:
        return None
    return session.get(WorldModelSnapshotRow, snapshot.id)
