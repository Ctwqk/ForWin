"""Plan a band just in time with ``plan.band``: validate and audit, retry once with the errors, else write nothing."""

from __future__ import annotations

import inspect
import logging
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import DBAPIError
from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import StoryCalendarRepository
from forwin.book_state.knowledge import KNOWLEDGE_STATES
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.schema import ACTOR_NODE_TYPES, WORLD_NODE_FIELDS
from forwin.models.planning_v6 import ArcBeatSheetRow, BandPlanRow
from forwin.models.draft import CandidateDraftRecord, ChapterDraft
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.observability.llm_trace import (
    build_workflow_trace, drain_workflow_attempts,
)
from forwin.planning.band_planner.inputs import BandPayloadBudgetExceeded, BandPlanInputs, build_band_inputs
from forwin.planning.band_planner.persistence import persist_band_plan
from forwin.planning.band_planner.types import (
    REWRITABLE_PLAN_STATUSES, WORLD_BINDING_KINDS, BandPlanDraft, BandPlanOutcome, BandPlanOutput,
    BandPlanningAuditError, ChapterPlanDraft, FeasibilityAuditor, PlanIssue,
)
from forwin.planning.band_planner.validation import band_plan_issues
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import READER_OBSERVER, ArcBeatSheetData, BandSpan
from forwin.runtime.policy import RuntimePolicy
from forwin.utils import parse_llm_json
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_types import ConflictDraft
from forwin.world_engine.types import INVALID_OUTPUT, LLM_UNAVAILABLE
from forwin.writer.prompt_core.planning import build_band_plan_messages


logger = logging.getLogger(__name__)


STAGE_KEY = "plan.band"
# The failure reason when every answer was valid but the feasibility audit found errors in it.
INFEASIBLE = "infeasible"
# The failure reason when validating or auditing an answer raised; the exception reaches the caller.
AUDIT_ERROR = "audit_error"
# The failure reason, and the issue code, when the book has no story calendar to measure chapter spans in.
NO_STORY_CALENDAR = "no_story_calendar"
PAYLOAD_BUDGET_EXCEEDED = "payload_budget_exceeded"
BAND_PLANNING_SOURCE = "band_planning"
_TASK_FAMILY = "planning"
_PARSER_NAME = "BandPlan JSON parser"
_OUTPUT_SCHEMA = BandPlanOutput.model_json_schema()


class _Unset:
    pass


_UNSET = _Unset()


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


class BandPlanUnavailable(LookupError):
    """The chapter has no arc, active beat sheet, or band to plan from."""


