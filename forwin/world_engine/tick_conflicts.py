"""World tick conflicts: held candidates become conflicts, and auto or Codex decisions run inside the tick.

A decided conflict's option candidates join the tick's own delta; its side effects, its closing and its
RESOLVED event land with the tick's commit in one SAVEPOINT (``TickConflicts.close_resolutions``).
Conflict rows are locked before that commit takes the project row, as a direct resolve locks them.
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from typing import Any

from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import IMPACT_LEVELS
from forwin.generation.gate_delegation import GateAuditWriter
from forwin.models.audit import DecisionEvent
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.runtime.policy import RuntimePolicy

from .conflict_delegate import WorldConflictDelegate, build_conflict_context_snapshot
from .conflict_events import conflict_detected_event
from .conflict_repository import WorldConflictRepository
from .conflict_resolution import (
    ConflictOptionUnavailable,
    ConflictStale,
    conflict_resolved_event,
    option_candidates,
    record_option_side_effects,
)
from .conflict_routing import AUTO_OPTION, available_options, route_conflict
from .conflict_types import ConflictDecision, ConflictDraft
from .constraints import check_narrative_boundaries, milestone_change_references
from .types import TickCandidate, TickContext


# What an auto decision gives as its rationale; auto and Codex decisions leave the operator's reason empty.
AUTO_RATIONALE = "A minor conflict: the world gives way."
# Why the tick handed a decided conflict to a human.
OPTION_UNAVAILABLE = "option_unavailable"
CONFLICT_STALE = "conflict_stale"
BUILD_ERROR = "build_error"
_VETO_OPTION = "veto_world"
_REVIEWED_IMPACT = IMPACT_LEVELS.index("major")


@dataclass(frozen=True)
class HeldForReview:
    draft: ConflictDraft
    # What the WORLD_CONFLICT_DETECTED event adds: the held candidate and the boundaries it breaks.
    extra: dict[str, Any]


@dataclass(frozen=True)
class InTickResolution:
    conflict_id: str
    actor_ref: str
    decision: ConflictDecision
    candidates: list[TickCandidate]
    # The Codex review that chose the option; empty for an auto decision.
    trace_id: str = ""


class ResolutionUnavailable(RuntimeError):
    """A decided option's side effects could not be recorded; the tick drops the option and escalates."""

    def __init__(self, conflict_id: str, detail: str) -> None:
        super().__init__(detail)
        self.conflict_id = conflict_id


def review_draft(
    candidate: TickCandidate, *, context: TickContext, runtime: BookStateRuntime,
    milestone_refs: frozenset[str] = frozenset(),
) -> HeldForReview | None:
    """The conflict that holds ``candidate`` back: it breaks a narrative boundary in the next chapter, or
    its impact is major or above."""
    next_chapter = context.chapter_number + 1
    violations = check_narrative_boundaries(runtime, candidate, next_chapter=next_chapter)
    if not violations and IMPACT_LEVELS.index(candidate.impact_level) < _REVIEWED_IMPACT:
        return None
    milestone = candidate.impact_level == "critical" and bool(milestone_refs & milestone_change_references(candidate))
    effects = candidate.effects
    refs = [
        candidate.intent_id, candidate.actor_id, *(violation.ref for violation in violations),
        *(change.ref for change in effects.entity_changes), *(move.entity_ref for move in effects.movements),
        *(transfer.item_ref for transfer in effects.item_transfers),
        *(ref for event in effects.events for ref in event.participant_refs),
    ]
    draft = ConflictDraft(
        project_id=context.project_id,
        source="world_tick",
        detected_after_chapter=context.chapter_number,
        story_time=context.now,
        involved_refs=[ref for ref in dict.fromkeys(ref.strip() for ref in refs) if ref],
        violated="milestone" if milestone else "protection" if violations else "impact_review",
        impact_level=candidate.impact_level,
        summary=candidate.summary.strip() or candidate.candidate_id,
        # A world-tick conflict holds the candidate itself; option_candidates rebuilds it (Ruling P20).
        proposed_change=candidate.model_dump(mode="json"),
        intent_ref=candidate.intent_id,
        actor_ref=candidate.actor_id,
        blocks_from_chapter=next_chapter,
    )
    return HeldForReview(draft, {
        "candidate_id": candidate.candidate_id, "violations": [asdict(violation) for violation in violations],
    })


