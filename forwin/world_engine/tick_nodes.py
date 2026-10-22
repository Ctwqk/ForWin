"""The world tick's LLM nodes: due offstage intents adjudicated, and due actors' turns.

Each node settles into a tick candidate or fails with a reason. A failed node changes nothing, so its
intent stays planned and its actor keeps its turn for a later tick.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field

from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import IMPACT_LEVELS
from forwin.book_state.story_clock import StoryCalendar
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.protocol.book_state import WorldNode
from forwin.runtime.policy import WorldPolicy
from forwin.writer.prompt_core.world_engine import ACTOR_CONSTRAINT_FEEDBACK, ACTOR_VETO_SUMMARY

from .actor_turns import ActorTurnRunner, ActorView, turn_to_candidate
from .adjudicator import WorldAdjudicator
from .constraints import ConstraintIssue
from .types import (
    INVALID_OUTPUT,
    LLM_UNAVAILABLE,
    PROMPT_TOO_LARGE,
    AdjudicatedOutcome,
    TickCandidate,
    TickContext,
)


SaveTrace = Callable[[dict], str]


@dataclass(frozen=True)
class NodeFailure:
    candidate_id: str
    origin: str
    actor_id: str
    intent_id: str
    reason: str
    issues: tuple[ConstraintIssue, ...]
    trace_id: str = ""


@dataclass(frozen=True)
class NodeResults:
    candidates: list[TickCandidate] = field(default_factory=list)
    failures: list[NodeFailure] = field(default_factory=list)


def adjudication_candidate_id(intent_id: str) -> str:
    return f"adjudication:{intent_id}"


def actor_turn_candidate_id(actor_id: str) -> str:
    # turn_to_candidate names a turn the same way.
    return f"actor_turn:{actor_id}"


def _owner(intent: WorldNode) -> str:
    return str(intent.profile.get("owner_ref") or "").strip()


# adjudication


def outcome_candidate(intent: WorldNode, outcome: AdjudicatedOutcome) -> TickCandidate:
    """One adjudicated outcome as a tick candidate."""
    declared = intent.profile.get("impact_level")
    return TickCandidate(
        candidate_id=adjudication_candidate_id(intent.id),
        origin="adjudication",
        actor_id=_owner(intent),
        intent_id=intent.id,
        summary=outcome.summary,
        effects=outcome.effects,
        intent_status_changes={intent.id: outcome.result},
        # A resolved intent lands with the impact its owner declared; a disrupted one did not happen as planned.
        impact_level=declared if outcome.result == "resolved" and declared in IMPACT_LEVELS else "minor",
    )


def adjudicate(
    adjudicator: WorldAdjudicator,
    *,
    context: TickContext,
    intents: Sequence[WorldNode],
    runtime: BookStateRuntime,
    lexicon: BookLexicon,
    calendar: StoryCalendar | None,
    save_trace: SaveTrace,
    feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
) -> NodeResults:
    """One adjudication of ``intents``: a candidate per settled intent, a failure per unsettled one."""
    if not intents:
        return NodeResults()
    call = adjudicator.adjudicate(
        context=context, intents=intents, runtime=runtime, lexicon=lexicon, calendar=calendar, feedback=feedback,
    )
    trace_id = save_trace(call.prompt_trace)
    outcomes = {outcome.intent_id: outcome for outcome in call.result.outcomes} if call.result is not None else {}
    results = NodeResults()
    # Every intent the call left unsettled fails for the call's one reason.
    reason = call.failure_reason or INVALID_OUTPUT
    for intent in intents:
        outcome = outcomes.get(intent.id)
        if outcome is not None:
            results.candidates.append(outcome_candidate(intent, outcome))
            continue
        messages = [issue for issue in call.issues if intent.id in issue] or list(call.issues)
        results.failures.append(NodeFailure(
            candidate_id=adjudication_candidate_id(intent.id), origin="adjudication", actor_id=_owner(intent),
            intent_id=intent.id, reason=reason,
            issues=tuple(ConstraintIssue(reason, message, intent.id) for message in messages), trace_id=trace_id,
        ))
    return results


# actor turns


def take_turns(
    runner: ActorTurnRunner,
    *,
    context: TickContext,
    views: Sequence[ActorView],
    policy: WorldPolicy,
    calendar: StoryCalendar | None,
    lexicon: BookLexicon,
    save_trace: SaveTrace,
    feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
) -> NodeResults:
    """The actors' turns: a candidate per actor with a valid turn, a failure per actor without one."""
    if not views:
        return NodeResults()
    calls = runner.run(
        context=context, views=views, policy=policy, calendar=calendar, lexicon=lexicon, feedback=feedback,
    )
    # The actors of one batch share one call, so its trace is saved once.
    trace_ids: dict[int, str] = {}
    results = NodeResults()
    for call in calls:
        batch = id(call.prompt_trace)
        if batch not in trace_ids:
            trace_ids[batch] = save_trace(call.prompt_trace)
        if call.result is not None:
            # A turn with a result is valid; its issues only note what was set aside, such as a duplicate turn.
            results.candidates.append(turn_to_candidate(call, now=context.now, calendar=calendar, lexicon=lexicon))
            continue
        reason = call.failure_reason or INVALID_OUTPUT
        results.failures.append(NodeFailure(
            candidate_id=actor_turn_candidate_id(call.actor_id), origin="actor_turn", actor_id=call.actor_id,
            intent_id="", reason=reason,
            issues=tuple(ConstraintIssue(reason, message, call.actor_id) for message in call.issues),
            trace_id=trace_ids[batch],
        ))
    return results


