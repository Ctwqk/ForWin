from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0019_rewrite_attempt_phase"
down_revision = "0018_publisher_bindings_covers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            "ALTER TABLE chapter_rewrite_attempts "
            "ADD COLUMN IF NOT EXISTS repair_phase VARCHAR NOT NULL DEFAULT 'review_repair'"
        )
    )
    op.execute(
        sa.text(
            "ALTER TABLE chapter_rewrite_attempts "
            "ADD COLUMN IF NOT EXISTS phase_attempt_no INTEGER NOT NULL DEFAULT 0"
        )
    )
    op.execute(
        sa.text(
            "UPDATE chapter_rewrite_attempts SET phase_attempt_no = attempt_no "
            "WHERE phase_attempt_no = 0"
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_chapter_rewrite_attempts_project_chapter_phase "
            "ON chapter_rewrite_attempts "
            "(project_id, chapter_number, repair_phase, phase_attempt_no)"
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text("DROP INDEX IF EXISTS ix_chapter_rewrite_attempts_project_chapter_phase")
    )
    op.execute(
        sa.text("ALTER TABLE chapter_rewrite_attempts DROP COLUMN IF EXISTS phase_attempt_no")
    )
    op.execute(
        sa.text("ALTER TABLE chapter_rewrite_attempts DROP COLUMN IF EXISTS repair_phase")
    )
