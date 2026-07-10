from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0023_drop_governance"
down_revision = "0022_runtime_policy"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.drop_column("projects", "governance_json")


def downgrade() -> None:
    op.add_column(
        "projects",
        sa.Column(
            "governance_json", sa.Text(), nullable=False, server_default="{}"
        ),
    )