class BandPlanner:
    """Writes band plans in the caller's session; the caller commits.

    Callers must not hold the project row lock: the model call runs first, then the planner locks the pending
    plot-response conflicts and the project row and re-reads the band before writing. Plan rows that are
    accepted, drafted or under review are never rewritten.

    ``llm_client`` is None when no model is configured: nothing is planned then. ``feasibility`` audits every
    valid answer. ``save_trace`` saves the call's PromptTrace and its LLM DecisionEvents in that session and
    returns the trace id; it must support ``independent=True`` for an audit failure. Every planning call saves one.
    """

    def __init__(
        self,
        llm_client: Any,
        *,
        feasibility: FeasibilityAuditor,
        save_trace: Callable[..., str],
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
        expected_band_plan_id: str | None | _Unset = _UNSET,
        amend_chapters: frozenset[int] = frozenset(),
    ) -> BandPlanOutcome:
        """Plan the band's chapters that are still to write; a failure only returns its issues.

        An explicit expected id (including None for a missing band) fences publication to the caller's
        observation. Omitted expectations retain the planner's own snapshot and the human cap override.
        ``amend_chapters`` lets an explicit human amendment rewrite old-planner planned/failed rows with draft history.
        """
        rows = _band_rows(session, project_id, band)
        numbers = _rewritable(session, band, rows, from_chapter=band.chapter_start, amend_chapters=amend_chapters)
        if not numbers:
            return BandPlanOutcome(band_plan_id=_active_band_plan_id(session, project_id, arc_plan.id, band))
        return self._plan(
            session, project_id=project_id, arc_plan=arc_plan, beat_sheet_row=beat_sheet_row, band=band,
            chapter_numbers=numbers, rows=rows, policy=policy, notes=extra_notes, reason="plan_band",
            expected_band_plan_id=expected_band_plan_id, amend_chapters=amend_chapters,
        ).outcome

    def ensure_plan_for_chapter(
        self, session: Session, *, project_id: str, chapter_number: int, policy: RuntimePolicy,
    ) -> ChapterPlan | None:
        """The chapter's plan row, planning its whole band when it has none.

        A row in any status comes back as it is, except one the old planner wrote (no band plan) that is still to
        write and was never drafted: the band is planned, rewriting that row in place (Ruling P4a-R31).
        Rows with draft or candidate history keep their existing plans. When the band cannot be planned
        this opens a ``band_planning`` conflict that blocks the band and returns None; while that conflict is open
        it returns None at once. A concurrent band plan that won the band without this chapter is followed by one
        more planning call.
        """
        existing = _chapter_plan(session, project_id, chapter_number)
        if _band_planned(session, existing):
            return existing
        arc_plan = _arc_for_chapter(session, project_id, chapter_number)
        repo = ArcBeatSheetRepository(session)
        beat_sheet_row = repo.active_for_arc(arc_plan.id)
        if beat_sheet_row is None:
            raise BandPlanUnavailable(f"arc plan {arc_plan.id} has no active beat sheet to plan chapter {chapter_number} from")
        band = next((item for item in repo.load(beat_sheet_row).band_breakdown
                     if item.chapter_start <= chapter_number <= item.chapter_end), None)
        if band is None:
            raise BandPlanUnavailable(f"beat sheet {beat_sheet_row.id} has no band for chapter {chapter_number}")
        if _open_band_conflict(session, project_id, band) is not None:
            return None

        def plan_rewritable(reason: str) -> _PlanResult:
            rows = _band_rows(session, project_id, band, refresh=True)
            return self._plan(
                session, project_id=project_id, arc_plan=arc_plan, beat_sheet_row=beat_sheet_row, band=band,
                chapter_numbers=_rewritable(session, band, rows, from_chapter=band.chapter_start), rows=rows, policy=policy,
                notes=(), reason=reason,
            )

        result = plan_rewritable("ensure_plan")
        if not result.failed and not _band_planned(session, _chapter_plan(session, project_id, chapter_number)):
            result = plan_rewritable("ensure_plan_retry")
        if not result.failed and _band_planned(session, plan := _chapter_plan(session, project_id, chapter_number)):
            return plan
        issues = result.outcome.issues if result.failed else [_error(
            "chapter_unplanned", f"concurrent band plans left chapter {chapter_number} without a plan",
        )]
        # Serialize with other planners before blocking the band: one may have planned the chapter, or opened the
        # conflict, since. No conflict row is locked here, so the project lock keeps the usual order.
        session.execute(select(Project).where(Project.id == project_id).with_for_update(key_share=True)).scalar_one()
        if _band_planned(session, plan := _chapter_plan(session, project_id, chapter_number)):
            return plan
        if _open_band_conflict(session, project_id, band) is None:
            _band_planning_conflict(session, project_id=project_id, band=band, issues=issues, as_of=result.as_of)
        return None

    def replan_remaining(
        self, session: Session, *, project_id: str, band_plan_id: str, from_chapter: int, policy: RuntimePolicy,
        notes: Sequence[str],
        expected_band_plan_id: str | None | _Unset = _UNSET,
        amend_chapters: frozenset[int] = frozenset(),
    ) -> BandPlanOutcome:
        """Plan again the band's chapters from ``from_chapter`` on that are still to write, from the arc's active
        beat sheet. Only unguarded callers follow later versions of a stale ``band_plan_id``."""
        named = session.get(BandPlanRow, band_plan_id)
        if named is None or named.project_id != project_id:
            raise LookupError(f"band plan {band_plan_id} is not a band plan of project {project_id}")
        active = session.scalar(
            select(BandPlanRow)
            .where(BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == named.arc_plan_id,
                   BandPlanRow.band_index == named.band_index, BandPlanRow.status == "active")
            .order_by(BandPlanRow.version.desc()).limit(1)
        ) or named
        if expected_band_plan_id is not _UNSET:
            active = named
        arc_plan = session.get(ArcPlanVersion, active.arc_plan_id)
        beat_sheet_row = ArcBeatSheetRepository(session).active_for_arc(active.arc_plan_id)
        if arc_plan is None or beat_sheet_row is None:
            raise LookupError(f"band plan {active.id} has no arc plan with an active beat sheet")
        band = BandSpan(band_index=active.band_index, chapter_start=active.chapter_start, chapter_end=active.chapter_end)
        rows = _band_rows(session, project_id, band)
        numbers = _rewritable(session, band, rows, from_chapter=from_chapter, amend_chapters=amend_chapters)
        if not numbers:
            return BandPlanOutcome(band_plan_id=active.id)
        return self._plan(
            session, project_id=project_id, arc_plan=arc_plan, beat_sheet_row=beat_sheet_row, band=band,
            chapter_numbers=numbers, rows=rows, policy=policy, notes=notes, reason="replan",
            expected_band_plan_id=expected_band_plan_id, amend_chapters=amend_chapters,
        ).outcome

    def _plan(
        self, session: Session, *, project_id: str, arc_plan: ArcPlanVersion, beat_sheet_row: ArcBeatSheetRow,
        band: BandSpan, chapter_numbers: list[int], rows: Mapping[int, ChapterPlan], policy: RuntimePolicy,
        notes: Sequence[str], reason: str,
        expected_band_plan_id: str | None | _Unset = _UNSET,
        amend_chapters: frozenset[int] = frozenset(),
    ) -> _PlanResult:
        # The refs are checked against the latest canon: chapter 0 at handoff.
        as_of = BookStateRepository(session).latest_canon_chapter(project_id) or 0
        trace = _TraceContext(project_id=project_id, arc_plan_id=arc_plan.id, band=band,
                              chapter_numbers=chapter_numbers, reason=reason)
        if StoryCalendarRepository(session).get(project_id) is None:
            # Chapter spans are story durations: without a calendar no answer could pass, so no model is asked.
            call = _BandPlanCall(issues=[_error(NO_STORY_CALENDAR, "the book has no story calendar")],
                                 failure_reason=NO_STORY_CALENDAR)
            outcome = BandPlanOutcome(band_plan_id=None, issues=call.issues, failure_reason=call.failure_reason)
            self.save_trace(session, self._prompt_trace(call, trace, outcome))
            return _PlanResult(outcome=outcome, failed=True, as_of=as_of)
        beat_sheet = ArcBeatSheetRepository(session).load(beat_sheet_row)
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=as_of)
        expected_active = (_active_band_plan_id(session, project_id, arc_plan.id, band)
                           if expected_band_plan_id is _UNSET else expected_band_plan_id)
        try:
            inputs = build_band_inputs(
                session, project_id=project_id, arc_plan=arc_plan, beat_sheet=beat_sheet, band=band,
                chapter_numbers=chapter_numbers, rows=rows, as_of=as_of, runtime=runtime, policy=policy, notes=notes,
                trope_cost_ceiling=self.trope_cost_ceiling,
            )
        except BandPayloadBudgetExceeded as exc:
            call = _BandPlanCall(issues=[_error(PAYLOAD_BUDGET_EXCEEDED, str(exc))],
                                 failure_reason=PAYLOAD_BUDGET_EXCEEDED)
        else:
            call = self._call(session, project_id=project_id, inputs=inputs, band=band, chapter_numbers=chapter_numbers,
                              beat_sheet=beat_sheet, policy=policy)
        if call.error is not None:
            # An audit failure may abort the caller's transaction; spent calls still need their audit trail.
            if isinstance(call.error, DBAPIError):
                logger.warning(
                    "Project %s: could not save the failed band audit trace in the failed audit unit; "
                    "attempting independent persistence after database error: %s",
                    project_id, call.error,
                )
            try:
                self.save_trace(session, self._prompt_trace(call, trace, BandPlanOutcome(band_plan_id=None)), independent=True)
            except Exception:  # noqa: BLE001 - preserve the audit error if independent persistence also fails
                logger.warning("Project %s: could not save the failed band audit trace", project_id, exc_info=True)
            raise BandPlanningAuditError(str(call.error), call.issues) from call.error
        if call.draft is None:
            outcome = BandPlanOutcome(band_plan_id=None, issues=call.issues, failure_reason=call.failure_reason)
            self.save_trace(session, self._prompt_trace(call, trace, outcome))
            return _PlanResult(outcome=outcome, failed=True, as_of=as_of)

        # The write locks live in a savepoint, so a path that writes nothing releases them at once: the caller
        # may plan again (a model call) in the same transaction.
        lock_scope = session.begin_nested()
        conflicts = WorldConflictRepository(session)
        locked = [conflicts.get(project_id, item.conflict_id, for_update=True)
                  for item in sorted(inputs.offered.pending, key=lambda item: item.conflict_id)]
        session.execute(select(Project).where(Project.id == project_id).with_for_update(key_share=True)).scalar_one()
        still_pending = {row.id for row in conflicts.pending_plot_responses(project_id)}
        active = _active_band_plan_id(session, project_id, arc_plan.id, band)
        if active != expected_active:
            # Another planner won since the caller's observation (or the unguarded planner's snapshot).
            lock_scope.rollback()
            outcome = BandPlanOutcome(band_plan_id=active, failure_reason="superseded_by_concurrent_plan")
            self.save_trace(session, self._prompt_trace(call, trace, outcome, superseded=True))
            return _PlanResult(outcome=outcome, failed=False, as_of=as_of)
        current_sheet = ArcBeatSheetRepository(session).active_for_arc(arc_plan.id)
        if current_sheet is None or current_sheet.id != beat_sheet_row.id:
            lock_scope.rollback()
            call.failure_reason = "superseded_beat_sheet"
            outcome = BandPlanOutcome(band_plan_id=None, failure_reason=call.failure_reason)
            self.save_trace(session, self._prompt_trace(call, trace, outcome))
            return _PlanResult(outcome=outcome, failed=True, as_of=as_of)
        if expected_band_plan_id is not _UNSET and active is not None:
            count = session.scalar(select(BandPlanRow.replan_count).where(BandPlanRow.id == active))
            if count >= policy.planning.max_replans_per_band:
                lock_scope.rollback()
                call.failure_reason = "replan_cap_reached"
                outcome = BandPlanOutcome(band_plan_id=None, failure_reason=call.failure_reason)
                self.save_trace(session, self._prompt_trace(call, trace, outcome))
                return _PlanResult(outcome=outcome, failed=True, as_of=as_of)
        # Locked, so a status change by a writer that does not take the project lock waits for this plan.
        fresh = _band_rows(session, project_id, band, refresh=True, lock=True)
        writable = [chapter for chapter in call.draft.chapters
                    if _can_rewrite(session, fresh.get(chapter.chapter_number), amend_chapters=amend_chapters)]
        if not writable:
            # Every chapter was taken up for writing during the model call: nothing is left to plan.
            lock_scope.rollback()
            outcome = BandPlanOutcome(band_plan_id=active)
            self.save_trace(session, self._prompt_trace(call, trace, outcome))
            return _PlanResult(outcome=outcome, failed=False, as_of=as_of)
        try:
            persisted = persist_band_plan(
                session, project_id=project_id, arc_plan=arc_plan, beat_sheet_id=beat_sheet_row.id,
                draft=call.draft.model_copy(update={"chapters": writable}), rows=fresh, inputs=inputs,
                pending_conflicts=[row for row in locked if row is not None and row.id in still_pending], notes=notes,
            )
        except Exception:
            lock_scope.rollback()
            raise
        lock_scope.commit()
        outcome = BandPlanOutcome(band_plan_id=persisted.band_plan.id,
                                  chapter_plan_ids=[plan.id for plan in persisted.plans], issues=call.warnings)
        self.save_trace(session, self._prompt_trace(call, trace, outcome))
        return _PlanResult(outcome=outcome, failed=False, as_of=as_of)

    def _call(
        self, session: Session, *, project_id: str, inputs: BandPlanInputs, band: BandSpan,
        chapter_numbers: Sequence[int], beat_sheet: ArcBeatSheetData, policy: RuntimePolicy,
    ) -> _BandPlanCall:
        """Up to ``max_attempts`` calls. An invalid, malformed or infeasible answer goes back to the model as
        feedback; a provider failure resends the same messages.

        When validation or the audit raises, the call stops with ``error`` set, never sent to the model.
        """
        call = _BandPlanCall()
        retry_feedback: list[str] = []
        allowed = {**_ALLOWED, "time_units": sorted(inputs.offered.time_units)}
        # The issues and failure reason of the last attempt that returned an answer.
        last_answer: tuple[list[PlanIssue], str] | None = None
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
                try:
                    parsed = BandPlanOutput.model_validate(parse_llm_json(raw, error_prefix=_PARSER_NAME))
                except Exception as exc:  # noqa: BLE001 - a malformed answer is sent back to the model
                    call_error = exc
            if call_error is not None:
                code = LLM_UNAVAILABLE if provider_failed else INVALID_OUTPUT
                call.issues = [_error(code, f"{type(call_error).__name__}: {call_error}")]
            else:
                try:
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
                except Exception as exc:  # noqa: BLE001 - re-raised by the caller once the trace is saved
                    call.error, call.failure_reason = exc, AUDIT_ERROR
                    call.issues = [_error(AUDIT_ERROR, f"{type(exc).__name__}: {exc}")]
                    call.attempts.extend(self._drained_attempts(attempt_no, call.messages, raw, exc, str(exc)))
                    return call
            feedback = [_feedback_line(issue) for issue in call.issues]
            call.attempts.extend(self._drained_attempts(
                attempt_no, call.messages, raw, call_error, "\n".join(feedback),
                parse_error=call_error if not provider_failed else None,
            ))
            if not call.issues:
                call.draft = BandPlanDraft(band=band, chapters=chapters)
                call.failure_reason = ""
                return call
            if provider_failed:
                call.failure_reason = LLM_UNAVAILABLE
                if last_answer is not None:
                    # The answered attempt's problems stay the reason; the provider error is only added.
                    call.issues = [*last_answer[0], *call.issues]
                    call.failure_reason = last_answer[1]
            else:
                retry_feedback = feedback
                call.failure_reason = INFEASIBLE if infeasible else INVALID_OUTPUT
                last_answer = (call.issues, call.failure_reason)
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
        *, parse_error: Exception | None = None,
    ) -> list[dict[str, Any]]:
        return drain_workflow_attempts(
            self.llm_client, attempt_no=attempt_no, stage_key=STAGE_KEY, task_family=_TASK_FAMILY,
            messages=messages, raw=raw, call_error=call_error, error_message=error_message,
            parse_error=parse_error, parser_name=_PARSER_NAME, schema_name="BandPlanOutput",
        )

    def _prompt_trace(
        self, call: _BandPlanCall, context: _TraceContext, outcome: BandPlanOutcome, *, superseded: bool = False,
    ) -> dict[str, Any]:
        band = context.band
        return build_workflow_trace(
            self.llm_client, messages=call.messages, attempts=call.attempts,
            trace_scope="planning",
            stage_key=STAGE_KEY,
            template_id="planning:band_plan",
            input_snapshot={
                "project_id": context.project_id, "chapter_number": band.chapter_start,
                "arc_plan_id": context.arc_plan_id, "band_index": band.band_index,
                "band": [band.chapter_start, band.chapter_end], "chapters_to_plan": list(context.chapter_numbers),
                "reason": context.reason, "stage_key": STAGE_KEY, "selected_skills": [],
            },
            output_summary={
                "failure_reason": call.failure_reason, "issues": [issue.model_dump(mode="json") for issue in call.issues],
                "warnings": [issue.model_dump(mode="json") for issue in call.warnings],
                "band_plan_id": outcome.band_plan_id, "chapter_plan_ids": list(outcome.chapter_plan_ids),
                "superseded_by_concurrent_plan": superseded,
                "skill_summary": [], "business_retry_events": call.business_retries,
            },
        )


