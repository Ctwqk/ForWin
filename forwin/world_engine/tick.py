"""The world tick: the first post-Canon step moves the offstage world on after each chapter.

``WorldTickService.run`` works on the BookState as of the chapter just committed (N), over the
chapter's story minutes from its start (``window_start``) to its end (``now``):

1. Collect candidates: the chapter's intent interactions, clocks that ran out, due intents (onstage
   ones go to planning, offstage ones are adjudicated; one whose prompt is over the guard even alone
   waits for the next tick), due actors' turns and world-variable drift.
2. Check each candidate against the hard rules and the world gate on its own delta. An adjudicated
   outcome, actor turn or drift that fails is asked for again once, with the errors; whatever still fails
   is dropped with a WORLD_TICK_CANDIDATE_DROPPED event.
3. Classify impacts and keep major events within the tick's budget.
4. Hold back for review whatever breaks a narrative boundary or is major or above (tick_conflicts).
5. Check what will commit against the running state, the base candidates and then the in-tick
   resolutions, each against the ones accepted before it (running_state): a candidate that contradicts
   an earlier one is dropped, a resolution whose change no longer holds goes to a human.
6. Spread the news of what will commit, build one world_engine delta, and commit it through canon
   admission together with the side effects of the conflicts decided in the tick. News an active
   protection forbids waits in the queue for the next tick.

A rewritten chapter's world step expires the open conflicts the rewrite took the ground from
(conflict_revalidation). For the latest chapter, the tick that replaces the retired one leaves the retired
tick's conflicts out of its open set and expires them, with the chapter conflicts over a drafted next chapter,
when it commits, so no conflict row stays locked through its LLM calls. For an earlier chapter, the step expires
the ones whose held change no longer stands, then stops.

An LLM outage never fails the tick: the node is skipped and the deterministic rest still commits. Canon
that moved under the tick ends it with CanonStaleVersion: before each LLM phase, at the commit, and for
a tick that commits nothing (Ruling P27).
"""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventType
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.names import normalized_name
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reviewer import CHAPTER_EXTRACTION_SOURCE_TYPE
from forwin.canon.admission import CanonAdmissionService, CanonStaleVersion
from forwin.generation.gate_delegation import GateAuditWriter
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import GraphDeltaRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.project import ChapterPlan, Project
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, WorldNode
from forwin.runtime.policy import RuntimePolicy

from .actor_turns import ActorTurnRunner, ActorView, build_actor_view, rank_actors, recent_vetoes
from .adjudicator import WorldAdjudicator
from .clocks import expire_clocks
from .drift import WorldDriftRunner, due_world_variables
from .conflict_delegate import WorldConflictDelegate
from .conflict_repository import WorldConflictRepository
from .conflict_resolution import (
    PropagatedNews, next_tick_minute, propagation_candidate, reconcile_plot_responses, unprotected_news,
)
from .conflict_revalidation import (
    expire_next_chapter_conflicts,
    expire_retired_tick_conflicts,
    expire_stale_open_conflicts,
)
from .conflict_types import ConflictDraft
from .constraints import ConstraintIssue, apply_budgets, classify_impact
from .delta_builder import WorldTickDeltaBuilder
from .intents import (
    apply_intent_interactions, due_onstage_intents, onstage_bookkeeping, release_held_intents, select_due_intents,
)
from .plan_bindings import onstage_plan_bindings
from .propagation import chapter_events, latest_news_in_transit, tick_events
from .running_state import RunningStateCheck
from .tick_conflicts import (
    BUILD_ERROR,
    CONFLICT_STALE,
    OPTION_UNAVAILABLE,
    ResolutionUnavailable,
    TickConflicts,
    review_draft,
)
from .tick_nodes import (
    NodeFailure,
    NodeResults,
    actor_safe_feedback,
    actor_safe_vetoes,
    adjudicate,
    adjudication_candidate_id,
    drift,
    protected_refs,
    take_turns,
    too_large,
    unavailable,
)
from .types import (
    TICK_ID_PREFIX, WORLD_ENGINE_SOURCE_TYPE, EffectEntityChange, NewsItem, TickCandidate, TickContext, WorldEffects,
    tick_seed,
)


# The tick's one news candidate; it goes after every candidate whose events it spreads.
NEWS_CANDIDATE_ID = "propagation"
# Due offstage intents one tick adjudicates, earliest first; the rest stay due for the next tick.
# An intent whose prompt is over the guard even alone gives its place to the next due intent.
MAX_ADJUDICATION_INTENTS_PER_TICK = 20
# Why a checked candidate left the tick: it broke a check, or an open conflict already holds such a change.
CONSTRAINT_VIOLATION = "constraint_violation"
ALREADY_HELD = "already_held"
# Why a due intent left the tick unasked: its prompt is over the guard even alone, so it waits for the next tick.
PROMPT_TOO_LARGE_DEFERRED = "prompt_too_large_deferred"


@dataclass(frozen=True)
class WorldTickResult:
    tick_id: str
    graph_delta_ids: list[str]
    conflict_ids: list[str]
    dropped_candidate_ids: list[str]
    # Over the major-event budget, or due intents beyond the adjudication cap: their intents wait for a
    # later tick, or they are simply not committed.
    postponed_candidate_ids: list[str]
    # The builder's warnings, {code, message, candidate_id}: parts of the tick that did not apply.
    warnings: list[dict[str, str]] = field(default_factory=list)
    # Why the tick did not run; empty when it did.
    skipped_reason: str = ""

    def step_result(self) -> dict[str, Any]:
        """The world step's result_json entries."""
        return asdict(self)


