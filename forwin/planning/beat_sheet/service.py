"""Generate an arc's beat sheet with ``plan.arc_beats``: validate, retry once with the errors, else fall back."""

from __future__ import annotations

import inspect
import re
from collections.abc import Callable, Collection, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.knowledge import FACT_REF_PREFIX, HIDDEN_SENSITIVITY, KNOWLEDGE_STATES, is_concealed
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reveal_schedule import active_genesis_pack, earliest_reveal_chapters
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import ACTOR_NODE_TYPES, WORLD_NODE_FIELDS
from forwin.canon.admission import GENESIS_SEED_SOURCE_TYPE, CanonAdmissionService
from forwin.genesis.arc_activation_review import build_arc_activation_review_pack
from forwin.genesis.handoff.world_seed_committer import seed_ref_id
from forwin.models.planning_v6 import ArcBeatSheetRow
from forwin.models.project import ArcPlanVersion, Project
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.observability.llm_trace import (
    RAW_REQUEST_KEY, RAW_RESPONSE_KEY, mark_latest_attempt_parse_failure, mark_latest_attempt_workflow,
)
from forwin.planning.beat_sheet.fallback import arc_bounds, build_fallback_beat_sheet, fallback_protagonist_ref
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import (
    MAX_BAND_CHAPTERS, MIN_BAND_CHAPTERS, PROTECTION_KINDS, READER_OBSERVER, SETTING_BRIEF_MAX_CHARS, ArcBeatSheetData,
    BandSpan,
)
from forwin.planning.fact_text import stated_facts
from forwin.planning.world_digest import build_world_digest
from forwin.protocol.book_state import WorldNode
from forwin.runtime.policy import RuntimePolicy
from forwin.skills import serialize_prompt_layers
from forwin.utils import parse_llm_json
from forwin.world_engine.types import INVALID_OUTPUT, LLM_UNAVAILABLE
from forwin.writer.prompt_core.planning import build_arc_beats_messages
from forwin.writer.prompt_core.world_engine import EVENT_FACT_TYPE, MAX_ADJUDICATION_EVENT_FACTS


STAGE_KEY = "plan.arc_beats"
_TASK_FAMILY = "planning"
_PARSER_NAME = "ArcBeatSheet JSON parser"
# Story minutes the world digest looks ahead per arc chapter: one day.
_DIGEST_MINUTES_PER_CHAPTER = 24 * 60
_OUTPUT_SCHEMA = ArcBeatSheetData.model_json_schema()
# The state fields an exit-state item ends a ref with: its alive status (death) or its status (an ended status).
_ENDING_FIELDS = frozenset({"alive_status", "status"})
_ALLOWED = {
    "protection_kind": list(PROTECTION_KINDS),
    "reveal_target_state": list(KNOWLEDGE_STATES),
    "reader_observer": READER_OBSERVER,
    # The fields must_hold_state and exit_state may name, by actor kind.
    "actor_fields": {
        kind: {section: sorted(WORLD_NODE_FIELDS[kind][section]) for section in ("state", "profile")}
        for kind in ACTOR_NODE_TYPES
    },
}