def _utcnow() -> datetime:
    return datetime.now(UTC)


class TickConflicts:
    """The conflicts one tick raises, and the decisions it carries out itself."""

    def __init__(
        self,
        session: Session,
        *,
        context: TickContext,
        runtime: BookStateRuntime,
        policy: RuntimePolicy,
        tick_id: str,
        delegate: WorldConflictDelegate,
        audit_writer_factory: Callable[[], GateAuditWriter],
    ) -> None:
        self.session = session
        self.context = context
        self.runtime = runtime
        self.policy = policy
        self.tick_id = tick_id
        self.delegate = delegate
        self.audit_writer_factory = audit_writer_factory
        self.repo = WorldConflictRepository(session)
        self.conflict_ids: list[str] = []
        self.resolutions: list[InTickResolution] = []
        # The open conflict over each intent and violation: it already holds a new change of that kind.
        self._open = {
            (row.intent_ref, row.violated): row.id
            for row in self.repo.list(context.project_id, status="open") if row.intent_ref
        }

    def hold(self, draft: ConflictDraft, *, extra: Mapping[str, Any]) -> str:
        """Store the conflict and route it: auto and Codex decisions run in this tick, the rest wait for a human.

        Returns the id of the open conflict that already holds such a change for the same intent, in which
        case nothing is stored; otherwise "".
        """
        key = (draft.intent_ref, draft.violated)
        if draft.intent_ref and key in self._open:
            return self._open[key]
        decider = route_conflict(draft, policy=self.policy)
        row = self.repo.create(draft, required_decider=decider, options=available_options(draft))
        if draft.intent_ref:
            self._open[key] = row.id
        self.session.add(conflict_detected_event(
            row, chapter_number=self.context.chapter_number, extra={"tick_id": self.tick_id, **extra},
        ))
        self.conflict_ids.append(row.id)
        if decider == "auto":
            self._decide(row.id, ConflictDecision(
                option=AUTO_OPTION, decided_by="auto", rationale=AUTO_RATIONALE, decided_at=_utcnow(),
            ))
        elif decider == "codex":
            delegation = self.delegate.decide(
                conflict=row,
                context_snapshot=build_conflict_context_snapshot(self.session, conflict=row),
                policy=self.policy,
                audit_writer=self.audit_writer_factory(),
            )
            if delegation.escalate or delegation.option is None:
                # The delegate recorded the escalation; the conflict only changes hands.
                self._hand_to_human(self._locked(row.id))
            else:
                self._decide(row.id, ConflictDecision(
                    option=delegation.option, decided_by="codex", rationale=delegation.rationale,
                    decided_at=_utcnow(),
                ), trace_id=delegation.trace_id)
        return ""

    def cancel(self, conflict_id: str, *, failure_reason: str, detail: str) -> None:
        """Drop a decided option from the tick and hand its conflict to a human; the change stays held."""
        resolution = next(item for item in self.resolutions if item.conflict_id == conflict_id)
        self.resolutions.remove(resolution)
        self._escalate(
            self._locked(conflict_id), failure_reason, option=resolution.decision.option, detail=detail,
            trace_id=resolution.trace_id,
        )

    def close_resolutions(self, *, graph_delta_ids: Sequence[str], warnings: Sequence[Mapping[str, str]]) -> None:
        """Record each decided option's side effects, close its conflict and add its RESOLVED event.

        Runs inside the tick commit's SAVEPOINT; a refused side effect raises ResolutionUnavailable.
        """
        chapter = self.context.chapter_number
        for resolution in self.resolutions:
            row = self._locked(resolution.conflict_id)
            try:
                effects = record_option_side_effects(self.session, row, resolution.decision, latest_chapter=chapter)
            except ConflictOptionUnavailable as exc:
                raise ResolutionUnavailable(resolution.conflict_id, str(exc)) from exc
            delta_ids = list(graph_delta_ids) if resolution.candidates else []
            refs = [*delta_ids, effects.obligation_id, effects.plan_patch_id, resolution.trace_id]
            self.repo.mark_resolved(
                row, decision=resolution.decision, result_refs=[ref for ref in refs if ref],
                pending_plan_response=effects.pending_plan_response,
            )
            self.session.add(conflict_resolved_event(row, chapter_number=chapter, extra={
                "tick_id": self.tick_id,
                "graph_delta_ids": delta_ids,
                "obligation_id": effects.obligation_id,
                "plan_patch_id": effects.plan_patch_id,
                "trace_id": resolution.trace_id,
                "warnings": [dict(warning) for warning in warnings],
            }))

    def _decide(self, conflict_id: str, decision: ConflictDecision, *, trace_id: str = "") -> None:
        """Turn a decision into option candidates for this tick, or hand the conflict to a human."""
        context = self.context
        row = self._locked(conflict_id)
        reroll_count = 0
        if row.actor_ref.strip():
            reroll_count = self.repo.reroll_count(
                context.project_id, actor_ref=row.actor_ref,
                since_chapter=max(0, context.chapter_number - self.policy.world.max_rerolls),
            )
            # A veto decided earlier in this tick is not resolved yet.
            reroll_count += sum(
                item.actor_ref == row.actor_ref and item.decision.option == _VETO_OPTION for item in self.resolutions
            )
        try:
            candidates = option_candidates(
                row, decision, runtime=self.runtime, policy=self.policy, reroll_count=reroll_count, now=context.now,
            )
        except ConflictOptionUnavailable as exc:
            self._escalate(row, OPTION_UNAVAILABLE, option=decision.option, detail=str(exc), trace_id=trace_id)
            return
        except ConflictStale as exc:
            self._escalate(row, CONFLICT_STALE, option=decision.option, detail=str(exc), trace_id=trace_id)
            return
        self.resolutions.append(InTickResolution(row.id, row.actor_ref, decision, candidates, trace_id))

    def _locked(self, conflict_id: str) -> WorldPlanConflictRow:
        # Re-read under lock: a rolled-back SAVEPOINT leaves loaded rows expired.
        row = self.repo.get(self.context.project_id, conflict_id, for_update=True)
        if row is None:
            raise LookupError(f"World conflict {conflict_id} vanished during world tick {self.tick_id}")
        return row

    def _hand_to_human(self, row: WorldPlanConflictRow) -> None:
        if row.status == "open":
            self.repo.escalate(row)

    def _escalate(
        self, row: WorldPlanConflictRow, failure_reason: str, *, option: str, detail: str, trace_id: str = "",
    ) -> None:
        """Hand a conflict the tick could not carry out to a human, with its one ESCALATED event."""
        self._hand_to_human(row)
        self.session.add(DecisionEvent(
            project_id=row.project_id,
            chapter_number=self.context.chapter_number,
            scope="chapter",
            event_family="business_event",
            event_type=DecisionEventType.WORLD_CONFLICT_ESCALATED,
            actor_type="system",
            summary=f"World conflict {row.id} escalated to a human: {failure_reason}.",
            reason=detail,
            payload_json=json.dumps({
                "conflict_id": row.id, "failure_reason": failure_reason, "option": option, "trace_id": trace_id,
                "tick_id": self.tick_id,
            }, ensure_ascii=False, sort_keys=True),
            related_object_type="world_conflict",
            related_object_id=row.id,
        ))


__all__ = [
    "AUTO_RATIONALE",
    "BUILD_ERROR",
    "CONFLICT_STALE",
    "OPTION_UNAVAILABLE",
    "HeldForReview",
    "InTickResolution",
    "ResolutionUnavailable",
    "TickConflicts",
    "review_draft",
]
