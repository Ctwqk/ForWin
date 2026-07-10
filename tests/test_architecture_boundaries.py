from __future__ import annotations

import importlib
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

import forwin.book_state as book_state
import forwin.map as book_map
import forwin.review as review
import forwin.world_v4_review_gate as world_v4_review_gate
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
        "forwin/knowledge_system/README.md": "Status: disposable projection and retrieval domain.",
        "forwin/review/README.md": "Status: DRAFT REVIEW domain.",
        "forwin/map/README.md": "Status: CANON map runtime.",
    }
    for rel_path, marker in expectations.items():
        assert marker in _read(rel_path)

    assert "CANON BookState runtime" in inspect.getdoc(book_state)
    assert "Chapter draft review domain" in inspect.getdoc(review)
    assert "Canonical import path" in inspect.getdoc(world_v4_review_gate)
    assert "CANON Scheme C BookMap runtime" in inspect.getdoc(book_map)


def test_pipeline_book_state_runtime_has_no_legacy_projection_markers() -> None:
    source = _read("forwin/generation/pipeline.py")
    projection_source = _read("forwin/generation/pipeline_core/world_projection.py")

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
    assert "BookStateDirectCommitService" not in projection_source
    assert "KnowledgeProjectionRefresher" not in projection_source


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
        for group in (
            CoreDeps,
            TaskDeps,
            ProjectDeps,
            GovernanceDeps,
            ObservabilityDeps,
            PublisherDeps,
        )
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
    assert "`forwin.world_model` | removed | `forwin.knowledge_system`" in status_doc
    assert (
        "`forwin.reviewer_v4` | removed | `forwin.world_v4_review_gate`"
        in status_doc
    )
    assert "`forwin.planning.scenario_rehearsal` | removed" in status_doc


def test_v5_legacy_alias_modules_stay_removed() -> None:
    assert not (ROOT / "forwin/reviewer_v4").exists()
    assert not (ROOT / "forwin/planning/scenario_rehearsal.py").exists()


def test_planning_runtime_has_one_explicit_composition_service() -> None:
    container_source = _read("forwin/runtime/container.py")
    run_control_source = _read("forwin/generation/pipeline_core/run_control.py")

    assert "PlanningService.build_default" in container_source
    assert "arc_envelope_manager.services" not in container_source
    assert "arc_envelope_manager.services" not in run_control_source
    assert "bind_runtime_hooks" in run_control_source


def test_removed_world_v4_projection_modules_stay_removed() -> None:
    assert not (ROOT / "forwin/world_model").exists()
    assert not (ROOT / "forwin/api_world_model_v4_routes.py").exists()
    assert not (ROOT / "forwin/world_model_v4").exists()
    assert not (ROOT / "forwin/world_v4_compat").exists()


def test_v5_schema_and_accepted_state_have_single_authorities() -> None:
    versions = sorted(
        path.name
        for path in (ROOT / "forwin/migrations/versions").glob("*.py")
        if path.name != "__init__.py"
    )
    assert versions == ["0001_v5_baseline.py"]

    base_source = _read("forwin/models/base.py")
    for removed in (
        "POSTGRES_BASELINE_MIGRATIONS",
        "schema_migrations",
        "def _upgrade_",
        "upgrade_db",
    ):
        assert removed not in base_source
    assert "def require_v5_schema(" in base_source
    assert "def run_migrations(" in base_source

    production_init_callers = [
        path.relative_to(ROOT).as_posix()
        for path in sorted((ROOT / "forwin").rglob("*.py"))
        if path != ROOT / "forwin/models/base.py"
        and "init_db(" in path.read_text(encoding="utf-8")
    ]
    assert production_init_callers == []

    for removed_model in ("event.py", "thread.py", "timeline.py"):
        assert not (ROOT / "forwin/models" / removed_model).exists()
    entity_source = _read("forwin/models/entity.py")
    assert "class EntityState" not in entity_source
    assert "class RelationEdge" not in entity_source

    baseline = _read("forwin/migrations/versions/0001_v5_baseline.py")
    for removed_table in (
        "entity_states",
        "relation_edges",
        "canon_events",
        "event_entity_links",
        "plot_threads",
        "plot_thread_beats",
        "story_time_points",
        "chapter_timelines",
        "world_model_pages",
        "world_edit_proposals",
    ):
        assert removed_table not in baseline
    for current_table in (
        "graph_deltas",
        "world_nodes",
        "narrative_nodes",
        "knowledge_projection_pages",
        "quality_analysis_runs",
    ):
        assert f'op.create_table(\n        "{current_table}"' in baseline