class ArcBeatSheetService:
    """Writes beat sheet versions in the caller's session; the caller commits.

    Callers must not hold the project row lock across the LLM call, with one exception: the
    start-writing handoff holds it to keep arc/map/seed materialization and the first beat sheet's
    canon constraints atomic. Other callers acquire the publication lock after generation; this
    service then re-reads the active sheet before writing it and its canon constraints.

    ``llm_client`` is None when no model is configured: every generation then writes the fallback.
    ``save_trace`` saves the call's PromptTrace and its LLM DecisionEvents in that session and returns the
    trace id; every generation saves one, a fallback included.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        save_trace: Callable[[Session, dict], str],
        max_attempts: int = 2,
        max_tokens: int = 8000,
    ) -> None:
        self.llm_client = llm_client
        self.save_trace = save_trace
        self.max_attempts = max(1, int(max_attempts))
        self.max_tokens = max_tokens
        self._chat_parameters = inspect.signature(llm_client.chat).parameters if llm_client is not None else {}

    def ensure_for_arc(
        self, session: Session, *, project_id: str, arc_plan: ArcPlanVersion, policy: RuntimePolicy,
        reason: str = "activation",
    ) -> ArcBeatSheetRow:
        """The arc's active beat sheet, generated when it has none."""
        existing = ArcBeatSheetRepository(session).active_for_arc(arc_plan.id)
        if existing is not None:
            return existing
        return self._generate(session, project_id=project_id, arc_plan=arc_plan, policy=policy, reason=reason)

    def revise(
        self, session: Session, *, project_id: str, arc_plan: ArcPlanVersion, policy: RuntimePolicy,
        reason: str, conflict_summary: str,
    ) -> ArcBeatSheetRow:
        """A new version that resolves ``conflict_summary``; the active one becomes superseded."""
        repo = ArcBeatSheetRepository(session)
        current = repo.active_for_arc(arc_plan.id)
        return self._generate(
            session, project_id=project_id, arc_plan=arc_plan, policy=policy, reason=reason,
            current=repo.load(current) if current is not None else None,
            expected_sheet_id=current.id if current is not None else None,
            revision={"reason": reason, "conflict_summary": conflict_summary},
        )

    def _generate(
        self, session: Session, *, project_id: str, arc_plan: ArcPlanVersion, policy: RuntimePolicy, reason: str,
        expected_sheet_id: str | None = None,
        current: ArcBeatSheetData | None = None, revision: Mapping[str, str] | None = None,
    ) -> ArcBeatSheetRow:
        start, end = arc_bounds(arc_plan)
        # The refs are checked against the latest canon: chapter 0 at handoff.
        as_of = BookStateRepository(session).latest_canon_chapter(project_id) or 0
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of)
        pack = active_genesis_pack(session, project_id)
        fallback_protagonist = fallback_protagonist_ref(project_id, pack, runtime)
        protagonist = (current.protagonist_ref
                       if current is not None and _is_seeded_character(runtime, current.protagonist_ref)
                       else fallback_protagonist)
        now = BookStateQuery(session).current_story_minute(project_id, as_of_chapter=as_of) or 0
        digest = build_world_digest(
            session, project_id=project_id, as_of_chapter=as_of, protagonist_ref=protagonist,
            window_end_minute=now + (end - start + 1) * _DIGEST_MINUTES_PER_CHAPTER,
            budget_chars=policy.planning.digest_budget_chars, runtime=runtime,
        )
        actors = _actors(runtime)
        payload: dict[str, Any] = {
            "arc": {
                "arc_number": int(arc_plan.arc_number or 0), "title": arc_plan.title, "goal": arc_plan.goal,
                "stakes": arc_plan.stakes, "payoff_direction": arc_plan.payoff_direction,
                "arc_synopsis": arc_plan.arc_synopsis, "chapter_start": start, "chapter_end": end,
                "chapter_count": end - start + 1,
            },
            "band_size": policy.planning.band_size,
            "band_limits": [MIN_BAND_CHAPTERS, MAX_BAND_CHAPTERS],
            "actors": actors,
            "facts": _facts(runtime),
            "reveal_candidates": _reveal_candidates(project_id, pack, runtime),
            "previous_arc_closing_state": build_arc_activation_review_pack(
                session, project_id=project_id, arc_number=int(arc_plan.arc_number or 0), chapter_start=start,
            ).to_prompt_payload(),
            "open_obligations": [
                {"id": item.id, "summary": item.summary, "deadline_chapter": item.deadline_chapter,
                 "priority": item.priority, "subject_refs": item.subject_refs, "payoff_test": item.payoff_test}
                for item in NarrativeObligationRepository(session).list_active_for_context(
                    project_id, chapter_number=as_of + 1,
                )
            ],
        }
        if revision is not None:
            payload["current_beat_sheet"] = current.model_dump(mode="json") if current is not None else None
            payload["revision"] = dict(revision)
        # Beat-sheet text reaches the writer verbatim, in every chapter of the arc. After a mid-arc revise the latest
        # canon is past the arc's start, so what the reader knows there is not what they knew in the arc's first
        # chapters, which a later historical rewrite renders it for (FU-R52).
        reader_runtime = (BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=start - 1)
                          if start - 1 < as_of else runtime)
        call = self._call(
            payload, digest.text, start=start, end=end, runtime=runtime, actor_ids={actor["id"] for actor in actors},
            reveal_chapters=earliest_reveal_chapters(session, project_id),
            reader_known=_reader_known_facts(reader_runtime),
        )
        data, source = call.data, "llm"
        if data is None:
            data, source = build_fallback_beat_sheet(
                arc_plan, protagonist_ref=fallback_protagonist, band_size=policy.planning.band_size,
            ), "fallback"
        session.execute(select(Project).where(Project.id == project_id).with_for_update(key_share=True)).scalar_one()
        repo = ArcBeatSheetRepository(session)
        active = repo.active_for_arc(arc_plan.id)
        if active is not None and active.id != expected_sheet_id:
            self.save_trace(session, self._prompt_trace(
                call, project_id=project_id, as_of=as_of, arc_plan=arc_plan, reason=reason, source=source, row=active,
            ))
            return active
        row = repo.create(project_id, arc_plan.id, data, source=source, reason=reason)
        CanonAdmissionService().commit_plan_constraints(
            session=session, project_id=project_id, beat_sheet_id=row.id, creates=data.protections, releases=[],
            release_sheet_id=active.id if active is not None else None,
        )
        self.save_trace(session, self._prompt_trace(
            call, project_id=project_id, as_of=as_of, arc_plan=arc_plan, reason=reason, source=source, row=row,
        ))
        return row

    def _call(
        self, payload: Mapping[str, Any], digest_text: str, *, start: int, end: int, runtime: BookStateRuntime,
        actor_ids: Collection[str], reveal_chapters: Mapping[str, int], reader_known: frozenset[str],
    ) -> _BeatSheetCall:
        """Up to ``max_attempts`` calls. A malformed or invalid answer goes back to the model as feedback;
        a provider failure resends the same messages."""
        call = _BeatSheetCall()
        retry_feedback: list[str] = []
        # Whether any call returned an answer, valid or not.
        answered = False
        for attempt_no in range(1, self.max_attempts + 1):
            call.messages = build_arc_beats_messages(
                payload=payload, digest_text=digest_text, output_schema=_OUTPUT_SCHEMA,
                allowed=_ALLOWED, retry_feedback=retry_feedback,
            )
            if self.llm_client is None:
                call.issues, call.failure_reason = ["no LLM client is configured"], LLM_UNAVAILABLE
                return call
            raw = ""
            call_error: Exception | None = None
            provider_failed = False
            try:
                raw = self._chat(call.messages)
            except Exception as exc:  # noqa: BLE001 - a provider failure is retried, then the arc falls back
                call_error, provider_failed = exc, True
            else:
                answered = True
                try:
                    parsed = ArcBeatSheetData.model_validate(parse_llm_json(raw, error_prefix=_PARSER_NAME))
                except Exception as exc:  # noqa: BLE001 - a malformed answer is sent back to the model
                    call_error = exc
                    mark_latest_attempt_parse_failure(
                        self.llm_client, parser_name=_PARSER_NAME, stage_key=STAGE_KEY,
                        schema_name="ArcBeatSheetData", raw_output=raw, error=exc,
                    )
            if call_error is not None:
                call.issues = [f"{type(call_error).__name__}: {call_error}"]
            else:
                call.issues = beat_sheet_issues(
                    parsed, chapter_start=start, chapter_end=end, runtime=runtime, actor_ids=actor_ids,
                    reveal_chapters=reveal_chapters, reader_known=reader_known,
                )
            mark_latest_attempt_workflow(self.llm_client, attempt_no=attempt_no, stage_key=STAGE_KEY)
            call.attempts.extend(self._drained_attempts(
                attempt_no, call.messages, raw, call_error, "\n".join(call.issues),
            ))
            if not call.issues:
                call.data = _normalized(parsed)
                return call
            if not provider_failed:
                retry_feedback = call.issues
            if attempt_no < self.max_attempts:
                call.business_retries.append({
                    "stage": STAGE_KEY, "attempt_no": attempt_no, "max_attempts": self.max_attempts,
                    "reason_class": type(call_error).__name__ if call_error else "BeatSheetValidation",
                    "reason": "\n".join(call.issues)[:500], "retry_kind": "business_validation",
                })
        call.failure_reason = INVALID_OUTPUT if answered else LLM_UNAVAILABLE
        return call

    def _chat(self, messages: list[dict]) -> str:
        kwargs: dict[str, Any] = {
            "temperature": 0.3, "max_tokens": self.max_tokens,
            "response_format": {"type": "json_object"}, "task_family": _TASK_FAMILY,
            "stage_key": STAGE_KEY, "output_schema": _OUTPUT_SCHEMA,
        }
        if not any(param.kind == inspect.Parameter.VAR_KEYWORD for param in self._chat_parameters.values()):
            kwargs = {key: value for key, value in kwargs.items() if key in self._chat_parameters}
        return self.llm_client.chat(messages, **kwargs)

    def _drained_attempts(
        self, attempt_no: int, messages: list[dict], raw: str, call_error: Exception | None, error_message: str,
    ) -> list[dict[str, Any]]:
        drain = getattr(self.llm_client, "drain_llm_attempt_events", None)
        drained = drain() if callable(drain) else []
        attempts = drained if isinstance(drained, list) and drained else [{
            "attempt_no": 1, "stage_key": STAGE_KEY, "task_family": _TASK_FAMILY,
        }]
        for event in attempts:
            event.setdefault("workflow_attempt_no", attempt_no)
            event.setdefault("workflow_retry", attempt_no > 1)
            event.setdefault(RAW_REQUEST_KEY, {"messages": messages})
            event.setdefault(RAW_RESPONSE_KEY, raw)
            if call_error is not None:
                event.setdefault("error_class", type(call_error).__name__)
                event.setdefault("error_message", error_message)
        return attempts

    def _prompt_trace(
        self, call: _BeatSheetCall, *, project_id: str, as_of: int, arc_plan: ArcPlanVersion, reason: str,
        source: str, row: ArcBeatSheetRow,
    ) -> dict[str, Any]:
        last_call_result = getattr(self.llm_client, "last_call_result", None) if call.attempts else None
        trace = getattr(last_call_result, "trace", {}) or {}
        layers = serialize_prompt_layers(call.messages, [])
        return {
            "trace_scope": "planning", "stage_key": STAGE_KEY,
            "backend": str(trace.get("backend") or getattr(last_call_result, "backend", "") or ""),
            "codex_job_id": str(trace.get("codex_job_id", "") or ""),
            "permission_profile": str(trace.get("permission_profile", "") or ""),
            "fallback_used": bool(getattr(last_call_result, "fallback_used", False)),
            "template_id": "planning:arc_beats", "template_version": "v1",
            "effective_system_prompt": "\n\n".join(
                str(item["content"]).strip() for item in layers if item["role"] == "system"
            ),
            "prompt_layers": layers,
            "input_snapshot": {
                "project_id": project_id, "chapter_number": as_of, "arc_plan_id": arc_plan.id,
                "arc_number": int(arc_plan.arc_number or 0), "reason": reason, "stage_key": STAGE_KEY,
                "selected_skills": [],
            },
            "model_profile": {
                **{key: getattr(self.llm_client, key, "") for key in ("profile_id", "profile_name", "model", "base_url")},
                "actual_model": getattr(last_call_result, "actual_model", "") or "",
            },
            "attempts": call.attempts,
            "output_summary": {
                "source": source, "failure_reason": call.failure_reason, "issues": call.issues,
                "beat_sheet_id": row.id, "version": row.version,
                "skill_summary": [], "business_retry_events": call.business_retries,
            },
        }


