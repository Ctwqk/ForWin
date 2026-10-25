"""Drift due world variables with one structured call and bounded retries."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.story_clock import StoryCalendar
from forwin.observability.llm_trace import (
    build_workflow_trace, drain_workflow_attempts,
)
from forwin.protocol.book_state import WorldNode
from forwin.utils import parse_llm_json
from forwin.world_engine.constraints import ConstraintIssue
from forwin.world_engine.effect_validation import effect_reference_issues
from forwin.world_engine.types import (
    INVALID_OUTPUT, LLM_UNAVAILABLE, PROMPT_TOO_LARGE, DriftChange, DriftResult,
    EffectEntityChange, TickCandidate, TickContext, WorldEffects,
)
from forwin.writer.prompt_core.world_engine import MAX_DRIFT_PROMPT_CHARS, build_drift_messages


_STAGE_KEY = "world.drift"
_PARSER_NAME = "WorldDriftRunner JSON parser"


@dataclass(frozen=True)
class DriftCall:
    result: DriftResult | None
    prompt_trace: dict
    issues: list[str]
    missing_variable_ids: tuple[str, ...] = ()
    # Why the missing variables are missing: LLM_UNAVAILABLE when no call returned an answer,
    # INVALID_OUTPUT when the answers never covered them validly, PROMPT_TOO_LARGE when the prompt, or the
    # retry asking for them again, was over the guard; empty when none is.
    failure_reason: str = ""


def _validated_result(
    parsed: DriftResult, variable_ids: set[str], runtime: BookStateRuntime, lexicon: BookLexicon,
) -> tuple[DriftResult, list[str], int]:
    changes = []
    issues: list[str] = []
    seen: set[str] = set()
    for change in parsed.changes:
        if change.variable_id not in variable_ids:
            continue
        if change.variable_id in seen:
            issues.append(f"duplicate change for variable_id {change.variable_id!r}")
            continue
        invalid = effect_reference_issues(WorldEffects(events=change.events), runtime, lexicon)
        if invalid:
            issues.extend(f"{change.variable_id}: {issue}" for issue in invalid)
            continue
        seen.add(change.variable_id)
        changes.append(change)
    missing = variable_ids - seen
    if missing:
        issues.append(f"missing changes for variable_ids: {', '.join(sorted(missing))}")
    return DriftResult(changes=changes), issues, len(missing)


class WorldDriftRunner:
    def __init__(self, llm_client, *, max_attempts: int = 2, max_tokens: int = 4000):
        self.llm_client = llm_client
        self.max_attempts = max(1, int(max_attempts))
        self.max_tokens = max_tokens
        self._chat_parameters = inspect.signature(llm_client.chat).parameters

    def drift(
        self, *, context: TickContext, variables: Sequence[WorldNode], runtime: BookStateRuntime,
        lexicon: BookLexicon, calendar: StoryCalendar | None,
        feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> DriftCall:
        attempts: list[dict[str, Any]] = []
        business_retries: list[dict[str, Any]] = []
        retry_feedback: list[str] = []
        best_key: tuple[int, int] | None = None
        result = None
        issues: list[str] = []
        # Whether any call returned an answer, valid or not.
        answered = False
        # Whether a retry's prompt was over the guard after an earlier attempt settled some variables.
        too_large = False
        variable_ids = {variable.id for variable in variables}
        if not variables:
            result = DriftResult()
            prompt_trace = self._build_prompt_trace(
                messages=[], context=context, variable_ids=[], attempts=[], business_retries=[],
                result=result, issues=[],
            )
            return DriftCall(result, prompt_trace, [])
        # The last prompt sent, which the trace records. Only when nothing was sent does the trace record the
        # unsent prompt, with no attempts and failure_reason prompt_too_large.
        messages: list[dict] = []
        for attempt_no in range(1, self.max_attempts + 1):
            prompt = build_drift_messages(
                context=context, variables=variables, runtime=runtime, lexicon=lexicon, calendar=calendar,
                feedback=feedback, retry_feedback=retry_feedback,
            )
            if len(prompt[-1]["content"]) > MAX_DRIFT_PROMPT_CHARS:
                if result is not None:
                    # The retry's feedback put it over the guard: keep what the earlier attempt settled.
                    too_large = True
                    issues.append("prompt_too_large")
                    break
                issues = ["prompt_too_large"]
                prompt_trace = self._build_prompt_trace(
                    messages=messages or prompt, context=context, variable_ids=sorted(variable_ids), attempts=attempts,
                    business_retries=business_retries, result=None, issues=issues,
                    failure_reason="prompt_too_large",
                )
                return DriftCall(None, prompt_trace, issues, tuple(sorted(variable_ids)), PROMPT_TOO_LARGE)
            messages = prompt
            retry_feedback = []
            raw = ""
            call_error: Exception | None = None
            failure_reason = ""
            try:
                raw = self._call_chat(messages)
                answered = True
                try:
                    parsed = DriftResult.model_validate(parse_llm_json(raw, error_prefix=_PARSER_NAME))
                except Exception as exc:  # noqa: BLE001
                    retry_feedback = [str(exc)]
                    raise
            except Exception as exc:  # noqa: BLE001
                call_error = exc
                failure_reason = "\n".join(retry_feedback) if retry_feedback else type(exc).__name__
            finally:
                attempts.extend(drain_workflow_attempts(
                    self.llm_client, attempt_no=attempt_no, stage_key=_STAGE_KEY, task_family="world_engine",
                    messages=messages, raw=raw, call_error=call_error, error_message=failure_reason,
                    replace_error_message=not retry_feedback,
                    parse_error=call_error if retry_feedback else None,
                    parser_name=_PARSER_NAME, schema_name="DriftResult",
                ))

            if call_error is None:
                candidate, candidate_issues, missing_count = _validated_result(parsed, variable_ids, runtime, lexicon)
                key = (missing_count, len(candidate_issues))
                if best_key is None or key <= best_key:
                    result, issues, best_key = candidate, candidate_issues, key
                retry_feedback = candidate_issues if missing_count else []

            if (retry_feedback or call_error is not None) and attempt_no < self.max_attempts:
                business_retries.append({
                    "stage": _STAGE_KEY, "attempt_no": attempt_no, "max_attempts": self.max_attempts,
                    "reason_class": type(call_error).__name__ if call_error else "DriftValidation",
                    "reason": ("\n".join(retry_feedback) or failure_reason)[:500], "retry_kind": "business_validation",
                })
                continue
            break

        if result is None:
            issues.append(f"Drift failed after {attempt_no} attempts: {failure_reason}")
        prompt_trace = self._build_prompt_trace(
            messages=messages, context=context, variable_ids=sorted(variable_ids), attempts=attempts,
            business_retries=business_retries, result=result, issues=issues,
            failure_reason="prompt_too_large" if too_large else "",
        )
        missing = variable_ids - {change.variable_id for change in result.changes} if result is not None else variable_ids
        failure_reason = (
            (PROMPT_TOO_LARGE if too_large else INVALID_OUTPUT if answered else LLM_UNAVAILABLE) if missing else ""
        )
        return DriftCall(result, prompt_trace, issues, tuple(sorted(missing)), failure_reason)

    def _call_chat(self, messages: list[dict]) -> str:
        kwargs: dict[str, Any] = {
            "temperature": 0.2, "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"}, "task_family": "world_engine",
            "stage_key": _STAGE_KEY, "output_schema": DriftResult.model_json_schema(),
        }
        if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in self._chat_parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in self._chat_parameters}
        return self.llm_client.chat(messages, **kwargs)

    def _build_prompt_trace(
        self, *, messages: list[dict], context: TickContext, variable_ids: list[str],
        attempts: list[dict[str, Any]], business_retries: list[dict[str, Any]],
        result: DriftResult | None, issues: list[str], failure_reason: str = "",
    ) -> dict[str, Any]:
        missing = set(variable_ids) - {change.variable_id for change in result.changes} if result is not None else set(variable_ids)
        return build_workflow_trace(
            self.llm_client, messages=messages, attempts=attempts,
            trace_scope="world_engine",
            stage_key=_STAGE_KEY,
            template_id="world_engine:drift",
            input_snapshot={**context.model_dump(mode="json"), "variable_ids": variable_ids,
                               "stage_key": _STAGE_KEY, "selected_skills": []},
            output_summary={
                "drift_ok": result is not None and not missing, "issues": issues,
                "missing_variable_ids": sorted(missing), "failure_reason": failure_reason,
                "change_count": len(result.changes) if result is not None else 0,
                "skill_summary": [], "business_retry_events": business_retries,
            },
        )


def due_world_variables(runtime: BookStateRuntime, *, now: int, every: int, limit: int) -> list[WorldNode]:
    """Active variables due at ``now``, oldest first; a zero cadence disables drift."""
    if every <= 0 or limit <= 0:
        return []
    due = []
    for node in runtime.world.nodes_by_id.values():
        if node.node_type != "world_variable" or not node.is_active:
            continue
        state = runtime.world.get_state(node.id)
        last = state.get("last_drift_at")
        if state.get("status") == "active" and type(last) is int and last + every <= now:
            due.append((last, node.id, node.model_copy(update={"state": dict(state)})))
    return [node for _, _, node in sorted(due, key=lambda item: item[:2])[:limit]]


def drift_candidate(variable: WorldNode, change: DriftChange, *, now: int) -> TickCandidate:
    """A successful drift advances its cadence only if the candidate commits."""
    return TickCandidate(
        candidate_id=f"drift:{variable.id}", origin="drift", summary=change.summary,
        effects=WorldEffects(entity_changes=[
            EffectEntityChange(ref=variable.id, field=field, new_value=value)
            for field, value in (("level", change.new_level), ("trend", change.trend), ("last_drift_at", now))
        ], events=change.events),
    )
