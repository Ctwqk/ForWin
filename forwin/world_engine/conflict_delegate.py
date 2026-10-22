"""Codex chooses one option for a world-plan conflict; the caller applies the choice or escalates.

The delegate only records its audit trail: it never writes the conflict, BookState or plans.
"""

from __future__ import annotations

import json
from typing import Any, Literal, cast

from pydantic import BaseModel, ConfigDict, Field, field_validator
from sqlalchemy.orm import Session

from forwin.audit.events import DecisionEventInfo, DecisionEventType
from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.runtime import BookStateRuntime
from forwin.generation.gate_delegation import (
    CodexCallEnvelope,
    CodexCallEventTypes,
    CodexCallOutcome,
    CodexCallVerdict,
    GateAuditWriter,
    run_codex_structured_call,
)
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.goals import load_goals_json
from forwin.planning.query import PlanningQuery
from forwin.runtime.policy import RuntimePolicy, role_models
from forwin.writer.prompt_core.world_engine import MAX_CONFLICT_REVIEW_PROMPT_CHARS, build_conflict_review_messages

from .conflict_types import ConflictOption


WORLD_CONFLICT_STAGE_KEY = "review.world_conflict"
WORLD_CONFLICT_DEFAULT_MODEL = "gpt-6-astra"
UPCOMING_CHAPTER_LIMIT = 3
_GATE_KIND = "world_conflict"
# Only the user amends the plan, so Codex choosing it hands the conflict to a human.
_ESCALATING_OPTIONS = frozenset({"amend_plan"})
# The review records no requested or completed event; a failed call escalates.
_EVENT_TYPES = CodexCallEventTypes(
    requested="", completed="", failed=DecisionEventType.WORLD_CONFLICT_ESCALATED,
)


class WorldConflictDelegation(BaseModel):
    model_config = ConfigDict(frozen=True)

    option: ConflictOption | None = None
    rationale: str = ""
    failure_reason: str = ""
    trace_id: str = ""
    escalate: bool = True


class _ConflictReviewAnswer(BaseModel):
    model_config = ConfigDict(extra="forbid")

    option: str
    rationale: str = Field(min_length=1)
    risk_level: Literal["low", "medium", "high"]
    evidence: list[str]


def _answer_model(options: tuple[str, ...]) -> type[_ConflictReviewAnswer]:
    """The answer contract for one conflict: an option it does not offer fails validation."""

    class ConflictReviewAnswer(_ConflictReviewAnswer):
        @field_validator("option")
        @classmethod
        def _offered(cls, value: str) -> str:
            if value not in options:
                raise ValueError(f"option must be one of {list(options)}")
            return value

    return ConflictReviewAnswer


def _output_schema(options: tuple[str, ...]) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "option": {"type": "string", "enum": list(options)},
            "rationale": {"type": "string"},
            "risk_level": {"type": "string", "enum": ["low", "medium", "high"]},
            "evidence": {"type": "array", "items": {"type": "string"}},
        },
        "required": ["option", "rationale", "risk_level", "evidence"],
        "additionalProperties": False,
    }


def _escalation_payload(
    conflict: WorldPlanConflictRow,
    *,
    failure_reason: str,
    answer: _ConflictReviewAnswer | None,
    requested_model: str,
    trace_id: str = "",
    actual_model: str = "",
    backend: str = "",
    **extra: Any,
) -> dict[str, Any]:
    return {
        "conflict_id": conflict.id,
        "failure_reason": failure_reason,
        "option": answer.option if answer is not None else "",
        "rationale": answer.rationale if answer is not None else "",
        "risk_level": answer.risk_level if answer is not None else "",
        "evidence": answer.evidence if answer is not None else [],
        "trace_id": trace_id,
        "requested_model": requested_model,
        "actual_model": actual_model,
        "backend": backend,
        **extra,
    }


def _escalation_verdict(conflict: WorldPlanConflictRow, outcome: CodexCallOutcome) -> CodexCallVerdict:
    answer = cast(_ConflictReviewAnswer | None, outcome.parsed)
    cause = outcome.failure_reason or (answer.option if answer is not None else "")
    return CodexCallVerdict(
        summary=f"World conflict {conflict.id} escalated to a human: {cause}.",
        reason=answer.rationale if answer is not None else outcome.failure_detail,
        actor_id=outcome.actual_model or "world-conflict-router",
        payload=_escalation_payload(
            conflict,
            failure_reason=outcome.failure_reason,
            answer=answer,
            requested_model=outcome.requested_model,
            trace_id=outcome.trace_id,
            actual_model=outcome.actual_model,
            backend=outcome.backend,
        ),
    )


