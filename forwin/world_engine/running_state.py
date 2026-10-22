"""The running-state check: candidates checked one by one against what the ones accepted before them leave.

Each candidate passes the hard rules on the runtime as the accepted candidates change it, and the world
gate on the delta of the accepted candidates plus itself. Two changes that hold alone but contradict each
other, such as two transfers of one item or a move by an actor killed earlier in the same delta, are
caught on the second (Ruling P29). The world tick checks its base candidates and then its in-tick
resolutions this way; a direct conflict resolve checks the resolution's candidates against current canon
(Ruling P30).
"""

from __future__ import annotations

from copy import deepcopy

from sqlalchemy.orm import Session

from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import StoryCalendar
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta

from .constraints import ConstraintIssue, check_hard_rules
from .delta_builder import WorldTickDeltaBuilder
from .types import TickCandidate, TickContext, WorldEffects


class RunningStateCheck:
    """Check candidates in order against ``runtime`` (canon as of the context's chapter) plus the accepted ones.

    ``issues`` checks a candidate without accepting it; ``admit`` accepts it when it passes. ``mark`` and
    ``reset`` take back what a group of candidates accepted when a later one of the group fails.
    """

    def __init__(
        self,
        session: Session,
        *,
        context: TickContext,
        runtime: BookStateRuntime,
        lexicon: BookLexicon,
        calendar: StoryCalendar | None,
        tick_id: str,
        travel_tolerance: float,
    ) -> None:
        self.session = session
        self.context = context
        self.base = runtime
        self.runtime = runtime
        self.lexicon = lexicon
        # Without a calendar only the lexicon's aliases name units, as in the builder.
        self.calendar = calendar if calendar is not None else StoryCalendar("", 0, ())
        self.tick_id = tick_id
        self.travel_tolerance = travel_tolerance
        self.builder = WorldTickDeltaBuilder(session)
        self.accepted: list[TickCandidate] = []
        # The last candidate checked and the delta of the accepted ones plus it, for ``admit``.
        self._checked: tuple[TickCandidate, GraphDelta] | None = None

    def issues(self, candidate: TickCandidate) -> list[ConstraintIssue]:
        """Every hard-rule issue on the running runtime, and the world gate's errors on the running delta."""
        context = self.context
        issues: list[ConstraintIssue] = []
        # An interaction mirrors chapter text the gate already passed (Ruling P13).
        if candidate.origin != "intent_interaction":
            issues.extend(check_hard_rules(
                self.runtime, self.lexicon, candidate, now=context.now, calendar=self.calendar,
                window_start=context.window_start,
            ))
        # Bookkeeping alone gives the gate nothing to check; ``admit`` still builds it.
        if candidate.effects != WorldEffects() or candidate.new_intents or candidate.agenda_changes:
            built = self._build(candidate)
            if isinstance(built, list):
                return [*issues, *built]
            verdict = BookStateReviewGate(self.session).review(
                ApprovedGraphDeltaSet(
                    project_id=context.project_id, chapter_number=context.chapter_number, graph_deltas=[built],
                ),
                # The delta builds on the context's chapter, as the commit will.
                base_chapter=context.chapter_number,
                travel_tolerance=self.travel_tolerance,
            )
            issues.extend(
                ConstraintIssue(issue.code, issue.message, issue.target_ref)
                for issue in verdict.issues if issue.severity == "error"
            )
        return issues

    def admit(self, candidate: TickCandidate) -> list[ConstraintIssue]:
        """Accept ``candidate`` when it passes; its issues when it does not, leaving the running state as it was."""
        issues = self.issues(candidate)
        if issues:
            return issues
        checked = self._checked
        built = checked[1] if checked is not None and checked[0] is candidate else self._build(candidate)
        if isinstance(built, list):
            return built
        self.accepted.append(candidate)
        self.runtime = _applied(self.base, built)
        return []

    def mark(self) -> tuple[int, BookStateRuntime]:
        return len(self.accepted), self.runtime

    def reset(self, mark: tuple[int, BookStateRuntime]) -> None:
        count, runtime = mark
        del self.accepted[count:]
        self.runtime = runtime

    def _build(self, candidate: TickCandidate) -> GraphDelta | list[ConstraintIssue]:
        """The delta of the accepted candidates plus ``candidate``, or the candidate's build errors."""
        build = self.builder.build(
            context=self.context, candidates=[*self.accepted, candidate], news_in_transit=(), tick_id=self.tick_id,
        )
        if build.delta is None:
            self._checked = None
            return [ConstraintIssue(issue.code, issue.message, "") for issue in build.issues if issue.severity == "error"]
        self._checked = (candidate, build.delta)
        return build.delta


def _applied(runtime: BookStateRuntime, delta: GraphDelta) -> BookStateRuntime:
    """A copy of ``runtime`` whose objective world has ``delta`` applied; ``runtime`` is left as it was."""
    world = runtime.world
    states = dict(world.states_by_node_id)
    # A patch may write inside a nested state value, so the states it touches are copied whole.
    for patch in delta.node_patches:
        if patch.node_id in states:
            states[patch.node_id] = deepcopy(states[patch.node_id])
    applied = ObjectiveWorldGraph(
        nodes=list(world.nodes_by_id.values()),
        states_by_node_id=states,
        edges=list(world.edges_by_id.values()),
        facts=list(world.facts_by_id.values()),
    )
    applied.apply_delta(delta)
    return BookStateRuntime(
        project_id=runtime.project_id,
        as_of_chapter=runtime.as_of_chapter,
        world=applied,
        map_graph=runtime.map,
        cognition_by_observer=runtime.cognition_by_observer,
        narrative=runtime.narrative,
    )


__all__ = ["RunningStateCheck"]
