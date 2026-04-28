"""Add active generation task uniqueness guard.

Revision ID: 0002_active_generation_unique
Revises: 0001_postgres_baseline
Create Date: 2026-04-27
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0002_active_generation_unique"
down_revision = "0001_postgres_baseline"
branch_labels = None
depends_on = None


_INDEX_NAME = "ux_generation_tasks_one_active_per_project"
_WHERE = (
    "deleted_at IS NULL "
    "AND task_kind = 'generation' "
    "AND project_id <> '' "
    "AND status NOT IN ('completed', 'partial_failed', 'failed', 'needs_review', 'cancelled', 'paused')"
)


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        sa.text(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {_INDEX_NAME} "
            "ON generation_tasks (project_id) "
            f"WHERE {_WHERE}"
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text(f"DROP INDEX IF EXISTS {_INDEX_NAME}"))