@dataclass
class _BandPlanCall:
    """One planning call's model attempts: the valid draft, if any, and what the trace records."""

    draft: BandPlanDraft | None = None
    # The last answered attempt's errors, with a later provider error added; empty on success.
    issues: list[PlanIssue] = field(default_factory=list)
    # The feasibility warnings on the accepted draft.
    warnings: list[PlanIssue] = field(default_factory=list)
    # LLM_UNAVAILABLE when no call returned an answer, INFEASIBLE when the last answer was valid but failed the
    # audit, INVALID_OUTPUT when it was invalid, AUDIT_ERROR when checking it raised; NO_STORY_CALENDAR and
    # PAYLOAD_BUDGET_EXCEEDED stop before asking the model. Empty on success.
    failure_reason: str = ""
    # What validation or the audit raised; the caller re-raises it.
    error: Exception | None = None
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
        raise BandPlanUnavailable(f"project {project_id} has no arc plan holding chapter {chapter_number}")
    return arc


def _chapter_plan(session: Session, project_id: str, chapter_number: int) -> ChapterPlan | None:
    """Re-read: a concurrent planner may have rewritten an old planner's row this session has loaded."""
    return session.scalar(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id, ChapterPlan.chapter_number == chapter_number,
    ).execution_options(populate_existing=True))


