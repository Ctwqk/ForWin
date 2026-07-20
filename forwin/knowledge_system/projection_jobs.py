from __future__ import annotations

from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from sqlalchemy.orm import Session, sessionmaker

from forwin.knowledge_system.canon_projection import (
    CanonProjectionService,
    ProjectionRefreshError,
)
from forwin.knowledge_system.checkpoints import (
    PROJECTION_COMPONENTS,
    ProjectionEventIdentity,
)
from forwin.models.outbox import OutboxEvent
from forwin.outbox.store import enqueue_outbox_event
from forwin.outbox.worker import OutboxClaim


KNOWLEDGE_PROJECTION_REFRESH_EVENT = "knowledge.projection.refresh_requested"
CANON_PROJECTION_REQUESTED_EVENT = "canon.projection.requested"
CANON_PROJECTION_EVENT_SCHEMA_VERSION = 1
VALID_PROJECTION_KINDS = frozenset(
    {"all", "world_studio", *PROJECTION_COMPONENTS}
)
_PROJECTION_COMPONENTS_BY_KIND = {
    "all": PROJECTION_COMPONENTS,
    "world_studio": ("obsidian", "llm_kb"),
    "obsidian": ("obsidian",),
    "llm_kb": ("llm_kb",),
    "chapter_memory": ("chapter_memory",),
}


def normalize_projection_kind(projection_kind: str = "all") -> str:
    kind = str(projection_kind).strip().lower()
    if kind not in VALID_PROJECTION_KINDS:
        raise ValueError(
            "projection_kind must be all, world_studio, obsidian, llm_kb, "
            "or chapter_memory"
        )
    return kind


def projection_components_for_kind(projection_kind: str = "all") -> tuple[str, ...]:
    return _PROJECTION_COMPONENTS_BY_KIND[
        normalize_projection_kind(projection_kind)
    ]


def refresh_projection_now(
    session: Session | None = None,
    *,
    session_factory: Any | None = None,
    project_id: str,
    projection_kind: str = "all",
    as_of_chapter: int = 0,
    trigger: str = "projection_api_refresh",
    event_id: str = "",
    event_identity: ProjectionEventIdentity | None = None,
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
    memory_index_provider: Callable[[], Any] | None = None,
    component_runners: Mapping[str, Callable[[Any], Any]] | None = None,
) -> dict[str, Any]:
    del as_of_chapter
    kind = normalize_projection_kind(projection_kind)
    factory = _resolve_session_factory(session, session_factory)
    service = CanonProjectionService(
        factory,
        obsidian_root=obsidian_root,
        llm_kb_root=llm_kb_root,
        qdrant_url=qdrant_url,
        qdrant_collection=qdrant_collection,
        qdrant_client=qdrant_client,
        qdrant_models=qdrant_models,
        memory_index_provider=memory_index_provider,
        component_runners=component_runners,
    )
    payload = service.refresh(
        project_id,
        components=projection_components_for_kind(kind),
        trigger=trigger,
        event_id=event_id,
        event_identity=event_identity,
    )
    payload["projection_kind"] = kind
    payload["as_of_chapter"] = payload["target_chapter_number"]
    payload["trigger"] = trigger
    for component, result in payload["components"].items():
        payload[component] = result
    return payload


def enqueue_projection_refresh(
    session: Session,
    *,
    project_id: str,
    projection_kind: str = "all",
    as_of_chapter: int = 0,
    trigger: str = "projection_api_refresh",
) -> OutboxEvent:
    kind = normalize_projection_kind(projection_kind)
    return enqueue_outbox_event(
        session,
        aggregate_type="project",
        aggregate_id=project_id,
        event_type=KNOWLEDGE_PROJECTION_REFRESH_EVENT,
        payload={
            "project_id": project_id,
            "projection_kind": kind,
            "requested_as_of_chapter": int(as_of_chapter or 0),
            "trigger": trigger,
        },
    )