def _escalation_event(
    envelope: CodexCallEnvelope,
    wording: CodexCallVerdict,
    *,
    actor_type: Literal["system", "worker"],
    related_object_type: str,
    related_object_id: str,
    parent_event_id: str = "",
    causal_root_id: str = "",
) -> DecisionEventInfo:
    return DecisionEventInfo(
        project_id=envelope.project_id,
        chapter_number=envelope.chapter_number,
        scope=envelope.scope,
        event_family="evaluation_verdict",
        event_type=DecisionEventType.WORLD_CONFLICT_ESCALATED,
        actor_type=actor_type,
        actor_id=wording.actor_id,
        summary=wording.summary,
        reason=wording.reason,
        payload=wording.payload,
        related_object_type=related_object_type,
        related_object_id=related_object_id,
        parent_event_id=parent_event_id,
        causal_root_id=causal_root_id,
    )


class WorldConflictDelegate:
    def __init__(self, llm_client: Any) -> None:
        self.llm_client = llm_client

    def decide(
        self,
        *,
        conflict: WorldPlanConflictRow,
        context_snapshot: dict[str, Any],
        policy: RuntimePolicy,
        audit_writer: GateAuditWriter,
    ) -> WorldConflictDelegation:
        """Codex's choice among the conflict's options; it escalates on any failure and on amend_plan."""
        options = tuple(json.loads(conflict.options_json or "[]"))
        preferred = role_models(policy, WORLD_CONFLICT_STAGE_KEY)
        requested_model = preferred[0] if preferred else WORLD_CONFLICT_DEFAULT_MODEL
        output_schema = _output_schema(options)
        messages = build_conflict_review_messages(
            context_snapshot=context_snapshot, options=options, output_schema=output_schema,
        )
        envelope = CodexCallEnvelope(
            project_id=conflict.project_id,
            chapter_number=conflict.detected_after_chapter,
            scope="chapter",
            related_object_type="world_conflict",
            related_object_id=conflict.id,
        )
        prompt_chars = len(messages[-1]["content"])
        if prompt_chars > MAX_CONFLICT_REVIEW_PROMPT_CHARS:
            rationale = (
                f"The review prompt has {prompt_chars} characters; the limit is "
                f"{MAX_CONFLICT_REVIEW_PROMPT_CHARS}."
            )
            audit_writer.save_decision_event(
                _escalation_event(
                    envelope,
                    CodexCallVerdict(
                        summary=f"World conflict {conflict.id} escalated to a human: prompt_too_large.",
                        reason=rationale,
                        actor_id="",
                        payload=_escalation_payload(
                            conflict,
                            failure_reason="prompt_too_large",
                            answer=None,
                            requested_model=requested_model,
                            prompt_chars=prompt_chars,
                        ),
                    ),
                    actor_type="system",
                    related_object_type="world_conflict",
                    related_object_id=conflict.id,
                )
            )
            return WorldConflictDelegation(rationale=rationale, failure_reason="prompt_too_large")

        outcome = run_codex_structured_call(
            self.llm_client,
            audit_writer=audit_writer,
            gate_kind=_GATE_KIND,
            stage_key=WORLD_CONFLICT_STAGE_KEY,
            requested_model=requested_model,
            messages=messages,
            output_model=_answer_model(options),
            output_schema=output_schema,
            event_types=_EVENT_TYPES,
            request_payload={},
            envelope=envelope,
            input_snapshot=context_snapshot,
            verdict=lambda result: _escalation_verdict(conflict, result),
            trace_scope="world_engine",
            template_id="world_engine:conflict_review",
            label="Codex review",
        )
        answer = cast(_ConflictReviewAnswer | None, outcome.parsed)
        if answer is None:
            return WorldConflictDelegation(
                rationale=outcome.failure_detail,
                failure_reason=outcome.failure_reason,
                trace_id=outcome.trace_id,
            )
        escalate = answer.option in _ESCALATING_OPTIONS
        if escalate:
            audit_writer.save_decision_event(
                _escalation_event(
                    envelope,
                    _escalation_verdict(conflict, outcome),
                    actor_type="worker",
                    related_object_type="prompt_trace",
                    related_object_id=outcome.trace_id,
                    parent_event_id=outcome.decision_event_id,
                    causal_root_id=outcome.causal_root_id,
                )
            )
        return WorldConflictDelegation(
            option=cast(ConflictOption, answer.option),
            rationale=answer.rationale,
            trace_id=outcome.trace_id,
            escalate=escalate,
        )


