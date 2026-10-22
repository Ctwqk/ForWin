"""Settle due offstage intents with one structured call and bounded retries."""

from __future__ import annotations

import inspect
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.story_clock import StoryCalendar
from forwin.observability.llm_trace import (
    RAW_REQUEST_KEY, RAW_RESPONSE_KEY, mark_latest_attempt_parse_failure, mark_latest_attempt_workflow,
)
from forwin.protocol.book_state import WorldNode
from forwin.skills import serialize_prompt_layers
from forwin.utils import parse_llm_json
from forwin.world_engine.constraints import ConstraintIssue
from forwin.world_engine.types import AdjudicationResult, NEW_FACT_REF_PREFIX, TickContext, WorldEffects
from forwin.writer.prompt_core.world_engine import build_adjudication_messages


_STAGE_KEY = "world.adjudicate"
_PARSER_NAME = "WorldAdjudicator JSON parser"


@dataclass(frozen=True)
class AdjudicationCall:
    result: AdjudicationResult | None
    prompt_trace: dict
    issues: list[str]


def _reference_issues(effects: WorldEffects, runtime: BookStateRuntime, lexicon: BookLexicon) -> list[str]:
    allowed = (set(runtime.world.nodes_by_id) | set(runtime.map.nodes_by_id)
               | {f"{FACT_REF_PREFIX}{ref}" for ref in runtime.world.facts_by_id}) - {"reader"}
    new_facts = {f"{NEW_FACT_REF_PREFIX}{fact.key}" for fact in effects.new_facts}
    modes = {mode.id for mode in lexicon.travel_modes}
    issues: list[str] = []

    def check(ref: str, path: str, *, refs: set[str] = allowed, optional: bool = False) -> None:
        if optional and not ref:
            return
        if ref not in refs:
            issues.append(f"unknown_ref at {path}: {ref!r}")

    def value_refs(value: Any, path: str, *, is_ref: bool = False) -> None:
        if isinstance(value, str) and is_ref:
            check(value, path, optional=True)
        elif isinstance(value, dict):
            for key, item in value.items():
                value_refs(item, f"{path}.{key}", is_ref=key.endswith(("_id", "_ids", "_ref", "_refs")))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value_refs(item, f"{path}[{index}]", is_ref=is_ref)

    for index, item in enumerate(effects.entity_changes):
        check(item.ref, f"entity_changes[{index}].ref")
        value_refs(item.new_value, f"entity_changes[{index}].new_value",
                   is_ref=item.field.endswith(("_id", "_ids", "_ref", "_refs")))
    for index, item in enumerate(effects.relation_changes):
        check(item.source_ref, f"relation_changes[{index}].source_ref")
        check(item.target_ref, f"relation_changes[{index}].target_ref")
        value_refs(item.new_value, f"relation_changes[{index}].new_value",
                   is_ref=item.field.endswith(("_id", "_ids", "_ref", "_refs")))
    for index, item in enumerate(effects.movements):
        check(item.entity_ref, f"movements[{index}].entity_ref")
        check(item.to_location_id, f"movements[{index}].to_location_id", refs=set(runtime.map.nodes_by_id))
        check(item.travel_mode_id, f"movements[{index}].travel_mode_id", refs=modes, optional=True)
    for index, item in enumerate(effects.item_transfers):
        check(item.item_ref, f"item_transfers[{index}].item_ref")
        check(item.from_ref, f"item_transfers[{index}].from_ref", optional=True)
        check(item.to_ref, f"item_transfers[{index}].to_ref", optional=True)
    for index, item in enumerate(effects.events):
        check(item.location_id, f"events[{index}].location_id", optional=True)
        for ref in item.participant_refs:
            check(ref, f"events[{index}].participant_refs")
    for index, item in enumerate(effects.new_facts):
        for ref in item.known_by:
            check(ref, f"new_facts[{index}].known_by")
    for index, item in enumerate(effects.knowledge):
        check(item.observer_ref, f"knowledge[{index}].observer_ref")
        check(item.fact_ref, f"knowledge[{index}].fact_ref", refs=allowed | new_facts)
        check(item.source_ref, f"knowledge[{index}].source_ref", optional=True)
    return issues


def _validated_result(
    parsed: AdjudicationResult, intent_ids: set[str], runtime: BookStateRuntime, lexicon: BookLexicon,
) -> tuple[AdjudicationResult, list[str], int]:
    outcomes = []
    issues: list[str] = []
    seen: set[str] = set()
    for outcome in parsed.outcomes:
        if outcome.intent_id not in intent_ids:
            continue
        if outcome.intent_id in seen:
            issues.append(f"duplicate outcome for intent_id {outcome.intent_id!r}")
            continue
        invalid = _reference_issues(outcome.effects, runtime, lexicon)
        if invalid:
            issues.extend(f"{outcome.intent_id}: {issue}" for issue in invalid)
            continue
        seen.add(outcome.intent_id)
        outcomes.append(outcome)
    missing = intent_ids - seen
    if missing:
        issues.append(f"missing outcomes for intent_ids: {', '.join(sorted(missing))}")
    return AdjudicationResult(outcomes=outcomes), issues, len(missing)


