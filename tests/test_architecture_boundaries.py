from __future__ import annotations

import ast
import importlib
import inspect
from dataclasses import fields
from pathlib import Path

import pytest

import forwin.book_state as book_state
import forwin.map as book_map
import forwin.review as review
import forwin.book_state.extraction as book_state_extraction
from forwin.application.project_control import ProjectControlApplicationDeps
from forwin.http.routes import (
    ApiRouteDeps,
    CoreDeps,
    PublisherDeps,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("path", [
    "forwin/planning/band_plan",
    "forwin/planning/service.py",
    "forwin/book_state/query_interface.py",
])
def test_unused_planning_and_query_modules_are_removed(path: str) -> None:
    assert not (ROOT / path).exists()


@pytest.mark.parametrize("module_name,symbol", [
    ("forwin.planning", "PlanningService"),
    ("forwin.protocol.book_state", "CognitionState"),
    ("forwin.state.query_helpers", "_active_arc_rows_for_projects"),
    ("forwin.experience.service", "load_long_window_audience_trends"),
])
def test_unused_planning_and_query_symbols_are_removed(module_name: str, symbol: str) -> None:
    assert not hasattr(importlib.import_module(module_name), symbol)


def test_unused_llm_route_keys_are_removed() -> None:
    from forwin.writer.llm.routing import RoutingMixin

    source = ast.parse(_read("forwin/writer/llm/routing.py"))
    route = next(node for node in ast.walk(source) if isinstance(node, ast.FunctionDef)
                 and node.name == "_llm_task_route")
    literals = {node.value for node in ast.walk(route)
                if isinstance(node, ast.Constant) and isinstance(node.value, str)}
    removed_families = {"feedback", "reader_feedback", "arc_planning", "world_model"}
    removed_stages = {"band_plan", "chapter_preview", "chapter_preview_fallback"}
    assert not (removed_families | removed_stages) & literals
    for family in removed_families:
        assert RoutingMixin._llm_task_route(task_family=family) == "general"
    for stage in removed_stages:
        assert RoutingMixin._llm_task_route(stage_key=stage) == "general"


def test_dead_review_signals_and_drift_module_are_removed() -> None:
    import forwin.planning.checkpoints as checkpoints
    from typing import get_args
    from forwin.canon_quality.invariants import InvariantKind

    forbidden = (
        "band_task_completion", "future_resource_preservation", "next_band_compatibility",
        "OverClosureRiskCategory", "future_resource_keys", "form_invariant_drift",
        "InvariantDriftTarget", "ledger_state_drift",
    )
    offenders = [
        (path.relative_to(ROOT).as_posix(), token)
        for path in sorted((ROOT / "forwin").rglob("*.py"))
        for token in forbidden if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []
    assert not (ROOT / "forwin/planning/future_plan_audit/ledger_state_drift_pre_audit.py").exists()
    assert "category" not in checkpoints.BandCheckpointIssueInfo.model_fields
    assert not {"deadline", "state_transition"} & set(get_args(InvariantKind))


def test_task_contract_storage_and_routes_are_gone() -> None:
    from fastapi.testclient import TestClient
    from forwin.http import create_app
    from forwin.models import BandExperiencePlan, ChapterPlan

    assert not (ROOT / "forwin/planning/contracts.py").exists()
    for model in (ChapterPlan, BandExperiencePlan):
        assert "task_contract_json" not in model.__table__.columns
    client = TestClient(create_app())
    for scope in ("chapters/1", "bands/band-1"):
        path = f"/api/projects/project-1/{scope}/task-contract"
        for method in ("GET", "PUT"):
            assert client.request(method, path).status_code in {404, 405}
    client.close()
    editor = _read("forwin/ui_assets/home/app_task_control.js")
    assert "Task" + "Contract" not in editor
    assert "task-contract" not in editor

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
    assert "BookState candidate extraction" in inspect.getdoc(book_state_extraction)
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
        "project_control",
        "observability",
        "publisher",
    ]
    assert "get_publisher_manager" not in CoreDeps.__annotations__
    assert "render_publishers_page" not in CoreDeps.__annotations__
    assert "get_publisher_manager" in PublisherDeps.__annotations__
    assert "render_publishers_page" in PublisherDeps.__annotations__


def test_project_control_deps_do_not_keep_removed_display_callback() -> None:
    assert "display_datetime" not in {
        field.name for field in fields(ProjectControlApplicationDeps)
    }


def test_api_route_deps_reject_flat_dependency_kwargs() -> None:
    with pytest.raises(TypeError):
        ApiRouteDeps(get_session=lambda: None)


def test_design_status_contains_deprecation_matrix() -> None:
    status_doc = _read("Design-docs/DESIGN_STATUS.md")

    assert "兼容 / 弃用矩阵" in status_doc
    assert "`forwin.world_model` | removed | `forwin.knowledge_system`" in status_doc
    assert (
        "`forwin.reviewer_v4` | removed | `forwin.book_state.extraction`" in status_doc
    )
    assert "scenario_rehearsal" not in status_doc


def test_v5_legacy_alias_modules_stay_removed() -> None:
    assert not (ROOT / "forwin/reviewer_v4").exists()
    assert not (ROOT / "forwin/planning/scenario_rehearsal.py").exists()


def test_runtime_scenario_rehearsal_feature_family_stays_removed() -> None:
    removed_paths = (
        "forwin/application/read_models/scenario.py",
        "forwin/models/scenario_rehearsal.py",
        "forwin/planning/scenario_rehearsal_engine.py",
        "forwin/planning/scenario_rehearsal_resolution.py",
        "forwin/planning/scenario_rehearsal_service.py",
        "forwin/planning/scenario_triggers.py",
        "forwin/protocol/scenario_rehearsal.py",
    )
    assert all(not (ROOT / path).exists() for path in removed_paths)

    forbidden = (
        "Scenario Rehearsal",
        "ScenarioRehearsal",
        "scenario_rehearsal",
        "scenario-rehearsal",
        "scenarioRehearsal",
        "scenario_plan_patch",
        "scenarioPlanPatch",
        "rehearse_scenario",
        "ScenarioTriggerContext",
        "ScenarioTriggerEvaluator",
    )
    current_source_paths = [
        path
        for source_root in (
            ROOT / "forwin",
            ROOT / "frontend",
            ROOT / "browser_extension",
        )
        for path in sorted(source_root.rglob("*"))
        if path.suffix in {".py", ".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"}
        and "migrations/versions" not in path.as_posix()
    ]
    offenders = [
        (path.relative_to(ROOT).as_posix(), token)
        for path in current_source_paths
        for token in forbidden
        if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []

    baseline = _read("forwin/migrations/versions/0001_v5_baseline.py")
    assert "scenario_rehearsal_runs" not in baseline
    assert "scenario_plan_patches" not in baseline


def test_planning_runtime_has_one_explicit_composition_service() -> None:
    container_source = _read("forwin/runtime/container.py")
    run_control_source = _read("forwin/generation/pipeline_core/run_control.py")

    assert "def _band_planner(" in container_source
    assert "return BandPlanner(" in container_source
    assert "self.band_planner.ensure_plan_for_chapter" in run_control_source
    assert "arc_envelope_manager.services" not in container_source
    assert "arc_envelope_manager.services" not in run_control_source
    assert "bind_runtime_hooks" not in run_control_source


def test_removed_world_v4_projection_modules_stay_removed() -> None:
    assert not (ROOT / "forwin/world_model").exists()
    assert not (ROOT / "forwin/api_world_model_v4_routes.py").exists()
    assert not (ROOT / "forwin/world_model_v4").exists()
    assert not (ROOT / "forwin/world_v4_compat").exists()


def test_v5_schema_has_no_dead_world_v4_model_or_tables() -> None:
    assert not (ROOT / "forwin/models/world_v4.py").exists()

    production_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin").rglob("*.py"))
        if "migrations/versions" not in path.as_posix()
    )
    assert "forwin.models.world_v4" not in production_source

    baseline = _read("forwin/migrations/versions/0001_v5_baseline.py")
    dead_tables = {
        "beliefs",
        "cognition_snapshots",
        "knowledge_gaps",
        "knowledge_update_events",
        "reader_experience_deltas",
        "reveal_events",
        "world_compile_runs_v4",
        "world_deltas",
        "world_lines",
        "world_model_snapshots_v4",
    }
    assert [table for table in sorted(dead_tables) if f'"{table}"' in baseline] == []


def test_dead_tables_are_dropped_by_0002() -> None:
    from forwin.models import Base

    dead = {
        "world_projection_deltas": "WorldProjectionDeltaRow",
        "story_obligations": "StoryObligationRow",
        "reveal_registry_entries": "RevealRegistryEntryRow",
        "artifact_collection_ledgers": "ArtifactCollectionLedgerRow",
        "book_reader_experience_deltas": "BookReaderExperienceDeltaRow",
    }
    assert [name for name in dead if name in Base.metadata.tables] == []

    production_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin").rglob("*.py"))
        if "migrations/versions" not in path.as_posix()
    )
    assert [cls for cls in dead.values() if cls in production_source] == []

    migration = importlib.import_module("forwin.migrations.versions.0002_drop_dead_tables")
    assert migration.revision == "0002_drop_dead_tables"
    assert migration.down_revision == "0001_v5_recovery"
    upgrade_source = inspect.getsource(migration.upgrade)
    for table in dead:
        assert f'op.drop_table("{table}")' in upgrade_source