def _has_draft_history(session: Session, plan: ChapterPlan) -> bool:
    return session.scalar(select(ChapterDraft.id).where(
        ChapterDraft.chapter_plan_id == plan.id,
    ).limit(1)) is not None or session.scalar(select(CandidateDraftRecord.id).where(
        CandidateDraftRecord.project_id == plan.project_id,
        CandidateDraftRecord.chapter_number == plan.chapter_number,
    ).limit(1)) is not None


def _band_planned(session: Session, plan: ChapterPlan | None) -> bool:
    """Whether a plan already exists or its draft history requires preserving the old planner's row."""
    return plan is not None and (plan.band_plan_id is not None or not _can_rewrite(session, plan))


def _can_rewrite(
    session: Session, plan: ChapterPlan | None, *, amend_chapters: frozenset[int] = frozenset(),
) -> bool:
    """A plan still to write, unless a decided chapter conflict waits to resume it from its saved draft; only a
    human amendment of that chapter rewrites it then."""
    if plan is None:
        return True
    if plan.chapter_number in amend_chapters:
        return plan.status in REWRITABLE_PLAN_STATUSES
    return plan.status in REWRITABLE_PLAN_STATUSES and (
        plan.band_plan_id is not None or not _has_draft_history(session, plan)
    ) and not WorldConflictRepository(session).pending_chapter_resumes(
        plan.project_id, chapter_number=plan.chapter_number,
    )


