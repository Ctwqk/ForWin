"""Preserve generation history while uniquely identifying continuation children."""

import sqlalchemy as sa
from alembic import op

revision = "0008_generation_continuation"
down_revision = "0007_feedback_actions"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "generation_tasks",
        sa.Column("continuation_parent_task_id", sa.String(), nullable=True),
    )
    op.create_unique_constraint(
        "uq_generation_tasks_continuation_parent_task_id",
        "generation_tasks",
        ["continuation_parent_task_id"],
    )


def downgrade():
    raise RuntimeError("Cannot discard durable continuation identity")
