"""planning v6

Revision ID: 0006_planning_v6
Revises: 0005_world_plan_conflicts
Create Date: 2026-10-22 00:00:00.000000

Adds versioned arc beat sheets and band plans, and chapter planning fields.
Drops arc_world_contracts, band_world_contracts, and chapter_world_delta_intents;
downgrade recreates these tables empty without restoring their rows.
The subworld character table and identity links are also dropped; downgrade
restores their schema, with empty identity links, but cannot recover deleted data.
New NOT NULL chapter columns carry server defaults for existing rows.
The maintenance step rename and deletion are irreversible; downgrade only
reverts the schema; removed policy keys and maintenance history are not restored.
Downgrade leaves stored policies without use_llm_simulation, so pre-0006 code
cannot load them. The rollback runbook restores from backup.
"""

import json
from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0006_planning_v6"
down_revision: Union[str, Sequence[str], None] = "0005_world_plan_conflicts"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    _drop_subworld_character_paths()
    _drop_retired_arc_planner_tables()
    op.create_table(
        "arc_beat_sheets",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("arc_plan_id", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("protagonist_ref", sa.String(), nullable=False),
        sa.Column("phases_json", sa.Text(), nullable=False),
        sa.Column("milestones_json", sa.Text(), nullable=False),
        sa.Column("protections_json", sa.Text(), nullable=False),
        sa.Column("reveal_targets_json", sa.Text(), nullable=False),
        sa.Column("exit_state_json", sa.Text(), nullable=False),
        sa.Column("band_breakdown_json", sa.Text(), nullable=False),
        sa.Column("reader_promise_json", sa.Text(), nullable=False),
        sa.Column("arc_payoff_map_json", sa.Text(), nullable=False),
        sa.Column("setting_brief", sa.Text(), server_default="", nullable=False),
        sa.Column("revision_reason", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["arc_plan_id"], ["arc_plan_versions.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("arc_plan_id", "version", name="uq_arc_beat_sheets_arc_version"),
    )
    op.create_index("ix_arc_beat_sheets_project_status", "arc_beat_sheets", ["project_id", "status"], unique=False)
    op.create_table(
        "band_plans",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("arc_plan_id", sa.String(), nullable=False),
        sa.Column("beat_sheet_id", sa.String(), nullable=False),
        sa.Column("band_index", sa.Integer(), nullable=False),
        sa.Column("chapter_start", sa.Integer(), nullable=False),
        sa.Column("chapter_end", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("replan_count", sa.Integer(), nullable=False),
        sa.Column("plan_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.ForeignKeyConstraint(["arc_plan_id"], ["arc_plan_versions.id"]),
        sa.ForeignKeyConstraint(["beat_sheet_id"], ["arc_beat_sheets.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_band_plans_project_arc_band_status", "band_plans",
        ["project_id", "arc_plan_id", "band_index", "status"], unique=False,
    )
    op.add_column("chapter_plans", sa.Column("band_plan_id", sa.String(), nullable=True))
    op.create_foreign_key("fk_chapter_plans_band_plan", "chapter_plans", "band_plans", ["band_plan_id"], ["id"])
    op.add_column("chapter_plans", sa.Column("pov_ref", sa.String(), server_default="", nullable=False))
    for name in (
        "onstage_refs_json", "location_refs_json", "world_bindings_json",
        "reveal_directives_json", "hint_directives_json", "intended_changes_json",
        "milestone_ids_json",
    ):
        op.add_column("chapter_plans", sa.Column(name, sa.Text(), server_default="[]", nullable=False))
    op.add_column("chapter_plans", sa.Column("expected_span_minutes", sa.Integer(), nullable=True))
    op.add_column("chapter_plans", sa.Column("hook", sa.Text(), server_default="", nullable=False))
    op.drop_index("ix_chapter_plans_project_chapter", table_name="chapter_plans")
    op.create_index("uq_chapter_plans_project_chapter", "chapter_plans", ["project_id", "chapter_number"], unique=True)

    op.execute("UPDATE post_canon_maintenance_runs SET step_name = 'plan' WHERE step_name = 'planning'")
    op.execute(
        "UPDATE post_canon_maintenance_runs SET idempotency_key = "
        "left(idempotency_key, length(idempotency_key) - length('planning')) || 'plan' "
        "WHERE step_name = 'plan' AND idempotency_key LIKE '%planning'"
    )
    op.execute("DELETE FROM post_canon_maintenance_runs WHERE step_name = 'arc'")

    op.drop_table("arc_world_contracts")
    op.drop_table("band_world_contracts")
    op.drop_table("chapter_world_delta_intents")

    op.drop_table("world_simulation_turns")
    _drop_simulation_from_policies()

    op.drop_table("project_stage_analyses")
    op.drop_table("project_replan_events")
    op.drop_column("arc_plan_versions", "macro_progression_json")


def _drop_simulation_from_policies() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, runtime_policy_json FROM projects")).all()
    for project_id, raw_policy in rows:
        try:
            policy = json.loads(raw_policy or "")
        except ValueError:
            continue
        planning = policy.get("planning") if isinstance(policy, dict) else None
        if not isinstance(planning, dict):
            continue
        removed_keys = ("use_llm_simulation", "context_recency_window")
        if not any(key in planning for key in removed_keys):
            continue
        for key in removed_keys:
            planning.pop(key, None)
        bind.execute(
            sa.text("UPDATE projects SET runtime_policy_json = :policy WHERE id = :id"),
            {"policy": json.dumps(policy, ensure_ascii=False, separators=(",", ":")), "id": project_id},
        )


def downgrade() -> None:
    """Revert schema; the maintenance step data rewrite is not reversed."""
    _restore_subworld_character_paths()
    _restore_retired_arc_planner_tables()
    op.create_table(
        "project_replan_events",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("trigger_chapter", sa.Integer(), nullable=False),
        sa.Column("risk_level", sa.String(), nullable=False),
        sa.Column("reason", sa.Text(), nullable=False),
        sa.Column("focus_threads_json", sa.Text(), nullable=False),
        sa.Column("strategy", sa.String(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("cooldown_until_chapter", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "project_stage_analyses",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("stage_label", sa.String(), nullable=False),
        sa.Column("progress_ratio", sa.Float(), nullable=False),
        sa.Column("timeline_label", sa.String(), nullable=False),
        sa.Column("timeline_ordinal", sa.Integer(), nullable=False),
        sa.Column("pacing_verdict", sa.String(), nullable=False),
        sa.Column("pacing_summary", sa.Text(), nullable=False),
        sa.Column("stale_threads_json", sa.Text(), nullable=False),
        sa.Column("active_thread_count", sa.Integer(), nullable=False),
        sa.Column("unresolved_thread_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.add_column(
        "arc_plan_versions",
        sa.Column("macro_progression_json", sa.Text(), server_default="{}", nullable=False),
    )
    op.create_table(
        "arc_world_contracts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("arc_id", sa.String(), nullable=False),
        sa.Column("arc_number", sa.Integer(), nullable=False),
        sa.Column("contract_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_arc_world_contracts_project_arc",
        "arc_world_contracts",
        ["project_id", "arc_id"],
        unique=False,
    )
    op.create_table(
        "band_world_contracts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("arc_id", sa.String(), nullable=False),
        sa.Column("band_id", sa.String(), nullable=False),
        sa.Column("chapter_start", sa.Integer(), nullable=False),
        sa.Column("chapter_end", sa.Integer(), nullable=False),
        sa.Column("contract_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_band_world_contracts_project_arc_band",
        "band_world_contracts",
        ["project_id", "arc_id", "band_id"],
        unique=False,
    )
    op.create_table(
        "chapter_world_delta_intents",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("chapter_plan_id", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("intent_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_chapter_world_intents_project_chapter",
        "chapter_world_delta_intents",
        ["project_id", "chapter_number"],
        unique=False,
    )
    op.create_table(
        "world_simulation_turns",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("pressure_level", sa.String(), nullable=False),
        sa.Column("pressure_summary", sa.Text(), nullable=False),
        sa.Column("notable_shifts_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.drop_index("uq_chapter_plans_project_chapter", table_name="chapter_plans")
    op.create_index("ix_chapter_plans_project_chapter", "chapter_plans", ["project_id", "chapter_number"], unique=False)
    op.drop_column("chapter_plans", "hook")
    op.drop_column("chapter_plans", "expected_span_minutes")
    for name in (
        "milestone_ids_json", "intended_changes_json", "hint_directives_json",
        "reveal_directives_json", "world_bindings_json", "location_refs_json",
        "onstage_refs_json",
    ):
        op.drop_column("chapter_plans", name)
    op.drop_column("chapter_plans", "pov_ref")
    op.drop_constraint("fk_chapter_plans_band_plan", "chapter_plans", type_="foreignkey")
    op.drop_column("chapter_plans", "band_plan_id")
    op.drop_index("ix_band_plans_project_arc_band_status", table_name="band_plans")
    op.drop_table("band_plans")
    op.drop_index("ix_arc_beat_sheets_project_status", table_name="arc_beat_sheets")
    op.drop_table("arc_beat_sheets")


# Task 17a: removed planner schema; downgrade restores schema, not deleted rows.
def _drop_retired_arc_planner_tables() -> None:
    op.drop_index("ix_arc_envelope_analyses_project_arc", table_name="arc_envelope_analyses")
    op.drop_index("ix_arc_envelopes_project_arc", table_name="arc_envelopes")
    op.drop_index("ix_arc_structure_drafts_project_arc", table_name="arc_structure_drafts")
    op.drop_index("ix_provisional_promotions_project_arc_band", table_name="provisional_promotion_records")
    op.drop_table("arc_envelope_analyses")
    op.drop_table("arc_envelopes")
    op.drop_table("arc_structure_drafts")
    op.drop_table("provisional_promotion_records")


def _restore_retired_arc_planner_tables() -> None:
    op.create_table(
        "arc_envelope_analyses",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("arc_id", sa.String(), nullable=False),
        sa.Column("based_on_band_id", sa.String(), nullable=False),
        sa.Column("recommendation", sa.String(), nullable=False),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column("expansion_signals_json", sa.Text(), nullable=False),
        sa.Column("compression_signals_json", sa.Text(), nullable=False),
        sa.Column("suggested_target", sa.Integer(), nullable=False),
        sa.Column("suggested_soft_min", sa.Integer(), nullable=False),
        sa.Column("suggested_soft_max", sa.Integer(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["arc_id"],
            ["arc_plan_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_arc_envelope_analyses_project_arc",
        "arc_envelope_analyses",
        ["project_id", "arc_id"],
        unique=False,
    )
    op.create_table(
        "arc_envelopes",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("arc_id", sa.String(), nullable=False),
        sa.Column("base_target_size", sa.Integer(), nullable=False),
        sa.Column("base_soft_min", sa.Integer(), nullable=False),
        sa.Column("base_soft_max", sa.Integer(), nullable=False),
        sa.Column("resolved_target_size", sa.Integer(), nullable=False),
        sa.Column("resolved_soft_min", sa.Integer(), nullable=False),
        sa.Column("resolved_soft_max", sa.Integer(), nullable=False),
        sa.Column("detailed_band_size", sa.Integer(), nullable=False),
        sa.Column("frozen_zone_size", sa.Integer(), nullable=False),
        sa.Column("current_projected_size", sa.Integer(), nullable=False),
        sa.Column("current_confidence", sa.Float(), nullable=False),
        sa.Column("source_policy_tier", sa.String(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["arc_id"],
            ["arc_plan_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_arc_envelopes_project_arc",
        "arc_envelopes",
        ["project_id", "arc_id"],
        unique=False,
    )
    op.create_table(
        "arc_structure_drafts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("arc_id", sa.String(), nullable=False),
        sa.Column("phase_layout_json", sa.Text(), nullable=False),
        sa.Column("key_beats_json", sa.Text(), nullable=False),
        sa.Column("thread_priorities_json", sa.Text(), nullable=False),
        sa.Column("hotspot_candidates_json", sa.Text(), nullable=False),
        sa.Column("compression_candidates_json", sa.Text(), nullable=False),
        sa.Column("reader_promise_json", sa.Text(), nullable=False),
        sa.Column("arc_payoff_map_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["arc_id"],
            ["arc_plan_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_arc_structure_drafts_project_arc",
        "arc_structure_drafts",
        ["project_id", "arc_id"],
        unique=False,
    )
    op.create_table(
        "provisional_promotion_records",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("arc_id", sa.String(), nullable=False),
        sa.Column("band_id", sa.String(), nullable=False),
        sa.Column("promoted_chapter_ids_json", sa.Text(), nullable=False),
        sa.Column("promotion_reason", sa.Text(), nullable=False),
        sa.Column("based_on_analysis_id", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["arc_id"],
            ["arc_plan_versions.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_provisional_promotions_project_arc_band",
        "provisional_promotion_records",
        ["project_id", "arc_id", "band_id"],
        unique=False,
    )


def _drop_subworld_character_paths() -> None:
    op.drop_table("sub_world_roster_items")
    op.drop_column("character_identity_map", "roster_item_ids_json")


def _restore_subworld_character_paths() -> None:
    # Populate existing identities before restoring the baseline's no-default column.
    op.add_column(
        "character_identity_map",
        sa.Column("roster_item_ids_json", sa.Text(), server_default="[]", nullable=False),
    )
    op.alter_column("character_identity_map", "roster_item_ids_json", server_default=None)
    op.create_table(
        "sub_world_roster_items",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("subworld_id", sa.String(), nullable=False),
        sa.Column("entity_id", sa.String(), nullable=True),
        sa.Column("entity_kind", sa.String(), nullable=False),
        sa.Column("display_name", sa.String(), nullable=False),
        sa.Column("slot_key", sa.String(), nullable=False),
        sa.Column("role_hint", sa.Text(), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_core", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("activation_chapter", sa.Integer(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(), server_default=sa.text("now()"), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["entity_id"],
            ["entities.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.ForeignKeyConstraint(
            ["project_id", "subworld_id"],
            ["sub_worlds.project_id", "sub_worlds.id"],
            name="fk_sub_world_roster_project_subworld",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_sub_world_roster_project_display",
        "sub_world_roster_items",
        ["project_id", "display_name"],
        unique=False,
    )
    op.create_index(
        "ix_sub_world_roster_project_slot",
        "sub_world_roster_items",
        ["project_id", "slot_key"],
        unique=False,
    )
    op.create_index(
        "ix_sub_world_roster_project_status",
        "sub_world_roster_items",
        ["project_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_sub_world_roster_project_subworld",
        "sub_world_roster_items",
        ["project_id", "subworld_id"],
        unique=False,
    )