@dataclass
class _BeatSheetCall:
    """One generation's model calls: the valid sheet, if any, and what the trace records."""

    data: ArcBeatSheetData | None = None
    issues: list[str] = field(default_factory=list)
    # LLM_UNAVAILABLE when no call returned an answer, INVALID_OUTPUT when none was valid; empty on success.
    failure_reason: str = ""
    messages: list[dict] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    business_retries: list[dict[str, Any]] = field(default_factory=list)


def beat_sheet_issues(
    data: ArcBeatSheetData, *, chapter_start: int, chapter_end: int, runtime: BookStateRuntime,
    actor_ids: Collection[str], reveal_chapters: Mapping[str, int] | None = None,
    reader_known: frozenset[str] | None = None,
) -> list[str]:
    """Every deterministic problem with the sheet.

    The protagonist, milestone requirements and protection and exit-state refs must be actors the prompt offered
    (``actor_ids``); observers must be the reader or active nodes; fact refs must name the runtime's facts.
    ``reveal_chapters`` (fact node id -> earliest reader reveal chapter) schedules the facts the sheet's own
    reader targets do not. ``reader_known`` is the fact node ids the reader knows before ``chapter_start``; leave
    it out when ``runtime`` is as of ``chapter_start - 1`` or earlier, and the runtime's recorded reader knowledge
    stands for it.
    """
    world = runtime.world
    issues = _coverage_issues("phases", [phase.chapter_range for phase in data.phases], chapter_start, chapter_end)
    issues.extend(band_breakdown_issues(data.band_breakdown, chapter_start, chapter_end))

    def actor(ref: str, path: str) -> WorldNode | None:
        if ref not in actor_ids:
            issues.append(f"{path}: {ref!r} is not one of the offered actors")
            return None
        return world.get_node(ref)

    def observer(ref: str, path: str) -> None:
        if ref != READER_OBSERVER and not _is_active(runtime, ref):
            issues.append(f"{path}: {ref!r} is not an active BookState node")

    def fact(ref: str, path: str) -> None:
        if not ref.startswith(FACT_REF_PREFIX) or ref.removeprefix(FACT_REF_PREFIX) not in world.facts_by_id:
            issues.append(f"{path}: {ref!r} is not a fact ref")

    def within(first: int, last: int, path: str) -> None:
        if not chapter_start <= first <= last <= chapter_end:
            issues.append(f"{path} {first}-{last} is not an ordered range inside the arc {chapter_start}-{chapter_end}")

    def state_field(node: WorldNode | None, field: str, path: str) -> None:
        if node is not None and not _is_node_field(node, field):
            issues.append(f"{path} {field!r} is not a {str(node.node_type)} field")

    if actor(data.protagonist_ref, "protagonist_ref") is not None and not _is_seeded_character(
        runtime, data.protagonist_ref,
    ):
        issues.append(f"protagonist_ref: {data.protagonist_ref!r} is not a character node from the world seed")
    seen: set[str] = set()
    for index, milestone in enumerate(data.milestones):
        path = f"milestones[{index}]"
        if not milestone.id.strip() or milestone.id in seen:
            issues.append(f"{path}.id {milestone.id!r} is empty or repeated")
        seen.add(milestone.id)
        within(*milestone.target_range, f"{path}.target_range")
        for ref_index, ref in enumerate(milestone.required_refs):
            actor(ref, f"{path}.required_refs[{ref_index}]")
    for index, protection in enumerate(data.protections):
        path = f"protections[{index}]"
        if not chapter_start <= protection.until_chapter <= chapter_end:
            issues.append(f"{path}.until_chapter {protection.until_chapter} is outside the arc {chapter_start}-{chapter_end}")
        if protection.kind == "must_not_know":
            observer(protection.observer_ref, f"{path}.observer_ref")
            fact(protection.fact_ref, f"{path}.fact_ref")
            # The ref names the observer; the reader is an observer but no actor (Ruling P4a-R13).
            if not protection.ref == protection.observer_ref == READER_OBSERVER:
                actor(protection.ref, f"{path}.ref")
            continue
        node = actor(protection.ref, f"{path}.ref")
        if node is None:
            continue
        if protection.kind == "must_be_alive" and str(node.node_type) != "character":
            # Only characters carry alive_status: on anything else the protection could never fire.
            issues.append(f"{path}.ref: must_be_alive needs a character, not {str(node.node_type)!r}")
        elif protection.kind == "must_hold_state":
            if not protection.field.strip():
                issues.append(f"{path}.field is required for must_hold_state")
            else:
                state_field(node, protection.field, f"{path}.field")
    for index, target in enumerate(data.reveal_targets):
        path = f"reveal_targets[{index}]"
        fact(target.fact_ref, f"{path}.fact_ref")
        observer(target.observer, f"{path}.observer")
        if target.target_state not in KNOWLEDGE_STATES:
            issues.append(f"{path}.target_state {target.target_state!r} is not an allowed value")
        within(*target.range, f"{path}.range")
    for index, item in enumerate(data.exit_state):
        path = f"exit_state[{index}]"
        node = actor(item.ref, f"{path}.ref")
        if not item.field.strip():
            issues.append(f"{path}.field is required")
        else:
            state_field(node, item.field, f"{path}.field")
    if (length := len(data.setting_brief.strip())) > SETTING_BRIEF_MAX_CHARS:
        issues.append(f"setting_brief_too_long: setting_brief has {length} characters; it reaches the writer "
                      f"verbatim, so keep it to one to three sentences of at most {SETTING_BRIEF_MAX_CHARS}")
    issues.extend(_writer_text_issues(
        data, runtime, reveal_chapters or {}, chapter_start=chapter_start, reader_known=reader_known,
    ))
    return issues


