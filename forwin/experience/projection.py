"""Chapter-scoped experience data for writer consumers."""

from __future__ import annotations

from forwin.protocol.experience import ArcPayoffMap


def writer_payoff_map(payoff_map: ArcPayoffMap | None, chapter_number: int) -> ArcPayoffMap | None:
    """Keep author data intact and omit revelation layers from every writer projection.

    A layer's window grants no reveal permission. Chapter reveals reach the writer only
    through reveal directives in the world view; chapter_number is retained for callers.
    """
    if payoff_map is None:
        return None
    projected = payoff_map.model_copy(deep=True)
    projected.revelation_layers = []
    return projected
