"""audit requests: the direct audit-sync channel between ForWin and the extension

Revision ID: 0009_audit_requests
Revises: 0008_publish_hub
Create Date: 2026-11-01 00:00:00.000000

Audit sync (reading a book's review state, chapter states, cover and milestones from
the platform) no longer runs as an `audit_sync` upload job: publish-hub does not handle
it (D8) and the legacy upload runtime is going away (D11). ForWin keeps the requests
itself and the browser extension claims them, the way comment sync works.

publisher_audit_requests holds one row per request. A request is open while it is
"pending" or "claimed"; a unique partial index allows one open request per work binding,
so a second ask for the same book returns the open one. A claimed request nobody answers
becomes claimable again once lease_expires_at has passed.

In-flight legacy `audit_sync` upload jobs are not migrated; the legacy runtime finishes
them. The downgrade drops the table: open requests are only reads that can be asked for
again, nothing else refers to them.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0009_audit_requests"
down_revision: Union[str, Sequence[str], None] = "0008_publish_hub"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "publisher_audit_requests",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("platform_id", sa.String(), nullable=False),
        sa.Column("work_binding_id", sa.String(), nullable=False),
        sa.Column("book_name", sa.String(), nullable=False),
        sa.Column("remote_book_id", sa.String(), nullable=False),
        sa.Column("remote_url", sa.String(), nullable=False),
        sa.Column("status", sa.String(), server_default="pending", nullable=False),
        sa.Column("extension_client_id", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("claimed_at", sa.DateTime(), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(), nullable=True),
        sa.Column("finished_at", sa.DateTime(), nullable=True),
        sa.Column("result_summary_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_publisher_audit_requests_open_work",
        "publisher_audit_requests",
        ["work_binding_id"],
        unique=True,
        postgresql_where=sa.text("status IN ('pending', 'claimed')"),
    )
    op.create_index(
        "ix_publisher_audit_requests_status",
        "publisher_audit_requests",
        ["status", "platform_id", "created_at"],
        unique=False,
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_index(
        "ix_publisher_audit_requests_status", table_name="publisher_audit_requests"
    )
    op.drop_index(
        "ux_publisher_audit_requests_open_work", table_name="publisher_audit_requests"
    )
    op.drop_table("publisher_audit_requests")