def _writer_text_issues(
    data: ArcBeatSheetData, runtime: BookStateRuntime, reveal_chapters: Mapping[str, int],
    *, chapter_start: int, reader_known: frozenset[str] | None = None,
) -> list[str]:
    """Writer-facing text cannot state concealed facts before their reader reveal (P4a-R21/R36).

    Purposes and milestones feed band-wide key beats, so all these texts are checked from the arc's start
    (P4a-R38). The setting brief reaches every chapter of the arc, so it is checked from the arc's start as well,
    names included (FU-R51). Revelation-layer names use their window start (P4a-R39); their chapter projection is
    unchanged. Only reader knowledge from before the arc is allowed: a fact the reader learns inside the arc is
    not known in the chapters before it, and a rewrite of one of those renders this text (FU-R52).
    """
    # Imported here: the band planner's modules import this package.
    from forwin.planning.band_planner.inputs import _concealed_names

    known = reader_known if reader_known is not None else _reader_known_facts(runtime)
    reveal = dict(reveal_chapters)
    own: dict[str, int] = {}
    for target in data.reveal_targets:
        if target.observer == READER_OBSERVER:
            fact = target.fact_ref.removeprefix(FACT_REF_PREFIX)
            own[fact] = min(own.get(fact, target.range[0]), target.range[0])
    # The sheet's own reader targets replace the schedule for their facts.
    reveal.update(own)
    protected = {spec.fact_ref.removeprefix(FACT_REF_PREFIX) for spec in data.protections
                 if spec.kind == "must_not_know" and spec.observer_ref == READER_OBSERVER}
    concealed = {f"{FACT_REF_PREFIX}{fact.id}": fact.proposition for fact in runtime.world.facts_by_id.values()
                 if (fact.sensitivity_level in HIDDEN_SENSITIVITY or fact.id in protected) and fact.id not in known}
    texts = [(f"phases[{index}].purpose", phase.purpose, phase.chapter_range[0])
             for index, phase in enumerate(data.phases)]
    texts.extend((f"milestones[{index}].description", milestone.description, milestone.target_range[0])
                 for index, milestone in enumerate(data.milestones))
    for field, value in data.reader_promise.model_dump().items():
        if field == "ambiguity_mode":
            continue  # An enum, not author free text.
        if isinstance(value, list):
            texts.extend((f"reader_promise.{field}[{index}]", text, chapter_start)
                         for index, text in enumerate(value))
        else:
            texts.append((f"reader_promise.{field}", value, chapter_start))
    texts.append(("setting_brief", data.setting_brief, chapter_start))
    texts.extend((f"arc_payoff_map.ambiguity_constraints[{index}]", text, chapter_start)
                 for index, text in enumerate(data.arc_payoff_map.ambiguity_constraints))
    concealed_names = _concealed_names(runtime)
    issues = []
    for path, text, _ in texts:
        for ref in stated_facts([text], concealed):
            chapter = reveal.get(ref.removeprefix(FACT_REF_PREFIX))
            if chapter is not None and chapter <= chapter_start:
                continue
            when = f"the reader may learn only from chapter {chapter}" if chapter is not None else (
                "has no reader reveal scheduled"
            )
            issues.append(f"{path} states the proposition of {ref}, which {when}; it reaches the writer verbatim, "
                          f"so hint at it instead")
    for index, layer in enumerate(data.arc_payoff_map.revelation_layers):
        # Keep window-based naming checks for planner hygiene; writer projections omit revelation layers (FU-R48).
        window = re.fullmatch(r"\s*([0-9]+)(?:\s*[-–—]\s*([0-9]+))?\s*", layer.chapter_window)
        if window and 1 <= int(window[1]) and (window[2] is None or int(window[2]) >= int(window[1])):
            texts.append((f"arc_payoff_map.revelation_layers[{index}].summary", layer.summary, int(window[1])))
    for path, text, start in texts:
        # R38/R39 preserve naming once a related reader reveal starts, using the text's own range.
        revealed_nodes = {
            ref for fact in runtime.world.facts_by_id.values()
            if fact.id in known or (fact.id in reveal and reveal[fact.id] <= start)
            for ref in fact.related_node_refs
        }
        for ref, name in concealed_names.items():
            if name in text and ref not in revealed_nodes:
                issues.append(f"{path} names concealed entity {ref} ({name}); it reaches the writer verbatim "
                              f"and may name it only once a reader reveal about it has started")
    return issues


