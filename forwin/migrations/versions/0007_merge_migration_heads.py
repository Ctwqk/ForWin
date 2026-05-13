"""Merge world model and canon quality migration branches.

Revision ID: 0007_merge_migration_heads
Revises: 0005_character_identity_map, 0006_projection_cache_fields
Create Date: 2026-05-13
"""

from __future__ import annotations


revision = "0007_merge_migration_heads"
down_revision = ("0005_character_identity_map", "0006_projection_cache_fields")
branch_labels = None
depends_on = None


def upgrade() -> None:
    pass


def downgrade() -> None:
    pass
