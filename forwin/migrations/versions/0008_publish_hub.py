"""publish-hub mirror columns and sync state

Revision ID: 0008_publish_hub
Revises: 0007_drop_codex
Create Date: 2026-10-31 00:00:00.000000

Adds what ForWin keeps about the jobs it hands to publish-hub:

* publisher_upload_jobs: hub_job_id (unique when set), hub_status,
  hub_submit_body_json (the request body stored before it is sent, so a crash
  between POST and storing the id is recovered by resending it byte for byte),
  hub_superseded_reason. The status column is a plain string without a CHECK,
  so the new status "superseded" needs no schema change.
* publisher_work_bindings.hub_destination_id and
  publisher_chapter_bindings.hub_publication_id.
* publish_hub_sync_state: the sync process's cursor and cached destinations.

All new columns are nullable, so existing rows are untouched. The downgrade
turns "superseded" rows into "cancelled" (the older code does not know the
status and would count such a row as still active), then drops every added
column, the index and the table; what they held is not recoverable (publish-hub
keeps its own record).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa
from sqlalchemy.dialects import postgresql


revision: str = "0008_publish_hub"
down_revision: Union[str, Sequence[str], None] = "0007_drop_codex"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.add_column("publisher_upload_jobs", sa.Column("hub_job_id", sa.Text(), nullable=True))
    op.add_column("publisher_upload_jobs", sa.Column("hub_status", sa.Text(), nullable=True))
    op.add_column(
        "publisher_upload_jobs",
        sa.Column("hub_submit_body_json", postgresql.JSONB(), nullable=True),
    )
    op.add_column(
        "publisher_upload_jobs",
        sa.Column("hub_superseded_reason", sa.Text(), nullable=True),
    )
    op.create_index(
        "ux_publisher_upload_jobs_hub_job_id",
        "publisher_upload_jobs",
        ["hub_job_id"],
        unique=True,
    )
    op.add_column(
        "publisher_work_bindings",
        sa.Column("hub_destination_id", sa.Text(), nullable=True),
    )
    op.add_column(
        "publisher_chapter_bindings",
        sa.Column("hub_publication_id", sa.Text(), nullable=True),
    )
    op.create_table(
        "publish_hub_sync_state",
        sa.Column("key", sa.Text(), nullable=False),
        sa.Column("cursor", sa.BigInteger(), server_default="0", nullable=False),
        sa.Column("value_json", postgresql.JSONB(), nullable=True),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.execute(
        "UPDATE publisher_upload_jobs SET status = 'cancelled' "
        "WHERE status = 'superseded'"
    )
    op.drop_table("publish_hub_sync_state")
    op.drop_column("publisher_chapter_bindings", "hub_publication_id")
    op.drop_column("publisher_work_bindings", "hub_destination_id")
    op.drop_index(
        "ux_publisher_upload_jobs_hub_job_id", table_name="publisher_upload_jobs"
    )
    op.drop_column("publisher_upload_jobs", "hub_superseded_reason")
    op.drop_column("publisher_upload_jobs", "hub_submit_body_json")
    op.drop_column("publisher_upload_jobs", "hub_status")
    op.drop_column("publisher_upload_jobs", "hub_job_id")
