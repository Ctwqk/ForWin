from __future__ import annotations

from collections.abc import Iterable


def is_codex_model(model: str, codex_models: Iterable[str]) -> bool:
    normalized = model.strip().casefold()
    return any(normalized == declared.strip().casefold() for declared in codex_models)
