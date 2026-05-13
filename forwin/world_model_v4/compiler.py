from __future__ import annotations

"""Legacy world_model_v4 compatibility projection rows.

This module intentionally remains as a public legacy import path.
Canonical implementation has moved to `forwin.world_v4_compat.compiler`.
The legacy path must stay labeled as compatibility projection rows while
runtime canon is owned by BookState.
"""

from forwin.world_v4_compat.compiler import WorldModelCompiler

__all__ = ["WorldModelCompiler"]