def _reader_known_facts(runtime: BookStateRuntime) -> frozenset[str]:
    """The fact node ids ``runtime`` records the reader as knowing or believing."""
    # Imported here: the feasibility audit imports the band planner's modules, which import this package.
    from forwin.planning.feasibility import reader_known_facts

    return reader_known_facts(runtime)


def milestone_refs_for_chapter(session: Session, project_id: str, chapter_number: int) -> frozenset[str]:
    """Future hard milestones across active sheets, plus the current arc's exit-state refs."""
    refs: set[str] = set()
    repo = ArcBeatSheetRepository(session)
    for row in repo.active_for_project(project_id):
        data = repo.load(row)
        for milestone in data.milestones:
            if milestone.hard and milestone.target_range[1] >= chapter_number:
                refs.update(milestone.required_refs)
        arc = session.get(ArcPlanVersion, row.arc_plan_id)
        if arc is not None and arc.chapter_start <= chapter_number <= arc.chapter_end:
            refs.update(item.ref for item in data.exit_state)
    return frozenset(refs)


@dataclass(frozen=True)
class ChapterMilestoneRefs:
    """What a chapter's text may not end, and the endings the plan itself sets."""

    refs: frozenset[str] = frozenset()
    # (ref, state path, value) of the exit state's alive-status and status items; an ending among them is the goal.
    planned_endings: frozenset[tuple[str, str, str]] = frozenset()