# context


def _conflict_record(conflict: WorldPlanConflictRow) -> dict[str, Any]:
    return {
        "id": conflict.id,
        "source": conflict.source,
        "violated": conflict.violated,
        "impact_level": conflict.impact_level,
        "summary": conflict.summary,
        "detected_after_chapter": conflict.detected_after_chapter,
        "story_time": conflict.story_time,
        "blocks_from_chapter": conflict.blocks_from_chapter,
        "involved_refs": json.loads(conflict.involved_refs_json or "[]"),
        "proposed_change": json.loads(conflict.proposed_change_json or "{}"),
        "options": json.loads(conflict.options_json or "[]"),
        "intent_ref": conflict.intent_ref,
        "actor_ref": conflict.actor_ref,
    }


def _book_state_slice(runtime: BookStateRuntime, ref: str) -> dict[str, Any] | None:
    """The involved ref's objective truth: a world node with its current state, a world edge, a place or a fact."""
    world = runtime.world
    if (node := world.get_node(ref)) is not None:
        return {
            "ref": ref, "kind": "world_node",
            "value": {**node.model_dump(mode="json"), "state": world.get_state(ref)},
        }
    if (edge := world.edges_by_id.get(ref)) is not None:
        return {"ref": ref, "kind": "world_edge", "value": edge.model_dump(mode="json")}
    if (place := runtime.map.nodes_by_id.get(ref)) is not None:
        return {"ref": ref, "kind": "map_node", "value": place.model_dump(mode="json")}
    if (fact := world.facts_by_id.get(ref.removeprefix(FACT_REF_PREFIX))) is not None:
        return {"ref": ref, "kind": "fact", "value": fact.model_dump(mode="json")}
    return None


def build_conflict_context_snapshot(session: Session, *, conflict: WorldPlanConflictRow) -> dict[str, Any]:
    """What the reviewer sees: the conflict, its involved refs as of the detection chapter, the active
    volume's plan and the next unwritten chapter plans. Pieces that do not exist are left out."""
    snapshot: dict[str, Any] = {"conflict": _conflict_record(conflict)}
    runtime = BookStateProjection(session).load_runtime_as_of(
        conflict.project_id, as_of_chapter=conflict.detected_after_chapter,
    )
    slices = [
        item for ref in json.loads(conflict.involved_refs_json or "[]")
        if (item := _book_state_slice(runtime, ref)) is not None
    ]
    if slices:
        snapshot["book_state"] = slices
    planning = PlanningQuery()
    arc = planning.active_arc(session, conflict.project_id)
    if arc is not None:
        arc_plan = {
            key: value for key, value in (
                ("goal", arc.goal), ("stakes", arc.stakes),
                ("payoff_direction", arc.payoff_direction), ("synopsis", arc.arc_synopsis),
            ) if value
        }
        if arc_plan:
            snapshot["arc_plan"] = arc_plan
    chapters = planning.future_chapters(
        session, project_id=conflict.project_id, current_chapter=conflict.detected_after_chapter,
    )[:UPCOMING_CHAPTER_LIMIT]
    if chapters:
        snapshot["upcoming_chapters"] = [
            {"chapter_number": plan.chapter_number, "title": plan.title, "goals": load_goals_json(plan.goals_json)}
            for plan in chapters
        ]
    return snapshot


__all__ = [
    "UPCOMING_CHAPTER_LIMIT",
    "WORLD_CONFLICT_DEFAULT_MODEL",
    "WORLD_CONFLICT_STAGE_KEY",
    "WorldConflictDelegate",
    "WorldConflictDelegation",
    "build_conflict_context_snapshot",
]
