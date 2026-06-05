from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0019_chapter_rewrite_attempt_phase"
down_revision = "0018_publisher_bindings_covers"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "chapter_rewrite_attempts",
        sa.Column("repair_phase", sa.String(), nullable=False, server_default="review_repair"),
    )
    op.add_column(
        "chapter_rewrite_attempts",
        sa.Column("phase_attempt_no", sa.Integer(), nullable=False, server_default="0"),
    )
    op.create_index(
        "ix_chapter_rewrite_attempts_project_chapter_phase",
        "chapter_rewrite_attempts",
        ["project_id", "chapter_number", "repair_phase", "phase_attempt_no"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_chapter_rewrite_attempts_project_chapter_phase",
        table_name="chapter_rewrite_attempts",
    )
    op.drop_column("chapter_rewrite_attempts", "phase_attempt_no")
    op.drop_column("chapter_rewrite_attempts", "repair_phase")
