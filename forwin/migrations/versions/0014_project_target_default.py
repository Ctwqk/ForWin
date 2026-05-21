from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0014_project_target_default"
down_revision = "0013_trope_usage_records"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.alter_column(
        "projects",
        "target_total_chapters",
        server_default=sa.text("50"),
    )


def downgrade() -> None:
    op.alter_column(
        "projects",
        "target_total_chapters",
        server_default=sa.text("3"),
    )
