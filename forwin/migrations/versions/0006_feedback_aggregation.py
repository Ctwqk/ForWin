"""Directional, evidence-bound aggregate snapshots; preserve prior observations."""

import sqlalchemy as sa
from alembic import op

revision = "0006_feedback_aggregation"
down_revision = "0005_comment_analysis"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "comment_signal_candidates",
        sa.Column("direction", sa.String(), nullable=False, server_default="unknown"),
    )
    for column in (
        sa.Column("direction", sa.String(), nullable=False, server_default="unknown"),
        sa.Column(
            "aggregation_version", sa.String(), nullable=False, server_default=""
        ),
        sa.Column("evidence_sha256", sa.String(64), nullable=False, server_default=""),
        sa.Column("provenance_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column(
            "analyzed_comment_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "known_author_count", sa.Integer(), nullable=False, server_default="0"
        ),
        sa.Column(
            "unknown_author_comment_count",
            sa.Integer(),
            nullable=False,
            server_default="0",
        ),
        sa.Column(
            "source_qualified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    ):
        op.add_column("signal_window_aggregates", column)
    op.create_index(
        "uq_signal_aggregate_evidence",
        "signal_window_aggregates",
        ["project_id", "aggregation_version", "evidence_sha256"],
        unique=True,
        postgresql_where=sa.text("evidence_sha256 <> ''"),
    )


def downgrade():
    count = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM signal_window_aggregates WHERE evidence_sha256 <> ''"
        )
    )
    directional = op.get_bind().scalar(
        sa.text(
            "SELECT count(*) FROM comment_signal_candidates WHERE direction <> 'unknown'"
        )
    )
    if count or directional:
        raise RuntimeError(
            "Cannot discard directional comment or aggregate evidence; retain this forward schema"
        )
    op.drop_index("uq_signal_aggregate_evidence", table_name="signal_window_aggregates")
    for name in (
        "direction",
        "aggregation_version",
        "evidence_sha256",
        "provenance_json",
        "analyzed_comment_count",
        "known_author_count",
        "unknown_author_comment_count",
        "source_qualified",
    ):
        op.drop_column("signal_window_aggregates", name)
    op.drop_column("comment_signal_candidates", "direction")
