from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0018_publisher_bindings_covers"
down_revision = "0017_generation_task_payload"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.add_column(
        "publisher_upload_jobs",
        sa.Column(
            "task_kind",
            sa.String(),
            nullable=False,
            server_default="chapter_upload",
        ),
    )
    op.create_index(
        "ix_publisher_upload_jobs_task_status",
        "publisher_upload_jobs",
        ["task_kind", "status", "platform_id"],
    )

    op.create_table(
        "publisher_work_bindings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, server_default=""),
        sa.Column("platform_id", sa.String(), nullable=False),
        sa.Column("book_name", sa.String(), nullable=False, server_default=""),
        sa.Column("remote_book_id", sa.String(), nullable=False, server_default=""),
        sa.Column("remote_url", sa.String(), nullable=False, server_default=""),
        sa.Column("audit_state", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("audit_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("platform_status", sa.String(), nullable=False, server_default=""),
        sa.Column("cover_asset_id", sa.String(), nullable=False, server_default=""),
        sa.Column("cover_state", sa.String(), nullable=False, server_default="none"),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("raw_payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_publisher_work_bindings_project_platform",
        "publisher_work_bindings",
        ["project_id", "platform_id"],
    )
    op.create_index(
        "ux_publisher_work_bindings_project_platform",
        "publisher_work_bindings",
        ["project_id", "platform_id"],
        unique=True,
        postgresql_where=sa.text("project_id <> ''"),
    )
    op.create_index(
        "ix_publisher_work_bindings_platform_book",
        "publisher_work_bindings",
        ["platform_id", "book_name"],
    )

    op.create_table(
        "publisher_chapter_bindings",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "work_binding_id",
            sa.String(),
            sa.ForeignKey("publisher_work_bindings.id"),
            nullable=False,
        ),
        sa.Column("project_id", sa.String(), nullable=False, server_default=""),
        sa.Column("platform_id", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("chapter_title", sa.String(), nullable=False, server_default=""),
        sa.Column("remote_chapter_id", sa.String(), nullable=False, server_default=""),
        sa.Column("remote_url", sa.String(), nullable=False, server_default=""),
        sa.Column("publish_state", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("audit_state", sa.String(), nullable=False, server_default="unknown"),
        sa.Column("audit_reason", sa.Text(), nullable=False, server_default=""),
        sa.Column("word_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_synced_at", sa.DateTime(), nullable=True),
        sa.Column("raw_payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_publisher_chapter_bindings_work",
        "publisher_chapter_bindings",
        ["work_binding_id", "chapter_number"],
    )
    op.create_index(
        "ux_publisher_chapter_bindings_work_number",
        "publisher_chapter_bindings",
        ["work_binding_id", "chapter_number"],
        unique=True,
        postgresql_where=sa.text("chapter_number > 0"),
    )
    op.create_index(
        "ix_publisher_chapter_bindings_project_platform",
        "publisher_chapter_bindings",
        ["project_id", "platform_id"],
    )

    op.create_table(
        "publisher_cover_assets",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), nullable=False, server_default=""),
        sa.Column("work_binding_id", sa.String(), nullable=False, server_default=""),
        sa.Column("source", sa.String(), nullable=False, server_default="minimax"),
        sa.Column("prompt", sa.Text(), nullable=False, server_default=""),
        sa.Column("source_meta_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("status", sa.String(), nullable=False, server_default="generated"),
        sa.Column("selection_state", sa.String(), nullable=False, server_default="candidate"),
        sa.Column("score", sa.Float(), nullable=False, server_default="0"),
        sa.Column("score_reasons_json", sa.Text(), nullable=False, server_default="[]"),
        sa.Column("width", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("height", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_size_bytes", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("file_path", sa.Text(), nullable=False, server_default=""),
        sa.Column("mime_type", sa.String(), nullable=False, server_default=""),
        sa.Column("platform_validation_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("minimax_request_id", sa.String(), nullable=False, server_default=""),
        sa.Column("raw_payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index("ix_publisher_cover_assets_project", "publisher_cover_assets", ["project_id"])
    op.create_index("ix_publisher_cover_assets_work", "publisher_cover_assets", ["work_binding_id"])
    op.create_index(
        "ux_publisher_cover_assets_selected_work",
        "publisher_cover_assets",
        ["work_binding_id"],
        unique=True,
        postgresql_where=sa.text(
            "work_binding_id <> '' AND selection_state IN ('selected', 'approved')"
        ),
    )

    op.create_table(
        "publisher_milestones",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "work_binding_id",
            sa.String(),
            sa.ForeignKey("publisher_work_bindings.id"),
            nullable=False,
        ),
        sa.Column("milestone_type", sa.String(), nullable=False),
        sa.Column("state", sa.String(), nullable=False, server_default="open"),
        sa.Column("message", sa.Text(), nullable=False, server_default=""),
        sa.Column("evidence_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_publisher_milestones_work_state",
        "publisher_milestones",
        ["work_binding_id", "state"],
    )
    op.create_index("ix_publisher_milestones_type", "publisher_milestones", ["milestone_type"])


def downgrade() -> None:
    op.drop_index("ix_publisher_milestones_type", table_name="publisher_milestones")
    op.drop_index("ix_publisher_milestones_work_state", table_name="publisher_milestones")
    op.drop_table("publisher_milestones")

    op.drop_index("ux_publisher_cover_assets_selected_work", table_name="publisher_cover_assets")
    op.drop_index("ix_publisher_cover_assets_work", table_name="publisher_cover_assets")
    op.drop_index("ix_publisher_cover_assets_project", table_name="publisher_cover_assets")
    op.drop_table("publisher_cover_assets")

    op.drop_index("ix_publisher_chapter_bindings_project_platform", table_name="publisher_chapter_bindings")
    op.drop_index("ux_publisher_chapter_bindings_work_number", table_name="publisher_chapter_bindings")
    op.drop_index("ix_publisher_chapter_bindings_work", table_name="publisher_chapter_bindings")
    op.drop_table("publisher_chapter_bindings")

    op.drop_index("ix_publisher_work_bindings_platform_book", table_name="publisher_work_bindings")
    op.drop_index("ux_publisher_work_bindings_project_platform", table_name="publisher_work_bindings")
    op.drop_index("ix_publisher_work_bindings_project_platform", table_name="publisher_work_bindings")
    op.drop_table("publisher_work_bindings")

    op.drop_index("ix_publisher_upload_jobs_task_status", table_name="publisher_upload_jobs")
    op.drop_column("publisher_upload_jobs", "task_kind")
