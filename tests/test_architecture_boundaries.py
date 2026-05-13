from __future__ import annotations

import inspect
from pathlib import Path

import forwin.book_state as book_state
import forwin.map as book_map
import forwin.reviewer as reviewer
import forwin.reviewer_v4 as reviewer_v4
import forwin.world_model as world_model
import forwin.world_model_v4 as world_model_v4


ROOT = Path(__file__).resolve().parents[1]


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_core_packages_declare_current_architecture_roles() -> None:
    expectations = {
        "forwin/book_state/README.md": "Status: CANON runtime.",
        "forwin/world_model/README.md": "Status: LEGACY projection / wiki / export layer.",
        "forwin/world_model_v4/README.md": "Status: COMPATIBILITY projection / migration source / debug bridge.",
        "forwin/reviewer/README.md": "Status: MAIN REVIEW facade.",
        "forwin/reviewer_v4/README.md": "Status: COMPATIBILITY gate.",
        "forwin/map/README.md": "Status: CANON map runtime.",
    }
    for rel_path, marker in expectations.items():
        assert marker in _read(rel_path)

    assert "CANON BookState runtime" in inspect.getdoc(book_state)
    assert "LEGACY world model projection" in inspect.getdoc(world_model)
    assert "COMPATIBILITY world_v4 projection" in inspect.getdoc(world_model_v4)
    assert "MAIN chapter review facade" in inspect.getdoc(reviewer)
    assert "COMPATIBILITY world_v4 extraction review gate" in inspect.getdoc(reviewer_v4)
    assert "CANON Scheme C BookMap runtime" in inspect.getdoc(book_map)


def test_orchestrator_book_state_compile_precedes_world_v4_projection() -> None:
    source = _read("forwin/orchestrator/loop.py")
    book_state_index = source.index("book_state_result = commit_service.compile_approved")
    compatibility_index = source.index("compiler_result = WorldModelCompilerV4(session).compile_gate_verdict")

    assert "BookStateGraphDeltaExtractor" in source
    assert "BookStateDirectCommitService" in source
    assert book_state_index < compatibility_index
    assert "BookState canon 已保留" in source
    assert "LEGACY_PROJECTION_FAILED" in source


def test_legacy_world_model_is_labeled_projection_in_runtime_paths() -> None:
    source = _read("forwin/orchestrator/loop.py")
    v4_compiler = _read("forwin/world_model_v4/compiler.py")
    compat_compiler = _read("forwin/world_v4_compat/compiler.py")

    assert "LegacyWorldModelCompiler" in source
    assert "legacy_projection" in source
    assert "BookState canon 不回滚" in source
    assert "compatibility projection rows" in v4_compiler
    assert "class WorldModelCompiler" in compat_compiler
    assert "BookStateCompilerV46" in compat_compiler
    assert "sole v4 canon writer" not in v4_compiler


def test_design_docs_do_not_name_legacy_tables_as_current_source_of_truth() -> None:
    docs = [
        "Design-docs/CURRENT_ARCHITECTURE.md",
        "Design-docs/V4.5_markstone.md",
        "Design-docs/V4_final_book_state_runtime.md",
        "Design-docs/V4.6_knowledge_system.md",
    ]
    forbidden = [
        "EntityState 是 source of truth",
        "CanonEvent 是 source of truth",
        "world_model_v4 是最终 canon source",
        "world_model_v4 is the final canon source",
    ]
    offenders = [
        (rel_path, phrase)
        for rel_path in docs
        for phrase in forbidden
        if phrase in _read(rel_path)
    ]

    assert offenders == []


def test_skill_runtime_stays_instruction_only_and_traceable() -> None:
    skill_doc = _read("Design-docs/V2_9_3_skill_runtime.md")
    runtime_source = _read("forwin/skills/models.py")

    assert "prompt / workflow layer" in skill_doc
    assert "不直接写 `Canon`" in skill_doc
    assert "PromptTrace" in skill_doc
    assert "instruction_only" in runtime_source
