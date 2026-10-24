"""Carry out a decided world-plan conflict option through canon admission.

``option_candidates`` turns the decision into world changes and ``record_option_side_effects`` records
what the option owes the plan; ``ConflictResolutionService.resolve`` composes the two around one
commit, and the world tick reuses both inside its own delta. Either way the option's world changes are
checked against current canon first (running_state); a held change canon has since ruled out is stale.
What the plan and the obligation ledger tell the writer comes only from the change's public events.
"""

from __future__ import annotations

import json
import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, replace
from typing import TYPE_CHECKING, Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reviewer import CHAPTER_EXTRACTION_SOURCE_TYPE
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.canon.admission import CanonAdmissionService, CanonStaleVersion, CanonWriteFailure
from forwin.models.audit import DecisionEvent
from forwin.models.base import new_id
from forwin.models.project import ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.narrative_obligations.transaction import DeferAcceptanceTransaction, check_obligation_budget
from forwin.narrative_obligations.types import NarrativeObligation, NarrativePlanPatch
from forwin.planning.future_plan_audit.patches import FuturePlanPatchMixin
from forwin.planning.query import PlanningQuery
from forwin.protocol.book_state import ApprovedGraphDeltaSet
from forwin.runtime.policy import RuntimePolicy
from forwin.writer.prompt_core.world_engine import world_change_response

from .conflict_repository import ConflictNotOpen, WorldConflictRepository, conflict_result_refs
from .conflict_routing import PLAN_BOUNDARY_VIOLATIONS, PLAN_CONFLICT_SOURCES, live_intent
from .conflict_types import ConflictDecision
from .constraints import check_narrative_boundaries
from .delta_builder import WorldTickDeltaBuilder
from .propagation import latest_news_in_transit, propagate_news, tick_events
from .running_state import RunningStateCheck
from .types import EffectEvent, NewsItem, TickCandidate, TickContext, WorldEffects, tick_seed

if TYPE_CHECKING:
    # The amender plans through the band planner, which imports this module.
    from forwin.planning.amend import PlanAmender


logger = logging.getLogger(__name__)

# An accepted world change owes the plot a response within this many chapters of the latest one.
OBLIGATION_DEADLINE_CHAPTERS = 6
WORLD_CHANGE_OBLIGATION_TYPE = "world_change_response"
PLOT_RESPONSE_PATCH_TYPE = "world_conflict_response"
# The result warning of a world-tick amend_plan whose replan failed once the resolution committed.
REPLAN_FAILED = "replan_failed"
# A veto stops an open intent; one the story already settled keeps its status.
_SETTLED_INTENT_STATUSES = frozenset({"resolved", "disrupted", "vetoed"})
_EVENT_ACTOR_TYPES = {"human": "manual_ui", "codex": "worker", "auto": "system"}
_ONSTAGE_SLIP = "onstage_slip"


class ConflictStale(RuntimeError):
    """Canon moved under the held change, so it no longer applies; the conflict stays open."""


class ConflictOptionUnavailable(RuntimeError):
    """The decided option cannot be carried out now; the conflict stays open."""


@dataclass(frozen=True)
class ConflictResolutionResult:
    graph_delta_ids: tuple[str, ...]
    obligation_id: str
    plan_patch_id: str
    # The builder's warnings, {code, message}: parts of the change that did not apply.
    warnings: tuple[dict[str, str], ...] = ()
    # What an amend_plan's amender wrote: the band plan it planned and the beat sheet version it revised.
    band_plan_id: str = ""
    beat_sheet_id: str = ""
    # The plan_feasibility conflict a human amends through when a world-tick amendment's replan failed.
    followup_conflict_id: str = ""


@dataclass(frozen=True)
class OptionSideEffects:
    obligation_id: str
    plan_patch_id: str
    pending_plan_response: bool = False


@dataclass(frozen=True)
class PropagatedNews:
    # What the news delivers by now, as one candidate; None when nobody learns anything.
    candidate: TickCandidate | None
    # The queue the delta carries forward as its news_in_transit.
    in_transit: list[NewsItem]


