"""Add projection cache metadata fields to world model pages.

Revision ID: 0006_projection_cache_fields
Revises: 0005_canon_quality
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0006_projection_cache_fields"
down_revision = "0005_canon_quality"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("world_model_pages"):
        return

    columns = {column["name"] for column in inspector.get_columns("world_model_pages")}
    additions = (
        ("projection_kind", sa.String(), "world_studio"),
        ("projection_version", sa.String(), ""),
        ("source_digest", sa.String(), ""),
        ("section_digest_json", sa.Text(), "{}"),
        ("observer_type", sa.String(), ""),
        ("observer_id", sa.String(), ""),
        ("role_scope", sa.String(), ""),
        ("visibility_scope", sa.String(), ""),
        ("canon_status", sa.String(), "canon_projection"),
    )
    for name, column_type, default in additions:
        if name not in columns:
            op.add_column(
                "world_model_pages",
                sa.Column(name, column_type, nullable=False, server_default=default),
            )

    for sql in (
        "CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_projection ON world_model_pages (project_id, projection_kind, projection_version)",
        "CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_source_digest ON world_model_pages (project_id, source_digest)",
    ):
        op.execute(sa.text(sql))


def downgrade() -> None:
    for index_name in (
        "ix_world_model_pages_project_source_digest",
        "ix_world_model_pages_project_projection",
    ):
        op.drop_index(index_name, table_name="world_model_pages")
    for column_name in (
        "canon_status",
        "visibility_scope",
        "role_scope",
        "observer_id",
        "observer_type",
        "section_digest_json",
        "source_digest",
        "projection_version",
        "projection_kind",
    ):
        op.drop_column("world_model_pages", column_name)