def test_reader_promise_table_is_dropped_by_0003() -> None:
    from forwin.models import Base

    assert "book_reader_promises" not in Base.metadata.tables
    production_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin").rglob("*.py"))
        if "migrations/versions" not in path.as_posix()
    )
    assert "BookReaderPromiseRow" not in production_source
    assert "reader_promise_patches" not in production_source

    migration = importlib.import_module("forwin.migrations.versions.0003_book_state_v6")
    upgrade_source = inspect.getsource(migration.upgrade)
    assert 'op.drop_table("book_reader_promises")' in upgrade_source
    for index in (
        "ux_book_reader_promises_project_promise",
        "ix_book_reader_promises_project_status",
        "ix_book_reader_promises_project_chapter",
    ):
        assert upgrade_source.index(f'"{index}"') < upgrade_source.index(
            'op.drop_table("book_reader_promises")'
        )
    assert 'op.create_table(\n        "book_reader_promises"' in inspect.getsource(
        migration.downgrade
    )


def test_production_legacy_tables_are_dropped_by_0002_without_cascade() -> None:
    from forwin.models import Base

    # Production-only leftovers (never in 0001) whose project foreign keys
    # would otherwise block every project delete.
    legacy = (
        "beliefs",
        "cognition_snapshots",
        "knowledge_gaps",
        "knowledge_update_events",
        "npc_intent_snapshots",
        "provisional_band_executions",
        "provisional_chapter_ledgers",
        "reader_experience_deltas",
        "reveal_events",
        "scenario_plan_patches",
        "scenario_rehearsal_runs",
        "world_compile_runs_v4",
        "world_deltas",
        "world_lines",
        "world_model_snapshots_v4",
    )
    assert [name for name in legacy if name in Base.metadata.tables] == []

    migration = importlib.import_module("forwin.migrations.versions.0002_drop_dead_tables")
    upgrade_source = inspect.getsource(migration.upgrade)
    statements = {
        name: f'op.execute("DROP TABLE IF EXISTS {name}")' for name in legacy
    }
    assert [name for name, sql in statements.items() if sql not in upgrade_source] == []
    # scenario_plan_patches.run_id references scenario_rehearsal_runs.
    assert upgrade_source.index(statements["scenario_plan_patches"]) < upgrade_source.index(
        statements["scenario_rehearsal_runs"]
    )
    assert "CASCADE" not in upgrade_source.upper()
    downgrade_source = inspect.getsource(migration.downgrade)
    assert [name for name in legacy if f'"{name}"' in downgrade_source] == []


