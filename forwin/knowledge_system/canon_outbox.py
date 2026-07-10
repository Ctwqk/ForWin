from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from sqlalchemy import select

from forwin.governance import DecisionEventType
from forwin.knowledge_system.projection_jobs import refresh_projection_now
from forwin.models.base import new_id
from forwin.models.canon import CanonCommitRecord
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.governance import DecisionEvent
from forwin.models.outbox import OutboxEvent
from forwin.models.project import ChapterPlan, Project
from forwin.retrieval import create_memory_index


CANON_POST_COMMIT_EVENT = "canon.post_commit.requested"
CANON_PUBLISHER_EVENT = "canon.publisher.requested"
PUBLISHER_CANON_AVAILABLE_EVENT = "publisher_canon_available"


def handle_canon_post_commit_outbox_event(
    event: OutboxEvent,
    *,
    session_factory: Callable[[], Any],
    memory_index: Any,
    projection_runner: Callable[..., dict[str, Any]] = refresh_projection_now,
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_url: str | None = None,
    qdrant_collection: str | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Any]:
    payload = _event_payload(event)
    project_id = str(payload.get("project_id") or event.aggregate_id or "").strip()
    chapter_number = int(payload.get("chapter_number") or 0)
    candidate_id = str(payload.get("candidate_id") or "").strip()
    if not project_id or chapter_number <= 0 or not candidate_id:
        raise ValueError("Canon post-commit event payload is incomplete")
    if memory_index is None:
        raise RuntimeError("Canon post-commit handler requires a memory index")

    try:
        with session_factory.begin() as session:
            candidate, chapter, draft = _accepted_chapter_rows(
                session,
                project_id=project_id,
                chapter_number=chapter_number,
                candidate_id=candidate_id,
            )
            projection_result = projection_runner(
                session=session,
                project_id=project_id,
                projection_kind="all",
                as_of_chapter=chapter_number,
                trigger="canon_post_commit",
                obsidian_root=obsidian_root,
                llm_kb_root=llm_kb_root,
                qdrant_url=qdrant_url,
                qdrant_collection=qdrant_collection,
                qdrant_client=qdrant_client,
                qdrant_models=qdrant_models,
            )
            memory_index.upsert_chapter(
                project_id=project_id,
                chapter_number=chapter_number,
                title=str(chapter.title or ""),
                summary=str(draft.summary or ""),
                body=str(draft.body_text or ""),
            )
            return {
                "ok": True,
                "project_id": project_id,
                "chapter_number": chapter_number,
                "candidate_id": candidate.id,
                "projection": projection_result,
            }
    except Exception as exc:
        _record_projection_degradation(
            session_factory=session_factory,
            event=event,
            project_id=project_id,
            chapter_number=chapter_number,
            exc=exc,
        )
        raise


