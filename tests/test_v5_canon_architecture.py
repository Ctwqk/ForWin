from __future__ import annotations

from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _source(relative: str) -> str:
    return (ROOT / relative).read_text(encoding="utf-8")


def test_book_state_compiler_has_one_production_caller() -> None:
    callers = []
    for path in sorted((ROOT / "forwin").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "forwin/book_state/compiler.py":
            continue
        if "BookStateCompiler(" in path.read_text(encoding="utf-8"):
            callers.append(relative)

    assert callers == ["forwin/canon/admission.py"]


def test_canon_path_has_no_synchronous_projection_or_external_side_effects() -> None:
    canon_source = _source("forwin/canon/admission.py")
    preparation_source = _source("forwin/canon/preparation.py")
    generation_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin/generation").rglob("*.py"))
    )

    for forbidden in (
        "KnowledgeProjectionRefresher",
        "memory_index.upsert_chapter",
        "PublisherRuntimeService",
        "ObsidianExporter",
        "LLMKnowledgeBaseCompiler",
    ):
        assert forbidden not in canon_source
        assert forbidden not in preparation_source
        assert forbidden not in generation_source

    assert "enqueue_outbox_event(" in canon_source
    assert "canon.post_commit.requested" in preparation_source
    assert "canon.publisher.requested" in preparation_source


def test_old_canon_and_book_state_write_entrypoints_stay_deleted() -> None:
    assert not (ROOT / "forwin/book_state/review_gate_ext.py").exists()
    assert not (ROOT / "forwin/book_state/ports.py").exists()
    assert "BookStateDirectCommitService" not in _source(
        "forwin/book_state/__init__.py"
    )
    assert "def commit(" not in _source("forwin/canon/admission.py")
    assert "def mark_canon_committed(" not in _source("forwin/candidate_drafts.py")
    assert "def mark_canon_failed(" not in _source("forwin/candidate_drafts.py")

    production_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin").rglob("*.py"))
    )
    assert ".canon_admission.commit(" not in production_source
    assert "_commit_book_state_canon" not in production_source


def test_only_canon_admission_assigns_accepted_chapter_status() -> None:
    offenders = []
    for path in sorted((ROOT / "forwin").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative == "forwin/canon/admission.py":
            continue
        source = path.read_text(encoding="utf-8")
        if '.status = "accepted"' in source and "KnowledgeEditProposalRow" not in source:
            offenders.append(relative)

    assert offenders == []


def test_v5_baseline_contains_candidate_and_canon_commit_contracts() -> None:
    baseline = _source("forwin/migrations/versions/0001_v5_baseline.py")
    assert '"canon_commit_records"' in baseline
    for column in (
        "parent_candidate_id",
        "body_hash",
        "plan_revision",
        "writer_artifact_ref",
        "review_result_json",
        "repair_history_json",
        "entity_admission_plan_json",
        "eligibility_decision_json",
        "policy_version",
        "canon_commit_plan_json",
        "canon_commit_id",
        "idempotency_key",
    ):
        assert f'sa.Column("{column}"' in baseline
    for index in (
        "ux_candidate_drafts_project_chapter_version",
        "ux_candidate_drafts_draft",
        "ux_canon_commits_idempotency_key",
        "ux_canon_commits_candidate",
        "ux_canon_commits_project_chapter",
    ):
        assert f'"{index}"' in baseline


def test_post_commit_handlers_are_registered_and_retryable() -> None:
    handlers = _source("forwin/outbox/handlers.py")
    canon_outbox = _source("forwin/knowledge_system/canon_outbox.py")
    worker = _source("forwin/outbox/worker.py")

    assert "build_canon_outbox_handlers" in handlers
    assert 'CANON_POST_COMMIT_EVENT = "canon.post_commit.requested"' in canon_outbox
    assert 'CANON_PUBLISHER_EVENT = "canon.publisher.requested"' in canon_outbox
    assert "mark_outbox_event_failed(" in worker
    assert "mark_outbox_event_processed(" in worker


def test_npc_intent_projection_stays_deleted_from_v5_runtime() -> None:
    offenders = []
    for path in sorted((ROOT / "forwin").rglob("*.py")):
        source = path.read_text(encoding="utf-8").lower()
        if "npc_intent" in source:
            offenders.append(path.relative_to(ROOT).as_posix())

    assert offenders == []
