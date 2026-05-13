"""Add WorldModel canonical page identity columns.

Revision ID: 0004_world_model_canonical_pages
Revises: 0003_performance_spans
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0004_world_model_canonical_pages"
down_revision = "0003_performance_spans"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    _upgrade_world_model_canonical_pages()


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(sa.text("DROP INDEX IF EXISTS ux_world_model_pages_live_identity"))
    op.execute(sa.text("DROP INDEX IF EXISTS ix_world_model_pages_project_identity"))
    for column in (
        "canonical_rank",
        "supersedes_page_id",
        "canonical_source_id",
        "canonical_source_type",
        "logical_identity_key",
    ):
        op.execute(sa.text(f"ALTER TABLE world_model_pages DROP COLUMN IF EXISTS {column}"))


def _upgrade_world_model_canonical_pages() -> None:
    op.execute(sa.text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS logical_identity_key VARCHAR NOT NULL DEFAULT ''"))
    op.execute(sa.text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS canonical_source_type VARCHAR NOT NULL DEFAULT ''"))
    op.execute(sa.text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS canonical_source_id VARCHAR NOT NULL DEFAULT ''"))
    op.execute(sa.text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS supersedes_page_id VARCHAR NOT NULL DEFAULT ''"))
    op.execute(sa.text("ALTER TABLE world_model_pages ADD COLUMN IF NOT EXISTS canonical_rank INTEGER NOT NULL DEFAULT 0"))
    op.execute(
        sa.text(
            """
            UPDATE world_model_pages
            SET logical_identity_key = CASE
                    WHEN page_type IN ('character','faction','organization','family','institution','resource','region','node','location')
                         AND trim(title) <> ''
                    THEN page_type || ':name:' || regexp_replace(lower(trim(title)), '[[:space:]]+', '', 'g')
                    ELSE page_type || ':page:' || COALESCE(NULLIF(page_key, ''), regexp_replace(lower(trim(title)), '[[:space:]]+', '', 'g'))
                END,
                canonical_source_type = CASE
                    WHEN frontmatter_json LIKE '%"node_id"%' THEN 'book_state_node'
                    WHEN frontmatter_json LIKE '%"legacy_entity_id"%' THEN 'legacy_entity'
                    WHEN page_key LIKE '%:genesis:%' OR page_key LIKE 'genesis:%' THEN 'genesis'
                    ELSE 'world_model_page'
                END,
                canonical_source_id = COALESCE(NULLIF(page_key, ''), id),
                canonical_rank = CASE
                    WHEN frontmatter_json LIKE '%"node_id"%' THEN 30000
                    WHEN page_key LIKE '%:genesis:%' OR page_key LIKE 'genesis:%' THEN 10000
                    ELSE 20000
                END + LEAST(GREATEST(COALESCE(as_of_chapter, 0), 0), 9999)
            WHERE logical_identity_key = ''
            """
        )
    )
    op.execute(
        sa.text(
            """
            WITH ranked AS (
                SELECT
                    id,
                    first_value(id) OVER (
                        PARTITION BY project_id, page_type, logical_identity_key
                        ORDER BY canonical_rank DESC, as_of_chapter DESC, revision DESC, updated_at DESC, id DESC
                    ) AS canonical_id,
                    row_number() OVER (
                        PARTITION BY project_id, page_type, logical_identity_key
                        ORDER BY canonical_rank DESC, as_of_chapter DESC, revision DESC, updated_at DESC, id DESC
                    ) AS rn
                FROM world_model_pages
                WHERE status = 'canon_live'
                  AND logical_identity_key <> ''
            )
            UPDATE world_model_pages AS page
            SET status = 'superseded',
                supersedes_page_id = ranked.canonical_id
            FROM ranked
            WHERE page.id = ranked.id
              AND ranked.rn > 1
            """
        )
    )
    op.execute(
        sa.text(
            """
            UPDATE world_model_pages
            SET supersedes_page_id = ''
            WHERE status = 'canon_live'
            """
        )
    )
    op.execute(
        sa.text(
            "CREATE INDEX IF NOT EXISTS ix_world_model_pages_project_identity "
            "ON world_model_pages (project_id, page_type, logical_identity_key)"
        )
    )
    op.execute(
        sa.text(
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_world_model_pages_live_identity "
            "ON world_model_pages (project_id, page_type, logical_identity_key) "
            "WHERE status = 'canon_live' AND logical_identity_key <> ''"
        )
    )
