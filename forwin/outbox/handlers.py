from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from forwin.knowledge_system.canon_outbox import build_canon_outbox_handlers
from forwin.knowledge_system.projection_jobs import build_projection_outbox_handlers
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
) -> dict[str, Callable[[OutboxClaim], None]]:
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
        )
    )
    canon_kwargs: dict[str, Any] = {}
    if canon_projection_runner is not None:
        canon_kwargs["projection_runner"] = canon_projection_runner
    handlers.update(
        build_canon_outbox_handlers(
            session_factory=session_factory,
            config=config,
            memory_index=memory_index,
            memory_index_provider=memory_index_provider,
            obsidian_root=obsidian_root,
            llm_kb_root=llm_kb_root,
            qdrant_client=qdrant_client,
            qdrant_models=qdrant_models,
            **canon_kwargs,
        )
    )
    return handlers