def _check_decision(conflict: WorldPlanConflictRow, decision: ConflictDecision) -> None:
    options = json.loads(conflict.options_json or "[]")
    if decision.option not in options:
        raise ValueError(f"Conflict {conflict.id} offers {options}, not {decision.option}")
    if decision.option == "amend_plan" and decision.decided_by != "human":
        raise ValueError(f"Only a human may amend the plan; {decision.decided_by} chose amend_plan")
    if decision.option == "rewrite_chapter":
        raise ConflictOptionUnavailable("rewrite_chapter needs a chapter-extraction conflict, which P3 never raises")


def option_candidates(
    conflict: WorldPlanConflictRow,
    decision: ConflictDecision,
    *,
    runtime: BookStateRuntime,
    policy: RuntimePolicy,
    reroll_count: int,
    now: int,
    plot_responded: bool = False,
) -> list[TickCandidate]:
    """The world changes that carry out the decision; none when the option changes nothing in the world.

    ``runtime`` is the latest canon chapter's. veto_world stops the conflict's intent and, while
    ``reroll_count`` is under ``world.max_rerolls``, has its actor act again at ``now``. A plot
    response holds only its intent; accept and amend commit the proposed change. amend_plan
    also ends the protections it breaks with that chapter; the other options refuse a change
    that still breaks one (ConflictOptionUnavailable). A held
    change that sets the status of an intent canon has since settled is stale (ConflictStale).
    Milestone and reveal-schedule conflicts accept only veto_world or amend_plan.
    """
    _check_decision(conflict, decision)
    if conflict.violated in PLAN_BOUNDARY_VIOLATIONS and decision.option not in {"veto_world", "amend_plan"}:
        raise ConflictOptionUnavailable(
            f"Conflict {conflict.id}: the held change breaks {conflict.violated}; veto it or amend the plan"
        )
    if decision.option == "veto_world":
        veto = _veto_candidate(conflict, runtime, policy=policy, reroll_count=reroll_count, now=now)
        return [veto] if veto is not None else []
    if decision.option == "plot_responds" and (plot_responded or not live_intent(runtime, conflict.intent_ref)):
        raise ConflictOptionUnavailable(f"Conflict {conflict.id}: plot_responds requires a live, unanswered intent")
    held = _held_change(conflict)
    _check_intents_unsettled(conflict, held, runtime)
    if decision.option != "amend_plan":
        if _broken_protections(held, runtime):
            raise ConflictOptionUnavailable(
                f"Conflict {conflict.id}: the held change breaks an active protection; veto it or amend the plan"
            )
        if decision.option == "plot_responds":
            return [TickCandidate(
                candidate_id=f"conflict:{conflict.id}:hold", origin="conflict_resolution",
                actor_id=conflict.actor_ref, intent_id=conflict.intent_ref,
                summary=conflict.summary, effects=WorldEffects(),
                intent_status_changes={conflict.intent_ref: "held"},
            )]
        return [held]
    release = _release_candidate(conflict, held, runtime)
    return [held, release] if release is not None else [held]


def _veto_candidate(
    conflict: WorldPlanConflictRow, runtime: BookStateRuntime, *, policy: RuntimePolicy, reroll_count: int, now: int,
) -> TickCandidate | None:
    # The conflict row is the veto record the actor's next turn reads; the candidate only patches state.
    world = runtime.world
    intent_ref, actor_ref = conflict.intent_ref.strip(), conflict.actor_ref.strip()
    intent = world.get_node(intent_ref) if intent_ref else None
    actor = world.get_node(actor_ref) if actor_ref else None
    statuses = (
        {intent_ref: "vetoed"}
        if intent is not None and str(intent.node_type) == "intent"
        and world.get_state(intent_ref).get("status") not in _SETTLED_INTENT_STATUSES
        else {}
    )
    turns = (
        {actor_ref: now}
        if actor is not None and str(actor.node_type) in ACTOR_NODE_TYPES and reroll_count < policy.world.max_rerolls
        else {}
    )
    if not statuses and not turns:
        return None
    return TickCandidate(
        candidate_id=f"conflict:{conflict.id}:veto",
        origin="conflict_resolution",
        actor_id=actor_ref,
        intent_id=intent_ref,
        summary=conflict.summary,
        effects=WorldEffects(),
        intent_status_changes=statuses,
        next_turn_at=turns,
    )


def _held_change(conflict: WorldPlanConflictRow) -> TickCandidate:
    """The world change the conflict held back, as the tick stored it."""
    try:
        return TickCandidate.model_validate(json.loads(conflict.proposed_change_json or "{}"))
    except ValueError as exc:
        raise ConflictOptionUnavailable(f"Conflict {conflict.id} holds no world change to commit: {exc}") from exc


