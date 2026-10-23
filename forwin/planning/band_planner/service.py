"""Plan a band just in time with ``plan.band``: validate and audit, retry once with the errors, else write nothing."""

from __future__ import annotations

import inspect
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.knowledge import KNOWLEDGE_STATES
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.schema import ACTOR_NODE_TYPES, WORLD_NODE_FIELDS
from forwin.models.planning_v6 import ArcBeatSheetRow, BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.observability.llm_trace import (
    RAW_REQUEST_KEY, RAW_RESPONSE_KEY, mark_latest_attempt_parse_failure, mark_latest_attempt_workflow,
)
from forwin.planning.band_planner.inputs import BandPlanInputs, build_band_inputs
from forwin.planning.band_planner.persistence import persist_band_plan
from forwin.planning.band_planner.types import (
    REWRITABLE_PLAN_STATUSES, WORLD_BINDING_KINDS, BandPlanDraft, BandPlanOutcome, BandPlanOutput,
    ChapterPlanDraft, FeasibilityAuditor, PlanIssue,
)
from forwin.planning.band_planner.validation import band_plan_issues
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import READER_OBSERVER, ArcBeatSheetData, BandSpan
from forwin.runtime.policy import RuntimePolicy
from forwin.skills import serialize_prompt_layers
from forwin.utils import parse_llm_json
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_types import ConflictDraft
from forwin.world_engine.types import INVALID_OUTPUT, LLM_UNAVAILABLE
from forwin.writer.prompt_core.planning import build_band_plan_messages


STAGE_KEY = "plan.band"
# The failure reason when every answer was valid but the feasibility audit found errors in it.
INFEASIBLE = "infeasible"
BAND_PLANNING_SOURCE = "band_planning"
_TASK_FAMILY = "planning"
_PARSER_NAME = "BandPlan JSON parser"
_OUTPUT_SCHEMA = BandPlanOutput.model_json_schema()
_ALLOWED = {
    "world_binding_kinds": list(WORLD_BINDING_KINDS),
    "reveal_target_states": list(KNOWLEDGE_STATES),
    "reader_observer": READER_OBSERVER,
    # The fields intended_changes may name, by actor kind.
    "actor_fields": {
        kind: {section: sorted(WORLD_NODE_FIELDS[kind][section]) for section in ("state", "profile")}
        for kind in ACTOR_NODE_TYPES
    },
}