def test_v5_schema_and_accepted_state_have_single_authorities() -> None:
    versions = sorted(
        path.name
        for path in (ROOT / "forwin/migrations/versions").glob("*.py")
        if path.name != "__init__.py"
    )
    assert versions == [
        "0001_v5_baseline.py",
        "0002_drop_dead_tables.py",
        "0003_book_state_v6.py",
        "0004_drop_parallel_ledgers.py",
        "0005_world_plan_conflicts.py",
        "0006_planning_v6.py",
    ]

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


def test_outbox_runtime_has_only_fenced_nonterminal_claims() -> None:
    owned_paths = (
        "forwin/models/outbox.py",
        "forwin/outbox/store.py",
        "forwin/outbox/worker.py",
        "forwin/outbox/handlers.py",
        "forwin/canon/outbox_events.py",
        "forwin/knowledge_system/projection_jobs.py",
        "forwin/maintenance/events.py",
        "forwin/publisher_runtime/canon_jobs.py",
        "forwin/cli.py",
        "docker-compose.yml",
        "forwin/migrations/versions/0001_v5_baseline.py",
    )
    sources = {path: _read(path) for path in owned_paths}

    for forbidden in (
        "locked_by",
        "locked_at",
        "mark_outbox_event_failed",
        "FORWIN_OUTBOX_WORKER_MAX_ATTEMPTS",
        "--max-attempts",
    ):
        assert [path for path, source in sources.items() if forbidden in source] == []

    assert 'row.status = "failed"' not in sources["forwin/outbox/store.py"]
    assert '"failed"' not in sources["forwin/outbox/worker.py"]
    assert "event.payload_json" not in sources[
        "forwin/knowledge_system/projection_jobs.py"
    ]
    assert "event.payload_json" not in sources["forwin/maintenance/events.py"]
    assert "event.payload_json" not in sources[
        "forwin/publisher_runtime/canon_jobs.py"
    ]
    assert "OutboxClaim" in sources["forwin/outbox/handlers.py"]
    assert 'revision: str = "0001_v5_recovery"' in sources[
        "forwin/migrations/versions/0001_v5_baseline.py"
    ]


def test_pipeline_and_runtime_assembly_have_single_explicit_owners() -> None:
    for removed_path in (
        "forwin/api_artifacts.py",
        "forwin/api_task_history.py",
        "forwin/audience/analysis.py",
        "forwin/audience_metrics.py",
        "forwin/context/ports.py",
        "forwin/generation/ports.py",
        "forwin/generation/pipeline_core/quality_signal_utils.py",
        "forwin/map/pathfinding.py",
        "forwin/orchestration",
        "forwin/orchestrator",
        "forwin/orchestrator_loop_core",
        "forwin/personality/validation.py",
        "forwin/pipeline_loop_core",
        "forwin/review/decision/interval.py",
        "forwin/review/repair_handlers/__init__.py",
        "forwin/review/repair_handlers/active_rules.py",
        "forwin/runtime/ports.py",
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
        "forwin.audience.analysis",
        "forwin.audience_metrics",
    ):
        assert removed not in production_source
    assert all(
        "import *" not in path.read_text(encoding="utf-8") for path in production_files
    )

    pipeline_source = _read("forwin/generation/pipeline.py")
    assert "class ChapterPipeline(" in pipeline_source
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
        "get_audience_trends",
        "get_recent_review_notes",
        "list_chapter_rewrite_attempts_for_phase",
        "list_decision_events",
        "list_narrative_constraints",
        "list_prompt_traces",
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


def test_post_convergence_http_owners_have_no_duplicate_proposal_facades() -> None:
    routes = _read("forwin/http/routes.py")
    for removed_route in (
        "/world-model/export-obsidian",
        "/world-model/import-obsidian",
        "/world-model/proposals",
        "/obsidian/proposals",
    ):
        assert removed_route not in routes
    assert "/obsidian/export" in routes
    assert '"/api/projects/{project_id}/proposals"' in routes

    obsidian = _read("forwin/http/adapters/api_obsidian_routes.py")
    world_model = _read("forwin/http/adapters/api_world_model_routes.py")
    assert "KnowledgeEditProposalRow" not in obsidian
    assert "approve_world_edit_proposal" not in obsidian
    assert "api_obsidian_routes" not in world_model

    world_studio = _read("frontend/world-studio/src/App.tsx")
    mcp_client = _read("forwin/mcp/client.py")
    assert "/obsidian/export" in world_studio
    assert "/obsidian/export" in mcp_client
    assert "/world-model/export-obsidian" not in world_studio
    assert "/world-model/import-obsidian" not in world_studio
    assert "/world-model/export-obsidian" not in mcp_client

    adapter_sources = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin/http/adapters").glob("*.py"))
    )
    assert "def _require_project(" not in adapter_sources
    assert "def require_project(" in _read("forwin/http/request_support.py")


