"""drop parallel ledgers

Revision ID: 0004_drop_parallel_ledgers
Revises: 0003_book_state_v6
Create Date: 2026-10-21 12:00:00.000000

Drops the canon_quality countdown and character-state ledgers,
countdown_ledgers and character_state_transitions. Their facts live in the
BookState world ledger now: clock nodes carry deadlines, character nodes carry
alive_status, restraint and location_id. The chapter review form reads its
rows from BookState and no code writes these tables any more.

downgrade() recreates both tables empty, exactly as 0001 defined them.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0004_drop_parallel_ledgers"
down_revision: Union[str, Sequence[str], None] = "0003_book_state_v6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index("ix_countdown_ledgers_project_status", table_name="countdown_ledgers")
    op.drop_index("ix_countdown_ledgers_project_key", table_name="countdown_ledgers")
    op.drop_table("countdown_ledgers")

    op.drop_index(
        "ix_character_state_transitions_project_terminal",
        table_name="character_state_transitions",
    )
    op.drop_index(
        "ix_character_state_transitions_project_character",
        table_name="character_state_transitions",
    )
    op.drop_table("character_state_transitions")


def downgrade() -> None:
    """Downgrade schema (recreates the tables exactly as 0001 defined them)."""
    op.create_table(
        "character_state_transitions",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("character_id", sa.String(), nullable=False),
        sa.Column("character_name", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("transition_type", sa.String(), nullable=False),
        sa.Column("from_state", sa.String(), nullable=False),
        sa.Column("to_state", sa.String(), nullable=False),
        sa.Column("terminality", sa.String(), nullable=False),
        sa.Column("can_participate", sa.String(), nullable=False),
        sa.Column("requires_bridge_from_transition_id", sa.String(), nullable=False),
        sa.Column("bridge_event_id", sa.String(), nullable=False),
        sa.Column("evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_character_state_transitions_project_character",
        "character_state_transitions",
        ["project_id", "character_name", "chapter_number"],
        unique=False,
    )
    op.create_index(
        "ix_character_state_transitions_project_terminal",
        "character_state_transitions",
        ["project_id", "terminality", "chapter_number"],
        unique=False,
    )

    op.create_table(
        "countdown_ledgers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("countdown_key", sa.String(), nullable=False),
        sa.Column("label", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("normalized_remaining_minutes", sa.Integer(), nullable=False),
        sa.Column("raw_mention", sa.String(), nullable=False),
        sa.Column("is_reset_event", sa.String(), nullable=False),
        sa.Column("is_branch_clock", sa.String(), nullable=False),
        sa.Column("is_resolution_event", sa.String(), nullable=False),
        sa.Column("previous_remaining_minutes", sa.Integer(), nullable=True),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_countdown_ledgers_project_key",
        "countdown_ledgers",
        ["project_id", "countdown_key", "chapter_number"],
        unique=False,
    )
    op.create_index(
        "ix_countdown_ledgers_project_status",
        "countdown_ledgers",
        ["project_id", "status", "chapter_number"],
        unique=False,
    )
