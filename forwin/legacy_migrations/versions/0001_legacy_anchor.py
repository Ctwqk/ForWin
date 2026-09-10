"""Input-only anchor for the schema deployed from 57241ff0e4e2.

The original migration is not rerun or redefined here. This anchor permits
Alembic to recognize an existing revision; it cannot create that revision.
"""

revision = "0001_v5_baseline"
down_revision = None
branch_labels = None
depends_on = None


def upgrade():
    raise RuntimeError(
        "Legacy bridge requires an existing legacy database; use the main migrations for a fresh database."
    )


def downgrade():
    raise RuntimeError(
        "Legacy bridge is forward-only; restore a verified backup for rollback."
    )
