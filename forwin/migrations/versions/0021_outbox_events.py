from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0021_outbox_events"
down_revision = "0020_trope_usage_stage"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "outbox_events",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("event_id", sa.String(), nullable=False),
        sa.Column("aggregate_type", sa.String(), nullable=False, server_default=""),
        sa.Column("aggregate_id", sa.String(), nullable=False, server_default=""),
        sa.Column("event_type", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("available_at", sa.DateTime(), nullable=True),
        sa.Column("locked_by", sa.String(), nullable=False, server_default=""),
        sa.Column("locked_at", sa.DateTime(), nullable=True),
        sa.Column("processed_at", sa.DateTime(), nullable=True),
        sa.Column("error_message", sa.Text(), nullable=False, server_default=""),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ux_outbox_events_event_id", "outbox_events", ["event_id"], unique=True)
    op.create_index(
        "ix_outbox_events_status_available",
        "outbox_events",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_outbox_events_aggregate",
        "outbox_events",
        ["aggregate_type", "aggregate_id", "created_at"],
    )
    op.create_index(
        "ix_outbox_events_event_type",
        "outbox_events",
        ["event_type", "status", "created_at"],
    )


def downgrade() -> None:
    op.drop_index("ix_outbox_events_event_type", table_name="outbox_events")
    op.drop_index("ix_outbox_events_aggregate", table_name="outbox_events")
    op.drop_index("ix_outbox_events_status_available", table_name="outbox_events")
    op.drop_index("ux_outbox_events_event_id", table_name="outbox_events")
    op.drop_table("outbox_events")
