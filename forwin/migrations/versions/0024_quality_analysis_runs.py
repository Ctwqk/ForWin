"""Add shared quality analysis cache runs.

Revision ID: 0024_quality_analysis_runs
Revises: 0023_drop_governance
"""

from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0024_quality_analysis_runs"
down_revision = "0023_drop_governance"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "quality_analysis_runs",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(), nullable=False),
        sa.Column("plan_fingerprint", sa.String(), nullable=False),
        sa.Column("analysis_mode", sa.String(), nullable=False),
        sa.Column("analyzer_fingerprint", sa.String(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(),
            nullable=False,
            server_default=sa.text("CURRENT_TIMESTAMP"),
        ),
        sa.UniqueConstraint(
            "project_id",
            "chapter_number",
            "content_hash",
            "plan_fingerprint",
            "analysis_mode",
            "analyzer_fingerprint",
            name="uq_quality_analysis_run_cache_key",
        ),
    )
    op.create_index(
        "ix_quality_analysis_runs_project_chapter",
        "quality_analysis_runs",
        ["project_id", "chapter_number", "created_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_quality_analysis_runs_project_chapter",
        table_name="quality_analysis_runs",
    )
    op.drop_table("quality_analysis_runs")
