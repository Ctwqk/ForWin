from __future__ import annotations

from pathlib import Path



REPO_ROOT = Path(__file__).resolve().parents[1]

FIRST_BATCH_LIMITS = {
    "forwin/generation/pipeline.py": 300,
    "forwin/genesis/service.py": 300,
    "forwin/api.py": 20,
    "forwin/application/projects/service.py": 500,
}
REMOVED_COMPATIBILITY_SHELLS = (
    "forwin/api_project_ops.py",
    "forwin/api_project_payloads.py",
    "forwin/api_schemas.py",
    "forwin/book_genesis.py",
    "forwin/writer/prompts.py",
    "forwin/writer/llm_client.py",
    "forwin/context/assembler.py",
    "forwin/retrieval/broker.py",
)
NEW_MODULE_MAX_LINES = 1100


def test_giant_module_public_imports_remain_available() -> None:
    from forwin.project_payloads import build_project_detail, build_project_summaries
    from forwin.api_schema import ProjectDetail, ProjectSummary
    from forwin.genesis import BookGenesisService, GENESIS_STAGE_ORDER, StaleGenesisRevisionError
    from forwin.context.assembler_core import ChapterContextAssembler, assemble_context
    from forwin.generation.pipeline import ChapterPipeline
    from forwin.generation.pipeline_core.result import RunResult
    from forwin.planning.future_plan_audit import FuturePlanAuditor, FuturePlanAuditRun
    from forwin.retrieval.broker_core import RetrievalBroker
    from forwin.canon_quality.chapter_review_form.service import review_chapter_with_form
    from forwin.writer.llm import LLMClient
    from forwin.writer.prompt_core import build_single_chapter_draft_prompt
    from forwin.application.projects import ProjectApplicationService
    from forwin.application.publisher import PublisherApplicationService

    assert build_project_detail is not None
    assert build_project_summaries is not None
    assert ProjectDetail is not None
    assert ProjectSummary is not None
    assert BookGenesisService is not None
    assert GENESIS_STAGE_ORDER
    assert StaleGenesisRevisionError is not None
    assert ChapterContextAssembler is not None
    assert callable(assemble_context)
    assert RunResult is not None
    assert ChapterPipeline is not None
    assert FuturePlanAuditor is not None
    assert FuturePlanAuditRun is not None
    assert RetrievalBroker is not None
    assert callable(review_chapter_with_form)
    assert LLMClient is not None
    assert callable(build_single_chapter_draft_prompt)
    assert callable(ProjectApplicationService.create_project)
    assert callable(ProjectApplicationService.continue_project_generation)
    assert callable(ProjectApplicationService.get_chapter_review)
    assert callable(PublisherApplicationService.create_publisher_upload_job)


def test_first_batch_giant_files_stay_small() -> None:
    for relative_path, max_lines in FIRST_BATCH_LIMITS.items():
        path = REPO_ROOT / relative_path
        assert path.exists(), f"missing {relative_path}"
        line_count = len(path.read_text(encoding="utf-8").splitlines())
        assert line_count <= max_lines, f"{relative_path} has {line_count} lines; expected <= {max_lines}"


def test_obsolete_compatibility_shells_stay_removed() -> None:
    for relative_path in REMOVED_COMPATIBILITY_SHELLS:
        assert not (REPO_ROOT / relative_path).exists(), relative_path


def test_new_decomposition_modules_stay_context_sized() -> None:
    roots = [
        REPO_ROOT / "forwin" / "genesis",
        REPO_ROOT / "forwin" / "generation" / "pipeline_core",
        REPO_ROOT / "forwin" / "application" / "projects",
        REPO_ROOT / "forwin" / "application" / "publisher",
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
