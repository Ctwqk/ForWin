"""Keep existing projections unknown until regenerated from proven inputs."""

from alembic import op
import sqlalchemy as sa

revision = "0009_knowledge_dependencies"
down_revision = "0008_generation_continuation"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "knowledge_projection_pages",
        sa.Column(
            "dependency_manifest_json", sa.Text(), nullable=False, server_default="{}"
        ),
    )


def downgrade():
    raise RuntimeError("Cannot discard derived source provenance")
