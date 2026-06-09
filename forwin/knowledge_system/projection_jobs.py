from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from sqlalchemy.orm import Session

from forwin.knowledge_system.refresher import KnowledgeProjectionRefresher
from forwin.llm_kb import LLMKnowledgeBaseCompiler
from forwin.models.outbox import OutboxEvent
from forwin.models.project import Project
from forwin.obsidian import ObsidianExporter
from forwin.outbox.store import enqueue_outbox_event


KNOWLEDGE_PROJECTION_REFRESH_EVENT = "knowledge.projection.refresh_requested"
VALID_PROJECTION_KINDS = {"all", "obsidian", "world_studio", "llm_kb"}


def normalize_projection_kind(projection_kind: str = "all") -> str:
    kind = str(projection_kind or "all").strip().lower()
    if kind not in VALID_PROJECTION_KINDS:
        raise ValueError("projection_kind must be all, obsidian, world_studio, or llm_kb")
    return kind


def refresh_projection_now(
    session: Session,
    *,
    project_id: str,
    projection_kind: str = "all",
    as_of_chapter: int = 0,
    trigger: str = "projection_api_refresh",
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Any]:
    kind = normalize_projection_kind(projection_kind)
    chapter = int(as_of_chapter or 0)
    if kind == "obsidian":
        result = ObsidianExporter(session).export_project(
            project_id,
            vault_root=obsidian_root,
            as_of_chapter=chapter,
        )
        return {
            "ok": True,
            "project_id": project_id,
            "projection_kind": kind,
            "obsidian": result.as_dict(),
        }
    if kind in {"all", "world_studio"}:
        refresh = KnowledgeProjectionRefresher(
            session,
            obsidian_root=obsidian_root,
            llm_kb_root=llm_kb_root,
            qdrant_url=qdrant_url,
            qdrant_collection=qdrant_collection,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
        ).refresh(project_id, as_of_chapter=chapter, trigger=trigger)
        payload = refresh.as_dict()
        payload["projection_kind"] = kind
        return payload
    result = LLMKnowledgeBaseCompiler(
        session,
        root=llm_kb_root,
        qdrant_url=qdrant_url,
        qdrant_collection=qdrant_collection,
        qdrant_client=qdrant_client,
        qdrant_models=qdrant_models,
    ).rebuild(project_id, as_of_chapter=chapter)
    return {
        "ok": True,
        "project_id": project_id,
        "projection_kind": kind,
        "llm_kb": {
            "root": result.root,
            "as_of_chapter": result.as_of_chapter,
            "files": list(result.files),
            "source_digest": result.source_digest,
            "vector_index": dict(result.vector_index),
        },
    }


def enqueue_projection_refresh(
    session: Session,
    *,
    project_id: str,
    projection_kind: str = "all",
    as_of_chapter: int = 0,
    trigger: str = "projection_api_refresh",
) -> OutboxEvent:
    kind = normalize_projection_kind(projection_kind)
    chapter = int(as_of_chapter or 0)
    return enqueue_outbox_event(
        session,
        aggregate_type="project",
        aggregate_id=project_id,
        event_type=KNOWLEDGE_PROJECTION_REFRESH_EVENT,
        payload={
            "project_id": project_id,
            "projection_kind": kind,
            "as_of_chapter": chapter,
            "trigger": trigger,
        },
    )


def handle_projection_refresh_outbox_event(
    event: OutboxEvent,
    *,
    session_factory: Callable[[], Any],
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Any]:
    payload = json.loads(event.payload_json or "{}")
    project_id = str(payload.get("project_id") or event.aggregate_id or "").strip()
    if not project_id:
        raise ValueError("projection outbox event requires project_id")
    with session_factory.begin() as session:
        if session.get(Project, project_id) is None:
            raise ValueError("project not found")
        return refresh_projection_now(
            session,
            project_id=project_id,
            projection_kind=str(payload.get("projection_kind") or "all"),
            as_of_chapter=int(payload.get("as_of_chapter") or 0),
            trigger=str(payload.get("trigger") or "projection_outbox_worker"),
            obsidian_root=obsidian_root,
            llm_kb_root=llm_kb_root,
            qdrant_url=qdrant_url,
            qdrant_collection=qdrant_collection,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
        )


def build_projection_outbox_handlers(
    *,
    session_factory: Callable[[], Any],
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Callable[[OutboxEvent], None]]:
    def handle(event: OutboxEvent) -> None:
        handle_projection_refresh_outbox_event(
            event,
            session_factory=session_factory,
            obsidian_root=obsidian_root,
            llm_kb_root=llm_kb_root,
            qdrant_url=qdrant_url,
            qdrant_collection=qdrant_collection,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
        )

    return {KNOWLEDGE_PROJECTION_REFRESH_EVENT: handle}
