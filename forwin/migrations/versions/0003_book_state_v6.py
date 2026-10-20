"""book state v6

Revision ID: 0003_book_state_v6
Revises: 0002_drop_dead_tables
Create Date: 2026-10-20 18:00:00.000000

Adds the schema the world engine's first phase builds on:

* story_calendars and book_lexicons, one row per book;
* graph_deltas.story_time_start / story_time_end, integer minutes since the
  book's epoch;
* name, aliases_json, profile_json and is_active on world_node_states, so a
  node's identity travels with each state row;
* cognition_overlays.knowledge_by_ref_json;
* narrative_node_index_json / narrative_edge_index_json on world_snapshots;
* title, goal, stakes and payoff_direction on arc_plan_versions.

Every new NOT NULL column carries a server default so add_column works on
populated tables. downgrade() removes everything in reverse order.
"""

from typing import Sequence, Union

from alembic import op
import sqlalchemy as sa


# revision identifiers, used by Alembic.
revision: str = "0003_book_state_v6"
down_revision: Union[str, Sequence[str], None] = "0002_drop_dead_tables"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema."""
    op.create_table(
        "story_calendars",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("epoch_label", sa.Text(), nullable=False),
        sa.Column("opening_minute", sa.BigInteger(), nullable=False),
        sa.Column("units_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_story_calendars_project",
        "story_calendars",
        ["project_id"],
        unique=True,
    )
    op.create_table(
        "book_lexicons",
        sa.Column("id", sa.String(), nullable=False),
        sa.Column("project_id", sa.String(), nullable=False),
        sa.Column("lexicon_json", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(), nullable=False),
        sa.Column("updated_at", sa.DateTime(), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ux_book_lexicons_project",
        "book_lexicons",
        ["project_id"],
        unique=True,
    )

    op.add_column(
        "graph_deltas",
        sa.Column("story_time_start", sa.BigInteger(), nullable=True),
    )
    op.add_column(
        "graph_deltas",
        sa.Column("story_time_end", sa.BigInteger(), nullable=True),
    )

    op.add_column(
        "world_node_states",
        sa.Column("name", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "world_node_states",
        sa.Column("aliases_json", sa.Text(), server_default="[]", nullable=False),
    )
    op.add_column(
        "world_node_states",
        sa.Column("profile_json", sa.Text(), server_default="{}", nullable=False),
    )
    op.add_column(
        "world_node_states",
        sa.Column("is_active", sa.Boolean(), server_default=sa.true(), nullable=False),
    )

    op.add_column(
        "cognition_overlays",
        sa.Column(
            "knowledge_by_ref_json", sa.Text(), server_default="{}", nullable=False
        ),
    )

    op.add_column(
        "world_snapshots",
        sa.Column(
            "narrative_node_index_json", sa.Text(), server_default="{}", nullable=False
        ),
    )
    op.add_column(
        "world_snapshots",
        sa.Column(
            "narrative_edge_index_json", sa.Text(), server_default="{}", nullable=False
        ),
    )

    op.add_column(
        "arc_plan_versions",
        sa.Column("title", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "arc_plan_versions",
        sa.Column("goal", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "arc_plan_versions",
        sa.Column("stakes", sa.Text(), server_default="", nullable=False),
    )
    op.add_column(
        "arc_plan_versions",
        sa.Column("payoff_direction", sa.Text(), server_default="", nullable=False),
    )


def downgrade() -> None:
    """Downgrade schema."""
    op.drop_column("arc_plan_versions", "payoff_direction")
    op.drop_column("arc_plan_versions", "stakes")
    op.drop_column("arc_plan_versions", "goal")
    op.drop_column("arc_plan_versions", "title")

    op.drop_column("world_snapshots", "narrative_edge_index_json")
    op.drop_column("world_snapshots", "narrative_node_index_json")

    op.drop_column("cognition_overlays", "knowledge_by_ref_json")

    op.drop_column("world_node_states", "is_active")
    op.drop_column("world_node_states", "profile_json")
    op.drop_column("world_node_states", "aliases_json")
    op.drop_column("world_node_states", "name")

    op.drop_column("graph_deltas", "story_time_end")
    op.drop_column("graph_deltas", "story_time_start")

    op.drop_index("ux_book_lexicons_project", table_name="book_lexicons")
    op.drop_table("book_lexicons")
    op.drop_index("ux_story_calendars_project", table_name="story_calendars")
    op.drop_table("story_calendars")