def _check_intents_unsettled(conflict: WorldPlanConflictRow, held: TickCandidate, runtime: BookStateRuntime) -> None:
    """The held change may not set the status of an intent canon settled after it was held."""
    world = runtime.world
    targets = {ref.strip() for ref in held.intent_status_changes} | {
        change.ref.strip() for change in held.effects.entity_changes
        if change.field.strip().removeprefix("state.") == "status"
    }
    settled = sorted(
        ref for ref in targets
        if (node := world.get_node(ref)) is not None and str(node.node_type) == "intent"
        and world.get_state(ref).get("status") in _SETTLED_INTENT_STATUSES
    )
    if settled:
        raise ConflictStale(f"Conflict {conflict.id}: the held change sets the status of settled intents {settled}")


def _broken_protections(held: TickCandidate, runtime: BookStateRuntime) -> list[str]:
    """The active protections the held change breaks in the chapter after the latest one."""
    return sorted({
        violation.constraint_node_id
        for violation in check_narrative_boundaries(runtime, held, next_chapter=runtime.as_of_chapter + 1)
    })


def _release_candidate(
    conflict: WorldPlanConflictRow, held: TickCandidate, runtime: BookStateRuntime,
) -> TickCandidate | None:
    """Release every protection the held change breaks in the next chapter, including at chapter zero."""
    broken = _broken_protections(held, runtime)
    if not broken:
        return None
    return TickCandidate(
        candidate_id=f"conflict:{conflict.id}:release",
        origin="conflict_resolution",
        summary=conflict.summary,
        effects=WorldEffects(),
        protection_releases=broken,
    )


def propagation_candidate(
    runtime: BookStateRuntime,
    lexicon: BookLexicon,
    *,
    events: Sequence[tuple[EffectEvent, str]],
    in_transit: Sequence[NewsItem],
    now: int,
    candidate_id: str,
    summary: str,
) -> PropagatedNews:
    """The news of ``events`` (each with its ``fact:<id>``) and of the queue, as the tick spreads it.

    For a delta's own candidates the pairs come from ``propagation.tick_events``. The candidate
    goes after the ones that create the events' facts, and ``in_transit`` becomes the builder's
    news_in_transit.
    """
    news = propagate_news(
        runtime, lexicon,
        events=[event for event, _fact_ref in events],
        in_transit=in_transit,
        now=now,
        fact_ref_for_event={position: fact_ref for position, (_event, fact_ref) in enumerate(events)},
    )
    candidate = (
        TickCandidate(
            candidate_id=candidate_id, origin="propagation", summary=summary,
            effects=WorldEffects(knowledge=news.knowledge),
        )
        if news.knowledge else None
    )
    return PropagatedNews(candidate=candidate, in_transit=news.in_transit)


def record_option_side_effects(
    session: Session,
    conflict: WorldPlanConflictRow,
    decision: ConflictDecision,
    *,
    latest_chapter: int,
) -> OptionSideEffects:
    """The obligation or plan goal the option leaves; call it once the option's world changes committed.

    Bind to the next unaccepted plan when present; otherwise leave a pending response or an unbound
    obligation for the band planner. A refused obligation rolls back the committed changes with it.
    """
    _check_decision(conflict, decision)
    if decision.option == "accept_with_obligation":
        return _record_obligation(session, conflict, decision, latest_chapter=latest_chapter)
    if decision.option == "plot_responds":
        patch_id = _record_plot_response(session, conflict, latest_chapter=latest_chapter)
        return OptionSideEffects(obligation_id="", plan_patch_id=patch_id, pending_plan_response=not patch_id)
    return OptionSideEffects(obligation_id="", plan_patch_id="")


def writer_response_text(conflict: WorldPlanConflictRow) -> str:
    """What the writer is told of the conflict's accepted change: its public events, or a template.

    The held change's summary is objective truth (an adjudicated secret, an actor's plan), so it never
    reaches the writer (Ruling P32).
    """
    try:
        held = TickCandidate.model_validate(json.loads(conflict.proposed_change_json or "{}"))
    except ValueError:
        held = None
    public = [event.summary for event in held.effects.events if event.visibility == "public"] if held else []
    return world_change_response(public, onstage_slip=conflict.violated == _ONSTAGE_SLIP)


