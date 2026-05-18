"""COMPATIBILITY world_v4 extraction review gate; not the main chapter reviewer."""

from __future__ import annotations

import warnings

from forwin.world_v4_review_gate import V4ReviewGate, V4ReviewGateVerdict, V4ReviewIssue

warnings.warn(
    "forwin.reviewer_v4 is deprecated; import forwin.world_v4_review_gate instead. "
    "See Design-docs/DESIGN_STATUS.md.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = ["V4ReviewGate", "V4ReviewGateVerdict", "V4ReviewIssue"]
