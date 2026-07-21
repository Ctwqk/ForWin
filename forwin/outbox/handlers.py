from __future__ import annotations

from pathlib import Path
import threading
from typing import Any, Callable

from forwin.knowledge_system.canon_outbox import build_canon_outbox_handlers
from forwin.knowledge_system.projection_jobs import build_projection_outbox_handlers
from forwin.maintenance.events import POST_CANON_PHASE3_EVENT
from forwin.outbox.worker import OutboxClaim


def build_default_outbox_handlers(
    *,
    session_factory: Callable[[], Any],
    config: Any | None = None,
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
    memory_index: Any | None = None,
    memory_index_provider: Callable[[], Any] | None = None,
    canon_projection_runner: Callable[..., dict[str, Any]] | None = None,
    post_canon_service_provider: Callable[[], Any] | None = None,
) -> dict[str, Callable[[OutboxClaim], None]]:
    if memory_index is not None and memory_index_provider is not None:
        raise ValueError("Pass memory_index or memory_index_provider, not both")
    shared_memory_provider = _shared_provider(
        memory_index_provider
        or ((lambda: memory_index) if memory_index is not None else None),
        resource_name="memory index",
    )
    qdrant_url = getattr(config, "qdrant_url", None) if config is not None else None
    qdrant_collection = (
        getattr(config, "llm_kb_qdrant_collection", None) if config is not None else None
    )
    handlers: dict[str, Callable[[OutboxClaim], None]] = {}
    handlers.update(
        build_projection_outbox_handlers(
            session_factory=session_factory,
            obsidian_root=obsidian_root,
            llm_kb_root=llm_kb_root,
            qdrant_url=qdrant_url,
            qdrant_collection=qdrant_collection,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
            memory_index_provider=shared_memory_provider,
        )
    )
    canon_kwargs: dict[str, Any] = {}
    if canon_projection_runner is not None:
        canon_kwargs["projection_runner"] = canon_projection_runner
    handlers.update(
        build_canon_outbox_handlers(
            session_factory=session_factory,
            config=config,
            memory_index_provider=shared_memory_provider,
            obsidian_root=obsidian_root,
            llm_kb_root=llm_kb_root,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
            **canon_kwargs,
        )
    )
    if post_canon_service_provider is not None:
        resolve_post_canon_service = _shared_provider(
            post_canon_service_provider,
            resource_name="post-Canon maintenance service",
        )

        def handle_post_canon_phase3(event: OutboxClaim) -> None:
            service = resolve_post_canon_service()
            payload = event.payload
            project_id = str(payload.get("project_id") or event.aggregate_id or "").strip()
            chapter_number = int(payload.get("chapter_number") or 0)
            candidate_id = str(payload.get("candidate_id") or "").strip()
            commit_id = service.resolve_event_canon_commit(
                canon_commit_id=str(payload.get("canon_commit_id") or "").strip(),
                project_id=project_id,
                chapter_number=chapter_number,
                candidate_id=candidate_id,
            )
            service.run(
                canon_commit_id=commit_id,
                worker_id=(
                    f"outbox:{event.worker_id}:{event.row_id}:{event.lease_epoch}"
                ),
            )

        handlers[POST_CANON_PHASE3_EVENT] = handle_post_canon_phase3
    return handlers


def _shared_provider(
    provider: Callable[[], Any] | None,
    *,
    resource_name: str,
) -> Callable[[], Any] | None:
    if provider is None:
        return None
    missing = object()
    value: Any = missing
    lock = threading.Lock()

    def resolve() -> Any:
        nonlocal value
        if value is not missing:
            return value
        with lock:
            if value is not missing:
                return value
            resolved = provider()
            if resolved is None:
                raise RuntimeError(f"{resource_name} provider returned no resource")
            value = resolved
            return resolved

    return resolve
