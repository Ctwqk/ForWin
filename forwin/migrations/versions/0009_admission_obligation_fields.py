"""Add obligation audit fields to canon admission runs.

Revision ID: 0009_admission_obligation_fields
Revises: 0008_narrative_obligations
Create Date: 2026-05-13
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0009_admission_obligation_fields"
down_revision = "0008_narrative_obligations"
branch_labels = None
depends_on = None


def upgrade() -> None:
    for sql in (
        "ALTER TABLE canon_admission_runs ADD COLUMN IF NOT EXISTS admission_mode VARCHAR NOT NULL DEFAULT 'clean'",
        "ALTER TABLE canon_admission_runs ADD COLUMN IF NOT EXISTS obligation_ids_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE canon_admission_runs ADD COLUMN IF NOT EXISTS required_plan_patch_ids_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE canon_admission_runs ADD COLUMN IF NOT EXISTS blocking_reasons_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE canon_admission_runs ADD COLUMN IF NOT EXISTS expired_obligation_ids_json TEXT NOT NULL DEFAULT '[]'",
        "ALTER TABLE canon_admission_runs ADD COLUMN IF NOT EXISTS over_budget VARCHAR NOT NULL DEFAULT 'false'",
    ):
        op.execute(sa.text(sql))


def downgrade() -> None:
    for column in (
        "over_budget",
        "expired_obligation_ids_json",
        "blocking_reasons_json",
        "required_plan_patch_ids_json",
        "obligation_ids_json",
        "admission_mode",
    ):
        op.drop_column("canon_admission_runs", column)