def test_world_conflict_routes_have_dedicated_owner() -> None:
    routes = _read("forwin/http/routes.py")
    adapter = _read("forwin/http/adapters/api_world_conflict_routes.py")
    assert "api_world_conflict_routes.build_handlers" in routes
    for path in (
        "/api/projects/{project_id}/world-conflicts",
        "/api/projects/{project_id}/world-conflicts/{conflict_id}",
        "/api/projects/{project_id}/world-conflicts/{conflict_id}/resolve",
        "/api/projects/{project_id}/world-model/conflicts",
    ):
        assert f'"{path}"' in routes
    assert "WorldConflictRepository" in adapter
    assert "ConflictResolutionService" in adapter
    assert "CanonAdmissionService" in adapter
    assert "ProjectPolicyStore" in adapter
    assert "BookStateCompiler(" not in adapter
    assert "list_project_world_model_conflicts" not in adapter


def test_obsidian_reverse_import_stays_removed_with_generic_proposal_ownership() -> None:
    removed_paths = (
        "forwin/obsidian/importer.py",
        "forwin/obsidian/proposal_classifier.py",
        "forwin/obsidian/proposal_review.py",
        "forwin/obsidian/structured_patch.py",
    )
    assert all(not (ROOT / path).exists() for path in removed_paths)

    routes = _read("forwin/http/routes.py")
    obsidian_api = _read("forwin/http/adapters/api_obsidian_routes.py")
    schema = _read("forwin/api_schema/world.py")
    schema_exports = _read("forwin/api_schema/__init__.py")
    protocol = _read("forwin/protocol/world_model.py")
    world_studio = _read("frontend/world-studio/src/App.tsx")
    browser_fixtures = _read("tests/browser/fixtures.py")
    proposal_api = _read("forwin/http/adapters/api_proposal_routes.py")
    proposal_review = _read("forwin/proposals/proposal_review.py")
    structured_patch = _read("forwin/proposals/structured_patch.py")
    exporter = _read("forwin/obsidian/exporter.py")
    design_status = _read("Design-docs/DESIGN_STATUS.md")
    knowledge_design = _read("Design-docs/V4.6_knowledge_system.md")
    baseline = _read("forwin/migrations/versions/0001_v5_baseline.py")

    forbidden = (
        "ObsidianImporter",
        "ObsidianImportResult",
        "WorldModelImportRequest",
        "WorldModelImportResponse",
        "import_obsidian",
        "/obsidian/import",
        "proposal_classifier",
        "forwin.obsidian.proposal_review",
        "forwin.obsidian.structured_patch",
    )
    current_sources = (
        routes,
        obsidian_api,
        schema,
        schema_exports,
        protocol,
        world_studio,
        browser_fixtures,
        proposal_api,
        proposal_review,
        structured_patch,
    )
    assert all(token not in source for source in current_sources for token in forbidden)

    for obsolete in (
        "obsidian_delta_",
        "obsidian_proposal",
        "obsidian_page:",
        "obsidian_proposal_review",
    ):
        assert obsolete not in proposal_review
        assert obsolete not in structured_patch

    for current_surface in (exporter, design_status, knowledge_design):
        assert "/obsidian/import" not in current_surface
        assert "Import creates proposals" not in current_surface

    assert "/obsidian/export" in routes
    assert '"export_obsidian"' in obsidian_api
    assert "world_export_obsidian" in _read("forwin/mcp/client.py")
    assert "from forwin.proposals.proposal_review import approve_world_edit_proposal" in proposal_api
    assert "from forwin.proposals.structured_patch import proposal_chapter, proposal_to_graph_delta" in proposal_review
    assert 'trigger: str = "proposal_approve"' in proposal_review
    assert '"source": row.source' in structured_patch
    assert "human-indexed" in exporter
    assert '"knowledge_edit_proposals"' in baseline


def test_chapter_pipeline_uses_real_stage_owners_and_typed_collaborators() -> None:
    source = _read("forwin/generation/pipeline.py")
    module = ast.parse(source)
    pipeline = next(
        node
        for node in module.body
        if isinstance(node, ast.ClassDef) and node.name == "ChapterPipeline"
    )

    assigned_methods = [
        node for node in pipeline.body if isinstance(node, (ast.Assign, ast.AnnAssign))
    ]
    assert assigned_methods == []

    constructor = next(
        node
        for node in pipeline.body
        if isinstance(node, ast.FunctionDef) and node.name == "__init__"
    )
    annotations = [
        ast.unparse(argument.annotation)
        for argument in [*constructor.args.args, *constructor.args.kwonlyargs]
        if argument.annotation is not None
    ]
    assert "Any" not in annotations
    assert all("Any" not in annotation for annotation in annotations)

    bases = {ast.unparse(base) for base in pipeline.bases}
    assert {
        "RunControlStage",
        "AuditControlStage",
        "ReviewWorkflowStage",
        "ChapterExecutionStage",
        "WriterExecutionStage",
        "FinalizationStage",
    }.issubset(bases)


def test_repair_service_does_not_receive_the_complete_pipeline() -> None:
    repair_source = _read("forwin/review/repair/service.py")
    chapter_source = _read("forwin/generation/pipeline_core/project_chapters.py")

    assert "from forwin.generation.pipeline import ChapterPipeline" not in repair_source
    assert "runtime: ChapterPipeline" not in repair_source
    assert "runtime=self" not in chapter_source
    assert "RepairExecution" in repair_source


