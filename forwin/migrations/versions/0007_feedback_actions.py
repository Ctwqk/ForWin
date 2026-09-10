"""Separate feedback proposals, selection, prompt inputs and later observations."""

import sqlalchemy as sa
from alembic import op

revision = "0007_feedback_actions"
down_revision = "0006_feedback_aggregation"
branch_labels = None
depends_on = None

_STRINGS = {"direction": "unknown", "aggregate_id": "", "status": "proposed"}
_JSON = {
    "aggregate_evidence_json": "{}",
    "action_payload_json": "{}",
    "prompt_inclusions_json": "[]",
    "plan_application_json": "{}",
    "body_observation_json": "{}",
    "effect_observation_json": "{}",
}
_INTS = (
    "target_chapter_start",
    "target_chapter_end",
    "hint_valid_from_chapter",
    "hint_expires_at_chapter",
)


def upgrade():
    for name, default in _STRINGS.items():
        op.add_column(
            "feedback_action_records",
            sa.Column(name, sa.String(), nullable=False, server_default=default),
        )
    for name, default in _JSON.items():
        op.add_column(
            "feedback_action_records",
            sa.Column(name, sa.Text(), nullable=False, server_default=default),
        )
    for name in _INTS:
        op.add_column(
            "feedback_action_records",
            sa.Column(name, sa.Integer(), nullable=False, server_default="0"),
        )
    op.add_column(
        "feedback_action_records",
        sa.Column(
            "source_qualified", sa.Boolean(), nullable=False, server_default=sa.false()
        ),
    )
    op.add_column(
        "feedback_action_records",
        sa.Column("selected_at_chapter", sa.Integer(), nullable=True),
    )
    op.add_column(
        "feedback_action_records",
        sa.Column("selected_at", sa.DateTime(), nullable=True),
    )
    # No historical title, note or old cooldown proves a versioned selection.


def downgrade():
    table = sa.table(
        "feedback_action_records",
        *[
            sa.column(name)
            for name in (
                *_STRINGS,
                *_JSON,
                *_INTS,
                "source_qualified",
                "selected_at_chapter",
                "selected_at",
            )
        ],
    )
    nondefault = [
        table.c[name] != default for name, default in (_STRINGS | _JSON).items()
    ]
    nondefault += [table.c[name] != 0 for name in _INTS]
    nondefault += [
        table.c.source_qualified.is_(True),
        table.c.selected_at_chapter.is_not(None),
        table.c.selected_at.is_not(None),
    ]
    if op.get_bind().scalar(
        sa.select(sa.func.count()).select_from(table).where(sa.or_(*nondefault))
    ):
        raise RuntimeError(
            "Cannot discard feedback action lifecycle evidence; retain this forward schema"
        )
    for name in reversed(
        (
            *_STRINGS,
            *_JSON,
            *_INTS,
            "source_qualified",
            "selected_at_chapter",
            "selected_at",
        )
    ):
        op.drop_column("feedback_action_records", name)