class BandPlanner:
    """Writes band plans in the caller's session; the caller commits.

    Callers must not hold the project row lock: the model call runs first, then the planner locks the pending
    plot-response conflicts and the project row and re-reads the band before writing. Plan rows that are
    accepted, drafted or under review are never rewritten.

    ``llm_client`` is None when no model is configured: nothing is planned then. ``feasibility`` audits every
    valid answer. ``save_trace`` saves the call's PromptTrace and its LLM DecisionEvents in that session and
    returns the trace id; every planning call saves one.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        feasibility: FeasibilityAuditor,
        save_trace: Callable[[Session, dict], str],
        trope_cost_ceiling: int = 3,
        max_attempts: int = 2,
        max_tokens: int = 12000,
    ) -> None:
        self.llm_client = llm_client
        self.feasibility = feasibility
        self.save_trace = save_trace
        self.trope_cost_ceiling = max(0, int(trope_cost_ceiling))
        self.max_attempts = max(1, int(max_attempts))
        self.max_tokens = max_tokens
        self._chat_parameters = inspect.signature(llm_client.chat).parameters if llm_client is not None else {}

    def plan_band(
        self, session: Session, *, project_id: str, arc_plan: ArcPlanVersion, beat_sheet_row: ArcBeatSheetRow,
        band: BandSpan, policy: RuntimePolicy, extra_notes: Sequence[str] = (),
    ) -> BandPlanOutcome:
        """Plan the band's chapters that are still to write; a failure only returns its issues."""
        rows = _band_rows(session, project_id, band)
        numbers = _rewritable(band, rows, from_chapter=band.chapter_start)
        if not numbers:
            return BandPlanOutcome(band_plan_id=_active_band_plan_id(session, project_id, arc_plan.id, band))
        return self._plan(
            session, project_id=project_id, arc_plan=arc_plan, beat_sheet_row=beat_sheet_row, band=band,
            chapter_numbers=numbers, rows=rows, policy=policy, notes=extra_notes, reason="plan_band",
        ).outcome

    def ensure_plan_for_chapter(
        self, session: Session, *, project_id: str, chapter_number: int, policy: RuntimePolicy,
    ) -> ChapterPlan | None:
        """The chapter's plan row, planning its whole band when it has none.

        A row in any status comes back as it is. When the band cannot be planned this opens a ``band_planning``
        conflict that blocks the band and returns None; while that conflict is open it returns None at once.
        """
        existing = _chapter_plan(session, project_id, chapter_number)
        if existing is not None:
            return existing
        arc_plan = _arc_for_chapter(session, project_id, chapter_number)
        repo = ArcBeatSheetRepository(session)
        beat_sheet_row = repo.active_for_arc(arc_plan.id)
        if beat_sheet_row is None:
            raise LookupError(f"arc plan {arc_plan.id} has no active beat sheet to plan chapter {chapter_number} from")
        band = next((item for item in repo.load(beat_sheet_row).band_breakdown
                     if item.chapter_start <= chapter_number <= item.chapter_end), None)
        if band is None:
            raise LookupError(f"beat sheet {beat_sheet_row.id} has no band for chapter {chapter_number}")
        if _open_band_conflict(session, project_id, band) is not None:
            return None
        rows = _band_rows(session, project_id, band)
        result = self._plan(
            session, project_id=project_id, arc_plan=arc_plan, beat_sheet_row=beat_sheet_row, band=band,
            chapter_numbers=_rewritable(band, rows, from_chapter=band.chapter_start), rows=rows, policy=policy,
            notes=(), reason="ensure_plan",
        )
        if result.failed:
            _band_planning_conflict(session, project_id=project_id, band=band, issues=result.outcome.issues,
                                    as_of=result.as_of)
            return None
        return _chapter_plan(session, project_id, chapter_number)

    def replan_remaining(
        self, session: Session, *, project_id: str, band_plan_id: str, from_chapter: int, policy: RuntimePolicy,
        notes: Sequence[str],
    ) -> BandPlanOutcome:
        """Plan again the band's chapters from ``from_chapter`` on that are still to write, from the arc's active
        beat sheet; the band plan's later versions stand in for a stale ``band_plan_id``."""
        named = session.get(BandPlanRow, band_plan_id)
        if named is None or named.project_id != project_id:
            raise LookupError(f"band plan {band_plan_id} is not a band plan of project {project_id}")
        active = session.scalar(
            select(BandPlanRow)
            .where(BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == named.arc_plan_id,
                   BandPlanRow.band_index == named.band_index, BandPlanRow.status == "active")
            .order_by(BandPlanRow.version.desc()).limit(1)
        ) or named
        arc_plan = session.get(ArcPlanVersion, active.arc_plan_id)
        beat_sheet_row = ArcBeatSheetRepository(session).active_for_arc(active.arc_plan_id)
        if arc_plan is None or beat_sheet_row is None:
            raise LookupError(f"band plan {active.id} has no arc plan with an active beat sheet")
        band = BandSpan(band_index=active.band_index, chapter_start=active.chapter_start, chapter_end=active.chapter_end)
        rows = _band_rows(session, project_id, band)
        numbers = _rewritable(band, rows, from_chapter=from_chapter)
        if not numbers:
            return BandPlanOutcome(band_plan_id=active.id)
        return self._plan(
            session, project_id=project_id, arc_plan=arc_plan, beat_sheet_row=beat_sheet_row, band=band,
            chapter_numbers=numbers, rows=rows, policy=policy, notes=notes, reason="replan",
        ).outcome

    def _plan(
        self, session: Session, *, project_id: str, arc_plan: ArcPlanVersion, beat_sheet_row: ArcBeatSheetRow,
        band: BandSpan, chapter_numbers: list[int], rows: Mapping[int, ChapterPlan], policy: RuntimePolicy,
        notes: Sequence[str], reason: str,
    ) -> _PlanResult:
        beat_sheet = ArcBeatSheetRepository(session).load(beat_sheet_row)
        # The refs are checked against the latest canon: chapter 0 at handoff.
        as_of = BookStateRepository(session).latest_canon_chapter(project_id) or 0
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of)
        expected_active = _active_band_plan_id(session, project_id, arc_plan.id, band)
        inputs = build_band_inputs(
            session, project_id=project_id, arc_plan=arc_plan, beat_sheet=beat_sheet, band=band,
            chapter_numbers=chapter_numbers, rows=rows, as_of=as_of, runtime=runtime, policy=policy, notes=notes,
            trope_cost_ceiling=self.trope_cost_ceiling,
        )
        call = self._call(session, project_id=project_id, inputs=inputs, band=band, chapter_numbers=chapter_numbers,
                          beat_sheet=beat_sheet, policy=policy)
        trace = _TraceContext(project_id=project_id, arc_plan_id=arc_plan.id, band=band,
                              chapter_numbers=chapter_numbers, reason=reason)
        if call.draft is None:
            outcome = BandPlanOutcome(band_plan_id=None, issues=call.issues)
            self.save_trace(session, self._prompt_trace(call, trace, outcome))
            return _PlanResult(outcome=outcome, failed=True, as_of=as_of)

        conflicts = WorldConflictRepository(session)
        locked = [conflicts.get(project_id, item.conflict_id, for_update=True)
                  for item in sorted(inputs.offered.pending, key=lambda item: item.conflict_id)]
        session.execute(select(Project).where(Project.id == project_id).with_for_update(key_share=True)).scalar_one()
        still_pending = {row.id for row in conflicts.pending_plot_responses(project_id)}
        active = _active_band_plan_id(session, project_id, arc_plan.id, band)
        if active != expected_active:
            # Another planner wrote this band during the model call: its plan stands.
            outcome = BandPlanOutcome(band_plan_id=active, issues=call.warnings)
            self.save_trace(session, self._prompt_trace(call, trace, outcome, superseded=True))
            return _PlanResult(outcome=outcome, failed=False, as_of=as_of)
        fresh = _band_rows(session, project_id, band, refresh=True)
        writable = [chapter for chapter in call.draft.chapters
                    if chapter.chapter_number not in fresh
                    or fresh[chapter.chapter_number].status in REWRITABLE_PLAN_STATUSES]
        if not writable:
            # Every chapter was taken up for writing during the model call: nothing is left to plan.
            outcome = BandPlanOutcome(band_plan_id=active, issues=call.warnings)
            self.save_trace(session, self._prompt_trace(call, trace, outcome))
            return _PlanResult(outcome=outcome, failed=False, as_of=as_of)
        persisted = persist_band_plan(
            session, project_id=project_id, arc_plan=arc_plan, beat_sheet_id=beat_sheet_row.id,
            draft=call.draft.model_copy(update={"chapters": writable}), rows=fresh, inputs=inputs,
            pending_conflicts=[row for row in locked if row is not None and row.id in still_pending], notes=notes,
        )
        outcome = BandPlanOutcome(band_plan_id=persisted.band_plan.id,
                                  chapter_plan_ids=[plan.id for plan in persisted.plans], issues=call.warnings)
        self.save_trace(session, self._prompt_trace(call, trace, outcome))
        return _PlanResult(outcome=outcome, failed=False, as_of=as_of)

    def _call(
        self, session: Session, *, project_id: str, inputs: BandPlanInputs, band: BandSpan,
        chapter_numbers: Sequence[int], beat_sheet: ArcBeatSheetData, policy: RuntimePolicy,
    ) -> _BandPlanCall:
        """Up to ``max_attempts`` calls. An invalid, malformed or infeasible answer goes back to the model as
        feedback; a provider failure resends the same messages."""
        call = _BandPlanCall()
        retry_feedback: list[str] = []
        allowed = {**_ALLOWED, "time_units": sorted(inputs.offered.time_units)}
        # Whether any call returned an answer, valid or not.
        answered = False
        for attempt_no in range(1, self.max_attempts + 1):
            call.messages = build_band_plan_messages(
                payload=inputs.payload, digest_text=inputs.digest_text, output_schema=_OUTPUT_SCHEMA,
                allowed=allowed, retry_feedback=retry_feedback,
            )
            if self.llm_client is None:
                call.issues = [_error(LLM_UNAVAILABLE, "no LLM client is configured")]
                call.failure_reason = LLM_UNAVAILABLE
                return call
            raw = ""
            call_error: Exception | None = None
            provider_failed = infeasible = False
            chapters: list[ChapterPlanDraft] = []
            try:
                raw = self._chat(call.messages)
            except Exception as exc:  # noqa: BLE001 - a provider failure is retried, then nothing is planned
                call_error, provider_failed = exc, True
            else:
                answered = True
                try:
                    parsed = BandPlanOutput.model_validate(parse_llm_json(raw, error_prefix=_PARSER_NAME))
                except Exception as exc:  # noqa: BLE001 - a malformed answer is sent back to the model
                    call_error = exc
                    mark_latest_attempt_parse_failure(
                        self.llm_client, parser_name=_PARSER_NAME, stage_key=STAGE_KEY,
                        schema_name="BandPlanOutput", raw_output=raw, error=exc,
                    )
            if call_error is not None:
                code = LLM_UNAVAILABLE if provider_failed else INVALID_OUTPUT
                call.issues = [_error(code, f"{type(call_error).__name__}: {call_error}")]
            else:
                call.issues = band_plan_issues(
                    parsed, chapter_numbers=chapter_numbers, offered=inputs.offered, runtime=inputs.runtime,
                    calendar=inputs.calendar, lexicon=inputs.lexicon,
                )
                if not call.issues:
                    chapters = sorted(parsed.chapters, key=lambda chapter: chapter.chapter_number)
                    audit = self.feasibility.audit_plans(
                        session, project_id=project_id, plans=chapters, beat_sheet=beat_sheet, policy=policy,
                        runtime=inputs.runtime,
                    )
                    call.issues = [issue for issue in audit if issue.severity == "error"]
                    call.warnings = [issue for issue in audit if issue.severity != "error"]
                    infeasible = bool(call.issues)
            feedback = [_feedback_line(issue) for issue in call.issues]
            mark_latest_attempt_workflow(self.llm_client, attempt_no=attempt_no, stage_key=STAGE_KEY)
            call.attempts.extend(self._drained_attempts(attempt_no, call.messages, raw, call_error, "\n".join(feedback)))
            if not call.issues:
                call.draft = BandPlanDraft(band=band, chapters=chapters)
                call.failure_reason = ""
                return call
            if not provider_failed:
                retry_feedback = feedback
            call.failure_reason = INFEASIBLE if infeasible else INVALID_OUTPUT if answered else LLM_UNAVAILABLE
            if attempt_no < self.max_attempts:
                call.business_retries.append({
                    "stage": STAGE_KEY, "attempt_no": attempt_no, "max_attempts": self.max_attempts,
                    "reason_class": type(call_error).__name__ if call_error else "BandPlanValidation",
                    "reason": "\n".join(feedback)[:500], "retry_kind": "business_validation",
                })
        return call

    def _chat(self, messages: list[dict]) -> str:
        kwargs: dict[str, Any] = {
            "temperature": 0.4, "max_tokens": self.max_tokens,
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
        self, call: _BandPlanCall, context: _TraceContext, outcome: BandPlanOutcome, *, superseded: bool = False,
    ) -> dict[str, Any]:
        last_call_result = getattr(self.llm_client, "last_call_result", None) if call.attempts else None
        trace = getattr(last_call_result, "trace", {}) or {}
        layers = serialize_prompt_layers(call.messages, [])
        band = context.band
        return {
            "trace_scope": "planning", "stage_key": STAGE_KEY,
            "backend": str(trace.get("backend") or getattr(last_call_result, "backend", "") or ""),
            "codex_job_id": str(trace.get("codex_job_id", "") or ""),
            "permission_profile": str(trace.get("permission_profile", "") or ""),
            "fallback_used": bool(getattr(last_call_result, "fallback_used", False)),
            "template_id": "planning:band_plan", "template_version": "v1",
            "effective_system_prompt": "\n\n".join(
                str(item["content"]).strip() for item in layers if item["role"] == "system"
            ),
            "prompt_layers": layers,
            "input_snapshot": {
                "project_id": context.project_id, "chapter_number": band.chapter_start,
                "arc_plan_id": context.arc_plan_id, "band_index": band.band_index,
                "band": [band.chapter_start, band.chapter_end], "chapters_to_plan": list(context.chapter_numbers),
                "reason": context.reason, "stage_key": STAGE_KEY, "selected_skills": [],
            },
            "model_profile": {
                **{key: getattr(self.llm_client, key, "") for key in ("profile_id", "profile_name", "model", "base_url")},
                "actual_model": getattr(last_call_result, "actual_model", "") or "",
            },
            "attempts": call.attempts,
            "output_summary": {
                "failure_reason": call.failure_reason, "issues": [issue.model_dump(mode="json") for issue in call.issues],
                "warnings": [issue.model_dump(mode="json") for issue in call.warnings],
                "band_plan_id": outcome.band_plan_id, "chapter_plan_ids": list(outcome.chapter_plan_ids),
                "superseded_by_concurrent_plan": superseded,
                "skill_summary": [], "business_retry_events": call.business_retries,
            },
        }