def chapter_milestone_refs(session: Session, project_id: str, chapter_number: int) -> ChapterMilestoneRefs:
    """The refs chapter ``chapter_number``'s text may not end: strictly future hard milestones', plus the current
    arc's exit-state refs.

    A milestone whose range already includes the chapter may be under way in it, so its refs may change. An
    exit-state item that sets the alive status or the status is a planned ending, unless a future milestone still
    needs its ref.
    """
    future: set[str] = set()
    exit_refs: set[str] = set()
    endings: set[tuple[str, str, str]] = set()
    repo = ArcBeatSheetRepository(session)
    for row in repo.active_for_project(project_id):
        data = repo.load(row)
        for milestone in data.milestones:
            if milestone.hard and milestone.target_range[0] > chapter_number:
                future.update(milestone.required_refs)
        arc = session.get(ArcPlanVersion, row.arc_plan_id)
        if arc is None or not arc.chapter_start <= chapter_number <= arc.chapter_end:
            continue
        for item in data.exit_state:
            exit_refs.add(item.ref)
            field = item.field.strip().removeprefix("state.")
            if field in _ENDING_FIELDS and isinstance(item.value, str):
                endings.add((item.ref, f"state.{field}", item.value))
    return ChapterMilestoneRefs(
        refs=frozenset(future | exit_refs),
        planned_endings=frozenset(ending for ending in endings if ending[0] not in future),
    )


