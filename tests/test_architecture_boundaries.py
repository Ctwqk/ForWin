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
    "forwin/book_state/legacy_import.py",
}


def _read(rel_path: str) -> str:
    return (ROOT / rel_path).read_text(encoding="utf-8")


def test_core_packages_declare_current_architecture_roles() -> None:
    expectations = {
        "forwin/book_state/README.md": "Status: CANON runtime.",
        "forwin/world_model/README.md": "Status: deprecated projection / wiki / export facade.",
        "forwin/reviewer/README.md": "Status: MAIN REVIEW facade.",
        "forwin/reviewer_v4/README.md": "Status: COMPATIBILITY gate.",
        "forwin/map/README.md": "Status: CANON map runtime.",
    }
    for rel_path, marker in expectations.items():
        assert marker in _read(rel_path)

    assert "CANON BookState runtime" in inspect.getdoc(book_state)
    assert "Deprecated world model projection/export facade" in inspect.getdoc(world_model)
    assert "MAIN chapter review facade" in inspect.getdoc(reviewer)
    assert "COMPATIBILITY world_v4 extraction review gate" in inspect.getdoc(reviewer_v4)
    assert "CANON Scheme C BookMap runtime" in inspect.getdoc(book_map)


def test_orchestrator_book_state_runtime_has_no_legacy_projection_markers() -> None:
    source = _read("forwin/orchestrator/loop.py")
    projection_source = _read("forwin/orchestrator_loop_core/world_projection.py")

    forbidden = [
        "WorldModelCompilerV4",
        "LegacyWorldModelCompiler",
        "LEGACY_PROJECTION_FAILED",
        "legacy_projection",
        "projection.legacy_world_model_projection",
        "world_v4_compat_write_enabled",
    ]
    assert all(token not in source for token in forbidden)
    assert all(token not in projection_source for token in forbidden)
    assert "BookStateDirectCommitService" in projection_source
    assert "KnowledgeProjectionRefresher" in projection_source


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
        "from forwin.world_v4_compat",
        "import forwin.world_v4_compat",
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


def test_legacy_inventory_covers_current_production_references() -> None:
    from scripts.audit_legacy_inventory import audit_inventory

    result = audit_inventory(
        root=ROOT,
        inventory_path=ROOT / "docs/designs/legacy-inventory.yaml",
        strict_patterns=True,
    )

    assert result.ok, result.to_text()


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


def test_api_route_deps_reject_flat_dependency_kwargs() -> None:
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

    with pytest.raises(TypeError):
        ApiRouteDeps(**legacy_kwargs)


def test_design_status_contains_deprecation_matrix() -> None:
    status_doc = _read("Design-docs/DESIGN_STATUS.md")

    assert "兼容 / 弃用矩阵" in status_doc
    assert "`forwin.reviewer_v4` | deprecated | `forwin.world_v4_review_gate`" in status_doc
    assert "`forwin.planning.scenario_rehearsal` | deprecated" in status_doc
    assert "v5.0" in status_doc


def test_deprecated_legacy_modules_emit_deprecation_warning() -> None:
    for module_name in (
        "forwin.world_model",
        "forwin.reviewer_v4",
        "forwin.planning.scenario_rehearsal",
    ):
        sys.modules.pop(module_name, None)
        with pytest.warns(DeprecationWarning, match="DESIGN_STATUS"):
            importlib.import_module(module_name)


def test_removed_world_v4_projection_modules_stay_removed() -> None:
    assert not (ROOT / "forwin/api_world_model_v4_routes.py").exists()
    assert not (ROOT / "forwin/world_model_v4").exists()
    assert not (ROOT / "forwin/world_v4_compat").exists()


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
