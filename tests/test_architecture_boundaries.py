from __future__ import annotations

import importlib
import inspect
import sys
from dataclasses import fields
from pathlib import Path

import pytest

import forwin.book_state as book_state
import forwin.map as book_map
import forwin.review as review
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
        "forwin/review/README.md": "Status: DRAFT REVIEW domain.",
        "forwin/reviewer_v4/README.md": "Status: COMPATIBILITY gate.",
        "forwin/map/README.md": "Status: CANON map runtime.",
    }
    for rel_path, marker in expectations.items():
        assert marker in _read(rel_path)

    assert "CANON BookState runtime" in inspect.getdoc(book_state)
    assert "Deprecated world model projection/export facade" in inspect.getdoc(world_model)
    assert "Chapter draft review domain" in inspect.getdoc(review)
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


def test_phase_b_dead_ports_and_legacy_canon_names_stay_removed() -> None:
    assert not (ROOT / "forwin/orchestration/__init__.py").exists()
    assert not (ROOT / "forwin/orchestration/chapter_pipeline.py").exists()
    assert not (ROOT / "forwin/orchestration/events.py").exists()

    production_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin").rglob("*.py"))
    )
    for removed in (
        "ChapterPipelinePorts",
        "OrchestrationEvent",
        "_compile_world_model_after_acceptance",
        "_apply_world_v4_gate",
        "_apply_canon_candidate",
        "_coerce_canon_apply_outcome",
        "CanonApplyOutcome",
        "HistoricalReviewHub",
        "FinalAcceptanceGate",
        "final_gate_decision",
    ):
        assert removed not in production_source

    assert "_commit_book_state_canon" in _read(
        "forwin/orchestrator_loop_core/world_projection.py"
    )
    assert "class DraftReviewService" in _read(
        "forwin/review/draft_service.py"
    )
    assert "class FinalResidualPolicy" in _read(
        "forwin/review/decision/rules/final_residual.py"
    )
    assert "class CanonAdmissionService" in _read("forwin/canon/admission.py")
    assert "self.canon_admission.commit(" in _read(
        "forwin/orchestrator_loop_core/acceptance.py"
    )
    assert "self.canon_admission.commit(" in _read(
        "forwin/orchestrator_loop_core/project_chapters.py"
    )
    assert not (ROOT / "forwin/orchestrator_loop_core/quality_gate_types.py").exists()


def test_removed_repair_dead_code_stays_removed() -> None:
    assert not (ROOT / "forwin/orchestrator/repair_coordinator.py").exists()

    loop_detector = importlib.import_module("forwin.review.repair_loop_detector")
    assert hasattr(loop_detector, "RepairAttemptRecord")
    assert not hasattr(loop_detector, "RepairLoopDetector")
    assert not hasattr(loop_detector, "RepairLoopResult")
    assert not hasattr(loop_detector, "attempt_record_from_history_item")

    scope_router = importlib.import_module("forwin.review.repair_scope_router")
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
            "forwin/review/decision/rules/repair.py",
        ],
        "Obligation" "ScopeRouter": [
            "forwin/orchestrator_loop_core/quality_gates.py",
            "forwin/review/decision/rules/obligation_scope.py",
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
    for removed_package in ("reviser", "review_engine", "reviewer"):
        assert list((ROOT / f"forwin/{removed_package}").rglob("*.py")) == []
    assert "FinalResidualPolicy" in _read("forwin/review/decision/rules/final_residual.py")


def test_removed_generation_modes_and_review_flags_stay_removed() -> None:
    forbidden = (
        "operation_mode",
        "default_operation_mode",
        "progression_mode",
        "review_delegation_mode",
        "review_engine_",
        "chapter_review_form_mode",
        "chapter_blackbox_failure",
        "reckless",
        "RuntimeSettingsStore",
        "PREMIUM_OVERRIDES",
        "project_set_reckless_mode",
        "copilot",
    )
    offenders: list[tuple[str, str]] = []
    for path in sorted((ROOT / "forwin").rglob("*.py")):
        if "migrations" in path.relative_to(ROOT / "forwin").parts:
            continue
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()
        offenders.extend((relative, token) for token in forbidden if token in source)

    assert offenders == []


def test_v5_runtime_policy_is_the_only_generation_policy_surface() -> None:
    config_source = _read("forwin/config.py")
    task_payload_source = _read("forwin/generation/task_payload.py")
    policy_source = _read("forwin/runtime/policy.py")

    assert "class InfrastructureConfig" in config_source
    assert "class Config" not in config_source
    assert "policy_snapshot: RuntimePolicy" in task_payload_source
    assert "runtime_overrides" not in task_payload_source
    assert 'QualityProfile = Literal["standard", "pulp"]' in policy_source
    assert 'GateDelegate = Literal["human", "spark"]' in policy_source

    forbidden_import = "from forwin.config import Config"
    offenders = [
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "forwin", ROOT / "scripts")
        for path in root.rglob("*.py")
        if forbidden_import in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_generation_task_producers_use_application_service() -> None:
    for rel_path in (
        "forwin/api_core/generation.py",
        "forwin/generation/worker.py",
        "forwin/production/executor.py",
    ):
        assert "GenerationApplicationService" in _read(rel_path)


def test_runtime_container_is_the_only_orchestrator_constructor() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "forwin", ROOT / "scripts")
        for path in root.rglob("*.py")
        if path != ROOT / "forwin/runtime/container.py"
        and "WritingOrchestrator(" in path.read_text(encoding="utf-8")
    ]

    assert offenders == []


def test_home_console_has_no_removed_generation_policy_controls() -> None:
    source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin/ui_assets/home").glob("*"))
        if path.suffix in {".html", ".js"}
    )
    for removed in (
        "config_generation_operation_mode",
        "task_generation_operation_mode",
        "task_generation_progression_mode",
        "config_generation_freeze_failed_candidates",
        "model_form_api_key",
        "saveProjectGovernanceFromDrawer",
    ):
        assert removed not in source
    for required in (
        "runtime_policy_quality_profile",
        "runtime_policy_model_profile_id",
        "runtime_policy_min_chapter_chars",
        "runtime_policy_gate_",
    ):
        assert required in source