def band_breakdown_issues(bands: Sequence[BandSpan], chapter_start: int, chapter_end: int) -> list[str]:
    """Bands cover the arc in order; each spans MIN-MAX chapters, or one band covers an arc shorter than MIN."""
    issues = _coverage_issues(
        "band_breakdown", [(band.chapter_start, band.chapter_end) for band in bands], chapter_start, chapter_end,
    )
    length = chapter_end - chapter_start + 1
    if length < MIN_BAND_CHAPTERS:
        if len(bands) != 1:
            issues.append(f"band_breakdown: an arc of {length} chapters is a single band")
        return issues
    for index, band in enumerate(sorted(bands, key=lambda item: item.chapter_start)):
        size = band.chapter_end - band.chapter_start + 1
        if not MIN_BAND_CHAPTERS <= size <= MAX_BAND_CHAPTERS:
            issues.append(f"band {index} spans {size} chapters; a band spans {MIN_BAND_CHAPTERS}-{MAX_BAND_CHAPTERS}")
    return issues


def _coverage_issues(label: str, ranges: Sequence[tuple[int, int]], start: int, end: int) -> list[str]:
    if not ranges:
        return [f"{label} is empty"]
    issues: list[str] = []
    expected = start
    for first, last in sorted(ranges):
        if not start <= first <= last <= end:
            issues.append(f"{label} range {first}-{last} is not an ordered range inside the arc {start}-{end}")
            continue
        if first > expected:
            issues.append(f"{label} does not cover chapters {expected}-{first - 1}")
        elif first < expected:
            issues.append(f"{label} overlaps at chapter {first}")
        expected = max(expected, last + 1)
    if expected <= end:
        issues.append(f"{label} does not cover chapters {expected}-{end}")
    return issues