class WorldAdjudicator:
    def __init__(self, llm_client, *, max_attempts: int = 2, max_tokens: int = 6000):
        self.llm_client = llm_client
        self.max_attempts = max(1, int(max_attempts))
        self.max_tokens = max_tokens
        self._chat_parameters = inspect.signature(llm_client.chat).parameters

    def adjudicate(
        self, *, context: TickContext, intents: Sequence[WorldNode], runtime: BookStateRuntime,
        lexicon: BookLexicon, calendar: StoryCalendar | None,
        feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    ) -> AdjudicationCall:
        attempts: list[dict[str, Any]] = []
        business_retries: list[dict[str, Any]] = []
        retry_feedback: list[str] = []
        best_key: tuple[int, int] | None = None
        result = None
        issues: list[str] = []
        intent_ids = {intent.id for intent in intents}
        for attempt_no in range(1, self.max_attempts + 1):
            messages = build_adjudication_messages(
                context=context, intents=intents, runtime=runtime, lexicon=lexicon, calendar=calendar,
                feedback=feedback, retry_feedback=retry_feedback,
            )
            retry_feedback = []
            raw = ""
            call_error: Exception | None = None
            failure_reason = ""
            try:
                raw = self._call_chat(messages)
                try:
                    parsed = AdjudicationResult.model_validate(parse_llm_json(raw, error_prefix=_PARSER_NAME))
                except Exception as exc:  # noqa: BLE001
                    retry_feedback = [str(exc)]
                    mark_latest_attempt_parse_failure(
                        self.llm_client, parser_name=_PARSER_NAME, stage_key=_STAGE_KEY,
                        schema_name="AdjudicationResult", raw_output=raw, error=exc,
                    )
                    raise
            except Exception as exc:  # noqa: BLE001
                call_error = exc
                failure_reason = "\n".join(retry_feedback) if retry_feedback else type(exc).__name__
            finally:
                mark_latest_attempt_workflow(self.llm_client, attempt_no=attempt_no, stage_key=_STAGE_KEY)
                drain = getattr(self.llm_client, "drain_llm_attempt_events", None)
                drained = drain() if callable(drain) else []
                current_attempts = drained if isinstance(drained, list) and drained else [{
                    "attempt_no": 1, "stage_key": _STAGE_KEY, "task_family": "world_engine",
                }]
                for event in current_attempts:
                    event.setdefault("workflow_attempt_no", attempt_no)
                    event.setdefault("workflow_retry", attempt_no > 1)
                    event.setdefault(RAW_REQUEST_KEY, {"messages": messages})
                    event.setdefault(RAW_RESPONSE_KEY, raw)
                    if call_error is not None:
                        event.setdefault("error_class", type(call_error).__name__)
                        if retry_feedback:
                            event.setdefault("error_message", failure_reason)
                            event.setdefault("parse_ok", False)
                            event.setdefault("schema_ok", False)
                            event.setdefault("parse_error", failure_reason)
                        else:
                            event["error_message"] = failure_reason
                attempts.extend(current_attempts)

            if call_error is None:
                candidate, candidate_issues, missing_count = _validated_result(parsed, intent_ids, runtime, lexicon)
                key = (missing_count, len(candidate_issues))
                if best_key is None or key <= best_key:
                    result, issues, best_key = candidate, candidate_issues, key
                retry_feedback = candidate_issues

            if (retry_feedback or call_error is not None) and attempt_no < self.max_attempts:
                business_retries.append({
                    "stage": _STAGE_KEY, "attempt_no": attempt_no, "max_attempts": self.max_attempts,
                    "reason_class": type(call_error).__name__ if call_error else "AdjudicationValidation",
                    "reason": ("\n".join(retry_feedback) or failure_reason)[:500], "retry_kind": "business_validation",
                })
                continue
            break

        if result is None:
            issues.append(f"Adjudication failed after {attempt_no} attempts: {failure_reason}")
        prompt_trace = self._build_prompt_trace(
            messages=messages, context=context, intent_ids=sorted(intent_ids), attempts=attempts,
            business_retries=business_retries, result=result, issues=issues,
        )
        return AdjudicationCall(result, prompt_trace, issues)

    def _call_chat(self, messages: list[dict]) -> str:
        kwargs: dict[str, Any] = {
            "temperature": 0.2, "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"}, "task_family": "world_engine",
            "stage_key": _STAGE_KEY, "output_schema": AdjudicationResult.model_json_schema(),
        }
        if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in self._chat_parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in self._chat_parameters}
        return self.llm_client.chat(messages, **kwargs)

    def _build_prompt_trace(
        self, *, messages: list[dict], context: TickContext, intent_ids: list[str],
        attempts: list[dict[str, Any]], business_retries: list[dict[str, Any]],
        result: AdjudicationResult | None, issues: list[str],
    ) -> dict[str, Any]:
        last_call_result = getattr(self.llm_client, "last_call_result", None)
        trace = getattr(last_call_result, "trace", {}) or {}
        layers = serialize_prompt_layers(messages, [])
        return {
            "trace_scope": "world_engine", "stage_key": _STAGE_KEY,
            "backend": str(trace.get("backend") or getattr(last_call_result, "backend", "") or ""),
            "codex_job_id": str(trace.get("codex_job_id", "") or ""),
            "permission_profile": str(trace.get("permission_profile", "") or ""),
            "fallback_used": bool(getattr(last_call_result, "fallback_used", False)),
            "template_id": "world_engine:adjudicate", "template_version": "v1",
            "effective_system_prompt": "\n\n".join(str(item["content"]).strip() for item in layers if item["role"] == "system"),
            "prompt_layers": layers,
            "input_snapshot": {**context.model_dump(mode="json"), "intent_ids": intent_ids,
                               "stage_key": _STAGE_KEY, "selected_skills": []},
            "model_profile": {
                **{key: getattr(self.llm_client, key, "") for key in ("profile_id", "profile_name", "model", "base_url")},
                "actual_model": getattr(last_call_result, "actual_model", "") or "",
            },
            "attempts": attempts,
            "output_summary": {
                "adjudication_ok": result is not None, "issues": issues,
                "outcome_count": len(result.outcomes) if result is not None else 0,
                "skill_summary": [], "business_retry_events": business_retries,
            },
        }