def _next_unaccepted_plan(session: Session, project_id: str, *, latest_chapter: int) -> ChapterPlan | None:
    project = session.get(Project, project_id)
    total = int(project.target_total_chapters or 0) if project is not None else 0
    if total <= latest_chapter:
        raise ConflictOptionUnavailable(f"No chapter follows chapter {latest_chapter} within the book's {total}")
    plans = PlanningQuery().future_chapters(session, project_id=project_id, current_chapter=latest_chapter)
    return plans[0] if plans else None


def _record_obligation(
    session: Session, conflict: WorldPlanConflictRow, decision: ConflictDecision, *, latest_chapter: int,
) -> OptionSideEffects:
    project = session.get(Project, conflict.project_id)
    total = int(project.target_total_chapters or 0) if project is not None else 0
    plan = _next_unaccepted_plan(session, conflict.project_id, latest_chapter=latest_chapter)
    obligation_id = new_id()
    summary = writer_response_text(conflict)
    obligation = NarrativeObligation(
        id=obligation_id,
        project_id=conflict.project_id,
        origin_chapter_number=latest_chapter,
        obligation_type=WORLD_CHANGE_OBLIGATION_TYPE,
        summary=summary,
        deferral_reason=decision.rationale,
        subject_refs=json.loads(conflict.involved_refs_json or "[]"),
        evidence_refs=[f"world_conflict:{conflict.id}"],
        deadline_chapter=min(latest_chapter + OBLIGATION_DEADLINE_CHAPTERS, total),
        payoff_test=summary,
        created_by=decision.decided_by,
        metadata={"conflict_id": conflict.id},
    )
    if plan is None:
        budget = check_obligation_budget(
            session=session, obligation=obligation, current_chapter=latest_chapter, target_total_chapters=total,
        )
        if budget.over_budget:
            reasons = ", ".join(["obligation_budget_exceeded", *budget.reasons])
            raise ConflictOptionUnavailable(f"The obligation ledger refused the obligation: {reasons}")
        stored = NarrativeObligationRepository(session).create_obligation(
            obligation.model_copy(update={"status": "active"}),
        )
        return OptionSideEffects(obligation_id=stored.id, plan_patch_id="")
    plan_patch = NarrativePlanPatch(
        id=new_id(),
        project_id=conflict.project_id,
        patch_type="defer_acceptance",
        target_scope="chapter",
        target_plan_id=plan.id,
        target_arc_id=plan.arc_plan_id,
        affected_chapters=[plan.chapter_number],
        source_obligation_ids=[obligation_id],
        new_contract={"obligations_to_resolve": [obligation_id], "payoff_test": summary, "summary": summary},
        diff_summary=f"Bind world-change obligation {obligation_id} to chapter {plan.chapter_number}.",
        writer_context_injections=[{
            "type": "narrative_obligation",
            "obligation_id": obligation_id,
            "priority": obligation.priority,
            "summary": summary,
            "payoff_test": summary,
            "deadline_chapter": obligation.deadline_chapter,
        }],
        reviewer_context_injections=[
            {"type": "narrative_obligation", "obligation_id": obligation_id, "payoff_test": summary},
        ],
        expected_resolution_tests=[summary],
        metadata={"conflict_id": conflict.id},
    )
    result = DeferAcceptanceTransaction(session).run(
        obligation=obligation,
        plan_patch=plan_patch,
        current_chapter=latest_chapter,
        target_total_chapters=total,
    )
    if not result.success or result.obligation is None or result.plan_patch is None:
        raise ConflictOptionUnavailable(f"The obligation ledger refused the obligation: {', '.join(result.errors)}")
    return OptionSideEffects(obligation_id=result.obligation.id, plan_patch_id=result.plan_patch.id)


def _record_plot_response(session: Session, conflict: WorldPlanConflictRow, *, latest_chapter: int) -> str:
    """Put the response goal first in the next unaccepted plan's goals and progress markers."""
    plan = _next_unaccepted_plan(session, conflict.project_id, latest_chapter=latest_chapter)
    return record_plot_response_patch(session, conflict, plan) if plan is not None else ""


