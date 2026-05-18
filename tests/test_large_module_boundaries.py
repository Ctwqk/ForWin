from __future__ import annotations

from pathlib import Path

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]

FIRST_BATCH_LIMITS = {
    "forwin/orchestrator/loop.py": 900,
    "forwin/book_genesis.py": 250,
    "forwin/api.py": 700,
    "forwin/api_project_ops.py": 300,
    "forwin/planning/future_plan_auditor.py": 250,
}
NEW_MODULE_MAX_LINES = 1100


def test_giant_module_public_imports_remain_available() -> None:
    from forwin.book_genesis import BookGenesisService, GENESIS_STAGE_ORDER, StaleGenesisRevisionError
    from forwin.orchestrator.loop import RunResult, WritingOrchestrator
    from forwin.planning.future_plan_auditor import FuturePlanAuditor, FuturePlanAuditRun
    from forwin.canon_quality.countdown_ledger import analyze_countdowns, parse_countdown_minutes
    import forwin.api_project_ops as api_project_ops

    assert BookGenesisService is not None
    assert GENESIS_STAGE_ORDER
    assert StaleGenesisRevisionError is not None
    assert RunResult is not None
    assert WritingOrchestrator is not None
    assert FuturePlanAuditor is not None
    assert FuturePlanAuditRun is not None
    assert callable(analyze_countdowns)
    assert callable(parse_countdown_minutes)
    assert callable(api_project_ops.create_project)
    assert callable(api_project_ops.continue_project_generation)
    assert callable(api_project_ops.get_chapter_review)


@pytest.mark.skip(reason="enable after first-batch decomposition tasks complete")
def test_first_batch_giant_files_stay_small() -> None:
    for relative_path, max_lines in FIRST_BATCH_LIMITS.items():
        path = REPO_ROOT / relative_path
        assert path.exists(), f"missing {relative_path}"
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= max_lines, f"{relative_path} has {line_count} lines; expected <= {max_lines}"


def test_new_decomposition_modules_stay_context_sized() -> None:
    roots = [
        REPO_ROOT / "forwin" / "project_ops",
        REPO_ROOT / "forwin" / "genesis_pipeline",
        REPO_ROOT / "forwin" / "planning" / "future_plan_audit",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            assert line_count <= NEW_MODULE_MAX_LINES, f"{path.relative_to(REPO_ROOT)} has {line_count} lines"
