"""Compatibility exports for audience comment analysis.

Phase 4 is now the authoritative implementation location. This module stays as a
shim so existing imports do not break.
"""
from __future__ import annotations

from forwin.orchestrator.phase4 import (
    CommentAnalyzer,
    SignalDraft,
    aggregate_and_level_signals,
    build_reader_feedback_snapshot,
    classify_signal_level,
    load_recent_signals,
)

__all__ = [
    "CommentAnalyzer",
    "SignalDraft",
    "aggregate_and_level_signals",
    "build_reader_feedback_snapshot",
    "classify_signal_level",
    "load_recent_signals",
]
