"""CANON BookState runtime: GraphDelta ledger, review gate, compiler, snapshots, and projections."""

from __future__ import annotations

from .adapter import BookStateDeltaAdapter
from .compiler import BookStateCompiler
from .cognition import CognitionView
from .map_graph import MapGraph
from .narrative import NarrativeControlGraph
from .projection import BookStateProjection
from .query import BookStateQuery
from .repository import BookStateRepository
from .reviewer import BookStateReviewGate, BookStateReviewIssue, BookStateReviewVerdict
from .runtime import BookStateRuntime, ObjectiveWorldGraph, distance_between_world_nodes
from .writer_contract import WriterContractDeltaBuilder

__all__ = [
    "BookStateRuntime",
    "BookStateCompiler",
    "BookStateDeltaAdapter",
    "BookStateProjection",
    "BookStateQuery",
    "BookStateRepository",
    "BookStateReviewGate",
    "BookStateReviewIssue",
    "BookStateReviewVerdict",
    "CognitionView",
    "MapGraph",
    "NarrativeControlGraph",
    "ObjectiveWorldGraph",
    "WriterContractDeltaBuilder",
    "distance_between_world_nodes",
]
