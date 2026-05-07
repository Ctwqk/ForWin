from __future__ import annotations

from pathlib import Path

from forwin.reviewer_v4 import V4ReviewGate as LegacyV4ReviewGate
from forwin.world_model_v4.compiler import WorldModelCompiler as LegacyWorldModelCompiler
from forwin.world_v4_compat.compiler import WorldModelCompiler
from forwin.world_v4_review_gate import V4ReviewGate


ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_new_world_v4_import_paths_alias_legacy_compatibility_modules() -> None:
    assert V4ReviewGate is LegacyV4ReviewGate
    assert WorldModelCompiler is LegacyWorldModelCompiler


def test_production_orchestrator_imports_new_world_v4_compat_names() -> None:
    source = _read("forwin/orchestrator/loop.py")

    assert "from forwin.extractor.book_state_graph_delta import BookStateGraphDeltaExtractor" in source
    assert "from forwin.world_v4_compat.compiler import WorldModelCompiler as WorldModelCompilerV4" in source
    assert "from forwin.reviewer_v4 import V4ReviewGate" not in source
    assert "from forwin.world_model_v4.compiler import WorldModelCompiler as WorldModelCompilerV4" not in source
