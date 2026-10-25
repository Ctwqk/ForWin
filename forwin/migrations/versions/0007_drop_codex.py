"""drop Codex trace columns and rewrite retired policy literals

Revision ID: 0007_drop_codex
Revises: 0006_planning_v6
Create Date: 2026-10-25 00:00:00.000000

Rewrites the retired pause-gate and world-conflict decider literals in stored
runtime policies and world-plan conflicts, removes the retired model preference
keys that are no longer accepted by RuntimePolicy, and renames the pause-gate
stage key and delegate task family.

Drops prompt_traces.codex_job_id and prompt_traces.permission_profile. The
downgrade recreates those columns as nullable with an empty-string server
default and reverses the literal rewrites. Removed policy keys are not restored
because their original values are no longer recorded.
"""

import json
from typing import Any, Sequence, Union

from alembic import op
import sqlalchemy as sa


revision: str = "0007_drop_codex"
down_revision: Union[str, Sequence[str], None] = "0006_planning_v6"
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Upgrade schema and stored literals."""
    _rewrite_runtime_policies(downgrade=False)
    _rewrite_world_conflicts(downgrade=False)
    _rewrite_prompt_traces(downgrade=False)
    op.drop_column("prompt_traces", "permission_profile")
    op.drop_column("prompt_traces", "codex_job_id")


def downgrade() -> None:
    """Restore trace columns and reverse the stored literal rewrites."""
    op.add_column(
        "prompt_traces",
        sa.Column("codex_job_id", sa.String(), server_default="", nullable=True),
    )
    op.add_column(
        "prompt_traces",
        sa.Column("permission_profile", sa.String(), server_default="", nullable=True),
    )
    _rewrite_runtime_policies(downgrade=True)
    _rewrite_world_conflicts(downgrade=True)
    _rewrite_prompt_traces(downgrade=True)


def _load_json_object(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, str):
        return None
    try:
        value = json.loads(raw)
    except (TypeError, ValueError):
        return None
    return value if isinstance(value, dict) else None


def _dump_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _rewrite_runtime_policies(*, downgrade: bool) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, runtime_policy_json FROM projects")
    ).all()
    for project_id, raw_policy in rows:
        policy = _load_json_object(raw_policy)
        if policy is None:
            continue
        changed = False

        pause = policy.get("pause")
        if isinstance(pause, dict):
            expected = "spark" if downgrade else "llm"
            retired = "llm" if downgrade else "spark"
            if pause.get("gate_delegate") == retired:
                pause["gate_delegate"] = expected
                changed = True

        world = policy.get("world")
        if isinstance(world, dict):
            expected = "codex" if downgrade else "llm"
            retired = "llm" if downgrade else "codex"
            if world.get("conflict_delegate") == retired:
                world["conflict_delegate"] = expected
                changed = True

        if not downgrade:
            models = policy.get("models")
            if isinstance(models, dict) and "role_preferences" in models:
                models.pop("role_preferences", None)
                changed = True
            review = policy.get("review")
            if isinstance(review, dict) and "repair_models" in review:
                review.pop("repair_models", None)
                changed = True

        if changed:
            bind.execute(
                sa.text(
                    "UPDATE projects SET runtime_policy_json = :policy "
                    "WHERE id = :id"
                ),
                {"policy": _dump_json(policy), "id": project_id},
            )


def _rewrite_world_conflicts(*, downgrade: bool) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text(
            "SELECT id, required_decider, decision_json FROM world_plan_conflicts"
        )
    ).all()
    decider_from = "llm" if downgrade else "codex"
    decider_to = "codex" if downgrade else "llm"
    for conflict_id, required_decider, raw_decision in rows:
        next_decider = decider_to if required_decider == decider_from else required_decider
        decision = _load_json_object(raw_decision)
        next_decision = raw_decision
        if decision is not None and decision.get("decided_by") == decider_from:
            decision["decided_by"] = decider_to
            next_decision = _dump_json(decision)
        if next_decider != required_decider or next_decision != raw_decision:
            bind.execute(
                sa.text(
                    "UPDATE world_plan_conflicts "
                    "SET required_decider = :required_decider, "
                    "decision_json = :decision_json WHERE id = :id"
                ),
                {
                    "required_decider": next_decider,
                    "decision_json": next_decision,
                    "id": conflict_id,
                },
            )


def _rewrite_task_family(value: Any, *, old: str, new: str) -> tuple[Any, bool]:
    if isinstance(value, dict):
        changed = False
        rewritten: dict[Any, Any] = {}
        for key, item in value.items():
            if key == "task_family" and item == old:
                rewritten[key] = new
                changed = True
            else:
                rewritten[key], child_changed = _rewrite_task_family(
                    item, old=old, new=new
                )
                changed = changed or child_changed
        return rewritten, changed
    if isinstance(value, list):
        changed = False
        rewritten_items = []
        for item in value:
            rewritten_item, child_changed = _rewrite_task_family(
                item, old=old, new=new
            )
            rewritten_items.append(rewritten_item)
            changed = changed or child_changed
        return rewritten_items, changed
    return value, False


def _rewrite_prompt_traces(*, downgrade: bool) -> None:
    bind = op.get_bind()
    rows = bind.execute(
        sa.text("SELECT id, stage_key, attempts_json FROM prompt_traces")
    ).all()
    stage_from, stage_to = (
        ("pause_gate", "spark_pause_gate")
        if downgrade
        else ("spark_pause_gate", "pause_gate")
    )
    family_from, family_to = (
        ("delegate", "codex_delegate")
        if downgrade
        else ("codex_delegate", "delegate")
    )
    for trace_id, stage_key, raw_attempts in rows:
        next_stage = stage_to if stage_key == stage_from else stage_key
        next_attempts = raw_attempts
        try:
            attempts = json.loads(raw_attempts) if isinstance(raw_attempts, str) else None
        except ValueError:
            attempts = None
        if attempts is not None:
            attempts, changed = _rewrite_task_family(
                attempts, old=family_from, new=family_to
            )
            if changed:
                next_attempts = _dump_json(attempts)
        if next_stage != stage_key or next_attempts != raw_attempts:
            bind.execute(
                sa.text(
                    "UPDATE prompt_traces SET stage_key = :stage_key, "
                    "attempts_json = :attempts_json WHERE id = :id"
                ),
                {
                    "stage_key": next_stage,
                    "attempts_json": next_attempts,
                    "id": trace_id,
                },
            )
