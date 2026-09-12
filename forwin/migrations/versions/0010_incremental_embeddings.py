"""Add revision checkpoints and immutable embedding cache; preserve old data."""
from alembic import op
import sqlalchemy as sa

revision = "0010_incremental_embeddings"
down_revision = "0009_knowledge_dependencies"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("projection_checkpoints", sa.Column("target_book_revision", sa.Integer(), nullable=True))
    op.add_column("projection_checkpoints", sa.Column("projected_book_revision", sa.Integer(), nullable=True))
    op.create_index("ix_canon_commits_project_base_revision", "canon_commit_records", ["project_id", "base_book_revision"])
    op.create_table("embedding_cache_entries",
        sa.Column("input_hash", sa.String(64), primary_key=True),
        sa.Column("embedding_identity", sa.String(64), primary_key=True),
        sa.Column("dimensions", sa.Integer(), nullable=False),
        sa.Column("vector_json", sa.Text(), nullable=False))


def downgrade():
    raise RuntimeError("Cannot discard projection revision provenance or durable embeddings")
