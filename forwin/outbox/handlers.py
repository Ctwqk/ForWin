from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from forwin.knowledge_system.projection_jobs import build_projection_outbox_handlers
from forwin.models.outbox import OutboxEvent


def build_default_outbox_handlers(
    *,
    session_factory: Callable[[], Any],
    config: Any | None = None,
    obsidian_root: Path | None = None,
    llm_kb_root: Path | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> dict[str, Callable[[OutboxEvent], None]]:
    qdrant_url = getattr(config, "qdrant_url", None) if config is not None else None
    qdrant_collection = (
        getattr(config, "llm_kb_qdrant_collection", None) if config is not None else None
    )
    handlers: dict[str, Callable[[OutboxEvent], None]] = {}
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
    return handlers
