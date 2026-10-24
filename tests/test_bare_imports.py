from pathlib import Path
import subprocess
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("module", [
    "forwin.planning.beat_sheet",
    "forwin.planning.beat_sheet.types",
    "forwin.planning.beat_sheet.repository",
    "forwin.planning.beat_sheet.service",
    "forwin.planning.world_digest",
    "forwin.world_view.types",
    "forwin.world_view.builder",
    "forwin.experience",
    "forwin.planning.band_planner",
    "forwin.planning.band_planner.service",
    "forwin.book_state.reveal_schedule",
    "forwin.canon.admission",
    "forwin.world_engine.tick",
    "forwin.llm_kb.compiler",
    "forwin.llm_kb.vector_index",
    "forwin.llm_kb.retriever",
    "forwin.knowledge_system.canon_projection",
    "forwin.knowledge_system.projection_jobs",
    "forwin.outbox.handlers",
    "forwin.retrieval",
    "forwin.retrieval.broker_core.broker",
    "forwin.map.models",
    "forwin.map",
    "forwin.models",
    "forwin.models.base",
    "forwin.world_studio.search_service",
])
def test_module_imports_in_a_fresh_interpreter(module: str) -> None:
    result = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(ROOT)!r}); import {module}",
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
