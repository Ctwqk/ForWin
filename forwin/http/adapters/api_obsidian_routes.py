from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from forwin.api_schema import (
    WorldModelExportRequest,
    WorldModelExportResponse,
    WorldModelImportRequest,
    WorldModelImportResponse,
)
from forwin.http.request_support import require_project
from forwin.obsidian import ObsidianExporter, ObsidianImporter
from forwin.retrieval.obsidian_human_index import ObsidianHumanVectorIndex


def build_handlers(
    *,
    get_session: Callable[[], Any],
    get_config: Callable[[], Any] | None = None,
    qdrant_url: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Callable[..., Any]]:
    def _qdrant_url() -> str | None:
        if qdrant_url is not None:
            return qdrant_url
        config = get_config() if get_config is not None else None
        return getattr(config, "qdrant_url", None)

    def export_obsidian(
        project_id: str, req: WorldModelExportRequest
    ) -> WorldModelExportResponse:
        with get_session() as session:
            require_project(session, project_id)
            vault_root = (
                Path(req.vault_root) if str(req.vault_root or "").strip() else None
            )
            with session.begin_nested():
                result = ObsidianExporter(session).export_project(
                    project_id, vault_root=vault_root
                )
            session.commit()
            _rebuild_human_index(project_id, Path(result.vault_root))
            return WorldModelExportResponse(
                ok=True,
                project_id=project_id,
                vault_root=result.vault_root,
                exported_count=result.exported_count,
                message=f"exported BookState-backed Obsidian vault as of chapter {result.as_of_chapter}",
            )

    def import_obsidian(
        project_id: str, req: WorldModelImportRequest
    ) -> WorldModelImportResponse:
        with get_session() as session:
            require_project(session, project_id)
            vault_root = (
                Path(req.vault_root) if str(req.vault_root or "").strip() else None
            )
            with session.begin_nested():
                result = ObsidianImporter(session).import_project(
                    project_id, vault_root=vault_root
                )
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
    }