def test_legacy_governance_namespace_is_replaced_by_real_domain_owners() -> None:
    removed_paths = (
        "forwin/governance.py",
        "forwin/governance_checks.py",
        "forwin/governance_keywords.py",
        "forwin/codex_governance.py",
        "forwin/models/governance.py",
        "forwin/review/governance.py",
        "forwin/generation/pipeline_core/governance.py",
        "forwin/api_governance_ops.py",
        "forwin/api_governance_routes.py",
        "forwin/api_governance_support.py",
        "forwin/api_schema/governance.py",
        "forwin/ui_assets/home/app_task_governance.js",
    )
    assert [path for path in removed_paths if (ROOT / path).exists()] == []

    production_source = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin").rglob("*.py"))
    )
    for removed in (
        "from forwin.governance",
        "forwin.governance_checks",
        "forwin.governance_keywords",
        "forwin.codex_governance",
        "GovernanceReviewer",
        "GovernanceStage",
        "GovernanceDeps",
        "GovernanceInsightsResponse",
    ):
        assert removed not in production_source

    assert (ROOT / "forwin/audit/events.py").exists()
    assert not (ROOT / "forwin/planning/contracts.py").exists()
    assert not (ROOT / "forwin/planning/constraints.py").exists()
    assert (ROOT / "forwin/planning/checkpoints.py").exists()
    assert (ROOT / "forwin/review/plan_checks.py").exists()
    assert not (ROOT / "forwin/review/constraint_keywords.py").exists()
    assert (ROOT / "forwin/review/plan_reviewer.py").exists()


def test_removed_repair_dead_code_stays_removed() -> None:
    assert not (ROOT / "forwin/pipeline/repair_coordinator.py").exists()

    assert not (ROOT / "forwin/review/repair_loop_detector.py").exists()

    scope_router = importlib.import_module("forwin.review.repair_scope_router")
    assert hasattr(scope_router, "RepairScopeKind")
    assert hasattr(scope_router, "route_signal_kind")
    assert not hasattr(scope_router, "RepairScopeDispatch")
    assert not hasattr(scope_router, "route_review_repair_scopes")

    assert not (ROOT / "forwin/canon_quality/rule_profile.py").exists()
    assert not (ROOT / "forwin/canon_quality/chapter_review_form/replay_diff.py").exists()


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
        "forwin/api_task_center_service.py",
        "forwin/project_ops",
    ):
        assert not (ROOT / removed_path).exists()

    api_entry = _read("forwin/api.py")
    assert '__all__ = ["app", "create_app", "lifespan"]' in api_entry
    assert "ModuleType" not in api_entry
    assert "__class__" not in api_entry

    api_app = _read("forwin/http/app.py")
    assert "globals().update" not in api_app
    assert '__all__ = ["create_app", "lifespan"]' in api_app
    assert "def create_app(" in api_app
    assert "app = FastAPI" in api_app
    assert not (ROOT / "forwin/api_core").exists()
    assert not (ROOT / "forwin/api_route_registry.py").exists()

    project_routes = _read("forwin/http/adapters/api_project_routes.py")
    publisher_routes = _read("forwin/http/adapters/api_publisher_routes.py")
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
    assert "TaskApplicationService" in _read(
        "forwin/http/adapters/api_task_routes.py"
    )
    assert "ProjectControlApplicationService" in _read(
        "forwin/http/adapters/api_project_control_routes.py"
    )
    assert "TaskCenterService" in _read("forwin/application/task_center.py")


def test_generation_task_producers_use_application_service() -> None:
    for rel_path in (
        "forwin/http/automation.py",
        "forwin/http/generation.py",
        "forwin/generation/worker.py",
        "forwin/production/executor.py",
        "forwin/production/scheduler.py",
    ):
        assert "GenerationApplicationService" in _read(rel_path)


def test_generation_task_runtime_has_no_split_truth_or_automatic_pruning() -> None:
    removed_symbols = {
        "_cached_generation_task",
        "_sync_task_cache",
        "_prefer_cached_generation_task",
        "_prune_generation_tasks_db",
        "task_retention_seconds",
        "tasks_lock",
    }
    offenders: list[tuple[str, str]] = []
    for path in sorted((ROOT / "forwin").rglob("*.py")):
        source = path.read_text(encoding="utf-8")
        relative = path.relative_to(ROOT).as_posix()
        offenders.extend(
            (relative, symbol) for symbol in removed_symbols if symbol in source
        )

    assert offenders == []


def test_runtime_container_is_the_only_pipeline_constructor() -> None:
    offenders: list[str] = []
    for root in (ROOT / "forwin", ROOT / "scripts"):
        for path in root.rglob("*.py"):
            if path == ROOT / "forwin/runtime/container.py":
                continue
            module = ast.parse(path.read_text(encoding="utf-8"))
            if any(
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "ChapterPipeline"
                for node in ast.walk(module)
            ):
                offenders.append(path.relative_to(ROOT).as_posix())

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


