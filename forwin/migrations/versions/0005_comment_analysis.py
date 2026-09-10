"""Scoped comment origin and explicit versioned analysis completion."""

from hashlib import sha256

import sqlalchemy as sa
from alembic import op

revision = "0005_comment_analysis"
down_revision = "0004_revision_validation"
branch_labels = None
depends_on = None


def upgrade():
    for name in (
        "active_analysis_id",
        "source_scope",
        "account_id",
        "work_binding_id",
        "source_chapter_plan_id",
        "source_canon_commit_id",
        "source_publication_id",
        "content_sha256",
        "source_sha256",
    ):
        op.add_column(
            "publisher_raw_comments",
            sa.Column(
                name,
                sa.String(64)
                if name in {"content_sha256", "source_sha256"}
                else sa.String(),
                nullable=False,
                server_default="",
            ),
        )
    op.add_column(
        "publisher_raw_comments",
        sa.Column(
            "source_status", sa.String(), nullable=False, server_default="unknown"
        ),
    )
    op.add_column(
        "publisher_raw_comments",
        sa.Column("source_chapter_number", sa.Integer(), nullable=True),
    )
    op.add_column(
        "publisher_raw_comments", sa.Column("observed_at", sa.DateTime(), nullable=True)
    )
    op.add_column(
        "publisher_raw_comments", sa.Column("ingested_at", sa.DateTime(), nullable=True)
    )
    connection = op.get_bind()
    for row in connection.execute(
        sa.text("SELECT id, body_text, synced_at FROM publisher_raw_comments")
    ).mappings():
        # Existing display titles and old signal chapter numbers are not provenance.
        connection.execute(
            sa.text(
                "UPDATE publisher_raw_comments SET source_scope=:scope, content_sha256=:hash, ingested_at=:ingested WHERE id=:id"
            ),
            {
                "scope": "legacy:" + row["id"],
                "hash": sha256((row["body_text"] or "").encode()).hexdigest(),
                "ingested": row["synced_at"],
                "id": row["id"],
            },
        )
    op.drop_constraint(
        "uq_publisher_raw_comments_platform_remote",
        "publisher_raw_comments",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_publisher_raw_comments_scoped_remote",
        "publisher_raw_comments",
        ["platform_id", "source_scope", "work_id", "remote_comment_id"],
    )
    op.create_table(
        "comment_analysis_records",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "source_comment_id",
            sa.String(),
            sa.ForeignKey("publisher_raw_comments.id"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("content_sha256", sa.String(64), nullable=False),
        sa.Column("source_sha256", sa.String(64), nullable=False),
        sa.Column("analyzer_version", sa.String(), nullable=False),
        sa.Column("input_body", sa.Text(), nullable=False),
        sa.Column("source_identity_json", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("attempt_count", sa.Integer(), nullable=False),
        sa.Column("signal_count", sa.Integer(), nullable=False),
        sa.Column("generation_chapter_number", sa.Integer(), nullable=False),
        sa.Column("last_error", sa.Text(), nullable=False),
        sa.Column("attempted_at", sa.DateTime()),
        sa.Column("analyzed_at", sa.DateTime()),
        sa.Column("next_retry_at", sa.DateTime()),
        sa.Column("created_at", sa.DateTime(), server_default=sa.func.now()),
        sa.UniqueConstraint(
            "source_comment_id",
            "content_sha256",
            "source_sha256",
            "analyzer_version",
            name="uq_comment_analysis_version",
        ),
    )
    op.create_index(
        "ix_comment_analysis_pending",
        "comment_analysis_records",
        ["status", "next_retry_at"],
    )
    op.add_column(
        "comment_signal_candidates",
        sa.Column("analysis_id", sa.String(), nullable=True),
    )
    op.create_foreign_key(
        "fk_comment_signal_analysis",
        "comment_signal_candidates",
        "comment_analysis_records",
        ["analysis_id"],
        ["id"],
    )


def downgrade():
    if op.get_bind().scalar(sa.text("SELECT count(*) FROM publisher_raw_comments")):
        raise ValueError(
            "comment origin and analysis evidence cannot be discarded; restore a verified backup"
        )
    op.drop_constraint(
        "fk_comment_signal_analysis", "comment_signal_candidates", type_="foreignkey"
    )
    op.drop_column("comment_signal_candidates", "analysis_id")
    op.drop_table("comment_analysis_records")
    op.drop_constraint(
        "uq_publisher_raw_comments_scoped_remote",
        "publisher_raw_comments",
        type_="unique",
    )
    op.create_unique_constraint(
        "uq_publisher_raw_comments_platform_remote",
        "publisher_raw_comments",
        ["platform_id", "remote_comment_id"],
    )
    for name in (
        "active_analysis_id",
        "source_scope",
        "account_id",
        "work_binding_id",
        "source_status",
        "source_chapter_number",
        "source_chapter_plan_id",
        "source_canon_commit_id",
        "source_publication_id",
        "content_sha256",
        "source_sha256",
        "observed_at",
        "ingested_at",
    ):
        op.drop_column("publisher_raw_comments", name)
