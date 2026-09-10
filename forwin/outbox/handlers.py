from __future__ import annotations

import threading
from pathlib import Path
from typing import Any, Callable

from forwin.knowledge_system.projection_jobs import build_projection_outbox_handlers
from forwin.maintenance.events import build_post_canon_outbox_handlers
from forwin.maintenance.trace_upload import build_trace_upload_outbox_handlers
from forwin.novel_export.events import build_novel_export_handlers
from forwin.outbox.worker import OutboxClaim
from forwin.publisher_runtime.canon_jobs import (
    build_canon_publisher_outbox_handlers,
)


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
    post_canon_service_provider: Callable[[], Any] | None = None,
    publisher_job_service_provider: Callable[[], Any] | None = None,
    artifact_store_provider: Callable[[], Any] | None = None,
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
        getattr(config, "llm_kb_qdrant_collection", None)
        if config is not None
        else None
    )
    handler_maps = [
        build_novel_export_handlers(
            session_factory=session_factory,
            root=Path(getattr(config, "artifact_root", "data/artifacts")) / "novel_exports",
        ),
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
    ]
    if artifact_store_provider is not None:
        handler_maps.append(
            build_trace_upload_outbox_handlers(
                artifact_store_provider=artifact_store_provider,
            )
        )
    if post_canon_service_provider is not None:
        resolve_post_canon_service = _shared_provider(
            post_canon_service_provider,
            resource_name="post-Canon maintenance service",
        )
        assert resolve_post_canon_service is not None
        handler_maps.append(
            build_post_canon_outbox_handlers(
                service_provider=resolve_post_canon_service,
            )
        )
    if publisher_job_service_provider is not None:
        resolve_publisher_jobs = _shared_provider(
            publisher_job_service_provider,
            resource_name="Canon publisher job service",
        )
        assert resolve_publisher_jobs is not None
        handler_maps.append(
            build_canon_publisher_outbox_handlers(
                service_provider=resolve_publisher_jobs,
            )
        )
    return _merge_handler_maps(*handler_maps)


def _merge_handler_maps(
    *handler_maps: dict[str, Callable[[OutboxClaim], None]],
) -> dict[str, Callable[[OutboxClaim], None]]:
    merged: dict[str, Callable[[OutboxClaim], None]] = {}
    for handler_map in handler_maps:
        for event_type, handler in handler_map.items():
            if event_type in merged:
                raise ValueError(f"duplicate outbox handler owner: {event_type}")
            merged[event_type] = handler
    return merged


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
