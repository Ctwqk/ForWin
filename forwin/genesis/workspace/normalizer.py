from __future__ import annotations

from typing import Any


class GenesisNormalizer:
    """Normalizes current Genesis pack slices."""

    def __init__(self, owner: Any) -> None:
        self.owner = owner

    def normalize_world_root(self, *, project: Any, payload: dict[str, Any], fallback: dict[str, Any]) -> dict[str, Any]:
        return self.owner._normalize_world_root_payload(project=project, payload=payload, fallback=fallback)

    def normalize_book_blueprint(
        self,
        *,
        project: Any,
        payload: dict[str, Any],
        fallback: dict[str, Any],
    ) -> dict[str, Any]:
        return self.owner._normalize_blueprint_payload(project=project, payload=payload, fallback=fallback)
