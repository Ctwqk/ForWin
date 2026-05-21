"""Remove legacy character identity bridge.

Revision ID: 0011_no_legacy_char_identity
Revises: 0010_future_plan_audit
Create Date: 2026-05-21
"""

from __future__ import annotations

from alembic import op


revision = "0011_no_legacy_char_identity"
down_revision = "0010_future_plan_audit"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute("DROP INDEX IF EXISTS ix_character_identity_project_legacy")
    op.execute("ALTER TABLE character_identity_map DROP COLUMN IF EXISTS legacy_entity_id")
    op.execute("ALTER TABLE relation_edges DROP CONSTRAINT IF EXISTS relation_edges_source_entity_id_fkey")
    op.execute("ALTER TABLE relation_edges DROP CONSTRAINT IF EXISTS relation_edges_target_entity_id_fkey")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.dialect.name != "postgresql":
        return
    op.execute(
        "ALTER TABLE character_identity_map "
        "ADD COLUMN IF NOT EXISTS legacy_entity_id VARCHAR NOT NULL DEFAULT ''"
    )
    op.execute(
        "CREATE INDEX IF NOT EXISTS ix_character_identity_project_legacy "
        "ON character_identity_map (project_id, legacy_entity_id)"
    )
    op.execute(
        "ALTER TABLE relation_edges "
        "ADD CONSTRAINT relation_edges_source_entity_id_fkey "
        "FOREIGN KEY (source_entity_id) REFERENCES entities(id) NOT VALID"
    )
    op.execute(
        "ALTER TABLE relation_edges "
        "ADD CONSTRAINT relation_edges_target_entity_id_fkey "
        "FOREIGN KEY (target_entity_id) REFERENCES entities(id) NOT VALID"
    )
