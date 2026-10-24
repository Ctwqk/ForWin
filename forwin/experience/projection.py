"""Chapter-scoped experience data for writer consumers."""

from __future__ import annotations

import re

from forwin.protocol.experience import ArcPayoffMap


def writer_payoff_map(payoff_map: ArcPayoffMap | None, chapter_number: int) -> ArcPayoffMap | None:
    """Keep author data intact and expose a revelation only from its declared window's start.

    Empty or unparseable windows grant no permission to reveal. Numeric chapters and inclusive ranges are
    accepted; structured LLM windows are already normalized by RevelationLayer.
    """
    if payoff_map is None:
        return None
    projected = payoff_map.model_copy(deep=True)
    projected.revelation_layers = [
        layer for layer in projected.revelation_layers
        if (window := re.fullmatch(r"\s*([0-9]+)(?:\s*[-–—]\s*([0-9]+))?\s*", layer.chapter_window))
        and 1 <= int(window[1]) <= chapter_number
        and (window[2] is None or int(window[2]) >= int(window[1]))
    ]
    return projected