def handle_projection_refresh_outbox_event(
    event: OutboxClaim,
    *,
    session_factory: Any,
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
    memory_index_provider: Callable[[], Any] | None = None,
) -> dict[str, Any]:
    payload = event.payload
    project_id = str(payload.get("project_id") or event.aggregate_id or "").strip()
    if not project_id:
        raise ValueError("projection outbox event requires project_id")
    if event.aggregate_id and event.aggregate_id != project_id:
        raise ValueError("projection outbox aggregate identity mismatch")

    event_identity = None
    projection_kind = str(payload.get("projection_kind") or "all")
    if event.event_type == CANON_PROJECTION_REQUESTED_EVENT:
        event_identity = _canon_projection_event_identity(payload)
        projection_kind = "all"

    return refresh_projection_now(
        session_factory=session_factory,
        project_id=project_id,
        projection_kind=projection_kind,
        trigger=str(payload.get("trigger") or "projection_outbox_worker"),
        event_id=event.event_id,
        event_identity=event_identity,
        obsidian_root=obsidian_root,
        llm_kb_root=llm_kb_root,
        qdrant_url=qdrant_url,
        qdrant_collection=qdrant_collection,
        qdrant_client=qdrant_client,
        qdrant_models=qdrant_models,
        memory_index_provider=memory_index_provider,
    )


def build_projection_outbox_handlers(
    *,
    session_factory: Any,
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
    memory_index_provider: Callable[[], Any] | None = None,
) -> dict[str, Callable[[OutboxClaim], None]]:
    def handle(event: OutboxClaim) -> None:
        handle_projection_refresh_outbox_event(
            event,
            session_factory=session_factory,
            obsidian_root=obsidian_root,
            llm_kb_root=llm_kb_root,
            qdrant_url=qdrant_url,
            qdrant_collection=qdrant_collection,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
            memory_index_provider=memory_index_provider,
        )

    return {
        KNOWLEDGE_PROJECTION_REFRESH_EVENT: handle,
        CANON_PROJECTION_REQUESTED_EVENT: handle,
    }


def _canon_projection_event_identity(
    payload: Mapping[str, object],
) -> ProjectionEventIdentity:
    try:
        schema_version = int(payload.get("schema_version") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("canon projection event schema_version is invalid") from exc
    if schema_version != CANON_PROJECTION_EVENT_SCHEMA_VERSION:
        raise ValueError("unsupported canon projection event schema_version")

    required = {
        "canon_commit_id": str(payload.get("canon_commit_id") or "").strip(),
        "canon_idempotency_key": str(
            payload.get("canon_idempotency_key") or ""
        ).strip(),
        "project_id": str(payload.get("project_id") or "").strip(),
        "candidate_id": str(payload.get("candidate_id") or "").strip(),
    }
    missing = [key for key, value in required.items() if not value]
    try:
        chapter_number = int(payload.get("chapter_number") or 0)
    except (TypeError, ValueError) as exc:
        raise ValueError("canon projection event chapter_number is invalid") from exc
    if chapter_number <= 0:
        missing.append("chapter_number")
    if missing:
        raise ValueError(
            "canon projection event missing required fields: "
            + ", ".join(sorted(set(missing)))
        )
    return ProjectionEventIdentity(
        canon_commit_id=required["canon_commit_id"],
        canon_idempotency_key=required["canon_idempotency_key"],
        project_id=required["project_id"],
        chapter_number=chapter_number,
        candidate_id=required["candidate_id"],
    )


def _resolve_session_factory(
    session: Session | None,
    session_factory: Any | None,
) -> Any:
    if session_factory is not None:
        return session_factory
    if session is None:
        raise TypeError("session_factory or session is required")
    return sessionmaker(bind=session.get_bind(), expire_on_commit=False)


__all__ = [
    "CANON_PROJECTION_EVENT_SCHEMA_VERSION",
    "CANON_PROJECTION_REQUESTED_EVENT",
    "KNOWLEDGE_PROJECTION_REFRESH_EVENT",
    "ProjectionRefreshError",
    "VALID_PROJECTION_KINDS",
    "build_projection_outbox_handlers",
    "enqueue_projection_refresh",
    "handle_projection_refresh_outbox_event",
    "normalize_projection_kind",
    "projection_components_for_kind",
    "refresh_projection_now",
]
