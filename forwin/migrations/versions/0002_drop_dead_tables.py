"""drop dead tables

Revision ID: 0002_drop_dead_tables
Revises: 0001_v5_recovery
Create Date: 2026-10-20 12:00:00.000000

Drops five tables that no runtime code reads or writes any more:
world_projection_deltas, story_obligations, reveal_registry_entries,
artifact_collection_ledgers and book_reader_experience_deltas.
book_reader_promises is intentionally kept (approved world-edit proposals
still write it).
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0002_drop_dead_tables"
down_revision: Union[str, Sequence[str], None] = "0001_v5_recovery"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.drop_index(
        "ix_world_projection_deltas_project_projection",
        table_name="world_projection_deltas",
    )
    op.drop_table("world_projection_deltas")

    op.drop_index("ix_story_obligations_project_status", table_name="story_obligations")
    op.drop_index(
        "ix_story_obligations_project_deadline", table_name="story_obligations"
    )
    op.drop_table("story_obligations")

    op.drop_index(
        "ix_reveal_registry_project_status", table_name="reveal_registry_entries"
    )
    op.drop_index(
        "ix_reveal_registry_project_key", table_name="reveal_registry_entries"
    )
    op.drop_table("reveal_registry_entries")

    op.drop_index(
        "ix_artifact_ledgers_project_collection",
        table_name="artifact_collection_ledgers",
    )
    op.drop_table("artifact_collection_ledgers")

    op.drop_index(
        "ux_book_reader_exp_deltas_project_delta",
        table_name="book_reader_experience_deltas",
    )
    op.drop_index(
        "ix_book_reader_exp_deltas_project_payoff",
        table_name="book_reader_experience_deltas",
    )
    op.drop_index(
        "ix_book_reader_exp_deltas_project_chapter",
        table_name="book_reader_experience_deltas",
    )
    op.drop_table("book_reader_experience_deltas")


def downgrade() -> None:
    """Downgrade schema (recreates the tables exactly as 0001 defined them)."""
    op.create_table(
        "artifact_collection_ledgers",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("collection_key", sa.String(), nullable=False),
        sa.Column("collection_name", sa.String(), nullable=False),
        sa.Column("target_total", sa.Integer(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("mentioned_index", sa.Integer(), nullable=True),
        sa.Column("mentioned_remaining", sa.Integer(), nullable=True),
        sa.Column("collected_count_after", sa.Integer(), nullable=False),
        sa.Column("new_items_json", sa.Text(), nullable=False),
        sa.Column("consumed_items_json", sa.Text(), nullable=False),
        sa.Column("evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_artifact_ledgers_project_collection",
        "artifact_collection_ledgers",
        ["project_id", "collection_key", "chapter_number"],
        unique=False,
    )

    op.create_table(
        "book_reader_experience_deltas",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("reader_experience_delta_id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("reader_state_before", sa.Text(), nullable=False),
        sa.Column("reader_state_after", sa.Text(), nullable=False),
        sa.Column("cognition_transition", sa.Text(), nullable=False),
        sa.Column("payoff_type", sa.String(), nullable=False),
        sa.Column("reward_tags_json", sa.Text(), nullable=False),
        sa.Column("emotional_effect", sa.Text(), nullable=False),
        sa.Column("promise_debt_change", sa.Integer(), nullable=False),
        sa.Column("next_desire", sa.Text(), nullable=False),
        sa.Column("fairness_evidence_json", sa.Text(), nullable=False),
        sa.Column("source_refs_json", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_book_reader_exp_deltas_project_chapter",
        "book_reader_experience_deltas",
        ["project_id", "chapter_number"],
        unique=False,
    )
    op.create_index(
        "ix_book_reader_exp_deltas_project_payoff",
        "book_reader_experience_deltas",
        ["project_id", "payoff_type"],
        unique=False,
    )
    op.create_index(
        "ux_book_reader_exp_deltas_project_delta",
        "book_reader_experience_deltas",
        ["project_id", "reader_experience_delta_id"],
        unique=True,
    )

    op.create_table(
        "reveal_registry_entries",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("reveal_key", sa.String(), nullable=False),
        sa.Column("claim_summary", sa.Text(), nullable=False),
        sa.Column("first_revealed_chapter", sa.Integer(), nullable=False),
        sa.Column("latest_chapter", sa.Integer(), nullable=False),
        sa.Column("repeat_count", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("subject_refs_json", sa.Text(), nullable=False),
        sa.Column("evidence_refs_json", sa.Text(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_reveal_registry_project_key",
        "reveal_registry_entries",
        ["project_id", "reveal_key"],
        unique=False,
    )
    op.create_index(
        "ix_reveal_registry_project_status",
        "reveal_registry_entries",
        ["project_id", "status", "latest_chapter"],
        unique=False,
    )

    op.create_table(
        "story_obligations",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("obligation_type", sa.String(), nullable=False),
        sa.Column("summary", sa.Text(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("priority", sa.String(), nullable=False),
        sa.Column("started_at_chapter", sa.Integer(), nullable=False),
        sa.Column("deadline_chapter", sa.Integer(), nullable=False),
        sa.Column("resolved_at_chapter", sa.Integer(), nullable=False),
        sa.Column("source_signal_id", sa.String(), nullable=False),
        sa.Column("resolution_signal_id", sa.String(), nullable=False),
        sa.Column("payload_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_story_obligations_project_deadline",
        "story_obligations",
        ["project_id", "deadline_chapter"],
        unique=False,
    )
    op.create_index(
        "ix_story_obligations_project_status",
        "story_obligations",
        ["project_id", "status", "priority"],
        unique=False,
    )

    op.create_table(
        "world_projection_deltas",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("projection_id", sa.String(), nullable=False),
        sa.Column("projection_layer", sa.String(), nullable=False),
        sa.Column("delta_id", sa.String(), nullable=False),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column("world_delta_json", sa.Text(), nullable=False),
        sa.Column("promoted_compile_run_id", sa.String(), nullable=False),
        sa.Column("promotion_review_verdict_id", sa.String(), nullable=False),
        sa.Column("promotion_reason", sa.Text(), nullable=False),
        sa.Column("metadata_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_world_projection_deltas_project_projection",
        "world_projection_deltas",
        ["project_id", "projection_id", "projection_layer"],
        unique=False,
    )
