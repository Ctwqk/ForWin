from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import fields
from pathlib import Path

import pytest

import forwin.book_state as book_state
import forwin.map as book_map
import forwin.reviewer as reviewer
import forwin.reviewer_v4 as reviewer_v4
import forwin.world_model as world_model
import forwin.world_model_v4 as world_model_v4
from forwin.api_route_registry import (
    ApiRouteDeps,
    CoreDeps,
    GovernanceDeps,
    ObservabilityDeps,
    ProjectDeps,
    PublisherDeps,
    TaskDeps,
)


ROOT = Path(__file__).resolve().parents[1]

LEGACY_ALIAS_IMPORT_ALLOWLIST = {
    "forwin/api_route_registry.py",
    "forwin/api_world_model_v4_routes.py",
    "forwin/book_state/legacy_import.py",
    "forwin/world_model_v4/__init__.py",
    "forwin/world_model_v4/bootstrap.py",
    "forwin/world_model_v4/compiler.py",
    "forwin/world_model_v4/export.py",
    "forwin/world_model_v4/projection.py",
    "forwin/world_model_v4/provisional.py",
    "forwin/world_model_v4/repository.py",
}


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


def test_new_production_code_does_not_expand_legacy_v4_alias_imports() -> None:
    forbidden = (
        "from forwin.world_model_v4",
        "import forwin.world_model_v4",
        "from forwin.reviewer_v4",
        "import forwin.reviewer_v4",
    )
    offenders: list[str] = []
    for path in sorted((ROOT / "forwin").rglob("*.py")):
        relative = path.relative_to(ROOT).as_posix()
        if relative in LEGACY_ALIAS_IMPORT_ALLOWLIST:
            continue
        text = path.read_text(encoding="utf-8")
        if any(marker in text for marker in forbidden):
            offenders.append(relative)

    assert offenders == []


def test_api_route_deps_are_grouped_by_domain() -> None:
    assert [field.name for field in fields(ApiRouteDeps)] == [
        "core",
        "task",
        "project",
        "governance",
        "observability",
        "publisher",
    ]
    assert "get_publisher_manager" not in CoreDeps.__annotations__
    assert "render_publishers_page" not in CoreDeps.__annotations__
    assert "get_publisher_manager" in PublisherDeps.__annotations__
    assert "render_publishers_page" in PublisherDeps.__annotations__


def test_api_route_deps_legacy_flat_publisher_fields_resolve_to_publisher_group() -> None:
    def noop(*args, **kwargs):
        return None

    def noop_str(*args, **kwargs) -> str:
        return ""

    legacy_kwargs = {
        name: noop
        for group in (CoreDeps, TaskDeps, ProjectDeps, GovernanceDeps, ObservabilityDeps, PublisherDeps)
        for name in group.__annotations__
    }
    legacy_kwargs.update(
        {
            "render_home_page": noop_str,
            "render_publishers_page": noop_str,
            "build_home_page_settings": lambda *args, **kwargs: {},
            "active_generation_task_error_cls": RuntimeError,
            "display_datetime": lambda value: "",
            "json_load_object": lambda value: {},
            "serialize_task": lambda task_id, task: {},
            "get_generation_task_or_404": lambda task_id: {},
            "active_generation_task_ids": lambda project_id: [],
            "generation_task_conflict_message": lambda project_id: "",
            "list_generation_tasks": lambda limit: [],
            "serialize_generation_task_center_item": lambda task_id, task: {},
            "serialize_upload_task_center_item": lambda task: {},
            "list_project_backed_task_items": lambda limit: [],
            "parse_project_task_id": lambda task_id: None,
            "get_project_backed_task_item_or_404": lambda task_id: {},
            "task_is_terminal": lambda status: False,
            "task_is_terminable": lambda task: False,
            "task_is_pausable": lambda task: False,
            "task_is_deletable": lambda task: False,
            "require_genesis_project": lambda project: None,
            "genesis_patch_payload": lambda revision: {},
            "project_delete_blockers": lambda *args, **kwargs: [],
            "project_delete_conflict_message": lambda blockers: "",
            "require_reason": lambda reason: reason,
            "governance_request_payload": lambda payload: {},
            "decision_refs_for_chapter_review": lambda *args, **kwargs: [],
            "validate_constraint_payload": lambda *args, **kwargs: ("", "", ""),
            "serialize_constraint": lambda value: {},
            "list_decision_event_rows": lambda *args, **kwargs: [],
            "serialize_decision_event": lambda value: {},
        }
    )

    deps = ApiRouteDeps(**legacy_kwargs)

    assert deps.get_publisher_manager is deps.publisher.get_publisher_manager
    assert deps.render_publishers_page is deps.publisher.render_publishers_page


