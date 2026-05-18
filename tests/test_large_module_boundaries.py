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
SECOND_BATCH_SHELL_LIMITS = {
    "forwin/canon_quality/countdown_ledger.py": 300,
    "forwin/api_project_payloads.py": 250,
    "forwin/api_schemas.py": 250,
    "forwin/writer/prompts.py": 250,
    "forwin/writer/llm_client.py": 250,
    "forwin/context/assembler.py": 250,
    "forwin/retrieval/broker.py": 250,
}
NEW_MODULE_MAX_LINES = 1100


def test_giant_module_public_imports_remain_available() -> None:
    from forwin.api_project_payloads import build_project_detail, build_project_summaries
    from forwin.api_schemas import GenerateRequest, ProjectDetail, ProjectSummary
    from forwin.book_genesis import BookGenesisService, GENESIS_STAGE_ORDER, StaleGenesisRevisionError
    from forwin.context.assembler import ChapterContextAssembler, assemble_context
    from forwin.orchestrator.loop import RunResult, WritingOrchestrator
    from forwin.planning.future_plan_auditor import FuturePlanAuditor, FuturePlanAuditRun
    from forwin.retrieval.broker import RetrievalBroker
    from forwin.canon_quality.countdown_ledger import analyze_countdowns, parse_countdown_minutes
    from forwin.writer.llm_client import LLMClient
    from forwin.writer.prompts import build_single_chapter_draft_prompt
    import forwin.api_project_ops as api_project_ops

    assert build_project_detail is not None
    assert build_project_summaries is not None
    assert GenerateRequest is not None
    assert ProjectDetail is not None
    assert ProjectSummary is not None
    assert BookGenesisService is not None
    assert GENESIS_STAGE_ORDER
    assert StaleGenesisRevisionError is not None
    assert ChapterContextAssembler is not None
    assert callable(assemble_context)
    assert RunResult is not None
    assert WritingOrchestrator is not None
    assert FuturePlanAuditor is not None
    assert FuturePlanAuditRun is not None
    assert RetrievalBroker is not None
    assert callable(analyze_countdowns)
    assert callable(parse_countdown_minutes)
    assert LLMClient is not None
    assert callable(build_single_chapter_draft_prompt)
    assert callable(api_project_ops.create_project)
    assert callable(api_project_ops.continue_project_generation)
    assert callable(api_project_ops.get_chapter_review)


def test_first_batch_giant_files_stay_small() -> None:
    for relative_path, max_lines in FIRST_BATCH_LIMITS.items():
        path = REPO_ROOT / relative_path
        assert path.exists(), f"missing {relative_path}"
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= max_lines, f"{relative_path} has {line_count} lines; expected <= {max_lines}"


def test_second_batch_compatibility_shells_stay_small() -> None:
    for relative_path, max_lines in SECOND_BATCH_SHELL_LIMITS.items():
        path = REPO_ROOT / relative_path
        assert path.exists(), f"missing {relative_path}"
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= max_lines, f"{relative_path} has {line_count} lines; expected <= {max_lines}"


def test_new_decomposition_modules_stay_context_sized() -> None:
    roots = [
        REPO_ROOT / "forwin" / "book_genesis_core",
        REPO_ROOT / "forwin" / "orchestrator_loop_core",
        REPO_ROOT / "forwin" / "project_ops",
        REPO_ROOT / "forwin" / "genesis_pipeline",
        REPO_ROOT / "forwin" / "planning" / "future_plan_audit",
        REPO_ROOT / "forwin" / "canon_quality" / "countdown",
        REPO_ROOT / "forwin" / "api_schema",
        REPO_ROOT / "forwin" / "project_payloads",
        REPO_ROOT / "forwin" / "writer" / "prompt_core",
        REPO_ROOT / "forwin" / "writer" / "llm",
        REPO_ROOT / "forwin" / "context" / "assembler_core",
        REPO_ROOT / "forwin" / "retrieval" / "broker_core",
    ]
    for root in roots:
        if not root.exists():
            continue
        for path in root.rglob("*.py"):
            line_count = len(path.read_text(encoding="utf-8").splitlines())
            assert line_count <= NEW_MODULE_MAX_LINES, f"{path.relative_to(REPO_ROOT)} has {line_count} lines"
