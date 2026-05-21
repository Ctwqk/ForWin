from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0015_arc_macro_progression"
down_revision = "0014_project_target_default"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "arc_plan_versions",
        sa.Column(
            "macro_progression_json",
            sa.Text(),
            nullable=False,
            server_default="{}",
        ),
    )


def downgrade() -> None:
    op.drop_column("arc_plan_versions", "macro_progression_json")