def test_provisional_preview_runtime_is_removed() -> None:
    forbidden = {
        "provisional_preview",
        "provisional_writer",
        "ProvisionalPreviewService",
        "ProvisionalBandExecution",
        "ProvisionalChapterLedger",
        "PROVISIONAL_GATE_EVALUATED",
    }
    offenders: list[tuple[str, str]] = []
    for root in (ROOT / "forwin", ROOT / "scripts"):
        for path in sorted(root.rglob("*.py")):
            source = path.read_text(encoding="utf-8")
            relative = path.relative_to(ROOT).as_posix()
            offenders.extend(
                (relative, token) for token in forbidden if token in source
            )

    assert offenders == []

    phase_source = _read("forwin/models/phase.py")
    writer_source = _read("forwin/writer/chapter_writer.py")
    assert "class ProvisionalPromotionRecord" not in phase_source
    assert "def write_preview_chapter(" in writer_source

    for removed_path in (
        "forwin/planning/provisional_preview_service.py",
        "forwin/project_payloads/provisional.py",
        "scripts/provisional_preview_probe.py",
    ):
        assert not (ROOT / removed_path).exists()

    baseline = _read("forwin/migrations/versions/0001_v5_baseline.py")
    routes = _read("forwin/http/routes.py")
    assert "provisional_band_executions" not in baseline
    assert "provisional_chapter_ledgers" not in baseline
    assert "provisional_promotion_records" in baseline
    assert "/provisional/latest" not in routes


def test_future_plan_pre_audits_are_owner_local() -> None:
    assert sorted((ROOT / "forwin/planning").glob("*_pre_audit.py")) == []

    owner = ROOT / "forwin/planning/future_plan_audit"
    for module_name in (
        "obligation_pre_audit.py",
        "signal_pre_audit.py",
    ):
        assert (owner / module_name).is_file()


def test_book_state_extraction_has_one_current_owner() -> None:
    assert not list((ROOT / "forwin/world_v4_review_gate").glob("*.py"))
    assert not list((ROOT / "forwin/extractor").glob("*.py"))

    owner = ROOT / "forwin/book_state/extraction"
    assert sorted(path.name for path in owner.glob("*.py")) == [
        "__init__.py",
        "chapter_delta_extractor.py",
        "chapter_delta_schema.py",
        "chapter_input.py",
        "evidence.py",
        "ref_check.py",
        "translator.py",
        "world_review.py",
    ]
    assert not (ROOT / "forwin/book_state/adapter.py").exists()
    assert not (ROOT / "forwin/protocol/world_v4.py").exists()
    assert not (ROOT / "forwin/book_state/writer_contract.py").exists()
    assert "ChapterDeltaTranslator" in (owner / "translator.py").read_text(
        encoding="utf-8"
    )

    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in sorted((ROOT / "forwin").rglob("*.py"))
    )
    for removed_type in (
        "V4" + "ReviewGate",
        "V4" + "ReviewIssue",
        "V4" + "ReviewGateVerdict",
        "World" + "DeltaExtractor",
        "BookStateExtractionGate",
        "BookStateExtractionDeltaExtractor",
        "BookStateDeltaAdapter",
        "BookStateGraphDeltaExtractor",
        "WriterContractDeltaBuilder",
        "BookStateExtractionRequest",
    ):
        assert removed_type not in production


def test_writer_output_has_no_v4_fields() -> None:
    from forwin.protocol.writer import WriterOutput

    removed_fields = {
        "world_deltas",
        "belief_updates",
        "knowledge_gap_updates",
        "reveal_events",
        "reader_experience_deltas",
        "observer_visibility_updates",
        "must_preserve_facts",
        "must_not_reveal_violations",
    }
    assert removed_fields.isdisjoint(WriterOutput.model_fields)


def test_application_read_models_have_one_current_owner() -> None:
    assert not list((ROOT / "forwin/project_payloads").glob("*.py"))

    owner = ROOT / "forwin/application/read_models"
    for module_name in (
        "__init__.py",
        "arc_snapshot.py",
        "common.py",
        "generation.py",
        "genesis.py",
        "project_detail.py",
        "project_summary.py",
        "runtime_maps.py",
    ):
        assert (owner / module_name).is_file()

    removed_import = "forwin." + "project_payloads"
    offenders = []
    for path in sorted((ROOT / "forwin").rglob("*.py")):
        if removed_import in path.read_text(encoding="utf-8"):
            offenders.append(path.relative_to(ROOT).as_posix())
    assert offenders == []


P4A_REMOVED_PATHS = (
    "forwin/planning/contracts.py",
    "forwin/simulation",
    "forwin/models/phase4.py",
    "forwin/retrieval/broker_core/helpers.py",
    "forwin/retrieval/broker_core/visibility.py",
    "forwin/planning/world_" + "contracts.py",
    "forwin/planning/world_" + "contract_service.py",
    "forwin/models/world_" + "contract.py",
    "forwin/planning/stage_" + "analysis.py",
    "forwin/planning/macro_" + "progression.py",
    "forwin/planning/future_plan_audit/macro_" + "progression.py",
    "forwin/book_state/macro_" + "status.py",
    "forwin/director",
    "forwin/experience/arc_experience_planner.py",
    "forwin/genesis/handoff/chapter_materializer.py",
    "forwin/planning/arc_envelope.py",
    "forwin/planning/arc_envelope_resolver.py",
    "forwin/planning/arc_structure_service.py",
    "forwin/planning/band_window.py",
    "forwin/planning/band_plan_service.py",
    "forwin/planning/arc_activation_service.py",
    "forwin/subworld_" + "manager.py",
    "forwin/context/gates/recency_" + "truncate.py",
    "forwin/codex_bridge/governed_" + "actions.py",
)
P4A_REMOVED_TABLES = (
    "world_simulation_turns",
    "arc_world_" + "contracts", "band_world_" + "contracts",
    "chapter_world_delta_intents", "project_stage_analyses", "project_replan_events",
    "arc_envelopes", "arc_envelope_analyses", "arc_structure_drafts",
    "provisional_promotion_records", "sub_world_" + "roster_" + "items",
)


