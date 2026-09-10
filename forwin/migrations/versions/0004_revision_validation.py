"""Durable full-suffix validation evidence; preserve all existing acceptance rows."""

import sqlalchemy as sa
from alembic import op

revision = "0004_revision_validation"
down_revision = "0003_serial_capacity"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "canon_admission_runs",
        sa.Column("projection_json", sa.Text(), nullable=False, server_default=""),
    )
    op.add_column(
        "canon_admission_runs",
        sa.Column(
            "projection_fingerprint", sa.String(64), nullable=False, server_default=""
        ),
    )
    op.create_table(
        "canon_quality_acceptance_evidence",
        sa.Column(
            "acceptance_id",
            sa.String(),
            sa.ForeignKey("canon_commit_records.id"),
            primary_key=True,
        ),
        sa.Column(
            "project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column("chapter_number", sa.Integer(), nullable=False),
        sa.Column(
            "draft_id", sa.String(), sa.ForeignKey("chapter_drafts.id"), nullable=False
        ),
        sa.Column("body_sha256", sa.String(64), nullable=False),
        sa.Column(
            "source_admission_run_id",
            sa.String(),
            sa.ForeignKey("canon_admission_runs.id"),
            nullable=True,
        ),
        sa.Column("evidence_json", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index(
        "ix_canon_quality_acceptance_project_chapter",
        "canon_quality_acceptance_evidence",
        ["project_id", "chapter_number"],
    )
    op.create_table(
        "canon_revision_validations",
        sa.Column("id", sa.String(), primary_key=True),
        sa.Column(
            "project_id", sa.String(), sa.ForeignKey("projects.id"), nullable=False
        ),
        sa.Column(
            "candidate_id",
            sa.String(),
            sa.ForeignKey("candidate_draft_records.id"),
            nullable=False,
        ),
        sa.Column("base_book_revision", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(), nullable=False),
        sa.Column("result_json", sa.Text(), nullable=False),
        sa.Column("accepted_book_revision", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(), server_default=sa.func.now(), nullable=True
        ),
    )
    op.create_index(
        "ix_canon_revision_candidate",
        "canon_revision_validations",
        ["project_id", "candidate_id", "created_at"],
    )


def downgrade():
    if op.get_bind().scalar(
        sa.text("SELECT count(*) FROM canon_revision_validations")
    ) or op.get_bind().scalar(
        sa.text("SELECT count(*) FROM canon_quality_acceptance_evidence")
    ):
        raise ValueError("cannot discard durable revision validation evidence")
    op.drop_index(
        "ix_canon_revision_candidate", table_name="canon_revision_validations"
    )
    op.drop_table("canon_revision_validations")
    op.drop_table("canon_quality_acceptance_evidence")
    op.drop_column("canon_admission_runs", "projection_fingerprint")
    op.drop_column("canon_admission_runs", "projection_json")