def test_design_status_contains_deprecation_matrix() -> None:
    status_doc = _read("Design-docs/DESIGN_STATUS.md")

    assert "兼容 / 弃用矩阵" in status_doc
    assert "`forwin.world_model_v4` | deprecated | `forwin.world_v4_compat`" in status_doc
    assert "`forwin.reviewer_v4` | deprecated | `forwin.world_v4_review_gate`" in status_doc
    assert "`forwin.planning.scenario_rehearsal` | deprecated" in status_doc
    assert "v5.0" in status_doc


def test_deprecated_legacy_modules_emit_deprecation_warning() -> None:
    for module_name in (
        "forwin.world_model",
        "forwin.world_model_v4",
        "forwin.reviewer_v4",
        "forwin.planning.scenario_rehearsal",
    ):
        sys.modules.pop(module_name, None)
        with pytest.warns(DeprecationWarning, match="DESIGN_STATUS"):
            importlib.import_module(module_name)


def test_removed_repair_dead_code_stays_removed() -> None:
    assert not (ROOT / "forwin/orchestrator/repair_coordinator.py").exists()

    loop_detector = importlib.import_module("forwin.reviewer.repair_loop_detector")
    assert hasattr(loop_detector, "RepairAttemptRecord")
    assert not hasattr(loop_detector, "RepairLoopDetector")
    assert not hasattr(loop_detector, "RepairLoopResult")
    assert not hasattr(loop_detector, "attempt_record_from_history_item")

    scope_router = importlib.import_module("forwin.reviewer.repair_scope_router")
    assert hasattr(scope_router, "RepairScopeKind")
    assert hasattr(scope_router, "route_signal_kind")
    assert not hasattr(scope_router, "RepairScopeDispatch")
    assert not hasattr(scope_router, "route_review_repair_scopes")

    rule_profile = importlib.import_module("forwin.canon_quality.rule_profile")
    signature = inspect.signature(rule_profile.countdown_profiles_from_quality_context)
    assert "use_legacy_fallback" not in signature.parameters
    assert "use_legacy_fallback" not in _read("forwin/canon_quality/rule_profile.py")


def test_review_engine_safety_net_runtime_paths_are_removed() -> None:
    forbidden_runtime_tokens = {
        "Review" "OutcomeRouter": [
            "forwin/orchestrator_loop_core/common.py",
            "forwin/orchestrator_loop_core/quality_gates.py",
        ],
        "Repair" "Policy": [
            "forwin/runtime/container.py",
            "forwin/runtime/services.py",
            "forwin/orchestrator_loop_core/service.py",
            "forwin/orchestrator_loop_core/repair_loop.py",
            "forwin/review_engine/rules/repair.py",
        ],
        "Obligation" "ScopeRouter": [
            "forwin/orchestrator_loop_core/quality_gates.py",
            "forwin/review_engine/rules/obligation_scope.py",
        ],
        "select_" "cutover_pair": [
            "forwin/orchestrator_loop_core/quality_gates.py",
        ],
        "engine_" "live_enabled": [
            "forwin/orchestrator_loop_core/repair_loop.py",
        ],
    }
    offenders: list[tuple[str, str]] = []
    for token, rel_paths in forbidden_runtime_tokens.items():
        for rel_path in rel_paths:
            if token in _read(rel_path):
                offenders.append((rel_path, token))

    assert offenders == []
    assert "FinalAcceptanceGate" not in _read("forwin/runtime/container.py")
    assert "FinalAcceptanceGate" not in _read("forwin/orchestrator_loop_core/repair_loop.py")
    assert "FinalAcceptanceGate" in _read("forwin/review_engine/rules/final_acceptance.py")
