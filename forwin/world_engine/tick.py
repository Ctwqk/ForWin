"""The world tick: the first post-Canon step moves the offstage world on after each chapter.

``WorldTickService.run`` works on the BookState as of the chapter just committed (N), over the
chapter's story minutes from its start (``window_start``) to its end (``now``):

1. Collect candidates: the chapter's intent interactions, clocks that ran out, due intents (onstage
   ones go to planning, offstage ones are adjudicated) and the due actors' turns.
2. Check each candidate against the hard rules and the world gate on its own delta. An adjudicated
   outcome or an actor turn that fails is asked for again once, with the errors; whatever still fails
   is dropped with a WORLD_TICK_CANDIDATE_DROPPED event.
3. Classify impacts and keep major events within the tick's budget.
4. Hold back for review whatever breaks a narrative boundary or is major or above (tick_conflicts).
5. Spread the news of what will commit, build one world_engine delta, and commit it through canon
   admission together with the side effects of the conflicts decided in the tick.

An LLM outage never fails the tick: the node is skipped and the deterministic rest still commits.
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
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reviewer import CHAPTER_EXTRACTION_SOURCE_TYPE, BookStateReviewGate
from forwin.book_state.story_clock import StoryCalendar
from forwin.canon.admission import CanonAdmissionService, CanonStaleVersion
from forwin.generation.gate_delegation import GateAuditWriter
from forwin.models.audit import DecisionEvent
from forwin.models.book_state import GraphDeltaRow
from forwin.models.canon import CanonCommitRecord
from forwin.models.project import Project
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, WorldNode
from forwin.runtime.policy import RuntimePolicy

from .actor_turns import ActorTurnRunner, ActorView, build_actor_view, rank_actors
from .adjudicator import WorldAdjudicator
from .clocks import expire_clocks
from .conflict_delegate import WorldConflictDelegate
from .conflict_repository import WorldConflictRepository
from .conflict_resolution import PropagatedNews, propagation_candidate
from .conflict_types import ConflictDraft
from .constraints import ConstraintIssue, apply_budgets, check_hard_rules, check_narrative_boundaries, classify_impact
from .delta_builder import WorldTickDeltaBuilder
from .intents import apply_intent_interactions, due_onstage_intents, onstage_bookkeeping, select_due_intents
from .propagation import chapter_events, latest_news_in_transit, tick_events
from .tick_conflicts import BUILD_ERROR, OPTION_UNAVAILABLE, ResolutionUnavailable, TickConflicts, review_draft
from .tick_nodes import (
    PROMPT_TOO_LARGE,
    NodeResults,
    actor_safe_feedback,
    actor_safe_vetoes,
    adjudicate,
    protected_refs,
    take_turns,
    unavailable,
)
from .types import WORLD_ENGINE_SOURCE_TYPE, NewsItem, TickCandidate, TickContext, WorldEffects, tick_seed


# A world step's tick id: the prefix and the chapter's canon commit id. A direct resolve uses "conflict-".
TICK_ID_PREFIX = "tick-"
# The tick's one news candidate; it goes after every candidate whose events it spreads.
NEWS_CANDIDATE_ID = "propagation"
# Due offstage intents one tick adjudicates, earliest first; the rest stay due for the next tick.
MAX_ADJUDICATION_INTENTS_PER_TICK = 20
# Why a checked candidate left the tick: it broke a check, or an open conflict already holds such a change.
CONSTRAINT_VIOLATION = "constraint_violation"
ALREADY_HELD = "already_held"


@dataclass(frozen=True)
class WorldTickResult:
    tick_id: str
    graph_delta_ids: list[str]
    conflict_ids: list[str]
    dropped_candidate_ids: list[str]
    # Over the major-event budget: their intents wait for a later tick, or they are simply not committed.
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
    ) -> None:
        self.llm_client = llm_client
        self.admission = admission
        self.adjudicator = adjudicator
        self.actor_runner = actor_runner
        self.conflict_delegate = conflict_delegate
        self.audit_writer_factory = audit_writer_factory
        self.save_prompt_trace = save_prompt_trace

    def run(self, session: Session, commit: CanonCommitRecord, *, policy: RuntimePolicy) -> WorldTickResult:
        tick_id = f"{TICK_ID_PREFIX}{commit.id}"
        if not policy.world.enabled:
            return _skipped(tick_id, "world_disabled")
        number = int(commit.chapter_number or 0)
        # A rewritten chapter's world step runs after later chapters landed: the world already moved on.
        latest = BookStateRepository(session).latest_canon_chapter(commit.project_id) or 0
        if number != latest:
            return _skipped(tick_id, "chapter_not_latest")
        ticks = _chapter_ticks(session, commit.project_id, number)
        own = [delta_id for delta_id, delta_tick_id in ticks if delta_tick_id == tick_id]
        if own:
            # A retry of this very tick after it committed: its delta stands.
            return WorldTickResult(
                tick_id=tick_id, graph_delta_ids=own, conflict_ids=[], dropped_candidate_ids=[],
                postponed_candidate_ids=[], skipped_reason="tick_already_committed",
            )
        # Rewriting the latest chapter keeps the tick that ran after it; a second tick would move the world
        # on twice for one chapter. A direct resolve's delta ("conflict-") is no tick of the chapter.
        if any(delta_tick_id.startswith(TICK_ID_PREFIX) for _delta_id, delta_tick_id in ticks):
            return _skipped(tick_id, "chapter_already_ticked")
        return _Tick(self, session, commit, policy=policy, tick_id=tick_id).run()


class _Tick:
    """One run of the tick: its context, its candidates as they pass each phase, and what it dropped."""

    def __init__(
        self, service: WorldTickService, session: Session, commit: CanonCommitRecord, *, policy: RuntimePolicy,
        tick_id: str,
    ) -> None:
        self.service = service
        self.session = session
        self.policy = policy
        self.tick_id = tick_id
        project_id, number = commit.project_id, int(commit.chapter_number)
        self.chapter = _chapter_delta(session, commit)
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
        # Without a calendar only the lexicon's aliases name units, as in the builder and the actor runner.
        self.rules_calendar = self.calendar if self.calendar is not None else StoryCalendar("", 0, ())
        self.lexicon = BookLexiconRepository(session).get(project_id)
        self.builder = WorldTickDeltaBuilder(session)
        self.conflicts = TickConflicts(
            session, context=self.context, runtime=self.runtime, policy=policy, tick_id=tick_id,
            delegate=service.conflict_delegate, audit_writer_factory=service.audit_writer_factory,
        )
        self.views: dict[str, ActorView] = {}
        self.slip_conflicts: list[ConflictDraft] = []
        self.dropped: list[str] = []

    def run(self) -> WorldTickResult:
        checked = self._check(self._collect())
        classified = [
            candidate.model_copy(update={"impact_level": classify_impact(self.runtime, candidate)})
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
                candidate, context=self.context, runtime=self.runtime,
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
        graph_delta_ids, warnings = self._commit(base)
        return WorldTickResult(
            tick_id=self.tick_id,
            graph_delta_ids=graph_delta_ids,
            conflict_ids=list(self.conflicts.conflict_ids),
            dropped_candidate_ids=list(self.dropped),
            postponed_candidate_ids=[*postponed, *deferred],
            warnings=warnings,
        )

    # collection

    def _collect(self) -> list[TickCandidate]:
        """The tick's candidates, event producers first: interactions, clocks, onstage bookkeeping, outcomes, turns."""
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
        onstage = onstage_bookkeeping(
            runtime,
            onstage_due=_unique([*due.onstage, *onstage_named], excluded=touched),
            already_due=due_onstage_intents(runtime),
            interactions=interactions,
            now=now,
            max_slips=self.policy.world.max_onstage_slips,
            chapter_number=self.context.chapter_number,
        )
        # A slip past the cap comes back only as a conflict draft holding the slip.
        self.slip_conflicts = onstage.conflicts
        named_ids = {node.id for node in named}

        def scheduled(node: WorldNode) -> tuple[int, str]:
            # A clock brought its named intent forward to now.
            at = runtime.world.get_state(node.id).get("scheduled_at")
            return (at if type(at) is int and node.id not in named_ids else now, node.id)

        offstage = sorted(_unique([*due.offstage, *offstage_named], excluded=touched), key=scheduled)
        adjudicated = self._settled(self._adjudicate_due(offstage[:MAX_ADJUDICATION_INTENTS_PER_TICK]))
        turns = self._settled(self._take_turns())
        return [*applied, *clocks, *onstage.candidates, *adjudicated, *turns]

    def _adjudicate_due(self, intents: Sequence[WorldNode]) -> NodeResults:
        results = self._adjudicate(intents)
        if len(intents) > 1 and any(failure.reason == PROMPT_TOO_LARGE for failure in results.failures):
            # The earlier half may fit; the later half stays due for a later tick.
            results = self._adjudicate(intents[:len(intents) // 2])
        return results

    def _adjudicate(
        self, intents: Sequence[WorldNode], feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> NodeResults:
        if not intents:
            return NodeResults()
        if self.service.llm_client is None:
            return unavailable(intents=intents)
        return adjudicate(
            self.service.adjudicator, context=self.context, intents=intents, runtime=self.runtime,
            lexicon=self.lexicon, calendar=self.calendar, save_trace=self._save_trace, feedback=feedback,
        )

    def _take_turns(self) -> NodeResults:
        context = self.context
        actors = rank_actors(self.runtime, now=context.now, limit=self.policy.world.max_actor_turns_per_tick)
        if not actors:
            return NodeResults()
        if self.service.llm_client is None:
            return unavailable(actor_ids=[actor.id for actor in actors])
        vetoes = actor_safe_vetoes(WorldConflictRepository(self.session).list(context.project_id, status="resolved"))
        boundary_refs = protected_refs(self.runtime, next_chapter=context.chapter_number + 1)
        self.views = {
            actor.id: build_actor_view(
                self.runtime, self.lexicon, actor_id=actor.id, now=context.now, vetoes=vetoes,
                boundary_refs=boundary_refs,
            )
            for actor in actors
        }
        return self._turns(list(self.views.values()))

    def _turns(
        self, views: Sequence[ActorView], feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> NodeResults:
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
        holder = self.conflicts.hold(draft, extra=extra)
        if holder:
            self._drop_candidate(candidate, ALREADY_HELD, [ConstraintIssue(
                ALREADY_HELD, f"Open world conflict {holder} already holds this change.", candidate.intent_id,
            )], conflict_id=holder)

    # checks

    def _check(self, collected: Sequence[TickCandidate]) -> list[TickCandidate]:
        """The candidates that pass their checks, in collection order; outcomes and turns get one retry."""
        order = {candidate.candidate_id: index for index, candidate in enumerate(collected)}
        passed: list[TickCandidate] = []
        outcome_issues: dict[str, list[ConstraintIssue]] = {}
        turn_issues: dict[str, list[ConstraintIssue]] = {}
        for candidate in collected:
            issues = self._issues(candidate)
            if not issues:
                passed.append(candidate)
            elif candidate.origin == "adjudication":
                outcome_issues[candidate.intent_id] = issues
            elif candidate.origin == "actor_turn":
                turn_issues[candidate.actor_id] = issues
            else:
                self._drop_candidate(candidate, CONSTRAINT_VIOLATION, issues)
        if outcome_issues:
            intents = [self.runtime.world.nodes_by_id[intent_id] for intent_id in outcome_issues]
            passed.extend(self._recheck(self._adjudicate(intents, feedback=outcome_issues)))
        if turn_issues:
            views = [self.views[actor_id] for actor_id in turn_issues]
            passed.extend(self._recheck(self._turns(views, feedback=actor_safe_feedback(turn_issues, self.views))))
        return sorted(passed, key=lambda candidate: order[candidate.candidate_id])

    def _recheck(self, results: NodeResults) -> list[TickCandidate]:
        passed = []
        for candidate in self._settled(results):
            issues = self._issues(candidate)
            if issues:
                self._drop_candidate(candidate, CONSTRAINT_VIOLATION, issues)
            else:
                passed.append(candidate)
        return passed

    def _issues(self, candidate: TickCandidate) -> list[ConstraintIssue]:
        """Every hard-rule issue is an error; the world gate's errors count as well."""
        context = self.context
        issues: list[ConstraintIssue] = []
        # An interaction mirrors chapter text the gate already passed (Ruling P13).
        if candidate.origin != "intent_interaction":
            issues.extend(check_hard_rules(
                self.runtime, self.lexicon, candidate, now=context.now, calendar=self.rules_calendar,
                window_start=context.window_start,
            ))
        # Bookkeeping alone gives the gate nothing to check; the tick's build still resolves its refs.
        if candidate.effects != WorldEffects() or candidate.new_intents or candidate.agenda_changes:
            issues.extend(self._gate_issues(candidate))
        return issues

    def _gate_issues(self, candidate: TickCandidate) -> list[ConstraintIssue]:
        context = self.context
        build = self.builder.build(context=context, candidates=[candidate], news_in_transit=(), tick_id=self.tick_id)
        if build.delta is None:
            return [
                ConstraintIssue(issue.code, issue.message, "") for issue in build.issues if issue.severity == "error"
            ]
        verdict = BookStateReviewGate(self.session).review(
            ApprovedGraphDeltaSet(
                project_id=context.project_id, chapter_number=context.chapter_number, graph_deltas=[build.delta],
            ),
            # The tick builds on chapter N, as its delta will.
            base_chapter=context.chapter_number,
            travel_tolerance=self.policy.review.travel_tolerance,
        )
        return [
            ConstraintIssue(issue.code, issue.message, issue.target_ref)
            for issue in verdict.issues if issue.severity == "error"
        ]

    # build and commit

    def _commit(self, base: list[TickCandidate]) -> tuple[list[str], list[dict[str, str]]]:
        """Build the tick's delta, dropping what does not build, and commit it with the in-tick resolutions.

        Each retry drops a candidate or a resolution, so the loop ends.
        """
        context = self.context
        # From here on no other canon writer can commit; the lock comes after the conflict rows' locks.
        self.session.execute(
            select(Project.id).where(Project.id == context.project_id).with_for_update(key_share=True)
        )
        if BookStateRepository(self.session).latest_world_snapshot_id(
            context.project_id, context.chapter_number,
        ) != self.snapshot_id:
            # Canon changed while the tick was reasoning: its view is stale, so the step rolls back and retries.
            raise CanonStaleVersion(f"Canon moved under world tick {self.tick_id}")
        queue = latest_news_in_transit(self.session, context.project_id)
        spread_news = True
        while True:
            candidates = [*base, *(item for resolution in self.conflicts.resolutions for item in resolution.candidates)]
            news = self._news(candidates, queue) if spread_news else PropagatedNews(candidate=None, in_transit=queue)
            built = [*candidates, news.candidate] if news.candidate is not None else candidates
            build = self.builder.build(
                context=context, candidates=built, news_in_transit=news.in_transit, tick_id=self.tick_id,
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
            if not changes and not self.conflicts.resolutions:
                return [], warnings
            try:
                with self.session.begin_nested():
                    graph_delta_ids = self._commit_delta(delta) if changes else ()
                    self.conflicts.close_resolutions(graph_delta_ids=graph_delta_ids, warnings=warnings)
                    self.session.flush()
            except ResolutionUnavailable as exc:
                # The SAVEPOINT rolled the commit back with it; build again without that option.
                self.conflicts.cancel(exc.conflict_id, failure_reason=OPTION_UNAVAILABLE, detail=str(exc))
                continue
            return list(graph_delta_ids), warnings

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
        return PropagatedNews(candidate=self._unprotected(news.candidate), in_transit=news.in_transit)

    def _unprotected(self, news: TickCandidate | None) -> TickCandidate | None:
        """The news without what an active protection forbids an observer to know."""
        if news is None:
            return None
        violations = check_narrative_boundaries(self.runtime, news, next_chapter=self.context.chapter_number + 1)
        forbidden = set()
        for violation in violations:
            payload = self.runtime.narrative.nodes_by_id[violation.constraint_node_id].payload
            fact = str(payload.get("fact_ref") or "")
            forbidden.add((payload.get("observer_ref"), f"{FACT_REF_PREFIX}{fact.removeprefix(FACT_REF_PREFIX)}"))
        knowledge = [item for item in news.effects.knowledge if (item.observer_ref, item.fact_ref) not in forbidden]
        if not knowledge:
            return None
        return news.model_copy(update={"effects": WorldEffects(knowledge=knowledge)})

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
    ) -> None:
        self._drop(
            candidate.candidate_id, origin=candidate.origin, actor_id=candidate.actor_id,
            intent_id=candidate.intent_id, reason=reason, issues=issues, conflict_id=conflict_id,
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
    "TICK_ID_PREFIX",
    "WorldTickResult",
    "WorldTickService",
]