def _skipped(tick_id: str, reason: str) -> WorldTickResult:
    return WorldTickResult(
        tick_id=tick_id, graph_delta_ids=[], conflict_ids=[], dropped_candidate_ids=[], postponed_candidate_ids=[],
        skipped_reason=reason,
    )


class WorldTickService:
    """Runs one world tick in the caller's session; the caller commits.

    ``llm_client`` is None when no model is configured: the LLM nodes are then skipped as unavailable.
    ``audit_writer_factory`` gives the Codex review a writer that commits on its own, so its audit trail
    survives a tick that rolls back; ``save_prompt_trace`` saves a world role's trace in the tick's session.
    """

    def __init__(
        self,
        *,
        llm_client: Any,
        admission: CanonAdmissionService,
        adjudicator: WorldAdjudicator,
        actor_runner: ActorTurnRunner,
        conflict_delegate: WorldConflictDelegate,
        audit_writer_factory: Callable[[], GateAuditWriter],
        save_prompt_trace: Callable[[Session, dict], str],
        drift_runner: WorldDriftRunner | None = None,
    ) -> None:
        self.llm_client = llm_client
        self.admission = admission
        self.adjudicator = adjudicator
        self.actor_runner = actor_runner
        self.drift_runner = drift_runner
        self.conflict_delegate = conflict_delegate
        self.audit_writer_factory = audit_writer_factory
        self.save_prompt_trace = save_prompt_trace

    def run(self, session: Session, commit: CanonCommitRecord, *, policy: RuntimePolicy) -> WorldTickResult:
        tick_id = f"{TICK_ID_PREFIX}{commit.id}"
        number = int(commit.chapter_number or 0)
        latest = BookStateRepository(session).latest_canon_chapter(commit.project_id) or 0
        if not policy.world.enabled:
            if number == latest and _rewritten(commit):
                # No tick follows, so no model call: the next chapter's stale conflicts can expire right away.
                expire_next_chapter_conflicts(session, project_id=commit.project_id, chapter_number=number)
            return _skipped(tick_id, "world_disabled")
        project = session.get(Project, commit.project_id)
        total = int(project.target_total_chapters or 0) if project is not None else 0
        book_complete = 0 < total <= number
        # A rewritten chapter's world step runs after later chapters landed: the world already moved on, so
        # only the open conflicts the rewrite took the ground from go.
        if number != latest:
            expire_stale_open_conflicts(
                session, project_id=commit.project_id, from_chapter=number, policy=policy,
                reason=f"rewrite of chapter {number}",
            )
            return _skipped(tick_id, "chapter_not_latest")
        ticks = _chapter_ticks(session, commit.project_id, number)
        own = [delta_id for delta_id, delta_tick_id in ticks if delta_tick_id == tick_id]
        if own:
            # A retry of this very tick after it committed: its delta stands.
            return WorldTickResult(
                tick_id=tick_id, graph_delta_ids=own, conflict_ids=[], dropped_candidate_ids=[],
                postponed_candidate_ids=[], skipped_reason="tick_already_committed",
            )
        # Rewriting the latest chapter retired the tick that ran after it, and this tick moves the world on in
        # its place; the retired tick's open conflicts expire when this one commits.
        tick = _Tick(self, session, commit, policy=policy, tick_id=tick_id, retired_tick_ids=_retired_tick_ids(commit),
                     rewritten=_rewritten(commit))
        return tick.finish_holds() if book_complete else tick.run()


def _actor_name_issues(candidate: TickCandidate, names: set[str]) -> list[ConstraintIssue]:
    return [ConstraintIssue("new_actor_name_taken", f"New actor name is unavailable: {actor.name!r}", "")
            for actor in candidate.effects.new_actors if normalized_name(actor.name) in names]


