from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0022_runtime_policy"
down_revision = "0021_outbox_events"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "projects",
        sa.Column("runtime_policy_json", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "projects",
        sa.Column(
            "runtime_policy_version", sa.Integer(), nullable=False, server_default="0"
        ),
    )


def downgrade() -> None:
    op.drop_column("projects", "runtime_policy_version")
    op.drop_column("projects", "runtime_policy_json")
