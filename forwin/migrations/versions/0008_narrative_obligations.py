"""Add narrative obligation ledger tables.

Revision ID: 0008_narrative_obligations
Revises: 0007_merge_migration_heads
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0008_narrative_obligations"
down_revision = "0007_merge_migration_heads"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("narrative_obligations"):
        op.create_table(
            "narrative_obligations",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("origin_chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("origin_draft_id", sa.String(), nullable=False, server_default=""),
            sa.Column("origin_review_id", sa.String(), nullable=False, server_default=""),
            sa.Column("origin_signal_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("origin_plan_snapshot_id", sa.String(), nullable=False, server_default=""),
            sa.Column("obligation_type", sa.String(), nullable=False, server_default=""),
            sa.Column("priority", sa.String(), nullable=False, server_default="P1"),
            sa.Column("status", sa.String(), nullable=False, server_default="proposed"),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("deferral_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("hardness", sa.String(), nullable=False, server_default="soft_gap"),
            sa.Column("subject_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("evidence_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("deadline_chapter", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deadline_policy", sa.String(), nullable=False, server_default="block_at_deadline"),
            sa.Column("payoff_test", sa.Text(), nullable=False, server_default=""),
            sa.Column("resolution_conditions_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("linked_plan_patch_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("linked_future_chapters_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("blocking_policy", sa.String(), nullable=False, server_default="block_at_deadline"),
            sa.Column("created_by", sa.String(), nullable=False, server_default="system"),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
            sa.Column("resolution_chapter", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("resolution_evidence_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("waive_reason", sa.Text(), nullable=False, server_default=""),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    if not inspector.has_table("narrative_plan_patches"):
        op.create_table(
            "narrative_plan_patches",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("patch_type", sa.String(), nullable=False, server_default="defer_acceptance"),
            sa.Column("target_scope", sa.String(), nullable=False, server_default="chapter"),
            sa.Column("target_plan_id", sa.String(), nullable=False, server_default=""),
            sa.Column("target_arc_id", sa.String(), nullable=False, server_default=""),
            sa.Column("target_band_id", sa.String(), nullable=False, server_default=""),
            sa.Column("affected_chapters_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("source_obligation_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("source_signal_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("old_plan_digest", sa.String(), nullable=False, server_default=""),
            sa.Column("new_plan_digest", sa.String(), nullable=False, server_default=""),
            sa.Column("old_contract_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("new_contract_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("diff_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("must_preserve_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("must_not_change_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("new_constraints_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("writer_context_injections_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("reviewer_context_injections_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("expected_resolution_tests_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("validation_status", sa.String(), nullable=False, server_default="pending"),
            sa.Column("validation_errors_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("applied", sa.Boolean(), nullable=False, server_default=sa.false()),
            sa.Column("applied_at", sa.DateTime(), nullable=True),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    for sql in (
        "CREATE INDEX IF NOT EXISTS ix_narrative_obligations_project_status ON narrative_obligations (project_id, status, priority)",
        "CREATE INDEX IF NOT EXISTS ix_narrative_obligations_project_deadline ON narrative_obligations (project_id, deadline_chapter)",
        "CREATE INDEX IF NOT EXISTS ix_narrative_obligations_origin_chapter ON narrative_obligations (project_id, origin_chapter_number)",
        "CREATE INDEX IF NOT EXISTS ix_narrative_plan_patches_project_scope ON narrative_plan_patches (project_id, target_scope)",
        "CREATE INDEX IF NOT EXISTS ix_narrative_plan_patches_project_applied ON narrative_plan_patches (project_id, applied)",
    ):
        op.execute(sa.text(sql))


def downgrade() -> None:
    op.drop_table("narrative_plan_patches")
    op.drop_table("narrative_obligations")
