from __future__ import annotations

import json
from collections.abc import Sequence

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.models.project import ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.types import NarrativePlanPatch

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
            origin_key=draft.origin_key,
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

    def open_by_origin(self, project_id: str, origin_keys: Sequence[str]) -> list[WorldPlanConflictRow]:
        origin_keys = [key for key in origin_keys if key]
        if not origin_keys:
            return []
        return list(self.session.scalars(
            select(WorldPlanConflictRow).where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.origin_key.in_(origin_keys),
                WorldPlanConflictRow.status == "open",
            ).order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
            .with_for_update().execution_options(populate_existing=True)
        ))

    def open_for_origin(self, project_id: str, origin_key: str) -> WorldPlanConflictRow | None:
        if not origin_key:
            return None
        return self.session.scalar(
            select(WorldPlanConflictRow).where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.origin_key == origin_key,
                WorldPlanConflictRow.status == "open",
            ).order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id).limit(1)
            .with_for_update().execution_options(populate_existing=True)
        )

    def expire(self, row: WorldPlanConflictRow, *, reason: str) -> None:
        if row.status != "open":
            raise ConflictNotOpen(f"Conflict {row.id} is {row.status}")
        row.decision_json = serialize_conflict_decision({"reason": reason})
        row.status = "expired"
        self.session.flush()

    def pending_chapter_resumes(self, project_id: str, *, chapter_number: int) -> list[WorldPlanConflictRow]:
        rows = self.session.scalars(
            select(WorldPlanConflictRow).where(
                WorldPlanConflictRow.project_id == project_id,
                WorldPlanConflictRow.source == "chapter_extraction",
                WorldPlanConflictRow.blocks_from_chapter == chapter_number,
                WorldPlanConflictRow.status == "resolved",
            ).order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id)
        )
        return [row for row in rows
                if json.loads(row.decision_json or "{}").get("option") in {"rewrite_chapter", "amend_plan"}
                and not any(ref.startswith("candidate:") for ref in conflict_result_refs(row))]

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
        """Retire response work, retaining the once-per-intent resolution history."""
        payload = json.loads(row.decision_json or "{}")
        if payload.get("option") == "plot_responds":
            payload["pending_plan_response"] = False
            payload["response_retired"] = True
            row.decision_json = serialize_conflict_decision(payload)
            self.session.flush()

    def active_plot_responses(self, project_id: str) -> list[WorldPlanConflictRow]:
        """Outstanding hold responses, oldest first; resolutions from before holding stay historical."""
        rows = self.session.scalars(select(WorldPlanConflictRow).where(
            WorldPlanConflictRow.project_id == project_id, WorldPlanConflictRow.status == "resolved",
        ).order_by(WorldPlanConflictRow.created_at, WorldPlanConflictRow.id))
        return [row for row in rows
                if (decision := json.loads(row.decision_json or "{}")).get("option") == "plot_responds"
                and decision.get("pending_plan_response") is not False
                and not decision.get("response_retired")
                and (decision.get("pending_plan_response") is True or self.response_patches(row))]

    def response_patches(self, row: WorldPlanConflictRow) -> list[NarrativePlanPatch]:
        """The response's unreverted patches, including P3-era bare result refs."""
        ledger = NarrativeObligationRepository(self.session)
        patches = ledger.list_patches_by_ids([
            ref.removeprefix(PLAN_PATCH_REF_PREFIX) for ref in conflict_result_refs(row)
        ])
        return [patch for patch in patches if patch.patch_type == "world_conflict_response"
                and patch.applied and not any(
                    other.patch_type == "plan_patch_revert"
                    and other.metadata.get("reverted_plan_patch_id") == patch.id
                    for other in ledger.list_patches_for_plan(row.project_id, target_plan_id=patch.target_plan_id)
                )]

    def response_carriers(self, row: WorldPlanConflictRow) -> list[ChapterPlan]:
        """Existing bound carriers; drafted/accepted carriers may finish their original response."""
        project = self.session.get(Project, row.project_id)
        total = int(project.target_total_chapters or 0) if project is not None else 0
        plan_ids = [patch.target_plan_id for patch in self.response_patches(row)]
        plans = self.session.scalars(select(ChapterPlan).where(ChapterPlan.id.in_(plan_ids))
                                     .execution_options(populate_existing=True))
        return [plan for plan in plans if plan is not None and plan.project_id == row.project_id
                and 0 < plan.chapter_number <= total
                and plan.status in {"planned", "failed", "drafted", "needs_review", "accepted"}
                and (not row.intent_ref or any(
                    binding.get("kind") == "intent" and binding.get("ref") == row.intent_ref
                    for binding in json.loads(plan.world_bindings_json or "[]")
                ))]

    def pending_plot_responses(self, project_id: str) -> list[WorldPlanConflictRow]:
        """Unbound responses and responses whose carrier disappeared or became unreachable."""
        return [row for row in self.active_plot_responses(project_id) if not self.response_carriers(row)]

    def attach_result_refs(self, row: WorldPlanConflictRow, refs: Sequence[str]) -> None:
        """Append ``refs`` to the row's result_refs, each once."""
        row.result_refs_json = json.dumps(list(dict.fromkeys([*conflict_result_refs(row), *refs])), ensure_ascii=False)
        self.session.flush()

    def escalate(self, row: WorldPlanConflictRow) -> None:
        row.required_decider = "human"
        self.session.flush()

    def has_plot_response(self, project_id: str, intent_ref: str) -> bool:
        """A resolved response consumes the intent's one offer, including after a rewrite."""
        if not intent_ref:
            return False
        decisions = self.session.scalars(select(WorldPlanConflictRow.decision_json).where(
            WorldPlanConflictRow.project_id == project_id,
            WorldPlanConflictRow.intent_ref == intent_ref,
            WorldPlanConflictRow.status == "resolved",
        ))
        return any(json.loads(decision or "{}").get("option") == "plot_responds" for decision in decisions)

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