def record_plot_response_patch(session: Session, conflict: WorldPlanConflictRow, plan: ChapterPlan) -> str:
    """Apply and record the conflict's plot-response patch on ``plan``; returns the patch id."""
    goal = writer_response_text(conflict)
    patch = NarrativePlanPatch(
        id=new_id(),
        project_id=conflict.project_id,
        patch_type=PLOT_RESPONSE_PATCH_TYPE,
        target_scope="chapter",
        target_plan_id=plan.id,
        target_arc_id=plan.arc_plan_id,
        affected_chapters=[plan.chapter_number],
        new_contract={"form_instruction": goal, "conflict_id": conflict.id,
                      "world_bindings": ([{"kind": "intent", "ref": conflict.intent_ref, "how": goal}]
                                         if conflict.intent_ref else [])},
        diff_summary=goal,
        writer_context_injections=[{"type": PLOT_RESPONSE_PATCH_TYPE, "conflict_id": conflict.id, "instruction": goal}],
        reviewer_context_injections=[
            {"type": PLOT_RESPONSE_PATCH_TYPE, "conflict_id": conflict.id, "payoff_test": goal},
        ],
        expected_resolution_tests=[goal],
        validation_status="passed",
        applied=True,
        metadata={"conflict_id": conflict.id},
    )
    FuturePlanPatchMixin.apply_form_plan_patch(plan, patch)
    session.add(plan)
    return NarrativeObligationRepository(session).create_plan_patch(patch).id


def conflict_resolved_event(
    row: WorldPlanConflictRow, *, chapter_number: int, extra: Mapping[str, Any] | None = None,
) -> DecisionEvent:
    """An unsaved WORLD_CONFLICT_RESOLVED event for a conflict ``mark_resolved`` just closed."""
    decision = json.loads(row.decision_json or "{}")
    decided_by = str(decision.get("decided_by") or "")
    payload = {
        "conflict_id": row.id,
        "source": row.source,
        "violated": row.violated,
        "impact_level": row.impact_level,
        "option": decision.get("option", ""),
        "decided_by": decided_by,
        "rationale": decision.get("rationale", ""),
        "result_refs": json.loads(row.result_refs_json or "[]"),
        **(extra or {}),
    }
    return DecisionEvent(
        project_id=row.project_id,
        chapter_number=chapter_number,
        scope="chapter",
        event_family="business_event",
        event_type=DecisionEventType.WORLD_CONFLICT_RESOLVED,
        actor_type=_EVENT_ACTOR_TYPES.get(decided_by, "system"),
        actor_id=str(decision.get("actor_id") or decided_by),
        summary=f"World conflict {row.id} resolved: {payload['option']}.",
        reason=str(decision.get("reason") or decision.get("rationale") or ""),
        payload_json=json.dumps(payload, ensure_ascii=False, sort_keys=True),
        related_object_type="world_conflict",
        related_object_id=row.id,
    )


