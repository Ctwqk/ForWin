from __future__ import annotations

import sqlalchemy as sa
from alembic import op


revision = "0016_project_progression_rules"
down_revision = "0015_arc_macro_progression"
branch_labels = None
depends_on = None


def upgrade() -> None:
    op.create_table(
        "project_progression_rules",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
        sa.Column("rule_type", sa.String(), nullable=False),
        sa.Column("chapter_start", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("chapter_end", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("severity", sa.String(), nullable=False, server_default="warning"),
        sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
        sa.Column("active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("created_at", sa.DateTime(), nullable=True),
    )
    op.create_index(
        "ix_project_progression_rules_project_range",
        "project_progression_rules",
        ["project_id", "chapter_start", "chapter_end"],
    )
    op.create_index(
        "ix_project_progression_rules_project_type",
        "project_progression_rules",
        ["project_id", "rule_type"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_project_progression_rules_project_type",
        table_name="project_progression_rules",
    )
    op.drop_index(
        "ix_project_progression_rules_project_range",
        table_name="project_progression_rules",
    )
    op.drop_table("project_progression_rules")
