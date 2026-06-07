from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0020_trope_usage_stage"
down_revision = "0019_rewrite_attempt_phase"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE trope_usage_records "
            "ADD COLUMN IF NOT EXISTS usage_stage VARCHAR NOT NULL DEFAULT 'accepted'"
        )
    )
    op.execute(
        sa.text(
            "UPDATE trope_usage_records SET usage_stage = 'accepted' "
            "WHERE usage_stage IS NULL OR usage_stage = ''"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_trope_usage_project_stage_created "
            "ON trope_usage_records (project_id, usage_stage, created_at)"
        )
    )


def downgrade() -> None:
    op.execute(sa.text("DROP INDEX IF EXISTS ix_trope_usage_project_stage_created"))
    op.execute(sa.text("ALTER TABLE trope_usage_records DROP COLUMN IF EXISTS usage_stage"))
