from __future__ import annotations

from typing import Any, Callable
from pathlib import Path

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response
from sqlalchemy import select

from forwin import api_obsidian_routes
from forwin.api_pages_shared import join_page_assets
from forwin.api_projection_routes import _page_info
from forwin.api_schema import (
    WorldEditProposalReviewRequest,
    WorldModelConflictInfo,
    WorldModelExportRequest,
    WorldModelImportRequest,
    WorldModelSnapshotInfo,
)
from forwin.knowledge_system.page_repository import KnowledgePageRepository
from forwin.knowledge_system.store import load_json
from forwin.models.book_state import WorldSnapshotRow
from forwin.models.canon_quality import CanonQualitySignalRow
from forwin.models.project import Project


_WORLD_STUDIO_TOPBAR_ASSET_ID = "forwin-world-studio-shared-topbar"


def build_handlers(
    *,
    get_session: Callable[[], Any],
    get_config: Callable[[], Any] | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Callable[..., Any]]:
    obsidian_handlers = api_obsidian_routes.build_handlers(
        get_session=get_session,
        get_config=get_config,
        qdrant_client=qdrant_client,
        qdrant_models=qdrant_models,
    )

    def world_studio_page():
        return HTMLResponse(_world_studio_html())

    def world_studio_asset(asset_path: str):
        data, media_type = _world_studio_asset(asset_path)
        return Response(content=data, media_type=media_type)

    def list_project_world_model_snapshots(project_id: str):
        with get_session() as session:
            _require_project(session, project_id)
            rows = (
                session.execute(
                    select(WorldSnapshotRow)
                    .where(WorldSnapshotRow.project_id == project_id)
                    .order_by(
                        WorldSnapshotRow.as_of_chapter.desc(),
                        WorldSnapshotRow.built_at.desc(),
                    )
                )
                .scalars()
                .all()
            )
            return [_snapshot_info(row) for row in rows]

    def get_latest_project_world_model_snapshot(
        project_id: str, as_of_chapter: int | None = None
    ):
        with get_session() as session:
            _require_project(session, project_id)
            query = select(WorldSnapshotRow).where(
                WorldSnapshotRow.project_id == project_id
            )
            if as_of_chapter is not None:
                query = query.where(
                    WorldSnapshotRow.as_of_chapter <= int(as_of_chapter)
                )
            row = session.execute(
                query.order_by(
                    WorldSnapshotRow.as_of_chapter.desc(),
                    WorldSnapshotRow.built_at.desc(),
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(404, "BookState snapshot not found")
            return _snapshot_info(row)

    def list_project_world_model_pages(project_id: str):
        with get_session() as session:
            _require_project(session, project_id)
            return [
                _page_info(row)
                for row in KnowledgePageRepository(session).list_canonical_rows(
                    project_id
                )
            ]

    def get_project_world_model_page(project_id: str, page_key: str):
        with get_session() as session:
            _require_project(session, project_id)
            row = KnowledgePageRepository(session).resolve_page_key(
                project_id,
                page_key,
            )
            if row is None:
                raise HTTPException(404, "projection page not found")
            return _page_info(row)

    def list_project_world_model_conflicts(project_id: str):
        with get_session() as session:
            _require_project(session, project_id)
            rows = (
                session.execute(
                    select(CanonQualitySignalRow)
                    .where(CanonQualitySignalRow.project_id == project_id)
                    .order_by(
                        CanonQualitySignalRow.status.asc(),
                        CanonQualitySignalRow.created_at.desc(),
                    )
                )
                .scalars()
                .all()
            )
            return [_conflict_info(row) for row in rows]

    def export_project_world_model(project_id: str, req: WorldModelExportRequest):
        return obsidian_handlers["export_obsidian"](project_id, req)

    def import_project_world_model(project_id: str, req: WorldModelImportRequest):
        return obsidian_handlers["import_obsidian"](project_id, req)

    def list_project_world_model_proposals(project_id: str):
        return obsidian_handlers["list_obsidian_proposals"](project_id)

    def review_project_world_model_proposal(
        project_id: str,
        proposal_id: str,
        req: WorldEditProposalReviewRequest,
    ):
        handler_key = (
            "approve_obsidian_proposal"
            if req.status in {"accepted", "approved"}
            else "reject_obsidian_proposal"
        )
        return obsidian_handlers[handler_key](project_id, proposal_id, req)

    def search_project_world_studio(
        project_id: str,
        query: str,
        index_kind: str = "all",
        role: str = "human",
        as_of_chapter: int = 0,
        section_type: str = "",
        limit: int = 10,
    ):
        from forwin.world_studio.search_service import WorldStudioSearchService

        with get_session() as session:
            _require_project(session, project_id)
            config = get_config() if get_config is not None else None
            return WorldStudioSearchService(
                skill_root=Path(
                    getattr(config, "skill_registry_path", "forwin_skills")
                ),
                qdrant_url=getattr(config, "qdrant_url", None),
                llm_kb_collection=getattr(config, "llm_kb_qdrant_collection", None),
                qdrant_client=qdrant_client,
                qdrant_models=qdrant_models,
                session=session,
            ).search(
                project_id,
                query=query,
                index_kind=index_kind,
                role=role,
                as_of_chapter=as_of_chapter,
                section_type=section_type,
                limit=limit,
            )

    return {
        "world_studio_page": world_studio_page,
        "world_studio_asset": world_studio_asset,
        "search_project_world_studio": search_project_world_studio,
        "list_project_world_model_snapshots": list_project_world_model_snapshots,
        "get_latest_project_world_model_snapshot": get_latest_project_world_model_snapshot,
        "list_project_world_model_pages": list_project_world_model_pages,
        "get_project_world_model_page": get_project_world_model_page,
        "list_project_world_model_conflicts": list_project_world_model_conflicts,
        "export_project_world_model": export_project_world_model,
        "import_project_world_model": import_project_world_model,
        "list_project_world_model_proposals": list_project_world_model_proposals,
        "review_project_world_model_proposal": review_project_world_model_proposal,
    }


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(404, "project not found")
    return project


def _snapshot_info(row: WorldSnapshotRow) -> WorldModelSnapshotInfo:
    return WorldModelSnapshotInfo(
        id=row.id,
        project_id=row.project_id,
        as_of_chapter=row.as_of_chapter,
        version=1,
        status="live",
        source_digest=row.objective_graph_digest,
        snapshot={
            "as_of_story_time": row.as_of_story_time,
            "base_snapshot_id": row.base_snapshot_id,
            "objective_graph_digest": row.objective_graph_digest,
            "map_graph_digest": row.map_graph_digest,
            "reader_overlay_digest": row.reader_overlay_digest,
            "world_node_state_index": load_json(
                row.world_node_state_index_json,
                {},
            ),
            "active_edge_ids": load_json(row.active_edge_ids_json, []),
            "active_fact_ids": load_json(row.active_fact_ids_json, []),
            "active_world_line_ids": load_json(
                row.active_world_line_ids_json,
                [],
            ),
            "open_gap_ids": load_json(row.open_gap_ids_json, []),
            "active_promise_ids": load_json(row.active_promise_ids_json, []),
            "objective_state_summary": row.objective_state_summary,
            "reader_state_summary": row.reader_state_summary,
            "source_delta_ids": load_json(row.source_delta_ids_json, []),
        },
        created_at=row.built_at.isoformat() if row.built_at else "",
        updated_at=row.built_at.isoformat() if row.built_at else "",
    )


def _conflict_info(row: CanonQualitySignalRow) -> WorldModelConflictInfo:
    evidence_refs = [
        item
        if isinstance(item, dict)
        else {
            "source_type": "canon_quality",
            "source_id": str(item),
            "chapter_number": int(row.chapter_number or 0),
        }
        for item in load_json(row.evidence_refs_json, [])
    ]
    return WorldModelConflictInfo(
        id=row.id,
        project_id=row.project_id,
        conflict_type=row.signal_type,
        severity=row.severity,
        subject_key=row.subject_key,
        description=row.description,
        evidence_refs=evidence_refs,
        status=row.status,
        created_at=row.created_at.isoformat() if row.created_at else "",
        resolved_at=row.resolved_at.isoformat() if row.resolved_at else "",
    )


def _world_studio_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "frontend" / "world-studio"


def _world_studio_html() -> str:
    dist_index = _world_studio_root() / "dist" / "index.html"
    if dist_index.exists():
        return _inject_world_studio_shared_topbar(
            dist_index.read_text(encoding="utf-8")
        )
    return f"""<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ForWin 世界档案</title>
{_world_studio_shared_topbar_assets()}
</head>
<body>
  <forwin-topbar active="world"></forwin-topbar>
  <div id="root">世界档案尚未构建。运行 npm --prefix frontend/world-studio run build。</div>
</body>
</html>
"""


def _world_studio_shared_topbar_assets() -> str:
    return f"""  <style id="{_WORLD_STUDIO_TOPBAR_ASSET_ID}">
{join_page_assets("shared/topbar.css")}
  </style>
  <script>
{join_page_assets("shared/i18n.js", "shared/forwin-topbar.js")}
  </script>"""


def _inject_world_studio_shared_topbar(html: str) -> str:
    if _WORLD_STUDIO_TOPBAR_ASSET_ID in html:
        return html
    assets = _world_studio_shared_topbar_assets()
    if "</head>" not in html:
        return f"{assets}\n{html}"
    return html.replace("</head>", f"{assets}\n</head>", 1)


def _world_studio_asset(asset_path: str) -> tuple[bytes, str]:
    path = (_world_studio_root() / "dist" / "assets" / asset_path).resolve()
    asset_root = (_world_studio_root() / "dist" / "assets").resolve()
    try:
        path.relative_to(asset_root)
    except ValueError:
        raise HTTPException(404, "世界档案 asset not found") from None
    if not path.exists():
        raise HTTPException(404, "世界档案 asset not found")
    suffix = path.suffix.lower()
    media_type = {
        ".js": "text/javascript; charset=utf-8",
        ".css": "text/css; charset=utf-8",
        ".svg": "image/svg+xml",
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".woff2": "font/woff2",
    }.get(suffix, "application/octet-stream")
    return path.read_bytes(), media_type
