from __future__ import annotations

from typing import Any


class GenesisNormalizer:
    """Normalizes Genesis pack slices while preserving legacy payload compatibility."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def normalize_world_root(self, *, project: Any, payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        return self.owner._normalize_world_root_payload(project=project, payload=payload, fallback=fallback)

    def normalize_map(
        self,
        *,
        payload: dict[str, Any],
        fallback: dict[str, Any],
        world_bible: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.owner._normalize_map_payload(payload=payload, fallback=fallback, world_bible=world_bible)

    def normalize_story_engine(
        self,
        *,
        payload: dict[str, Any],
        fallback: dict[str, Any],
        world_bible: dict[str, Any] | None = None,
        map_atlas: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        return self.owner._normalize_story_engine_payload(
            payload=payload,
            fallback=fallback,
            world_bible=world_bible,
            map_atlas=map_atlas,
        )

    def normalize_book_blueprint(
        self,
        *,
        project: Any,
        payload: dict[str, Any],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        return self.owner._normalize_blueprint_payload(project=project, payload=payload, fallback=fallback)