def _normalized(data: ArcBeatSheetData) -> ArcBeatSheetData:
    """Phases and bands in chapter order, the bands numbered from 0 in that order, and the brief stripped."""
    bands = sorted(data.band_breakdown, key=lambda band: band.chapter_start)
    return data.model_copy(update={
        "phases": sorted(data.phases, key=lambda phase: phase.chapter_range),
        "band_breakdown": [band.model_copy(update={"band_index": index}) for index, band in enumerate(bands)],
        "setting_brief": data.setting_brief.strip(),
    })


def _is_active(runtime: BookStateRuntime, ref: str) -> bool:
    node = runtime.world.get_node(ref)
    return node is not None and node.is_active


def _is_seeded_character(runtime: BookStateRuntime, ref: str) -> bool:
    node = runtime.world.get_node(ref)
    return (node is not None and node.is_active and str(node.node_type) == "character"
            and node.metadata.get("source") == GENESIS_SEED_SOURCE_TYPE)


def _is_node_field(node: WorldNode, field: str) -> bool:
    """Whether ``field`` is a state or profile field of the node's type, bare or as a ``state.``/``profile.`` path."""
    fields = WORLD_NODE_FIELDS.get(str(node.node_type), {})
    section, _, rest = field.strip().partition(".")
    if section in ("state", "profile") and rest:
        return rest.split(".", 1)[0] in fields.get(section, ())
    return section in fields.get("state", set()) | fields.get("profile", set())


def _actors(runtime: BookStateRuntime) -> list[dict[str, Any]]:
    return [
        {"id": node.id, "name": node.name, "kind": str(node.node_type),
         "concealed": is_concealed(runtime.world, node),
         "tier": runtime.world.get_state(node.id).get("actor_tier"),
         "seeded": node.metadata.get("source") == GENESIS_SEED_SOURCE_TYPE}
        for node in sorted(runtime.world.nodes_by_id.values(), key=lambda item: item.id)
        if node.is_active and str(node.node_type) in ACTOR_NODE_TYPES
    ]


def _facts(runtime: BookStateRuntime) -> list[dict[str, Any]]:
    facts = runtime.world.facts_by_id.values()
    # Every standing fact and the newest event facts, as world adjudication lists them.
    events = sorted((fact for fact in facts if fact.fact_type == EVENT_FACT_TYPE),
                    key=lambda fact: (-fact.created_at_chapter, fact.id))[:MAX_ADJUDICATION_EVENT_FACTS]
    shown = [fact for fact in facts if fact.fact_type != EVENT_FACT_TYPE] + events
    return [
        {"ref": f"{FACT_REF_PREFIX}{fact.id}", "proposition": fact.proposition, "sensitivity": fact.sensitivity_level}
        for fact in sorted(shown, key=lambda item: item.id)
    ]


def _reveal_candidates(project_id: str, pack: Mapping[str, Any], runtime: BookStateRuntime) -> list[dict[str, Any]]:
    """The world seed's suggested reveal arcs, as fact refs to the facts canon still holds."""
    seed = pack.get("world_seed") if isinstance(pack.get("world_seed"), Mapping) else {}
    candidates = seed.get("reveal_candidates") if isinstance(seed.get("reveal_candidates"), list) else []
    result = []
    for candidate in candidates:
        if not isinstance(candidate, Mapping) or type(candidate.get("arc_number")) is not int:
            continue
        fact_id = seed_ref_id(project_id, "fact", str(candidate.get("fact_id") or "").strip())
        if fact_id in runtime.world.facts_by_id:
            result.append({"fact_ref": f"{FACT_REF_PREFIX}{fact_id}", "arc_number": candidate["arc_number"],
                           "note": str(candidate.get("note") or "")})
    return result


__all__ = [
    "STAGE_KEY",
    "ArcBeatSheetService",
    "ChapterMilestoneRefs",
    "band_breakdown_issues",
    "beat_sheet_issues",
    "chapter_milestone_refs",
    "milestone_refs_for_chapter",
]
