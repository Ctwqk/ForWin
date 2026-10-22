"""world plan conflicts

Revision ID: 0005_world_plan_conflicts
Revises: 0004_drop_parallel_ledgers
Create Date: 2026-10-22 00:00:00.000000
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0005_world_plan_conflicts"
down_revision: Union[str, Sequence[str], None] = "0004_drop_parallel_ledgers"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "world_plan_conflicts",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("source", sa.String(), nullable=False),
        sa.Column("detected_after_chapter", sa.Integer(), nullable=False),
        sa.Column("story_time", sa.BigInteger(), nullable=False),
        sa.Column("involved_refs_json", sa.Text(), nullable=False),
        sa.Column("violated", sa.String(), nullable=False),
        sa.Column("proposed_change_json", sa.Text(), nullable=False),
        sa.Column("impact_level", sa.String(), nullable=False),
        sa.Column("options_json", sa.Text(), nullable=False),
        sa.Column("required_decider", sa.String(), nullable=False),
        sa.Column("blocks_from_chapter", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("decision_json", sa.Text(), nullable=False),
        sa.Column("result_refs_json", sa.Text(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("intent_ref", sa.String(), nullable=False),
        sa.Column("actor_ref", sa.String(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"]),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_world_plan_conflicts_project_status",
        "world_plan_conflicts",
        ["project_id", "status"],
        unique=False,
    )
    op.create_index(
        "ix_world_plan_conflicts_project_blocks",
        "world_plan_conflicts",
        ["project_id", "blocks_from_chapter"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index("ix_world_plan_conflicts_project_blocks", table_name="world_plan_conflicts")
    op.drop_index("ix_world_plan_conflicts_project_status", table_name="world_plan_conflicts")
    op.drop_table("world_plan_conflicts")
