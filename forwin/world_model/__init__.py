"""LEGACY world model projection/export layer; not a new canon source."""

from __future__ import annotations

import warnings

from .compiler import WorldModelCompiler
from .exporter_obsidian import ObsidianWorldExporter
from .importer_obsidian import ObsidianWorldImporter
from .retriever import WorldModelRetriever
from .store import WorldModelStore

warnings.warn(
    "forwin.world_model is deprecated as a business dependency; use BookState as canon. "
    "See Design-docs/DESIGN_STATUS.md.",
    DeprecationWarning,
    stacklevel=2,
)

__all__ = [
    "WorldModelCompiler",
    "WorldModelStore",
    "WorldModelRetriever",
    "ObsidianWorldExporter",
    "ObsidianWorldImporter",
]