def _assert_no_p4a_removed_imports() -> None:
    modules = [path.removesuffix(".py").replace("/", ".") for path in P4A_REMOVED_PATHS]
    for path in (ROOT / "forwin").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        source = path.read_text(encoding="utf-8")
        assert not any(module in source for module in modules), path
        tree = ast.parse(source)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                imports = [node.module or ""]
                imports.extend(f"{node.module}.{alias.name}" for alias in node.names)
            elif isinstance(node, ast.Import):
                imports = [alias.name for alias in node.names]
            else:
                imports = []
            assert not any(
                name == module or name.startswith(module + ".")
                for name in imports for module in modules
            ), path


def test_p4a_deleted_modules_and_tables_stay_gone() -> None:
    from forwin.models import Base
    from forwin.runtime.policy import PlanningPolicy

    assert [path for path in P4A_REMOVED_PATHS if (ROOT / path).exists()] == []
    assert set(P4A_REMOVED_TABLES).isdisjoint(Base.metadata.tables)
    assert "context_recency_" + "window" not in PlanningPolicy.model_fields
    _assert_no_p4a_removed_imports()
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "forwin").rglob("*.py")
        if "migrations" not in path.parts
    )
    assert [table for table in P4A_REMOVED_TABLES if table in production] == []
    migration = importlib.import_module("forwin.migrations.versions.0006_planning_v6")
    upgrade = inspect.getsource(migration.upgrade)
    downgrade = inspect.getsource(migration.downgrade)
    for family in ("retired_arc_planner_tables", "subworld_character_paths"):
        assert f"_drop_{family}()" in upgrade
        assert f"_restore_{family}()" in downgrade
        upgrade += inspect.getsource(getattr(migration, f"_drop_{family}"))
        downgrade += inspect.getsource(getattr(migration, f"_restore_{family}"))
    for table in P4A_REMOVED_TABLES:
        assert f'op.drop_table("{table}")' in upgrade
        assert f'"{table}",' in downgrade


@pytest.mark.parametrize("source", [
    "import forwin.simulation",
    "import forwin.simulation.world as world",
    "from forwin.simulation import world",
    "from forwin import simulation",
    "from forwin import simulation as removed",
])
def test_simulation_import_guard_catches_import_forms(tmp_path, monkeypatch, source) -> None:
    package = tmp_path / "forwin"
    package.mkdir()
    (package / "consumer.py").write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError):
        _assert_no_p4a_removed_imports()


@pytest.mark.parametrize("source", [
    'importlib.import_module("forwin.planning.world_' + 'contracts")',
    'importlib.import_module("forwin.planning.world_' + 'contract_service")',
    'importlib.import_module("forwin.models.world_' + 'contract")',
])
def test_p4a_import_guard_catches_literal_dynamic_imports(tmp_path, monkeypatch, source) -> None:
    package = tmp_path / "forwin"
    package.mkdir()
    (package / "consumer.py").write_text(source, encoding="utf-8")
    monkeypatch.setitem(globals(), "ROOT", tmp_path)
    with pytest.raises(AssertionError):
        _assert_no_p4a_removed_imports()


def test_broker_has_no_world_model_pack() -> None:
    from forwin.protocol import context
    from forwin.retrieval.broker_core.broker import RetrievalBroker

    for method in (
        "build_world_" + "model_pack", "_load_reveal_ladder",
        "_filter_writer_safe_world_context", "_merge_writer_world_model_pack",
        "_augment_v46_context",
    ):
        assert not hasattr(RetrievalBroker, method)
    for pack_name in (
        "WorldModel" + "RetrievalPack", "PlanningPack", "WritingPack", "ReviewPack",
        "CompilerPack", "ReaderExperiencePack", "CognitionPack", "RevealPack",
    ):
        assert not hasattr(context, pack_name)
    removed_fields = {
        "knowledge_system_" + "context", "active_world_lines", "visible_world_lines",
        "hidden_world_lines", "recent_world_deltas", "recent_offscreen_deltas",
        "active_knowledge_gaps", "planned_reveal_ladder", "reader_cognition_state",
        "character_cognition_states", "observer_visibility_states", "promise_debts",
        "recent_reader_experience_deltas", "must_not_reveal",
        "fair_misdirection_requirements", "chapter_world_delta_intent",
    }
    assert removed_fields.isdisjoint(context.ChapterContextPack.model_fields)
    for path in ("forwin/http/routes.py", "forwin/http/adapters/api_llm_kb_routes.py"):
        assert "context-" + "pack" not in _read(path)

    from forwin.http import create_app

    assert not [
        route.path for route in create_app().routes
        if "context-" + "pack" in getattr(route, "path", "")
    ]


def test_replanner_and_macro_progression_are_gone() -> None:
    from forwin.models import Base

    column = "macro_" + "progression_json"
    assert column not in Base.metadata.tables["arc_plan_versions"].c
    production = "\n".join(
        path.read_text(encoding="utf-8")
        for path in (ROOT / "forwin").rglob("*")
        if path.suffix in {".py", ".js"} and "migrations" not in path.parts
    )
    for token in (
        "Stage" + "Analyzer", "Pacing" + "Strategist", "Replan" + "Governor",
        "ProjectStage" + "Analysis", "ProjectReplan" + "Event",
        "macro_" + "progression", "ProtagonistMacro" + "Status",
        "chapters_until_" + "replan",
    ):
        assert token not in production
    migration = importlib.import_module("forwin.migrations.versions.0006_planning_v6")
    assert f'op.drop_column("arc_plan_versions", "{column}")' in inspect.getsource(migration.upgrade)