def test_pipeline_and_runtime_assembly_have_single_explicit_owners() -> None:
    for removed_path in (
        "forwin/orchestration",
        "forwin/orchestrator",
        "forwin/orchestrator_loop_core",
        "forwin/pipeline_loop_core",
    ):
        assert not (ROOT / removed_path).exists()

    production_files = sorted((ROOT / "forwin").rglob("*.py"))
    production_source = "\n".join(
        path.read_text(encoding="utf-8") for path in production_files
    )
    for removed in (
        "ChapterPipelinePorts",
        "OrchestrationEvent",
        "WritingOrchestrator",
        "forwin.orchestrator",
        "orchestrator_loop_core",
        "_compile_world_model_after_acceptance",
        "_apply_world_v4_gate",
        "_apply_canon_candidate",
        "_coerce_canon_apply_outcome",
        "CanonApplyOutcome",
        "_review_and_maybe_rewrite",
        "_run_canon_repair_for_block",
        "_run_obligation_form_gate",
        "HistoricalReviewHub",
        "FinalAcceptanceGate",
        "final_gate_decision",
        "globals().update",
        "__class__ =",
        "__module__ =",
        "from types import ModuleType",
    ):
        assert removed not in production_source
    assert all("import *" not in path.read_text(encoding="utf-8") for path in production_files)

    pipeline_source = _read("forwin/generation/pipeline.py")
    assert "class ChapterPipeline:" in pipeline_source
    assert "RuntimeServices" not in pipeline_source
    assert "ChapterPipeline." not in pipeline_source
    assert "ChapterPipeline" not in _read("forwin/generation/pipeline_core/__init__.py")

    projection_source = _read("forwin/generation/pipeline_core/world_projection.py")
    assert "_commit_book_state_canon" not in projection_source
    assert "_ensure_genesis_canon_seed_entities" not in projection_source
    assert "class DraftReviewService" in _read("forwin/review/draft_service.py")
    assert "class FinalResidualPolicy" in _read(
        "forwin/review/decision/rules/final_residual.py"
    )
    assert "class CanonAdmissionService" in _read("forwin/canon/admission.py")

    state_updater = _read("forwin/state/updater.py")
    for removed_writer in (
        "apply_state_changes",
        "apply_events",
        "apply_thread_beats",
        "apply_time_advance",
    ):
        assert f"def {removed_writer}(" not in state_updater
    state_repository = _read("forwin/state/repo.py")
    for removed_reader in (
        "get_active_entities",
        "get_allowed_entity_snapshots",
        "get_allowed_entity_names",
        "get_active_relations",
        "get_active_threads",
        "get_current_timeline",
        "get_recent_canon_events",
        "get_entity_by_name",
        "get_entities_by_names",
        "get_thread_by_name",
        "get_chapter_summaries",
    ):
        assert f"def {removed_reader}(" not in state_repository

    project_chapters = _read("forwin/generation/pipeline_core/project_chapters.py")
    acceptance = _read("forwin/generation/pipeline_core/acceptance.py")
    assert "self.canon_admission.commit(" not in acceptance
    assert "self.canon_admission.commit(" not in project_chapters
    assert "self.canon_admission.commit_plan(" in acceptance
    assert "self.canon_admission.commit_plan(" in project_chapters
    assert "self.repair.review_candidate(" in project_chapters
    assert "self.repair.repair_canon_block(" in project_chapters
    assert "class RepairService" in _read("forwin/review/repair/service.py")


def test_removed_repair_dead_code_stays_removed() -> None:
    assert not (ROOT / "forwin/pipeline/repair_coordinator.py").exists()

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


def test_quality_analysis_cache_is_shared_and_versioned() -> None:
    model_source = _read("forwin/models/canon_quality.py")
    service_source = _read("forwin/canon_quality/service.py")
    cache_source = _read("forwin/canon_quality/cache.py")
    assert "class QualityAnalysisRunRow" in model_source
    assert "uq_quality_analysis_run_cache_key" in model_source
    assert "content_hash" in cache_source
    assert "plan_fingerprint" in cache_source
    assert "analyzer_fingerprint" in cache_source
    assert '"quality_context": quality_context' in cache_source
    assert "find_quality_analysis_run" in service_source
    assert "save_quality_analysis_run" in service_source
    assert "analyze_writer_output_quality(" in _read("forwin/review/draft_service.py")
    assert "analyze_writer_output_quality(" in _read(
        "forwin/generation/pipeline_core/quality_gates.py"
    )
    baseline = _read("forwin/migrations/versions/0001_v5_baseline.py")
    assert '"quality_analysis_runs"' in baseline
    production_cache_bypasses = [
        path
        for path in sorted((ROOT / "forwin").rglob("*.py"))
        if "use_cache=False" in path.read_text(encoding="utf-8")
    ]
    assert production_cache_bypasses == [
        ROOT / "forwin/canon_quality/chapter_review_form/replay.py"
    ]