class ConflictResolutionService:
    """Resolve one open conflict in the caller's session; the caller commits.

    ``plan_amender`` carries out amend_plan's planning. Without it amend_plan on a plan conflict is unavailable
    and a world-tick amend_plan only commits its change. With it an amend_plan resolution commits the session
    itself before the amender's model calls, so pass a plain session: an amend_plan inside a
    ``Session.begin()`` / ``sessionmaker.begin()`` or ``begin_nested()`` block is refused before any work.
    """

    def __init__(
        self,
        session: Session,
        *,
        admission: CanonAdmissionService,
        policy: RuntimePolicy,
        plan_amender: PlanAmender | None = None,
    ) -> None:
        self.session = session
        self.admission = admission
        self.policy = policy
        self.plan_amender = plan_amender

    def resolve(self, *, project_id: str, conflict_id: str, decision: ConflictDecision) -> ConflictResolutionResult:
        """Carry out ``decision``: commit its world changes, record its side effects and close the conflict.

        The project row is locked right after the conflict row, so no other canon writer commits while
        the resolution reads canon and the news queue. The option's world changes are checked against
        that canon first: one that no longer holds raises ConflictStale. Everything else lands in one
        SAVEPOINT. ConflictStale and ConflictOptionUnavailable roll it back whole, leaving the conflict
        open and the session usable; any other CanonWriteFailure propagates after the same rollback.

        A plan conflict never takes that path: its only option, amend_plan, plans again (_resolve_plan_conflict).
        A world-tick amend_plan hands the plan to the amender once its change is committed; a replan that fails
        then leaves the resolution standing, with a ``replan_failed`` warning and a follow-up plan conflict.
        """
        session = self.session
        if decision.option == "amend_plan" and self.plan_amender is not None and _in_transaction_block(session):
            raise RuntimeError(
                "amend_plan with a plan amender commits between its model calls: resolve it with a plain session, "
                "outside a transaction block"
            )
        repo = WorldConflictRepository(session)
        found = repo.get(project_id, conflict_id)
        if found is None:
            raise LookupError(f"World conflict {conflict_id} not found in project {project_id}")
        if found.source in PLAN_CONFLICT_SOURCES:
            return self._resolve_plan_conflict(found, decision)
        row = repo.get(project_id, conflict_id, for_update=True)
        # FOR NO KEY UPDATE, as canon admission takes it: after the conflict row, as the world tick orders them.
        session.execute(select(Project.id).where(Project.id == project_id).with_for_update(key_share=True))
        if row.status != "open":
            raise ConflictNotOpen(f"Conflict {row.id} is {row.status}")
        _check_decision(row, decision)
        latest = BookStateRepository(session).latest_canon_chapter(project_id) or 0
        # Count the actor's recent vetoes before this one closes; older ones no longer cap its rerolls.
        reroll_count = (
            repo.reroll_count(
                project_id, actor_ref=row.actor_ref, since_chapter=max(0, latest - self.policy.world.max_rerolls),
            )
            if row.actor_ref.strip() else 0
        )
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=latest)
        context = _resolution_context(session, project_id, chapter_number=latest, fallback_now=row.story_time)
        candidates = option_candidates(
            row, decision, runtime=runtime, policy=self.policy, reroll_count=reroll_count, now=context.now,
            plot_responded=repo.has_plot_response(project_id, row.intent_ref),
        )
        tick_id = f"conflict-{row.id}"
        self._check_against_canon(row, candidates, runtime=runtime, context=context, tick_id=tick_id)
        graph_delta_ids: tuple[str, ...] = ()
        warnings: tuple[dict[str, str], ...] = ()
        try:
            with session.begin_nested():
                if candidates:
                    graph_delta_ids, warnings = self._commit(
                        row, decision, candidates, runtime=runtime, tick_id=tick_id, context=context,
                    )
                effects = record_option_side_effects(session, row, decision, latest_chapter=latest)
                refs = [*graph_delta_ids, effects.obligation_id, effects.plan_patch_id]
                repo.mark_resolved(
                    row, decision=decision, result_refs=[ref for ref in refs if ref],
                    pending_plan_response=effects.pending_plan_response,
                )
                session.add(conflict_resolved_event(row, chapter_number=latest, extra={
                    "tick_id": tick_id if graph_delta_ids else "",
                    "graph_delta_ids": list(graph_delta_ids),
                    "obligation_id": effects.obligation_id,
                    "plan_patch_id": effects.plan_patch_id,
                    "warnings": [dict(warning) for warning in warnings],
                }))
                session.flush()
        except CanonStaleVersion as exc:
            raise ConflictStale(f"Conflict {row.id}: canon moved under the held change: {exc}") from exc
        except CanonWriteFailure as exc:
            if (BookStateRepository(session).latest_canon_chapter(project_id) or 0) != latest:
                raise ConflictStale(
                    f"Conflict {row.id}: chapter {latest} is no longer the latest canon chapter"
                ) from exc
            raise
        result = ConflictResolutionResult(
            graph_delta_ids=graph_delta_ids,
            obligation_id=effects.obligation_id,
            plan_patch_id=effects.plan_patch_id,
            warnings=warnings,
        )
        if decision.option != "amend_plan" or self.plan_amender is None:
            return result
        # Commit the resolution first: neither its conflict row lock nor its project row lock may last
        # through the amender's model calls.
        session.commit()
        return self._amend_after_world_change(row, decision, result)

    def _amend_after_world_change(
        self, row: WorldPlanConflictRow, decision: ConflictDecision, result: ConflictResolutionResult,
    ) -> ConflictResolutionResult:
        """Plan again the chapters a committed world-tick amend_plan changed. Nothing here fails the request: the
        resolution stands. A failed or raising replan is recorded by the amender, in a new transaction, with a
        follow-up plan conflict, and reported as a ``replan_failed`` warning."""
        session = self.session
        conflict_id = row.id
        try:
            amended = self.plan_amender.amend(session, conflict=row, decision=decision, policy=self.policy)
        except Exception as exc:  # noqa: BLE001 - the resolution has committed; a failed replan may not hide it
            session.rollback()
            logger.warning("World conflict %s: the replan after amend_plan raised", conflict_id, exc_info=True)
            issues = (self.plan_amender.error_issue(exc),)
        else:
            if not amended.failed:
                return replace(result, band_plan_id=amended.band_plan_id or "",
                               beat_sheet_id=amended.beat_sheet_id or "")
            issues = amended.issues
        details = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
        try:
            failure = self.plan_amender.record_world_amendment_failure(
                session, conflict=row, issues=issues, policy=self.policy,
            )
        except Exception:  # noqa: BLE001 - even a failed failure record cannot undo the durable resolution
            session.rollback()
            logger.warning("World conflict %s: recording the failed amendment raised", conflict_id, exc_info=True)
            warning = {"code": REPLAN_FAILED, "message": (
                f"the resolution stands, but its chapters could not be planned again ({details}); "
                "the planning failure could not be recorded"
            )}
            return replace(result, warnings=(*result.warnings, warning))
        warning = {"code": REPLAN_FAILED, "message": (
            f"the resolution stands, but its chapters could not be planned again ({details}); amend the plan "
            f"through conflict {failure.followup_conflict_id}"
        )}
        return replace(result, warnings=(*result.warnings, warning), followup_conflict_id=failure.followup_conflict_id)

    def _resolve_plan_conflict(
        self, row: WorldPlanConflictRow, decision: ConflictDecision,
    ) -> ConflictResolutionResult:
        """amend_plan on a plan conflict: the amender plans the conflict's band again, then the conflict closes.

        No lock is held through the amender's model calls. The conflict row is locked only to close it, so a
        concurrent resolution that closed it first leaves the new plan standing and raises ConflictNotOpen. When
        no band plan could be made, ConflictOptionUnavailable leaves the conflict open; the amender has already
        committed its traces.
        """
        session = self.session
        if row.status != "open":
            raise ConflictNotOpen(f"Conflict {row.id} is {row.status}")
        _check_decision(row, decision)
        if self.plan_amender is None:
            raise ConflictOptionUnavailable(f"Conflict {row.id}: amend_plan needs a plan amender; none is configured")
        amended = self.plan_amender.amend(session, conflict=row, decision=decision, policy=self.policy)
        if amended.band_plan_id is None:
            problems = "; ".join(f"{issue.code}: {issue.message}" for issue in amended.issues) or "no band plan"
            raise ConflictOptionUnavailable(f"Conflict {row.id}: its chapters could not be planned again: {problems}")
        repo = WorldConflictRepository(session)
        row = repo.get(row.project_id, row.id, for_update=True)
        if row.status != "open":
            raise ConflictNotOpen(f"Conflict {row.id} is {row.status}")
        repo.mark_resolved(row, decision=decision, result_refs=conflict_result_refs(row))
        session.add(conflict_resolved_event(
            row, chapter_number=BookStateRepository(session).latest_canon_chapter(row.project_id) or 0,
            extra={"band_plan_id": amended.band_plan_id, "beat_sheet_id": amended.beat_sheet_id or ""},
        ))
        session.flush()
        return ConflictResolutionResult(
            graph_delta_ids=(), obligation_id="", plan_patch_id="", band_plan_id=amended.band_plan_id,
            beat_sheet_id=amended.beat_sheet_id or "",
        )

    def _check_against_canon(
        self,
        conflict: WorldPlanConflictRow,
        candidates: Sequence[TickCandidate],
        *,
        runtime: BookStateRuntime,
        context: TickContext,
        tick_id: str,
    ) -> None:
        """Raise ConflictStale when the option's changes no longer hold against current canon (Ruling P30).

        The held change was checked against the canon of its tick; earlier resolutions or a rewrite may
        have changed it since. A veto changes nothing that can fail.
        """
        check = RunningStateCheck(
            self.session, context=context, runtime=runtime,
            lexicon=BookLexiconRepository(self.session).get(conflict.project_id),
            calendar=StoryCalendarRepository(self.session).get(conflict.project_id),
            tick_id=tick_id, travel_tolerance=self.policy.review.travel_tolerance,
        )
        for candidate in candidates:
            issues = check.admit(candidate)
            if issues:
                details = "; ".join(f"{issue.code}: {issue.message}" for issue in issues)
                raise ConflictStale(f"Conflict {conflict.id}: the held change no longer holds against canon: {details}")

    def _commit(
        self,
        conflict: WorldPlanConflictRow,
        decision: ConflictDecision,
        candidates: Sequence[TickCandidate],
        *,
        runtime: BookStateRuntime,
        tick_id: str,
        context: TickContext,
    ) -> tuple[tuple[str, ...], tuple[dict[str, str], ...]]:
        """Build the candidates and their news into one world_engine delta on the latest canon chapter,
        commit it, and return its ids with the builder's warnings."""
        project_id = conflict.project_id
        # A plot response writes only the hold; queued news waits for an actual world tick.
        # Read under the project lock, so no other writer's queue is lost.
        queue = latest_news_in_transit(self.session, project_id)
        news = (PropagatedNews(candidate=None, in_transit=queue) if decision.option == "plot_responds" else
                propagation_candidate(
                    runtime, BookLexiconRepository(self.session).get(project_id),
                    events=tick_events(project_id, tick_id, candidates), in_transit=queue, now=context.now,
                    candidate_id=f"conflict:{conflict.id}:news", summary=conflict.summary,
                ))
        build = WorldTickDeltaBuilder(self.session).build(
            context=context,
            candidates=[*candidates, news.candidate] if news.candidate is not None else candidates,
            news_in_transit=news.in_transit,
            tick_id=tick_id,
        )
        if build.delta is None:
            errors = "; ".join(issue.message for issue in build.issues if issue.severity == "error")
            raise ConflictStale(f"The held change no longer builds against canon: {errors}")
        delta = build.delta.model_copy(update={"metadata": {
            **build.delta.metadata, "conflict_id": conflict.id, "conflict_option": decision.option,
        }})
        outcome = self.admission.commit_world_tick(
            session=self.session,
            project_id=project_id,
            chapter_number=context.chapter_number,
            approved_changes=ApprovedGraphDeltaSet(
                project_id=project_id, chapter_number=context.chapter_number, graph_deltas=[delta],
            ),
            tick_id=tick_id,
        )
        warnings = tuple(
            {"code": issue.code, "message": issue.message} for issue in build.issues if issue.severity == "warning"
        )
        return outcome.graph_delta_ids, warnings