def test_legacy_pipeline_run_entry_is_gone() -> None:
    from forwin.generation.pipeline_core.run_control import RunControlStage
    assert not hasattr(RunControlStage, "run")


# Composite writer/review traces retain the individual attempt's stage_key.
# Each registered owner persists the trace in the chapter or maintenance transaction.
LLM_STAGE_TRACE_OWNERS = {
    "chapter_review_form": ("forwin/generation/pipeline_core/quality_gates.py", "save_prompt_trace("),
    "scene_generation": ("forwin/review/repair/service.py", "self._save_prompt_trace_payload("),
    "scene_stitch": ("forwin/review/repair/service.py", "self._save_prompt_trace_payload("),
    "scene_breakdown": ("forwin/review/repair/service.py", "self._save_prompt_trace_payload("),
    "chapter_review": ("forwin/review/repair/service.py", "self._save_prompt_trace_payload("),
    "chapter_review_json_repair": ("forwin/review/repair/service.py", "self._save_prompt_trace_payload("),
    "repair_verification": ("forwin/review/repair/service.py", "self._save_prompt_trace_payload("),
    "comment_analysis": ("forwin/maintenance/post_canon.py", "save_world_prompt_trace("),
    "spark_pause_gate": ("forwin/generation/gate_delegation.py", "audit_writer.save_prompt_trace("),
    "review.world_conflict": ("forwin/generation/gate_delegation.py", "audit_writer.save_prompt_trace("),
    "chapter_delta_extraction": ("forwin/generation/pipeline_core/review_autofix.py", "save_trace(extraction.prompt_trace)"),
    "world.adjudicate": ("forwin/world_engine/tick.py", "self.service.save_prompt_trace("),
    "world.actor_turn": ("forwin/world_engine/tick.py", "self.service.save_prompt_trace("),
    "world.drift": ("forwin/world_engine/tick.py", "self.service.save_prompt_trace("),
    "plan.arc_beats": ("forwin/planning/beat_sheet/service.py", "self.save_trace("),
    "plan.band": ("forwin/planning/band_planner/service.py", "self.save_trace("),
}


def test_literal_llm_stages_have_registered_prompt_trace_owners() -> None:
    stages = set()
    for path in (ROOT / "forwin").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        tree = ast.parse(path.read_text())
        constants = {
            target.id: node.value.value
            for node in tree.body if isinstance(node, ast.Assign)
            and isinstance(node.value, ast.Constant) and isinstance(node.value.value, str)
            for target in node.targets if isinstance(target, ast.Name)
        }
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            for keyword in node.keywords:
                if keyword.arg != "stage_key":
                    continue
                value = keyword.value
                if isinstance(value, ast.Constant) and isinstance(value.value, str):
                    stages.add(value.value)
                elif isinstance(value, ast.Name) and value.id in constants:
                    stages.add(constants[value.id])
    assert stages == LLM_STAGE_TRACE_OWNERS.keys()
    for path, persistence_call in LLM_STAGE_TRACE_OWNERS.values():
        assert persistence_call in _read(path)


def test_subworld_role_paths_are_gone() -> None:
    forbidden = (
        "SubWorld" + "Manager", "SubWorld" + "RosterItem", "sub_world_" + "roster",
        "roster_" + "item", "plan_band_" + "activation", "ensure_initial_" + "registry",
        "plan_subworld_" + "delta", "apply_arc_" + "delta",
        "SubWorld" + "SlotPlan", "SubWorld" + "CharacterSeed",
    )
    offenders = [
        (str(path.relative_to(ROOT)), token)
        for source_root in (ROOT / "forwin", ROOT / "tests")
        for path in source_root.rglob("*")
        if path.suffix in {".py", ".md", ".toml"} and "migrations" not in path.parts
        for token in forbidden if token in path.read_text(encoding="utf-8")
    ]
    assert offenders == []


def test_narrative_constraints_are_removed() -> None:
    from fastapi.testclient import TestClient
    from forwin import models
    from forwin.api_schema.project import ProjectDetail
    from forwin.http import create_app
    from forwin.protocol.context import ChapterContextPack

    assert not hasattr(models, "NarrativeConstraint")
    assert "narrative_constraints" not in models.Base.metadata.tables
    assert not (ROOT / "forwin/planning/constraints.py").exists()
    assert "narrative_constraints" not in ProjectDetail.model_fields
    assert "active_future_constraints" not in ChapterContextPack.model_fields
    client = TestClient(create_app())
    try:
        assert not any("/constraints" in route.path for route in client.app.routes)
        for method, suffix in (("GET", ""), ("POST", ""), ("PATCH", "/constraint-1")):
            assert client.request(method, f"/api/projects/project-1/constraints{suffix}").status_code in {404, 405}
    finally:
        client.close()
    editor = _read("forwin/ui_assets/home/app_task_control.js")
    for token in ("NarrativeConstraint", "narrative_constraints", "/constraints", "新增 Constraint"):
        assert token not in editor
    for path in (ROOT / "forwin").rglob("*.py"):
        if "migrations" in path.parts:
            continue
        source = path.read_text()
        if path == ROOT / "forwin/runtime/policy.py":
            # Allow only the load shim until post-deploy cleanup.
            source = source.replace('loaded["planning"].pop("future_constraints", None)', "")
        for token in ("NarrativeConstraint", "narrative_constraints", "thread_keep_open",
                      "future_constraint", "CONSTRAINT_CREATED", "CONSTRAINT_UPDATED", "CONSTRAINT_ARCHIVED"):
            assert token not in source, (path, token)
