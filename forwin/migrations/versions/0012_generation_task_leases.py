from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0012_generation_task_leases"
down_revision = "0010_future_plan_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column("generation_tasks", sa.Column("lease_owner", sa.String(), nullable=False, server_default=""))
    op.add_column("generation_tasks", sa.Column("lease_expires_at", sa.DateTime(), nullable=True))
    op.add_column("generation_tasks", sa.Column("heartbeat_at", sa.DateTime(), nullable=True))
    op.add_column("generation_tasks", sa.Column("resume_from_chapter", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("generation_tasks", sa.Column("run_until_chapter", sa.Integer(), nullable=False, server_default="0"))
    op.add_column("generation_tasks", sa.Column("max_chapters", sa.Integer(), nullable=False, server_default="0"))
    op.create_index("ix_generation_tasks_lease", "generation_tasks", ["status", "lease_expires_at"])


def downgrade() -> None:
    op.drop_index("ix_generation_tasks_lease", table_name="generation_tasks")
    op.drop_column("generation_tasks", "max_chapters")
    op.drop_column("generation_tasks", "run_until_chapter")
    op.drop_column("generation_tasks", "resume_from_chapter")
    op.drop_column("generation_tasks", "heartbeat_at")
    op.drop_column("generation_tasks", "lease_expires_at")
    op.drop_column("generation_tasks", "lease_owner")
