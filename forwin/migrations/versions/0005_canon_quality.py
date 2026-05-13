"""Add canon quality gate tables.

Revision ID: 0005_canon_quality
Revises: 0004_legacy_checkpoint_statuses
Create Date: 2026-05-12
"""

from __future__ import annotations

from alembic import op
import sqlalchemy as sa


revision = "0005_canon_quality"
down_revision = "0004_legacy_checkpoint_statuses"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    inspector = sa.inspect(bind)
    if not inspector.has_table("canon_quality_signals"):
        op.create_table(
            "canon_quality_signals",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("draft_id", sa.String(), nullable=False, server_default=""),
            sa.Column("signal_id", sa.String(), nullable=False, server_default=""),
            sa.Column("signal_type", sa.String(), nullable=False, server_default=""),
            sa.Column("severity", sa.String(), nullable=False, server_default="warning"),
            sa.Column("target_scope", sa.String(), nullable=False, server_default="chapter"),
            sa.Column("subject_key", sa.String(), nullable=False, server_default=""),
            sa.Column("description", sa.Text(), nullable=False, server_default=""),
            sa.Column("evidence_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("span_start", sa.Integer(), nullable=True),
            sa.Column("span_end", sa.Integer(), nullable=True),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("status", sa.String(), nullable=False, server_default="open"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("resolved_at", sa.DateTime(), nullable=True),
        )
    if not inspector.has_table("story_obligations"):
        op.create_table(
            "story_obligations",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("obligation_type", sa.String(), nullable=False, server_default="hook"),
            sa.Column("summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("status", sa.String(), nullable=False, server_default="open"),
            sa.Column("priority", sa.String(), nullable=False, server_default="P1"),
            sa.Column("started_at_chapter", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("deadline_chapter", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("resolved_at_chapter", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("source_signal_id", sa.String(), nullable=False, server_default=""),
            sa.Column("resolution_signal_id", sa.String(), nullable=False, server_default=""),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    if not inspector.has_table("character_state_transitions"):
        op.create_table(
            "character_state_transitions",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("character_id", sa.String(), nullable=False, server_default=""),
            sa.Column("character_name", sa.String(), nullable=False, server_default=""),
            sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("transition_type", sa.String(), nullable=False, server_default=""),
            sa.Column("from_state", sa.String(), nullable=False, server_default=""),
            sa.Column("to_state", sa.String(), nullable=False, server_default=""),
            sa.Column("terminality", sa.String(), nullable=False, server_default="none"),
            sa.Column("can_participate", sa.String(), nullable=False, server_default="true"),
            sa.Column("requires_bridge_from_transition_id", sa.String(), nullable=False, server_default=""),
            sa.Column("bridge_event_id", sa.String(), nullable=False, server_default=""),
            sa.Column("evidence_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    if not inspector.has_table("artifact_collection_ledgers"):
        op.create_table(
            "artifact_collection_ledgers",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("collection_key", sa.String(), nullable=False, server_default="main"),
            sa.Column("collection_name", sa.String(), nullable=False, server_default="core_artifacts"),
            sa.Column("target_total", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("mentioned_index", sa.Integer(), nullable=True),
            sa.Column("mentioned_remaining", sa.Integer(), nullable=True),
            sa.Column("collected_count_after", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("new_items_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("consumed_items_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("evidence_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("confidence", sa.Float(), nullable=False, server_default="1.0"),
            sa.Column("status", sa.String(), nullable=False, server_default="consistent"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    if not inspector.has_table("countdown_ledgers"):
        op.create_table(
            "countdown_ledgers",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("countdown_key", sa.String(), nullable=False, server_default="main"),
            sa.Column("label", sa.String(), nullable=False, server_default="main"),
            sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("normalized_remaining_minutes", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("raw_mention", sa.String(), nullable=False, server_default=""),
            sa.Column("is_reset_event", sa.String(), nullable=False, server_default="false"),
            sa.Column("is_branch_clock", sa.String(), nullable=False, server_default="false"),
            sa.Column("is_resolution_event", sa.String(), nullable=False, server_default="false"),
            sa.Column("previous_remaining_minutes", sa.Integer(), nullable=True),
            sa.Column("status", sa.String(), nullable=False, server_default="consistent"),
            sa.Column("evidence_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    if not inspector.has_table("reveal_registry_entries"):
        op.create_table(
            "reveal_registry_entries",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("reveal_key", sa.String(), nullable=False, server_default=""),
            sa.Column("claim_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("first_revealed_chapter", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("latest_chapter", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("repeat_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("status", sa.String(), nullable=False, server_default="new"),
            sa.Column("subject_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("evidence_refs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("payload_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
            sa.Column("updated_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    if not inspector.has_table("chapter_body_metrics"):
        op.create_table(
            "chapter_body_metrics",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("draft_id", sa.String(), nullable=False, server_default=""),
            sa.Column("paragraph_hashes_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("dialogue_fingerprints_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("scene_fingerprints_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("duplicate_spans_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("style_motifs_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("metrics_json", sa.Text(), nullable=False, server_default="{}"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )
    if not inspector.has_table("canon_admission_runs"):
        op.create_table(
            "canon_admission_runs",
            sa.Column("id", sa.String(), primary_key=True),
            sa.Column("project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False),
            sa.Column("chapter_number", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("draft_id", sa.String(), nullable=False, server_default=""),
            sa.Column("review_id", sa.String(), nullable=False, server_default=""),
            sa.Column("commit_allowed", sa.String(), nullable=False, server_default="false"),
            sa.Column("verdict", sa.String(), nullable=False, server_default="warn"),
            sa.Column("blocking_issue_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("warning_issue_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("gate_summary", sa.Text(), nullable=False, server_default=""),
            sa.Column("signals_json", sa.Text(), nullable=False, server_default="[]"),
            sa.Column("created_at", sa.DateTime(), nullable=False, server_default=sa.text("CURRENT_TIMESTAMP")),
        )

    for sql in (
        "CREATE INDEX IF NOT EXISTS ix_canon_quality_signals_project_status ON canon_quality_signals (project_id, status, created_at)",
        "CREATE INDEX IF NOT EXISTS ix_canon_quality_signals_project_chapter ON canon_quality_signals (project_id, chapter_number)",
        "CREATE INDEX IF NOT EXISTS ix_canon_quality_signals_project_type ON canon_quality_signals (project_id, signal_type)",
        "CREATE INDEX IF NOT EXISTS ix_story_obligations_project_status ON story_obligations (project_id, status, priority)",
        "CREATE INDEX IF NOT EXISTS ix_story_obligations_project_deadline ON story_obligations (project_id, deadline_chapter)",
        "CREATE INDEX IF NOT EXISTS ix_character_state_transitions_project_character ON character_state_transitions (project_id, character_name, chapter_number)",
        "CREATE INDEX IF NOT EXISTS ix_character_state_transitions_project_terminal ON character_state_transitions (project_id, terminality, chapter_number)",
        "CREATE INDEX IF NOT EXISTS ix_artifact_ledgers_project_collection ON artifact_collection_ledgers (project_id, collection_key, chapter_number)",
        "CREATE INDEX IF NOT EXISTS ix_countdown_ledgers_project_key ON countdown_ledgers (project_id, countdown_key, chapter_number)",
        "CREATE INDEX IF NOT EXISTS ix_countdown_ledgers_project_status ON countdown_ledgers (project_id, status, chapter_number)",
        "CREATE INDEX IF NOT EXISTS ix_reveal_registry_project_key ON reveal_registry_entries (project_id, reveal_key)",
        "CREATE INDEX IF NOT EXISTS ix_reveal_registry_project_status ON reveal_registry_entries (project_id, status, latest_chapter)",
        "CREATE INDEX IF NOT EXISTS ix_chapter_body_metrics_project_chapter ON chapter_body_metrics (project_id, chapter_number)",
        "CREATE INDEX IF NOT EXISTS ix_canon_admission_runs_project_chapter ON canon_admission_runs (project_id, chapter_number, created_at)",
    ):
        op.execute(sa.text(sql))


def downgrade() -> None:
    for table in (
        "canon_admission_runs",
        "chapter_body_metrics",
        "reveal_registry_entries",
        "countdown_ledgers",
        "artifact_collection_ledgers",
        "character_state_transitions",
        "story_obligations",
        "canon_quality_signals",
    ):
        op.drop_table(table)
