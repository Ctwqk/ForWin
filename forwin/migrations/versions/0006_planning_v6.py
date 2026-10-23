"""planning v6

Revision ID: 0006_planning_v6
Revises: 0005_world_plan_conflicts
Create Date: 2026-10-22 00:00:00.000000

Adds versioned arc beat sheets and band plans, and chapter planning fields.
New NOT NULL chapter columns carry server defaults for existing rows.
The maintenance step rename and deletion are irreversible; downgrade only
reverts the schema; removed policy keys and maintenance history are not restored.
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
    op.execute("DELETE FROM post_canon_maintenance_runs WHERE step_name = 'arc'")

    op.drop_table("world_simulation_turns")
    _drop_simulation_from_policies()


def _drop_simulation_from_policies() -> None:
    bind = op.get_bind()
    rows = bind.execute(sa.text("SELECT id, runtime_policy_json FROM projects")).all()
    for project_id, raw_policy in rows:
        try:
            policy = json.loads(raw_policy or "")
        except ValueError:
            continue
        planning = policy.get("planning") if isinstance(policy, dict) else None
        if not isinstance(planning, dict) or "use_llm_simulation" not in planning:
            continue
        del planning["use_llm_simulation"]
        bind.execute(
            sa.text("UPDATE projects SET runtime_policy_json = :policy WHERE id = :id"),
            {"policy": json.dumps(policy, ensure_ascii=False, separators=(",", ":")), "id": project_id},
        )


def downgrade() -> None:
    """Revert schema; the maintenance step data rewrite is not reversed."""
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