def handle_canon_publisher_outbox_event(
    event: OutboxEvent,
    *,
    session_factory: Callable[[], Any],
) -> None:
    payload = _event_payload(event)
    project_id = str(payload.get("project_id") or event.aggregate_id or "").strip()
    chapter_number = int(payload.get("chapter_number") or 0)
    candidate_id = str(payload.get("candidate_id") or "").strip()
    with session_factory.begin() as session:
        candidate, _chapter, _draft = _accepted_chapter_rows(
            session,
            project_id=project_id,
            chapter_number=chapter_number,
            candidate_id=candidate_id,
        )
        existing = session.execute(
            select(DecisionEvent).where(
                DecisionEvent.project_id == project_id,
                DecisionEvent.event_type == PUBLISHER_CANON_AVAILABLE_EVENT,
                DecisionEvent.related_object_type == "candidate_draft",
                DecisionEvent.related_object_id == candidate.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        session.add(
            DecisionEvent(
                id=new_id(),
                project_id=project_id,
                chapter_number=chapter_number,
                scope="chapter",
                event_family="business_event",
                event_type=PUBLISHER_CANON_AVAILABLE_EVENT,
                actor_type="system",
                summary=f"Chapter {chapter_number} is available to Publisher.",
                payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
                related_object_type="candidate_draft",
                related_object_id=candidate.id,
            )
        )


def build_canon_outbox_handlers(
    *,
    session_factory: Callable[[], Any],
    config: Any | None = None,
    memory_index: Any | None = None,
    projection_runner: Callable[..., dict[str, Any]] = refresh_projection_now,
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Callable[[OutboxEvent], None]]:
    resolved_memory_index = memory_index or _memory_index_from_config(config)
    qdrant_url = getattr(config, "qdrant_url", None) if config is not None else None
    qdrant_collection = (
        getattr(config, "llm_kb_qdrant_collection", None)
        if config is not None
        else None
    )

    def handle_post_commit(event: OutboxEvent) -> None:
        handle_canon_post_commit_outbox_event(
            event,
            session_factory=session_factory,
            memory_index=resolved_memory_index,
            projection_runner=projection_runner,
            obsidian_root=obsidian_root,
            llm_kb_root=llm_kb_root,
            qdrant_url=qdrant_url,
            qdrant_collection=qdrant_collection,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
        )

    def handle_publisher(event: OutboxEvent) -> None:
        handle_canon_publisher_outbox_event(
            event,
            session_factory=session_factory,
        )

    return {
        CANON_POST_COMMIT_EVENT: handle_post_commit,
        CANON_PUBLISHER_EVENT: handle_publisher,
    }


def _accepted_chapter_rows(
    session: Any,
    *,
    project_id: str,
    chapter_number: int,
    candidate_id: str,
) -> tuple[CandidateDraftRecord, ChapterPlan, ChapterDraft]:
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError("project not found")
    candidate = session.get(CandidateDraftRecord, candidate_id)
    if (
        candidate is None
        or candidate.project_id != project_id
        or int(candidate.chapter_number or 0) != chapter_number
        or candidate.status != "accepted"
    ):
        raise ValueError("candidate is not accepted Canon")
    commit = session.get(CanonCommitRecord, candidate.canon_commit_id)
    if (
        commit is None
        or commit.candidate_id != candidate.id
        or commit.status != "committed"
    ):
        raise ValueError("candidate has no committed Canon record")
    chapter = session.get(ChapterPlan, candidate.chapter_plan_id)
    if chapter is None or chapter.status != "accepted":
        raise ValueError("chapter is not accepted Canon")
    draft = session.get(ChapterDraft, candidate.candidate_draft_id)
    if draft is None:
        raise ValueError("accepted candidate draft not found")
    return candidate, chapter, draft


def _record_projection_degradation(
    *,
    session_factory: Callable[[], Any],
    event: OutboxEvent,
    project_id: str,
    chapter_number: int,
    exc: Exception,
) -> None:
    if not project_id:
        return
    with session_factory.begin() as session:
        if session.get(Project, project_id) is None:
            return
        existing = session.execute(
            select(DecisionEvent).where(
                DecisionEvent.project_id == project_id,
                DecisionEvent.event_type
                == DecisionEventType.DEFERRED_MAINTENANCE_RECORDED,
                DecisionEvent.related_object_type == "outbox_event",
                DecisionEvent.related_object_id == event.id,
            )
        ).scalar_one_or_none()
        if existing is not None:
            return
        session.add(
            DecisionEvent(
                id=new_id(),
                project_id=project_id,
                chapter_number=chapter_number,
                scope="chapter",
                event_family="runtime_observation",
                event_type=DecisionEventType.DEFERRED_MAINTENANCE_RECORDED,
                actor_type="system",
                summary="Deferred maintenance recorded: canon_post_commit",
                reason=str(exc),
                payload_json=json.dumps(
                    {
                        "task_type": "canon_post_commit",
                        "error_class": exc.__class__.__name__,
                        "error_summary": str(exc),
                    },
                    ensure_ascii=False,
                    sort_keys=True,
                ),
                related_object_type="outbox_event",
                related_object_id=event.id,
            )
        )


def _memory_index_from_config(config: Any | None) -> Any | None:
    if config is None:
        return None
    return create_memory_index(
        backend=config.retrieval_backend,
        root_dir=config.retrieval_root,
        qdrant_url=config.qdrant_url,
        qdrant_collection=config.qdrant_collection,
        embedding_backend=config.embedding_backend,
        embedding_base_url=config.embedding_base_url,
        embedding_api_key=config.embedding_api_key,
        embedding_model=config.embedding_model,
        embedding_dims=config.embedding_dims,
        embedding_required=config.embedding_required,
    )


def _event_payload(event: OutboxEvent) -> dict[str, Any]:
    try:
        payload = json.loads(event.payload_json or "{}")
    except (TypeError, json.JSONDecodeError):
        return {}
    return payload if isinstance(payload, dict) else {}


__all__ = [
    "CANON_POST_COMMIT_EVENT",
    "CANON_PUBLISHER_EVENT",
    "build_canon_outbox_handlers",
    "handle_canon_post_commit_outbox_event",
    "handle_canon_publisher_outbox_event",
]
