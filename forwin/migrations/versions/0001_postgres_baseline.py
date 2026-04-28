"""Postgres baseline schema.

Revision ID: 0001_postgres_baseline
Revises:
Create Date: 2026-04-27
"""

from __future__ import annotations

from alembic import op

from forwin import models as _models  # noqa: F401
from forwin.models.base import Base


revision = "0001_postgres_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade() -> None:
    Base.metadata.create_all(bind=op.get_bind())


def downgrade() -> None:
    Base.metadata.drop_all(bind=op.get_bind())