@dataclass
class _BandPlanCall:
    """One planning call's model attempts: the valid draft, if any, and what the trace records."""

    draft: BandPlanDraft | None = None
    # The last attempt's errors; empty on success.
    issues: list[PlanIssue] = field(default_factory=list)
    # The feasibility warnings on the accepted draft.
    warnings: list[PlanIssue] = field(default_factory=list)
    # LLM_UNAVAILABLE when no call returned an answer, INFEASIBLE when the last valid answer failed the audit,
    # INVALID_OUTPUT otherwise; empty on success.
    failure_reason: str = ""
    messages: list[dict] = field(default_factory=list)
    attempts: list[dict[str, Any]] = field(default_factory=list)
    business_retries: list[dict[str, Any]] = field(default_factory=list)


@dataclass(frozen=True)
class _TraceContext:
    project_id: str
    arc_plan_id: str
    band: BandSpan
    chapter_numbers: Sequence[int]
    reason: str


@dataclass(frozen=True)
class _PlanResult:
    outcome: BandPlanOutcome
    failed: bool
    # The latest canon chapter the plan was made as of.
    as_of: int


def _band_planning_conflict(
    session: Session, *, project_id: str, band: BandSpan, issues: Sequence[PlanIssue], as_of: int,
) -> WorldPlanConflictRow:
    """A human ``band_planning`` conflict that blocks the band's first chapter; ``amend_plan`` is its only option."""
    first = issues[0].message if issues else "no plan"
    draft = ConflictDraft(
        project_id=project_id, source=BAND_PLANNING_SOURCE, detected_after_chapter=as_of,
        story_time=BookStateQuery(session).current_story_minute(project_id, as_of_chapter=as_of) or 0,
        involved_refs=list(dict.fromkeys(issue.ref for issue in issues if issue.ref)), violated="feasibility",
        impact_level="major",
        summary=f"Chapters {band.chapter_start}-{band.chapter_end} could not be planned: {first}",
        proposed_change={
            "kind": "plan_issues", "band": [band.chapter_start, band.chapter_end],
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
        blocks_from_chapter=band.chapter_start,
    )
    return WorldConflictRepository(session).create(draft, required_decider="human", options=("amend_plan",))


def _open_band_conflict(session: Session, project_id: str, band: BandSpan) -> WorldPlanConflictRow | None:
    return session.scalar(select(WorldPlanConflictRow).where(
        WorldPlanConflictRow.project_id == project_id, WorldPlanConflictRow.source == BAND_PLANNING_SOURCE,
        WorldPlanConflictRow.status == "open", WorldPlanConflictRow.blocks_from_chapter == band.chapter_start,
    ).limit(1))


def _arc_for_chapter(session: Session, project_id: str, chapter_number: int) -> ArcPlanVersion:
    """The arc whose fixed chapter range holds the chapter, an active one before a planned one."""
    arcs = session.scalars(select(ArcPlanVersion).where(
        ArcPlanVersion.project_id == project_id, ArcPlanVersion.status != "superseded",
        ArcPlanVersion.chapter_start <= chapter_number, ArcPlanVersion.chapter_end >= chapter_number,
    ).order_by(ArcPlanVersion.version.desc(), ArcPlanVersion.id))
    arc = min(arcs, key=lambda item: item.status != "active", default=None)
    if arc is None:
        raise LookupError(f"project {project_id} has no arc plan holding chapter {chapter_number}")
    return arc


def _chapter_plan(session: Session, project_id: str, chapter_number: int) -> ChapterPlan | None:
    return session.scalar(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == chapter_number,
    ))


