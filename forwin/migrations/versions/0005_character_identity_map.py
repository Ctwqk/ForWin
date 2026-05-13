"""Add canonical character identity map.

Revision ID: 0005_character_identity_map
Revises: 0004_world_model_canonical_pages
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_character_identity_map"
down_revision = "0004_world_model_canonical_pages"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.create_table(
        "character_identity_map",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("canonical_character_id", sa.String(), nullable=False, server_default=""),
        sa.Column("book_state_node_id", sa.String(), nullable=False, server_default=""),
        sa.Column("legacy_entity_id", sa.String(), nullable=False, server_default=""),
        sa.Column("genesis_ref_id", sa.String(), nullable=False, server_default=""),
        sa.Column("roster_item_ids_json", sa.Text(), server_default="[]"),
        sa.Column("aliases_json", sa.Text(), server_default="[]"),
        sa.Column("display_name", sa.Text(), server_default=""),
        sa.Column("status", sa.String(), server_default="active"),
        sa.Column("metadata_json", sa.Text(), server_default="{}"),
        sa.Column("created_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
        sa.Column("updated_at", sa.DateTime(), server_default=sa.text("CURRENT_TIMESTAMP")),
    )
    op.create_index(
        "ix_character_identity_project_canonical",
        "character_identity_map",
        ["project_id", "canonical_character_id"],
    )
    op.create_index(
        "ix_character_identity_project_book_node",
        "character_identity_map",
        ["project_id", "book_state_node_id"],
    )
    op.create_index(
        "ix_character_identity_project_legacy",
        "character_identity_map",
        ["project_id", "legacy_entity_id"],
    )
    op.create_index(
        "ix_character_identity_project_genesis",
        "character_identity_map",
        ["project_id", "genesis_ref_id"],
    )
    op.create_index(
        "ix_character_identity_project_status",
        "character_identity_map",
        ["project_id", "status"],
    )
    op.execute(
        sa.text(
            """
            INSERT INTO character_identity_map (
                id,
                project_id,
                canonical_character_id,
                book_state_node_id,
                legacy_entity_id,
                genesis_ref_id,
                roster_item_ids_json,
                aliases_json,
                display_name,
                status,
                metadata_json
            )
            SELECT
                'char_identity_' || id,
                project_id,
                id,
                id,
                COALESCE((metadata_json::jsonb ->> 'legacy_entity_id'), ''),
                COALESCE((metadata_json::jsonb ->> 'genesis_ref_id'), ''),
                COALESCE((metadata_json::jsonb -> 'roster_item_ids')::text, '[]'),
                CASE
                    WHEN aliases_json IS NULL OR aliases_json = '' OR aliases_json = '[]'
                    THEN jsonb_build_array(name)::text
                    ELSE (aliases_json::jsonb || to_jsonb(name))::text
                END,
                name,
                'active',
                jsonb_build_object('backfilled_from', 'world_nodes')::text
            FROM world_nodes
            WHERE node_type = 'character'
            """
        )
    )


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.drop_index("ix_character_identity_project_status", table_name="character_identity_map")
    op.drop_index("ix_character_identity_project_genesis", table_name="character_identity_map")
    op.drop_index("ix_character_identity_project_legacy", table_name="character_identity_map")
    op.drop_index("ix_character_identity_project_book_node", table_name="character_identity_map")
    op.drop_index("ix_character_identity_project_canonical", table_name="character_identity_map")
    op.drop_table("character_identity_map")
