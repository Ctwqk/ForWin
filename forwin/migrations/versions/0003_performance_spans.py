"""Add observability performance spans.

Revision ID: 0003_performance_spans
Revises: 0002_active_generation_unique
Create Date: 2026-05-05
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0003_performance_spans"
down_revision = "0002_active_generation_unique"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if not sa.inspect(bind).has_table("performance_spans"):
        op.create_table(
            "performance_spans",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), nullable=False, server_default=""),
            sa.Column("task_id", sa.String(), nullable=False, server_default=""),
            sa.Column("operation_id", sa.String(), nullable=False, server_default=""),
            sa.Column("trace_id", sa.String(), nullable=False, server_default=""),
            sa.Column("span_id", sa.String(), nullable=False, server_default=""),
            sa.Column("parent_span_id", sa.String(), nullable=False, server_default=""),
            sa.Column("span_name", sa.String(), nullable=False, server_default=""),
            sa.Column("span_kind", sa.String(), nullable=False, server_default=""),
            sa.Column("component", sa.String(), nullable=False, server_default=""),
            sa.Column("stage", sa.String(), nullable=False, server_default=""),
            sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("arc_id", sa.String(), nullable=False, server_default=""),
            sa.Column("band_id", sa.String(), nullable=False, server_default=""),
            sa.Column("status", sa.String(), nullable=False, server_default="ok"),
            sa.Column("start_time_unix_ms", sa.BigInteger(), nullable=False, server_default="0"),
            sa.Column("duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("self_duration_ms", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("tags_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("error_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_performance_spans_project_created ON performance_spans (project_id, created_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_performance_spans_task_created ON performance_spans (task_id, created_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_performance_spans_operation ON performance_spans (operation_id, created_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_performance_spans_parent ON performance_spans (parent_span_id, created_at)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_performance_spans_name_duration ON performance_spans (span_name, duration_ms)"))
    op.execute(sa.text("CREATE INDEX IF NOT EXISTS ix_performance_spans_chapter ON performance_spans (project_id, chapter_number, created_at)"))


def downgrade() -> None:
    op.drop_index("ix_performance_spans_chapter", table_name="performance_spans")
    op.drop_index("ix_performance_spans_name_duration", table_name="performance_spans")
    op.drop_index("ix_performance_spans_parent", table_name="performance_spans")
    op.drop_index("ix_performance_spans_operation", table_name="performance_spans")
    op.drop_index("ix_performance_spans_task_created", table_name="performance_spans")
    op.drop_index("ix_performance_spans_project_created", table_name="performance_spans")
    op.drop_table("performance_spans")
