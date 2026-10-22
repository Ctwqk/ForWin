from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.world_conflict import WorldPlanConflictRow

from .conflict_types import ConflictDecider, ConflictDecision, ConflictDraft, ConflictOption


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
        return list(self.session.scalars(statement.order_by(WorldPlanConflictRow.created_at.desc())))

    def open_blocking(self, project_id: str, *, next_chapter: int) -> WorldPlanConflictRow | None:
        return self.session.scalar(
            select(WorldPlanConflictRow)
            .where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.status == "open",
                WorldPlanConflictRow.required_decider == "human",
                WorldPlanConflictRow.blocks_from_chapter <= next_chapter,
            )
            .order_by(WorldPlanConflictRow.blocks_from_chapter, WorldPlanConflictRow.created_at)
            .limit(1)
        )

    def mark_resolved(
        self,
        row: WorldPlanConflictRow,
        *,
        decision: ConflictDecision,
        result_refs: Sequence[str],
    ) -> None:
        if row.status != "open":
            raise ConflictNotOpen(f"Conflict {row.id} is {row.status}")
        row.decision_json = decision.model_dump_json()
        row.result_refs_json = json.dumps(list(result_refs), ensure_ascii=False)
        row.status = "resolved"
        self.session.flush()

    def escalate(self, row: WorldPlanConflictRow) -> None:
        row.required_decider = "human"
        self.session.flush()

    def reroll_count(self, project_id: str, *, actor_ref: str) -> int:
        decisions = self.session.scalars(
            select(WorldPlanConflictRow.decision_json).where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.actor_ref == actor_ref,
                WorldPlanConflictRow.status == "resolved",
            )
        )
        return sum(json.loads(decision).get("option") == "veto_world" for decision in decisions)
