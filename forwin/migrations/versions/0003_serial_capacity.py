"""Persist serial capacity reservations and configuration revision evidence."""

import sqlalchemy as sa
from alembic import op

revision = "0003_serial_capacity"
down_revision = "0002_chapter_revisions"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "serial_capacity_config_revisions",
        sa.Column(
            "project_id",
            sa.String(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("version", sa.Integer(), primary_key=True),
        sa.Column("total_chapters", sa.Integer(), nullable=False),
        sa.Column("primary_platform", sa.String(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )
    op.create_table(
        "chapter_capacity_reservations",
        sa.Column(
            "project_id",
            sa.String(),
            sa.ForeignKey("projects.id", ondelete="CASCADE"),
            primary_key=True,
        ),
        sa.Column("chapter_number", sa.Integer(), primary_key=True),
        sa.Column(
            "chapter_plan_id",
            sa.String(),
            sa.ForeignKey("chapter_plans.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column(
            "task_id",
            sa.String(),
            sa.ForeignKey("generation_tasks.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("lease_epoch", sa.Integer(), nullable=False),
        sa.Column("config_version", sa.Integer(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), nullable=False, server_default=sa.func.now()
        ),
    )


def downgrade():
    op.drop_table("chapter_capacity_reservations")
    op.drop_table("serial_capacity_config_revisions")
