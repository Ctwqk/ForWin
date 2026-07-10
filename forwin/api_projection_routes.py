from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException
from sqlalchemy import select

from forwin.knowledge_system.projection_jobs import (
    KNOWLEDGE_PROJECTION_REFRESH_EVENT,
    enqueue_projection_refresh,
    normalize_projection_kind,
    refresh_projection_now,
)
from forwin.knowledge_system.store import load_json
from forwin.models.project import Project
from forwin.models.knowledge import KnowledgeProjectionPageRow
from forwin.api_schema import WorldModelPageInfo


def build_handlers(
    *,
    get_session: Callable[[], Any],
    get_config: Callable[[], Any] | None = None,
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Callable[..., Any]]:
    def _qdrant_url() -> str | None:
        config = get_config() if get_config is not None else None
        return getattr(config, "qdrant_url", None)

    def _llm_kb_collection() -> str | None:
        config = get_config() if get_config is not None else None
        return getattr(config, "llm_kb_qdrant_collection", None)

    def refresh_projection(
        project_id: str,
        projection_kind: str = "all",
        as_of_chapter: int = 0,
        observer_type: str = "reader",  # noqa: ARG001 - reserved for role-aware projections.
        observer_id: str = "reader",  # noqa: ARG001
        role_scope: str = "human",  # noqa: ARG001
        force: bool = False,  # noqa: ARG001 - digest skip/write policy is handled by compilers.
        defer: bool = False,
    ) -> dict[str, Any]:
        _ = (observer_type, observer_id, role_scope, force)
        with get_session() as session:
            _require_project(session, project_id)
            try:
                kind = normalize_projection_kind(projection_kind)
                if defer:
                    event = enqueue_projection_refresh(
                        session,
                        project_id=project_id,
                        projection_kind=kind,
                        as_of_chapter=as_of_chapter,
                        trigger="projection_api_refresh",
                    )
                    session.commit()
                    return {
                        "ok": True,
                        "deferred": True,
                        "project_id": project_id,
                        "projection_kind": kind,
                        "as_of_chapter": int(as_of_chapter or 0),
                        "event_type": KNOWLEDGE_PROJECTION_REFRESH_EVENT,
                        "outbox_event_id": event.event_id,
                        "outbox_row_id": event.id,
                    }
                payload = refresh_projection_now(
                    session,
                    project_id=project_id,
                    projection_kind=kind,
                    as_of_chapter=as_of_chapter,
                    trigger="projection_api_refresh",
                    obsidian_root=obsidian_root,
                    llm_kb_root=llm_kb_root,
                    qdrant_url=_qdrant_url(),
                    qdrant_collection=_llm_kb_collection(),
                    qdrant_client=qdrant_client,
                    qdrant_models=qdrant_models,
                )
                session.commit()
                return payload
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc

    def get_projection_status(
        project_id: str, projection_kind: str = ""
    ) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            rows = _page_rows(session, project_id, projection_kind=projection_kind)
            latest = max(
                (row.updated_at for row in rows if row.updated_at is not None),
                default=None,
            )
            return {
                "project_id": project_id,
                "projection_kind": projection_kind or "all",
                "page_count": len(rows),
                "latest_updated_at": latest.isoformat(sep=" ", timespec="seconds")
                if latest
                else "",
                "projection_versions": sorted(
                    {row.projection_version for row in rows if row.projection_version}
                ),
            }

    def list_projection_pages(
        project_id: str,
        projection_kind: str = "",
        role_scope: str = "",
        as_of_chapter: int = 0,
    ) -> list[Any]:
        with get_session() as session:
            _require_project(session, project_id)
            rows = _page_rows(
                session,
                project_id,
                projection_kind=projection_kind,
                role_scope=role_scope,
                as_of_chapter=as_of_chapter,
            )
            return [_page_info(row) for row in rows]

    def get_projection_page(
        project_id: str, page_key: str, projection_kind: str = ""
    ) -> Any:
        with get_session() as session:
            _require_project(session, project_id)
            query = select(KnowledgeProjectionPageRow).where(
                KnowledgeProjectionPageRow.project_id == project_id,
                KnowledgeProjectionPageRow.page_key == page_key,
            )
            if projection_kind:
                query = query.where(
                    KnowledgeProjectionPageRow.projection_kind == projection_kind
                )
            row = session.execute(
                query.order_by(
                    KnowledgeProjectionPageRow.as_of_chapter.desc(),
                    KnowledgeProjectionPageRow.updated_at.desc(),
                ).limit(1)
            ).scalar_one_or_none()
            if row is None:
                raise HTTPException(status_code=404, detail="projection page not found")
            return _page_info(row)

    return {
        "refresh_projection": refresh_projection,
        "get_projection_status": get_projection_status,
        "list_projection_pages": list_projection_pages,
        "get_projection_page": get_projection_page,
    }


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def _page_rows(
    session,
    project_id: str,
    *,
    projection_kind: str = "",
    role_scope: str = "",
    as_of_chapter: int = 0,
) -> list[KnowledgeProjectionPageRow]:
    query = select(KnowledgeProjectionPageRow).where(
        KnowledgeProjectionPageRow.project_id == project_id
    )
    if projection_kind:
        query = query.where(
            KnowledgeProjectionPageRow.projection_kind == projection_kind
        )
    if role_scope:
        query = query.where(KnowledgeProjectionPageRow.role_scope == role_scope)
    if int(as_of_chapter or 0) > 0:
        query = query.where(
            KnowledgeProjectionPageRow.as_of_chapter <= int(as_of_chapter)
        )
    return list(
        session.execute(
            query.order_by(
                KnowledgeProjectionPageRow.projection_kind.asc(),
                KnowledgeProjectionPageRow.page_type.asc(),
                KnowledgeProjectionPageRow.title.asc(),
            )
        )
        .scalars()
        .all()
    )


def _page_info(row: KnowledgeProjectionPageRow) -> WorldModelPageInfo:
    return WorldModelPageInfo(
        id=row.id,
        project_id=row.project_id,
        page_key=row.page_key,
        page_type=row.page_type,
        title=row.title,
        vault_path=row.vault_path,
        markdown=row.markdown,
        frontmatter=load_json(row.frontmatter_json, {}),
        projection_kind=row.projection_kind or "world_studio",
        projection_version=row.projection_version,
        source_digest=row.source_digest,
        section_digest=load_json(row.section_digest_json, {}),
        observer_type=row.observer_type,
        observer_id=row.observer_id,
        role_scope=row.role_scope,
        visibility_scope=row.visibility_scope,
        canon_status=row.canon_status or "canon_projection",
        content_hash=row.content_hash,
        revision=row.revision,
        status=row.status,
        as_of_chapter=row.as_of_chapter,
        logical_identity_key=row.logical_identity_key,
        canonical_source_type=row.canonical_source_type,
        canonical_source_id=row.canonical_source_id,
        supersedes_page_id=row.supersedes_page_id,
        canonical_rank=row.canonical_rank,
        updated_at=row.updated_at.isoformat() if row.updated_at else "",
    )
