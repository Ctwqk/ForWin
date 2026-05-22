from __future__ import annotations

from typing import Any


def __getattr__(name: str) -> Any:
    if name == "WritingOrchestrator":
        from .loop import WritingOrchestrator

        return WritingOrchestrator
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

__all__ = ["WritingOrchestrator"]