def test_genesis_workflow_contains_delegation_only() -> None:
    for removed_path in (
        "forwin/book_genesis.py",
        "forwin/book_genesis_core",
        "forwin/genesis_workspace",
        "forwin/genesis_handoff",
    ):
        assert not (ROOT / removed_path).exists()
    service = _read("forwin/genesis/service.py")
    assert "from forwin.genesis.workflow" not in service
    assert "def patch_pack(" in service
    assert "return self.workspace.patch_pack(" in service
    assert "BookGenesisService." not in service
    workspace = _read("forwin/genesis/workspace/service.py")
    for method in ("patch_pack", "generate_stage", "refine_stage", "lock_stage"):
        method_source = workspace.split(f"    def {method}(", 1)[1]
        assert "_ensure_genesis_mutable(" in method_source.split("    def ", 1)[0]


def test_review_engine_safety_net_runtime_paths_are_removed() -> None:
    forbidden_runtime_tokens = {
        "ReviewOutcomeRouter": [
            "forwin/generation/pipeline_core/common.py",
            "forwin/generation/pipeline_core/quality_gates.py",
        ],
        "RepairPolicy": [
            "forwin/runtime/container.py",
            "forwin/runtime/services.py",
            "forwin/generation/pipeline.py",
            "forwin/review/repair/service.py",
            "forwin/review/decision/rules/repair.py",
        ],
        "ObligationScopeRouter": [
            "forwin/generation/pipeline_core/quality_gates.py",
            "forwin/review/decision/rules/obligation_scope.py",
        ],
        "select_cutover_pair": [
            "forwin/generation/pipeline_core/quality_gates.py",
        ],
        "engine_live_enabled": [
            "forwin/review/repair/service.py",
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
    assert "FinalResidualPolicy" in _read(
        "forwin/review/decision/rules/final_residual.py"
    )


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


def test_entry_adapters_use_explicit_application_services() -> None:
    for removed_path in (
        "forwin/api_core/exports.py",
        "forwin/api_project_ops.py",
        "forwin/api_project_policy.py",
        "forwin/api_publisher_ops.py",
        "forwin/project_ops",
    ):
        assert not (ROOT / removed_path).exists()

    api_entry = _read("forwin/api.py")
    assert '__all__ = ["app", "lifespan"]' in api_entry
    assert "ModuleType" not in api_entry
    assert "__class__" not in api_entry

    api_app = _read("forwin/api_core/app.py")
    assert "globals().update" not in api_app
    assert '__all__ = ["app", "lifespan"]' in api_app

    project_routes = _read("forwin/api_project_routes.py")
    publisher_routes = _read("forwin/api_publisher_routes.py")
    assert "ProjectApplicationService" in project_routes
    assert "PublisherApplicationService" in publisher_routes
    assert "project_ops" not in project_routes
    assert "api_publisher_ops" not in publisher_routes
    assert "class ProjectApplicationService" in _read(
        "forwin/application/projects/service.py"
    )
    assert "class PublisherApplicationService" in _read(
        "forwin/application/publisher/service.py"
    )


def test_generation_task_producers_use_application_service() -> None:
    for rel_path in (
        "forwin/api_core/automation.py",
        "forwin/api_core/generation.py",
        "forwin/generation/worker.py",
        "forwin/production/executor.py",
        "forwin/production/scheduler.py",
    ):
        assert "GenerationApplicationService" in _read(rel_path)


def test_runtime_container_is_the_only_pipeline_constructor() -> None:
    offenders = [
        path.relative_to(ROOT).as_posix()
        for root in (ROOT / "forwin", ROOT / "scripts")
        for path in root.rglob("*.py")
        if path != ROOT / "forwin/runtime/container.py"
        and "ChapterPipeline(" in path.read_text(encoding="utf-8")
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
