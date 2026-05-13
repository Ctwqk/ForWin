"""Normalize legacy band checkpoint statuses.

Revision ID: 0004_legacy_checkpoint_statuses
Revises: 0003_performance_spans
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_legacy_checkpoint_statuses"
down_revision = "0003_performance_spans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE band_checkpoints
            SET status = 'overridden',
                resolved_at = COALESCE(resolved_at, updated_at, created_at)
            WHERE status = 'approved'
            """
        )
    )


def downgrade() -> None:
    op.execute(
        sa.text(
            """
            UPDATE band_checkpoints
            SET status = 'approved'
            WHERE status = 'overridden'
              AND reason LIKE '%legacy_status=approved%'
            """
        )
    )