def _band_rows(session: Session, project_id: str, band: BandSpan, *, refresh: bool = False) -> dict[int, ChapterPlan]:
    statement = select(ChapterPlan).where(
        ChapterPlan.project_id == project_id,
        ChapterPlan.chapter_number.between(band.chapter_start, band.chapter_end),
    )
    if refresh:
        statement = statement.execution_options(populate_existing=True)
    return {plan.chapter_number: plan for plan in session.scalars(statement)}


def _rewritable(band: BandSpan, rows: Mapping[int, ChapterPlan], *, from_chapter: int) -> list[int]:
    """The band's chapters from ``from_chapter`` on that have no plan row, or one still to write."""
    return [number for number in range(max(from_chapter, band.chapter_start), band.chapter_end + 1)
            if number not in rows or rows[number].status in REWRITABLE_PLAN_STATUSES]


def _active_band_plan_id(session: Session, project_id: str, arc_plan_id: str, band: BandSpan) -> str | None:
    return session.scalar(
        select(BandPlanRow.id)
        .where(BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == arc_plan_id,
               BandPlanRow.band_index == band.band_index, BandPlanRow.status == "active")
        .order_by(BandPlanRow.version.desc()).limit(1)
    )


def _error(code: str, message: str) -> PlanIssue:
    return PlanIssue(code=code, severity="error", message=message)


def _feedback_line(issue: PlanIssue) -> str:
    where = f"chapter {issue.chapter_number}: " if issue.chapter_number is not None else ""
    return f"{where}[{issue.code}] {issue.message}"


__all__ = ["BAND_PLANNING_SOURCE", "INFEASIBLE", "STAGE_KEY", "BandPlanner"]
