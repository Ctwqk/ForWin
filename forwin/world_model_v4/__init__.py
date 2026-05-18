"""COMPATIBILITY world_v4 projection and migration bridge; final canon belongs to book_state."""

from __future__ import annotations

import warnings

from forwin.world_v4_compat import WorldModelCompiler, WorldModelProjection, WorldModelRepository

warnings.warn(
    "forwin.world_model_v4 is deprecated; import forwin.world_v4_compat instead. "
    "See Design-docs/DESIGN_STATUS.md.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["WorldModelCompiler", "WorldModelProjection", "WorldModelRepository"]