def _band_rows(
    session: Session, project_id: str, band: BandSpan, *, refresh: bool = False, lock: bool = False,
) -> dict[int, ChapterPlan]:
    """The band's plan rows; ``refresh`` re-reads loaded rows, ``lock`` also locks them ``FOR UPDATE``."""
    statement = select(ChapterPlan).where(
        ChapterPlan.project_id == project_id,
        ChapterPlan.chapter_number.between(band.chapter_start, band.chapter_end),
    )
    if lock:
        statement = statement.with_for_update()
    if refresh or lock:
        statement = statement.execution_options(populate_existing=True)
    return {plan.chapter_number: plan for plan in session.scalars(statement)}


def _rewritable(
    session: Session, band: BandSpan, rows: Mapping[int, ChapterPlan], *, from_chapter: int,
    amend_chapters: frozenset[int] = frozenset(),
) -> list[int]:
    """The band's chapters from ``from_chapter`` on that have no plan row, or one still to write."""
    return [number for number in range(max(from_chapter, band.chapter_start), band.chapter_end + 1)
            if _can_rewrite(session, rows.get(number), amend_chapters=amend_chapters)]


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


__all__ = ["AUDIT_ERROR", "BAND_PLANNING_SOURCE", "INFEASIBLE", "NO_STORY_CALENDAR", "STAGE_KEY", "BandPlanner", "BandPlanUnavailable"]