def unavailable(*, intents: Sequence[WorldNode] = (), actor_ids: Sequence[str] = ()) -> NodeResults:
    """Without a model, every node fails as unavailable and nothing is asked."""
    return NodeResults(failures=[
        *(NodeFailure(
            adjudication_candidate_id(intent.id), "adjudication", _owner(intent), intent.id, LLM_UNAVAILABLE, (),
        ) for intent in intents),
        *(NodeFailure(actor_turn_candidate_id(actor_id), "actor_turn", actor_id, "", LLM_UNAVAILABLE, ())
          for actor_id in actor_ids),
    ])


# what an actor may be told


def protected_refs(runtime: BookStateRuntime, *, next_chapter: int) -> list[str]:
    """The refs active protections guard in ``next_chapter``: an actor may not touch them and is not told why."""
    refs: set[str] = set()
    for node in runtime.narrative.nodes_by_id.values():
        if node.node_type != "review_constraint" or node.status != "active":
            continue
        until = node.payload.get("until_chapter", 0)
        if type(until) is int and until != 0 and until < next_chapter:
            continue
        ref = str(node.payload.get("ref") or "").strip()
        if ref:
            refs.add(ref)
    return sorted(refs)


def actor_safe_vetoes(rows: Sequence[WorldPlanConflictRow]) -> list[WorldPlanConflictRow]:
    """Unsaved copies of resolved conflicts for the actors' views.

    A conflict's own summary may name a narrative boundary or a truth the actor does not know, so each
    copy only says the plan was vetoed; the vetoed plan itself is the actor's own.
    """
    return [
        WorldPlanConflictRow(
            id=row.id, project_id=row.project_id, status=row.status, actor_ref=row.actor_ref, intent_ref=row.intent_ref,
            decision_json=row.decision_json, proposed_change_json=row.proposed_change_json, story_time=row.story_time,
            detected_after_chapter=row.detected_after_chapter, summary=ACTOR_VETO_SUMMARY,
        )
        for row in rows
    ]


def actor_safe_feedback(
    issues_by_actor: Mapping[str, Sequence[ConstraintIssue]], views: Mapping[str, ActorView],
) -> dict[str, list[ConstraintIssue]]:
    """Constraint feedback an actor may read: the rule's code and only refs from its own view.

    A check's message can state objective truth, such as who really holds an item, so it is replaced.
    """
    safe: dict[str, list[ConstraintIssue]] = {}
    for actor_id, issues in issues_by_actor.items():
        known = views[actor_id].references()
        unique = dict.fromkeys((issue.code, issue.ref if issue.ref in known else "") for issue in issues)
        safe[actor_id] = [
            ConstraintIssue(code, ACTOR_CONSTRAINT_FEEDBACK.format(code=code), ref) for code, ref in unique
        ]
    return safe


__all__ = [
    "INVALID_OUTPUT",
    "LLM_UNAVAILABLE",
    "PROMPT_TOO_LARGE",
    "NodeFailure",
    "NodeResults",
    "actor_safe_feedback",
    "actor_safe_vetoes",
    "actor_turn_candidate_id",
    "adjudicate",
    "adjudication_candidate_id",
    "outcome_candidate",
    "protected_refs",
    "take_turns",
    "unavailable",
]
