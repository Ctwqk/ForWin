from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0017_generation_task_payload"
down_revision = "0016_project_progression_rules"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "generation_tasks",
        sa.Column("execution_payload_json", sa.Text(), nullable=False, server_default="{}"),
    )


def downgrade() -> None:
    op.drop_column("generation_tasks", "execution_payload_json")
