"""Add future plan audit run table.

Revision ID: 0010_future_plan_audit
Revises: 0009_admission_obligation_fields
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0010_future_plan_audit"
down_revision = "0009_admission_obligation_fields"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("future_plan_audit_runs"):
        op.create_table(
            "future_plan_audit_runs",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("current_chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("trigger_stage", sa.String(), nullable=False, server_default=""),
            sa.Column("inspected_chapters_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("status", sa.String(), nullable=False, server_default="pass"),
            sa.Column("issues_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("applied_plan_patch_ids_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("blocking_reasons_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("metadata_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    for sql in (
        "CREATE INDEX IF NOT EXISTS ix_future_plan_audit_project_created ON future_plan_audit_runs (project_id, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_future_plan_audit_project_chapter ON future_plan_audit_runs (project_id, current_chapter_number)",
    ):
        op.execute(sa.text(sql))


def downgrade() -> None:
    op.drop_table("future_plan_audit_runs")