def _in_transaction_block(session: Session) -> bool:
    """Whether the session runs inside a ``begin()`` context or a SAVEPOINT, where a commit is refused."""
    # SQLAlchemy keeps the begin() context it is in on the session; it has no public accessor for it.
    return session.in_nested_transaction() or getattr(session, "_trans_context_manager", None) is not None


def _resolution_context(session: Session, project_id: str, *, chapter_number: int, fallback_now: int) -> TickContext:
    """The latest canon chapter's window: a resolution lands at the chapter's end, and a change held in the
    chapter's tick keeps the times that tick gave it."""
    story_end = BookStateQuery(session).current_story_minute(project_id, as_of_chapter=chapter_number)
    now = story_end if story_end is not None else fallback_now
    starts = [
        delta.story_time_start for delta in BookStateRepository(session).list_graph_deltas(
            project_id, after_chapter=chapter_number - 1, through_chapter=chapter_number,
        )
        if delta.source_type == CHAPTER_EXTRACTION_SOURCE_TYPE and delta.story_time_start is not None
    ]
    start = starts[-1] if starts else now
    return TickContext(
        project_id=project_id, chapter_number=chapter_number, now=now, window_start=min(start, now),
        seed=tick_seed(project_id, chapter_number),
    )


__all__ = [
    "OBLIGATION_DEADLINE_CHAPTERS",
    "PLOT_RESPONSE_PATCH_TYPE",
    "REPLAN_FAILED",
    "WORLD_CHANGE_OBLIGATION_TYPE",
    "ConflictOptionUnavailable",
    "ConflictResolutionResult",
    "ConflictResolutionService",
    "ConflictStale",
    "OptionSideEffects",
    "PropagatedNews",
    "conflict_resolved_event",
    "option_candidates",
    "propagation_candidate",
    "record_option_side_effects",
    "record_plot_response_patch",
    "writer_response_text",
]
