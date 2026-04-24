from __future__ import annotations

from typing import Any, Callable

from fastapi import HTTPException
from fastapi.responses import HTMLResponse, Response

from forwin.api_schemas import WorldEditProposalReviewRequest, WorldModelExportRequest, WorldModelImportRequest
from forwin.world_model import api as world_model_api


def build_handlers(
    *,
    get_session: Callable[[], Any],
) -> dict[str, Callable[..., Any]]:
    def world_studio_page():
        return HTMLResponse(_world_studio_html())

    def world_studio_asset(asset_path: str):
        data, media_type = _world_studio_asset(asset_path)
        return Response(content=data, media_type=media_type)

    def list_project_world_model_snapshots(project_id: str):
        return world_model_api.list_snapshots(project_id, get_session=get_session)

    def get_latest_project_world_model_snapshot(project_id: str, as_of_chapter: int | None = None):
        return world_model_api.latest_snapshot(
            project_id,
            as_of_chapter=as_of_chapter,
            get_session=get_session,
        )

    def list_project_world_model_pages(project_id: str):
        return world_model_api.list_pages(project_id, get_session=get_session)

    def get_project_world_model_page(project_id: str, page_key: str):
        return world_model_api.get_page(project_id, page_key, get_session=get_session)

    def list_project_world_model_conflicts(project_id: str):
        return world_model_api.list_conflicts(project_id, get_session=get_session)

    def export_project_world_model(project_id: str, req: WorldModelExportRequest):
        return world_model_api.export_obsidian(project_id, req, get_session=get_session)

    def import_project_world_model(project_id: str, req: WorldModelImportRequest):
        return world_model_api.import_obsidian(project_id, req, get_session=get_session)

    def list_project_world_model_proposals(project_id: str):
        return world_model_api.list_proposals(project_id, get_session=get_session)

    def review_project_world_model_proposal(
        project_id: str,
        proposal_id: str,
        req: WorldEditProposalReviewRequest,
    ):
        return world_model_api.review_proposal(
            project_id,
            proposal_id,
            req,
            get_session=get_session,
        )

    return {
        "world_studio_page": world_studio_page,
        "world_studio_asset": world_studio_asset,
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


def _world_studio_root():
    from pathlib import Path

    return Path(__file__).resolve().parents[1] / "frontend" / "world-studio"


def _world_studio_html() -> str:
    dist_index = _world_studio_root() / "dist" / "index.html"
    if dist_index.exists():
        return dist_index.read_text(encoding="utf-8")
    return """<!doctype html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>ForWin World Studio</title>
</head>
<body>
  <div id="root">World Studio 尚未构建。运行 npm --prefix frontend/world-studio run build。</div>
</body>
</html>
"""


def _world_studio_asset(asset_path: str) -> tuple[bytes, str]:
    path = (_world_studio_root() / "dist" / "assets" / asset_path).resolve()
    asset_root = (_world_studio_root() / "dist" / "assets").resolve()
    if not str(path).startswith(str(asset_root)) or not path.exists():
        raise HTTPException(404, "World Studio asset not found")
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
