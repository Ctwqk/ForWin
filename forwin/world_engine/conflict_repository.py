from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_conflict import WorldPlanConflictRow

from .conflict_types import ConflictDecider, ConflictDecision, ConflictDraft, ConflictOption


# The result_refs prefix of a plan patch a band plan wrote for a resolution; P3 resolutions list bare patch ids.
PLAN_PATCH_REF_PREFIX = "plan_patch:"


def serialize_conflict_decision(payload: dict) -> str:
    """Compact JSON required by actor_turns' veto SQL LIKE prefilter."""
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


class ConflictNotOpen(RuntimeError):
    pass


class WorldConflictRepository:
    def __init__(self, session: Session) -> None:
        self.session = session

    def create(
        self,
        draft: ConflictDraft,
        *,
        required_decider: ConflictDecider,
        options: Sequence[ConflictOption],
    ) -> WorldPlanConflictRow:
        row = WorldPlanConflictRow(
            project_id=draft.project_id,
            source=draft.source,
            detected_after_chapter=draft.detected_after_chapter,
            story_time=draft.story_time,
            involved_refs_json=json.dumps(draft.involved_refs, ensure_ascii=False),
            violated=draft.violated,
            proposed_change_json=json.dumps(draft.proposed_change, ensure_ascii=False),
            impact_level=draft.impact_level,
            options_json=json.dumps(list(options)),
            required_decider=required_decider,
            blocks_from_chapter=draft.blocks_from_chapter,
            status="open",
            summary=draft.summary,
            intent_ref=draft.intent_ref,
            actor_ref=draft.actor_ref,
        )
        self.session.add(row)
        self.session.flush()
        return row

    def get(
        self, project_id: str, conflict_id: str, *, for_update: bool = False
    ) -> WorldPlanConflictRow | None:
        statement = select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == project_id,
            WorldPlanConflictRow.id == conflict_id,
        )
        if for_update:
            statement = statement.with_for_update().execution_options(populate_existing=True)
        return self.session.scalar(statement)

    def list(self, project_id: str, *, status: str | None = None) -> list[WorldPlanConflictRow]:
        statement = select(WorldPlanConflictRow).where(WorldPlanConflictRow.project_id == project_id)
        if status is not None:
            statement = statement.where(WorldPlanConflictRow.status == status)
        return list(self.session.scalars(statement.order_by(WorldPlanConflictRow.created_at.desc(), WorldPlanConflictRow.id)))

    def open_blocking(self, project_id: str, *, next_chapter: int) -> WorldPlanConflictRow | None:
        return self.session.scalar(
            select(WorldPlanConflictRow)
            .where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.status == "open",
                WorldPlanConflictRow.required_decider == "human",
                WorldPlanConflictRow.blocks_from_chapter <= next_chapter,
            )
            .order_by(WorldPlanConflictRow.blocks_from_chapter, WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
            .limit(1)
        )

    def mark_resolved(
        self,
        row: WorldPlanConflictRow,
        *,
        decision: ConflictDecision,
        result_refs: Sequence[str],
        pending_plan_response: bool = False,
    ) -> None:
        if row.status != "open":
            raise ConflictNotOpen(f"Conflict {row.id} is {row.status}")
        payload = decision.model_dump(mode="json")
        if pending_plan_response:
            payload["pending_plan_response"] = True
        row.decision_json = serialize_conflict_decision(payload)
        row.result_refs_json = json.dumps(list(result_refs), ensure_ascii=False)
        row.status = "resolved"
        self.session.flush()

    def clear_pending_plan_response(self, row: WorldPlanConflictRow) -> None:
        """Retire a response whose world change left canon, retaining the resolution record."""
        payload = json.loads(row.decision_json or "{}")
        if payload.get("pending_plan_response") is True:
            payload["pending_plan_response"] = False
            row.decision_json = serialize_conflict_decision(payload)
            self.session.flush()

    def pending_plot_responses(self, project_id: str) -> list[WorldPlanConflictRow]:
        """Resolved plot_responds conflicts still waiting for a plan to answer them, oldest first.

        A resolution waits when it found no plan to bind (``pending_plan_response`` in its decision) and no
        band plan has bound it since (no ``plan_patch:`` ref).
        """
        rows = self.session.scalars(
            select(WorldPlanConflictRow)
            .where(WorldPlanConflictRow.project_id == project_id, WorldPlanConflictRow.status == "resolved")
            .order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
        )
        pending = []
        for row in rows:
            decision = json.loads(row.decision_json or "{}")
            if (decision.get("option") == "plot_responds" and decision.get("pending_plan_response") is True
                    and not any(ref.startswith(PLAN_PATCH_REF_PREFIX) for ref in conflict_result_refs(row))):
                pending.append(row)
        return pending

    def attach_result_refs(self, row: WorldPlanConflictRow, refs: Sequence[str]) -> None:
        """Append ``refs`` to the row's result_refs, each once."""
        row.result_refs_json = json.dumps(list(dict.fromkeys([*conflict_result_refs(row), *refs])), ensure_ascii=False)
        self.session.flush()

    def escalate(self, row: WorldPlanConflictRow) -> None:
        row.required_decider = "human"
        self.session.flush()

    def reroll_count(self, project_id: str, *, actor_ref: str, since_chapter: int = 0) -> int:
        """The actor's veto_world resolutions on conflicts whose detected_after_chapter >= ``since_chapter``."""
        decisions = self.session.scalars(
            select(WorldPlanConflictRow.decision_json).where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.actor_ref == actor_ref,
                WorldPlanConflictRow.status == "resolved",
                WorldPlanConflictRow.detected_after_chapter >= since_chapter,
            )
        )
        return sum(json.loads(decision).get("option") == "veto_world" for decision in decisions)


def conflict_result_refs(row: WorldPlanConflictRow) -> list[str]:
    """The row's result_refs; anything but a list of strings reads as none."""
    try:
        refs = json.loads(row.result_refs_json or "[]")
    except ValueError:
        return []
    return [ref for ref in refs if isinstance(ref, str)] if isinstance(refs, list) else []