class _Tick:
    """One run of the tick: its context, its candidates as they pass each phase, and what it dropped."""

    def __init__(
        self, service: WorldTickService, session: Session, commit: CanonCommitRecord, *, policy: RuntimePolicy,
        tick_id: str, retired_tick_ids: Sequence[str] = (), rewritten: bool = False,
    ) -> None:
        self.service = service
        self.session = session
        self.policy = policy
        self.tick_id = tick_id
        # The ticks a rewrite of this chapter retired: this tick replaces them.
        self.retired_tick_ids = tuple(retired_tick_ids)
        # Whether a historical rewrite replaced this chapter's text.
        self.rewritten = rewritten
        project_id, number = commit.project_id, int(commit.chapter_number)
        self.chapter = _chapter_delta(session, commit)
        self.onstage_refs = _chapter_onstage_refs(session, project_id, number, self.chapter)
        now, window_start = _window(session, self.chapter, project_id, number)
        self.context = TickContext(
            project_id=project_id, chapter_number=number, now=now, window_start=window_start,
            seed=tick_seed(project_id, number),
        )
        # The canon the tick reasons on, read before the runtime: a write landing in between only makes the
        # tick stale, never hides it.
        self.snapshot_id = BookStateRepository(session).latest_world_snapshot_id(project_id, number)
        # Every observer's overlay: a runtime filtered to some observers makes actors look overlay-less.
        self.runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=number)
        self.calendar = StoryCalendarRepository(session).get(project_id)
        self.lexicon = BookLexiconRepository(session).get(project_id)
        self.builder = WorldTickDeltaBuilder(session)
        self.conflicts = TickConflicts(
            session, context=self.context, runtime=self.runtime, policy=policy, tick_id=tick_id,
            delegate=service.conflict_delegate, audit_writer_factory=service.audit_writer_factory,
            retired_tick_ids=frozenset(self.retired_tick_ids),
        )
        self.views: dict[str, ActorView] = {}
        self.slip_conflicts: list[ConflictDraft] = []
        self.dropped: list[str] = []
        # Due offstage intents the tick did not ask about: beyond the cap, they wait for a later tick.
        self.unasked: list[str] = []
        # Bookkeeping for the due intents over the prompt guard even alone: each waits for the next tick.
        self.oversized: list[TickCandidate] = []
        self.responses = WorldConflictRepository(session).active_plot_responses(project_id)
        self.book_complete = False

    def finish_holds(self) -> WorldTickResult:
        """The last chapter answers its holds without starting any new world activity."""
        self.book_complete = True
        bound = frozenset(node.id for node in self.runtime.world.nodes_by_id.values()
                          if node.node_type == "intent" and self.runtime.world.get_state(node.id).get("status") == "held")
        interactions = [item for item in _interactions(self.chapter) if item.get("intent_id") in bound]
        candidates = [
            *apply_intent_interactions(self.runtime, interactions=interactions),
            *release_held_intents(self.runtime, bound_intent_refs=bound, interactions=interactions,
                                 now=self.context.now),
        ]
        delta_ids, warnings = self._commit(candidates, spread_news=False)
        if not delta_ids:
            return _skipped(self.tick_id, "book_complete")
        return WorldTickResult(tick_id=self.tick_id, graph_delta_ids=delta_ids, conflict_ids=[],
                               dropped_candidate_ids=list(self.dropped), postponed_candidate_ids=[], warnings=warnings)

    def run(self) -> WorldTickResult:
        from forwin.planning.beat_sheet.service import milestone_refs_for_chapter

        milestone_refs = milestone_refs_for_chapter(
            self.session, self.context.project_id, self.context.chapter_number + 1,
        )
        checked = self._check(self._collect())
        classified = [
            candidate.model_copy(update={
                "impact_level": classify_impact(self.runtime, candidate, milestone_refs=milestone_refs),
            })
            for candidate in checked
        ]
        budget = apply_budgets(
            classified,
            impacts={candidate.candidate_id: candidate.impact_level for candidate in classified},
            max_major_events=self.policy.world.max_major_events_per_tick,
            runtime=self.runtime,
            now=self.context.now,
        )
        # Over-budget intents wait at now; candidates without an intent are simply asked again later.
        postponed = {candidate.candidate_id: candidate for candidate in budget.postponed}
        deferred = [candidate.candidate_id for candidate in budget.deferred_without_intent]
        held: set[str] = set()
        for candidate in budget.kept:
            # Interactions mirror chapter text that is already canon: there is nothing to hold back.
            review = None if candidate.origin == "intent_interaction" else review_draft(
                candidate, context=self.context, runtime=self.runtime, milestone_refs=milestone_refs,
                tick_id=self.tick_id,
            )
            if review is not None:
                held.add(candidate.candidate_id)
                self._hold(candidate, review.draft, extra=review.extra)
        for draft in self.slip_conflicts:
            slip = TickCandidate.model_validate(draft.proposed_change)
            self._hold(slip, draft, extra={"candidate_id": slip.candidate_id})
        base = [
            postponed.get(candidate.candidate_id, candidate) for candidate in classified
            if candidate.candidate_id not in held and candidate.candidate_id not in deferred
        ]
        # The oversized intents' bookkeeping goes last, after any clock that brought one of them forward.
        graph_delta_ids, warnings = self._commit([*base, *self.oversized])
        return WorldTickResult(
            tick_id=self.tick_id,
            graph_delta_ids=graph_delta_ids,
            conflict_ids=list(self.conflicts.conflict_ids),
            dropped_candidate_ids=list(self.dropped),
            postponed_candidate_ids=[*postponed, *deferred, *self.unasked],
            warnings=warnings,
        )

    # collection

    def _collect(self) -> list[TickCandidate]:
        """The tick's candidates, event producers first: interactions, clocks, onstage bookkeeping, outcomes, turns, drift."""
        runtime, now = self.runtime, self.context.now
        interactions = _interactions(self.chapter)
        applied = apply_intent_interactions(runtime, interactions=interactions)
        touched = {intent_id for candidate in applied for intent_id in candidate.intent_status_changes}
        clocks = expire_clocks(runtime, now=now, window_start=self.context.window_start)
        due = select_due_intents(runtime, now=now)
        # A clock naming an intent brings it forward to now, so it is due in this same tick.
        named = [
            node for candidate in clocks for change in candidate.effects.entity_changes
            if (node := runtime.world.get_node(change.ref)) is not None and str(node.node_type) == "intent"
            and runtime.world.get_state(node.id).get("status") == "planned"
        ]
        onstage_named = [node for node in named if node.profile.get("requires_onstage") is True]
        offstage_named = [node for node in named if node.profile.get("requires_onstage") is not True]
        bound_intent_refs, band_ended, band_bound_intent_refs = onstage_plan_bindings(
            self.session, self.context.project_id, self.context.chapter_number,
        )
        answered = frozenset(row.intent_ref for row in self.responses if any(
            plan.chapter_number <= self.context.chapter_number
            for plan in WorldConflictRepository(self.session).response_carriers(row)
        ))
        released = release_held_intents(runtime, bound_intent_refs=bound_intent_refs | answered,
                                       interactions=interactions, now=now)
        onstage = onstage_bookkeeping(
            runtime,
            onstage_due=_unique([*due.onstage, *onstage_named], excluded=touched),
            already_due=due_onstage_intents(runtime),
            interactions=interactions,
            now=now,
            max_slips=self.policy.world.max_onstage_slips,
            chapter_number=self.context.chapter_number,
            bound_intent_refs=bound_intent_refs,
            band_ended=band_ended,
            band_bound_intent_refs=band_bound_intent_refs,
            tick_id=self.tick_id,
        )
        # A slip past the cap comes back only as a conflict draft holding the slip.
        self.slip_conflicts = onstage.conflicts
        named_ids = {node.id for node in named}

        def scheduled(node: WorldNode) -> tuple[int, str]:
            # A clock brought its named intent forward to now.
            at = runtime.world.get_state(node.id).get("scheduled_at")
            return (at if type(at) is int and node.id not in named_ids else now, node.id)

        offstage = sorted(_unique([*due.offstage, *offstage_named], excluded=touched), key=scheduled)
        results, unasked, oversized = self._adjudicate_within_guard(offstage, limit=MAX_ADJUDICATION_INTENTS_PER_TICK)
        self.unasked = [adjudication_candidate_id(intent.id) for intent in unasked]
        self.oversized = [self._deferred(failure) for failure in too_large(oversized).failures]
        adjudicated = self._settled(results)
        turns = self._settled(self._take_turns())
        drifted = self._settled(self._drift(due_world_variables(
            runtime, now=now, every=self.policy.world.drift_every_story_minutes,
            limit=self.policy.world.max_drift_variables_per_tick,
        )))
        return [*applied, *released, *clocks, *onstage.candidates, *adjudicated, *turns, *drifted]

    def _adjudicate_within_guard(
        self, intents: Sequence[WorldNode], *, limit: int,
        feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> tuple[NodeResults, list[WorldNode], list[WorldNode]]:
        """Up to ``limit`` of ``intents``, in order, in one call; the intents left unasked; and the intents
        over the prompt guard even alone, which are never asked.

        When the first ``limit`` are over the guard together, the call takes intents in order while its prompt
        stays within the guard: one that does not fit beside the others waits for a later call, and the next
        intent may take its place.
        """
        batch = list(intents[:limit])
        # Without a model nothing is asked, so nothing needs to fit.
        if not batch or self.service.llm_client is None or self._fits(batch, feedback):
            return self._adjudicate(batch, feedback=feedback), list(intents[limit:]), []
        packed: list[WorldNode] = []
        unasked: list[WorldNode] = []
        oversized: list[WorldNode] = []
        for intent in intents:
            if len(packed) == limit:
                unasked.append(intent)
            elif self._fits([*packed, intent], feedback):
                packed.append(intent)
            elif packed and self._fits([intent], feedback):
                unasked.append(intent)
            else:
                oversized.append(intent)
        return self._adjudicate(packed, feedback=feedback), unasked, oversized

    def _adjudicate_all(
        self, intents: Sequence[WorldNode], feedback: Mapping[str, Sequence[ConstraintIssue]],
    ) -> NodeResults:
        """Every one of ``intents``, in as few calls as the prompt guard allows; one over it alone fails."""
        results, pending = NodeResults(), list(intents)
        while pending:
            asked, pending, oversized = self._adjudicate_within_guard(pending, limit=len(pending), feedback=feedback)
            results.candidates.extend(asked.candidates)
            self._new_actor_budget_remaining = max(
                0, getattr(self, "_new_actor_budget_remaining", self.policy.world.max_new_actors_per_tick)
                - sum(len(c.effects.new_actors) for c in asked.candidates),
            )
            results.failures.extend([*asked.failures, *too_large(oversized).failures])
        return results

    def _fits(self, intents: Sequence[WorldNode], feedback: Mapping[str, Sequence[ConstraintIssue]] | None) -> bool:
        return self.service.adjudicator.fits(
            context=self.context, intents=intents, runtime=self.runtime, lexicon=self.lexicon,
            calendar=self.calendar, feedback=feedback,
            max_new_actors=getattr(self, "_new_actor_budget_remaining", self.policy.world.max_new_actors_per_tick),
        )

    def _deferred(self, failure: NodeFailure) -> TickCandidate:
        """Drop an intent over the prompt guard even alone from this tick, and the bookkeeping that moves its
        scheduled_at past now: it is due again at the next tick, not asked again in this one.

        Like an intent the event budget postpones, the bookkeeping is no world change: it is neither classified
        nor reviewed, only checked against the running state with the rest.
        """
        self._drop(
            failure.candidate_id, origin=failure.origin, actor_id=failure.actor_id, intent_id=failure.intent_id,
            reason=PROMPT_TOO_LARGE_DEFERRED, issues=failure.issues,
        )
        return TickCandidate(
            candidate_id=failure.candidate_id, origin="adjudication", actor_id=failure.actor_id,
            intent_id=failure.intent_id, summary="Intent deferred: its adjudication prompt is over the guard",
            effects=WorldEffects(entity_changes=[EffectEntityChange(
                ref=failure.intent_id, field="scheduled_at", new_value=next_tick_minute(self.context.now),
            )]),
        )

    def _adjudicate(
        self, intents: Sequence[WorldNode], feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> NodeResults:
        if not intents:
            return NodeResults()
        if self.service.llm_client is None:
            return unavailable(intents=intents)
        self._check_snapshot()
        return adjudicate(
            self.service.adjudicator, context=self.context, intents=intents, runtime=self.runtime,
            lexicon=self.lexicon, calendar=self.calendar, save_trace=self._save_trace, feedback=feedback,
            max_new_actors=getattr(self, "_new_actor_budget_remaining", self.policy.world.max_new_actors_per_tick),
        )

    def _drift(
        self, variables: Sequence[WorldNode], feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> NodeResults:
        if not variables:
            return NodeResults()
        if self.service.llm_client is None or self.service.drift_runner is None:
            return unavailable(variables=variables)
        self._check_snapshot()
        return drift(
            self.service.drift_runner, context=self.context, variables=variables, runtime=self.runtime,
            lexicon=self.lexicon, calendar=self.calendar, save_trace=self._save_trace, feedback=feedback,
        )

    def _take_turns(self) -> NodeResults:
        context = self.context
        actors = rank_actors(
            self.runtime, now=context.now, limit=self.policy.world.max_actor_turns_per_tick,
            onstage_refs=self.onstage_refs,
        )
        if not actors:
            return NodeResults()
        if self.service.llm_client is None:
            return unavailable(actor_ids=[actor.id for actor in actors])
        vetoes = recent_vetoes(self.session, context.project_id, actor_ids=[actor.id for actor in actors])
        boundary_refs = protected_refs(self.runtime, next_chapter=context.chapter_number + 1)
        self.views = {
            actor.id: build_actor_view(
                self.runtime, self.lexicon, actor_id=actor.id, now=context.now,
                vetoes=actor_safe_vetoes(vetoes[actor.id]), boundary_refs=boundary_refs,
            )
            for actor in actors
        }
        return self._turns(list(self.views.values()))

    def _turns(
        self, views: Sequence[ActorView], feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> NodeResults:
        if views:
            self._check_snapshot()
        return take_turns(
            self.service.actor_runner, context=self.context, views=views, policy=self.policy.world,
            calendar=self.calendar, lexicon=self.lexicon, save_trace=self._save_trace, feedback=feedback,
        )

    def _settled(self, results: NodeResults) -> list[TickCandidate]:
        for failure in results.failures:
            self._drop(
                failure.candidate_id, origin=failure.origin, actor_id=failure.actor_id, intent_id=failure.intent_id,
                reason=failure.reason, issues=failure.issues, trace_id=failure.trace_id,
            )
        return results.candidates

    def _save_trace(self, prompt_trace: dict) -> str:
        return self.service.save_prompt_trace(self.session, prompt_trace)

    def _hold(self, candidate: TickCandidate, draft: ConflictDraft, *, extra: Mapping[str, Any]) -> None:
        # A Codex review may decide the conflict: a stale tick ends before it pays for one.
        self._check_snapshot()
        holder = self.conflicts.hold(draft, extra=extra)
        if holder:
            self._drop_candidate(candidate, ALREADY_HELD, [ConstraintIssue(
                ALREADY_HELD, f"Open world conflict {holder} already holds this change.", candidate.intent_id,
            )], conflict_id=holder)

    # checks

    def _check(self, collected: Sequence[TickCandidate]) -> list[TickCandidate]:
        """The candidates that pass their checks, in collection order; outcomes, turns and drift get one retry."""
        order = {candidate.candidate_id: index for index, candidate in enumerate(collected)}
        passed: list[TickCandidate] = []
        outcome_issues: dict[str, list[ConstraintIssue]] = {}
        turn_issues: dict[str, list[ConstraintIssue]] = {}
        drift_issues: dict[str, list[ConstraintIssue]] = {}
        actor_names: set[str] = set()
        for candidate in collected:
            issues = self._issues(candidate)
            issues.extend(_actor_name_issues(candidate, actor_names))
            if not issues:
                passed.append(candidate)
                actor_names.update(normalized_name(actor.name) for actor in candidate.effects.new_actors)
            elif any(issue.code == "touches_onstage_entity" for issue in issues):
                staged = self._drop_candidate(candidate, CONSTRAINT_VIOLATION, issues)
                if staged is not None:
                    passed.append(staged)
            elif candidate.origin == "adjudication":
                outcome_issues[candidate.intent_id] = issues
            elif candidate.origin == "drift":
                drift_issues[candidate.candidate_id.removeprefix("drift:")] = issues
            elif candidate.origin == "actor_turn":
                turn_issues[candidate.actor_id] = issues
            else:
                self._drop_candidate(candidate, CONSTRAINT_VIOLATION, issues)
        if outcome_issues:
            self._new_actor_budget_remaining = max(0, self.policy.world.max_new_actors_per_tick
                                                   - sum(len(c.effects.new_actors) for c in passed))
            intents = [self.runtime.world.nodes_by_id[intent_id] for intent_id in outcome_issues]
            passed.extend(self._recheck(self._adjudicate_all(intents, outcome_issues), actor_names=actor_names))
        if turn_issues:
            views = [self.views[actor_id] for actor_id in turn_issues]
            passed.extend(self._recheck(self._turns(views, feedback=actor_safe_feedback(turn_issues, self.views))))
        if drift_issues:
            variables = [self.runtime.world.nodes_by_id[ref] for ref in drift_issues]
            passed.extend(self._recheck(self._drift(variables, feedback=drift_issues)))
        return sorted(passed, key=lambda candidate: order[candidate.candidate_id])

    def _recheck(self, results: NodeResults, *, actor_names: set[str] | None = None) -> list[TickCandidate]:
        passed = []
        actor_names = set(actor_names or ())
        for candidate in self._settled(results):
            issues = self._issues(candidate)
            issues.extend(_actor_name_issues(candidate, actor_names))
            if issues:
                staged = self._drop_candidate(candidate, CONSTRAINT_VIOLATION, issues)
                if staged is not None:
                    passed.append(staged)
            else:
                passed.append(candidate)
                actor_names.update(normalized_name(actor.name) for actor in candidate.effects.new_actors)
        return passed

    def _issues(self, candidate: TickCandidate) -> list[ConstraintIssue]:
        """The candidate's issues on its own: the running-state check with nothing accepted yet."""
        return self._running_check().issues(candidate)

    def _running_check(self) -> RunningStateCheck:
        return RunningStateCheck(
            self.session, context=self.context, runtime=self.runtime, lexicon=self.lexicon, calendar=self.calendar,
            tick_id=self.tick_id, travel_tolerance=self.policy.review.travel_tolerance,
            onstage_refs=self.onstage_refs, chapter_delta=self.chapter,
        )

    def _admitted(self, base: Sequence[TickCandidate]) -> list[TickCandidate]:
        """The base candidates that hold against the running state, in order (Ruling P29).

        Each is checked against the ones accepted before it; one that fails is dropped. The in-tick
        resolutions follow, each one's candidates together: a resolution whose change no longer holds
        against the tick's canon goes to a human (Ruling P30).
        """
        check = self._running_check()
        admitted = []
        new_actor_count = 0

        def admit(candidate: TickCandidate) -> list[ConstraintIssue]:
            nonlocal new_actor_count
            count = len(candidate.effects.new_actors)
            if new_actor_count + count > self.policy.world.max_new_actors_per_tick:
                return [ConstraintIssue("new_actor_budget", "Candidate exceeds the remaining new actor budget",
                                        candidate.intent_id)]
            issues = check.admit(candidate)
            if not issues:
                new_actor_count += count
            return issues

        for candidate in base:
            issues = admit(candidate)
            if issues:
                staged = self._drop_candidate(candidate, CONSTRAINT_VIOLATION, issues)
                if staged is not None and not check.admit(staged):
                    admitted.append(staged)
            else:
                admitted.append(candidate)
        for resolution in list(self.conflicts.resolutions):
            mark = check.mark()
            actor_mark = new_actor_count
            for candidate in resolution.candidates:
                issues = admit(candidate)
                if not issues:
                    continue
                check.reset(mark)
                new_actor_count = actor_mark
                # The open conflict holds the intent; do not also stage it for the planner.
                self._drop(
                    candidate.candidate_id, origin=candidate.origin, actor_id=candidate.actor_id,
                    intent_id=candidate.intent_id,
                    reason=("touches_onstage_entity" if any(issue.code == "touches_onstage_entity" for issue in issues)
                            else CONSTRAINT_VIOLATION),
                    issues=issues, conflict_id=resolution.conflict_id,
                )
                self.conflicts.cancel(
                    resolution.conflict_id, failure_reason=CONFLICT_STALE,
                    detail="; ".join(f"{issue.code}: {issue.message}" for issue in issues),
                )
                break
        return admitted

    # build and commit

    def _commit(self, base: list[TickCandidate], *, spread_news: bool = True) -> tuple[list[str], list[dict[str, str]]]:
        """Build the tick's delta, dropping what does not build, and commit it with the in-tick resolutions.

        Each pass checks what is left against the running state first. Each retry drops a candidate or a
        resolution, so the loop ends.
        """
        context = self.context
        # Canon read before the project lock: a writer landing meanwhile changes the snapshot checked below.
        queue = latest_news_in_transit(self.session, context.project_id)
        while True:
            base = self._admitted(base)
            candidates = [*base, *(item for resolution in self.conflicts.resolutions for item in resolution.candidates)]
            news = self._news(candidates, queue) if spread_news else PropagatedNews(candidate=None, in_transit=queue)
            built = [*candidates, news.candidate] if news.candidate is not None else candidates
            build = self.builder.build(
                context=context, candidates=built, news_in_transit=news.in_transit, tick_id=self.tick_id,
                runtime=self.runtime,
            )
            errors = [issue for issue in build.issues if issue.severity == "error"]
            if errors:
                by_id = {candidate.candidate_id: candidate for candidate in built}
                bad = {issue.candidate_id for issue in errors}
                if not bad <= by_id.keys():
                    messages = [issue.message for issue in errors]
                    raise RuntimeError(f"World tick {self.tick_id} does not build: {messages}")
                for candidate_id in sorted(bad):
                    self._drop_candidate(by_id[candidate_id], BUILD_ERROR, [
                        ConstraintIssue(issue.code, issue.message, "")
                        for issue in errors if issue.candidate_id == candidate_id
                    ])
                spread_news = spread_news and NEWS_CANDIDATE_ID not in bad
                base = [candidate for candidate in base if candidate.candidate_id not in bad]
                for resolution in list(self.conflicts.resolutions):
                    if any(candidate.candidate_id in bad for candidate in resolution.candidates):
                        self.conflicts.cancel(
                            resolution.conflict_id, failure_reason=BUILD_ERROR,
                            detail="The decided option's changes do not build against canon.",
                        )
                continue
            delta = build.delta
            warnings = [
                {"code": issue.code, "message": issue.message, "candidate_id": issue.candidate_id}
                for issue in build.issues if issue.severity == "warning"
            ]
            changes = any((
                delta.node_patches, delta.edge_patches, delta.fact_patches, delta.cognition_patches,
                delta.narrative_patches,
            ))
            if not changes and not self.conflicts.resolutions and not self.responses:
                # A quiet tick takes no project lock and holds none through the rest of the world step, but the
                # conflicts it raised still rest on the canon it read (Ruling P27).
                self._expire_rewrite_conflicts()
                self._check_snapshot()
                return [], warnings
            # Lifecycle rows follow the same conflict-before-project lock order as decisions.
            repo = WorldConflictRepository(self.session)
            responses = [repo.get(context.project_id, row.id, for_update=True)
                         for row in sorted(self.responses, key=lambda row: row.id)]
            self._expire_rewrite_conflicts()
            self._lock_fresh_canon()
            try:
                with self.session.begin_nested():
                    graph_delta_ids = self._commit_delta(delta) if changes else ()
                    self.conflicts.close_resolutions(graph_delta_ids=graph_delta_ids, warnings=warnings)
                    statuses = {node.id: self.runtime.world.get_state(node.id).get("status")
                                for node in self.runtime.world.nodes_by_id.values() if node.node_type == "intent"}
                    for candidate in candidates:
                        statuses.update(candidate.intent_status_changes)
                    reconcile_plot_responses(
                        self.session, [row for row in responses if row is not None],
                        latest_chapter=context.chapter_number, book_complete=self.book_complete,
                        finished_refs={ref for ref, status in statuses.items() if status != "held"},
                    )
                    self.session.flush()
            except ResolutionUnavailable as exc:
                # The SAVEPOINT rolled the commit back with it; build again without that option.
                self.conflicts.cancel(exc.conflict_id, failure_reason=OPTION_UNAVAILABLE, detail=str(exc))
                continue
            return list(graph_delta_ids), warnings

    def _expire_rewrite_conflicts(self) -> None:
        """Expire the retired ticks' open conflicts, and after a rewrite the chapter conflicts over a drafted next
        chapter, locking their rows only now, after the tick's last LLM call.

        A conflict resolved meanwhile is no longer open and stays as it was decided.
        """
        if self.retired_tick_ids:
            expire_retired_tick_conflicts(
                self.session, project_id=self.context.project_id, chapter_number=self.context.chapter_number,
                tick_ids=self.retired_tick_ids,
            )
        if self.rewritten:
            expire_next_chapter_conflicts(
                self.session, project_id=self.context.project_id, chapter_number=self.context.chapter_number,
            )

    def _lock_fresh_canon(self) -> None:
        """Take the project lock and check the canon the tick reasoned on still stands (Ruling P27).

        From here on no other canon writer can commit; the lock comes after the conflict rows' locks.
        """
        self.session.execute(
            select(Project.id).where(Project.id == self.context.project_id).with_for_update(key_share=True)
        )
        self._check_snapshot()

    def _check_snapshot(self) -> None:
        """Raise CanonStaleVersion once canon moved since the tick read it (Ruling P27); no lock is taken.

        Before each LLM phase it ends a stale tick before it pays for more calls.
        """
        context = self.context
        if BookStateRepository(self.session).latest_world_snapshot_id(
            context.project_id, context.chapter_number,
        ) != self.snapshot_id:
            # Canon changed while the tick was reasoning: its view is stale, so the step rolls back and retries.
            raise CanonStaleVersion(f"Canon moved under world tick {self.tick_id}")

    def _news(self, candidates: Sequence[TickCandidate], queue: Sequence[NewsItem]) -> PropagatedNews:
        """One propagation of the chapter's events, the events that will commit, and the queue."""
        context = self.context
        news = propagation_candidate(
            self.runtime, self.lexicon,
            events=[
                *(chapter_events(self.chapter) if self.chapter is not None else []),
                *tick_events(context.project_id, self.tick_id, candidates),
            ],
            in_transit=queue, now=context.now, candidate_id=NEWS_CANDIDATE_ID, summary="",
        )
        return unprotected_news(self.runtime, news, now=context.now)

    def _commit_delta(self, delta: GraphDelta) -> tuple[str, ...]:
        context = self.context
        outcome = self.service.admission.commit_world_tick(
            session=self.session,
            project_id=context.project_id,
            chapter_number=context.chapter_number,
            approved_changes=ApprovedGraphDeltaSet(
                project_id=context.project_id, chapter_number=context.chapter_number, graph_deltas=[delta],
            ),
            tick_id=self.tick_id,
        )
        if outcome.idempotent:
            # The run began with no delta of this tick, so another run committed one meanwhile.
            raise CanonStaleVersion(f"World tick {self.tick_id} was committed by another run")
        return outcome.graph_delta_ids

    # drops

    def _drop_candidate(
        self, candidate: TickCandidate, reason: str, issues: Sequence[ConstraintIssue], *, conflict_id: str = "",
    ) -> TickCandidate | None:
        touches_onstage = any(issue.code == "touches_onstage_entity" for issue in issues)
        if touches_onstage:
            reason = "touches_onstage_entity"
        self._drop(
            candidate.candidate_id, origin=candidate.origin, actor_id=candidate.actor_id,
            intent_id=candidate.intent_id, reason=reason, issues=issues, conflict_id=conflict_id,
        )
        if not touches_onstage:
            return None
        intent = self.runtime.world.get_node(candidate.intent_id)
        if (intent is None or intent.node_type != "intent"
                or self.runtime.world.get_state(intent.id).get("status") not in {"planned", "due", "held"}):
            return None
        # Keep the intent available to the planner; none of the rejected physical effects survive.
        return TickCandidate(
            candidate_id=candidate.candidate_id, origin="onstage", intent_id=intent.id,
            summary=candidate.summary, intent_status_changes={intent.id: "due"},
            effects=WorldEffects(entity_changes=[
                {"ref": intent.id, "field": "requires_onstage", "new_value": True},
            ]),
        )

    def _drop(
        self, candidate_id: str, *, origin: str, actor_id: str, intent_id: str, reason: str,
        issues: Sequence[ConstraintIssue], trace_id: str = "", conflict_id: str = "",
    ) -> None:
        self.dropped.append(candidate_id)
        self.session.add(DecisionEvent(
            project_id=self.context.project_id,
            chapter_number=self.context.chapter_number,
            scope="chapter",
            event_family="business_event",
            event_type=DecisionEventType.WORLD_TICK_CANDIDATE_DROPPED,
            actor_type="system",
            summary=f"World tick {self.tick_id} dropped candidate {candidate_id}: {reason}.",
            reason=reason,
            payload_json=json.dumps({
                "tick_id": self.tick_id, "candidate_id": candidate_id, "origin": origin, "actor_id": actor_id,
                "intent_id": intent_id, "reason": reason, "issues": [asdict(issue) for issue in issues],
                "trace_id": trace_id, "conflict_id": conflict_id,
            }, ensure_ascii=False, sort_keys=True),
            related_object_type="world_tick",
            related_object_id=self.tick_id,
        ))


def _chapter_onstage_refs(
    session: Session, project_id: str, chapter_number: int, chapter: GraphDelta | None,
) -> frozenset[str]:
    """The plan's cast plus every mover and event participant the chapter actually committed."""
    plan = session.scalar(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == chapter_number,
    ))
    refs = set(json.loads(plan.onstage_refs_json or "[]")) if plan is not None else set()
    if plan is not None and plan.pov_ref:
        refs.add(plan.pov_ref)
    if chapter is not None:
        refs.update(move.get("entity_id") for move in chapter.metadata.get("movements", []) if isinstance(move, dict))
        for patch in chapter.node_patches:
            if str(patch.op) == "create" and patch.node_type == "event" and isinstance(patch.new_value, dict):
                refs.update((patch.new_value.get("state") or {}).get("participant_ids") or [])
    return frozenset(ref for ref in refs if isinstance(ref, str) and ref)


def _chapter_ticks(session: Session, project_id: str, chapter_number: int) -> list[tuple[str, str]]:
    """``(delta id, tick id)`` of every world_engine delta in the chapter, in commit order."""
    rows = session.execute(
        select(GraphDeltaRow.id, GraphDeltaRow.metadata_json)
        .where(
            GraphDeltaRow.project_id == project_id,
            GraphDeltaRow.chapter_number == chapter_number,
            GraphDeltaRow.source_type == WORLD_ENGINE_SOURCE_TYPE,
        )
        .order_by(GraphDeltaRow.created_at, GraphDeltaRow.id)
    ).all()
    ticks = []
    for delta_id, metadata_json in rows:
        metadata = json.loads(metadata_json or "{}")
        tick_id = metadata.get("tick_id") if isinstance(metadata, dict) else None
        ticks.append((delta_id, tick_id if isinstance(tick_id, str) else ""))
    return ticks


def _rewritten(commit: CanonCommitRecord) -> bool:
    """Whether the commit replaced the chapter's earlier canon through a historical rewrite."""
    result = json.loads(commit.result_json or "{}")
    return isinstance(result, dict) and isinstance(result.get("historical_rewrite"), dict)


def _retired_tick_ids(commit: CanonCommitRecord) -> list[str]:
    """The ticks the commit's historical rewrite retired; none for a first commit of the chapter."""
    result = json.loads(commit.result_json or "{}")
    rewrite = result.get("historical_rewrite") if isinstance(result, dict) else None
    tick_ids = rewrite.get("retired_tick_ids") if isinstance(rewrite, dict) else None
    return [tick_id for tick_id in tick_ids if isinstance(tick_id, str)] if isinstance(tick_ids, list) else []


def _chapter_delta(session: Session, commit: CanonCommitRecord) -> GraphDelta | None:
    """The chapter_extraction delta of the commit's chapter, preferring the one the commit lists."""
    number = int(commit.chapter_number)
    deltas = [
        delta for delta in BookStateRepository(session).list_graph_deltas(
            commit.project_id, after_chapter=number - 1, through_chapter=number,
        )
        if delta.source_type == CHAPTER_EXTRACTION_SOURCE_TYPE
    ]
    listed = json.loads(commit.graph_delta_ids_json or "[]")
    owned = [delta for delta in deltas if isinstance(listed, list) and delta.id in listed]
    pool = owned or deltas
    return pool[-1] if pool else None


def _window(session: Session, chapter: GraphDelta | None, project_id: str, number: int) -> tuple[int, int]:
    """The tick's ``now`` and ``window_start``: the chapter's end and start story minutes."""
    if chapter is not None and chapter.story_time_end is not None:
        now = chapter.story_time_end
        start = chapter.story_time_start if chapter.story_time_start is not None else now
    else:
        now = BookStateQuery(session).current_story_minute(project_id, as_of_chapter=number) or 0
        start = now
    return now, min(start, now)


def _interactions(chapter: GraphDelta | None) -> list[Mapping]:
    raw = chapter.metadata.get("intent_interactions") if chapter is not None else None
    return [item for item in raw if isinstance(item, Mapping)] if isinstance(raw, list) else []


def _unique(nodes: Sequence[WorldNode], *, excluded: set[str]) -> list[WorldNode]:
    seen = set(excluded)
    unique = []
    for node in nodes:
        if node.id not in seen:
            seen.add(node.id)
            unique.append(node)
    return unique


__all__ = [
    "ALREADY_HELD",
    "CONSTRAINT_VIOLATION",
    "MAX_ADJUDICATION_INTENTS_PER_TICK",
    "NEWS_CANDIDATE_ID",
    "PROMPT_TOO_LARGE_DEFERRED",
    "TICK_ID_PREFIX",
    "WorldTickResult",
    "WorldTickService",
]
