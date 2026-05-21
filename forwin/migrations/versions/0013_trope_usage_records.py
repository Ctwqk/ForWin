from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0013_trope_usage_records"
down_revision = "0012_generation_task_leases"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "trope_usage_records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("arc_id", sa.String(), nullable=False, server_default=""),
        sa.Column("band_id", sa.String(), nullable=False, server_default=""),
        sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("template_id", sa.String(), nullable=False),
        sa.Column("category", sa.String(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_trope_usage_project_band",
        "trope_usage_records",
        ["project_id", "band_id", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_trope_usage_project_band", table_name="trope_usage_records")
    op.drop_table("trope_usage_records")
