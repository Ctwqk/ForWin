"""The feasibility audit: whether the world, as the latest canon holds it, can carry out planned chapters.

Chapters are checked in order against the latest canon's runtime. Who is where carries over from one audited
chapter to the next: an onstage actor ends a chapter at its last location, or where an intended change puts it.
Chapters between the latest canon and the audited ones are not replayed. Band-wide checks also read the band's
and the arc's other plan rows, which the audit never changes.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from sqlalchemy import select
from sqlalchemy.orm import Session

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.knowledge import FACT_REF_PREFIX, HIDDEN_SENSITIVITY
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.protections import active_protections
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.reveal_schedule import earliest_reveal_chapters
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.story_clock import StoryCalendar, StoryClockError
from forwin.models.planning_v6 import BandPlanRow
from forwin.models.project import ArcPlanVersion, ChapterPlan, Project
from forwin.models.world_conflict import WorldPlanConflictRow
from forwin.planning.band_planner.types import BandPlanOutcome, ChapterPlanDraft, PlanIssue
from forwin.planning.band_planner.validation import expected_span_minutes
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.types import READER_OBSERVER, ArcBeatSheetData, BandSpan
from forwin.planning.fact_text import stated_facts
from forwin.runtime.policy import RuntimePolicy
from forwin.world_engine.conflict_repository import WorldConflictRepository
from forwin.world_engine.conflict_routing import PLAN_CONFLICT_SOURCES, available_options, route_conflict
from forwin.world_engine.conflict_types import ConflictDraft
from forwin.world_engine.constraints import narrative_boundary_violations
from forwin.world_engine.travel import estimate_travel


PLAN_FEASIBILITY_SOURCE = "plan_feasibility"
# A plan_feasibility conflict's ``violated``, by the code of its first error; any other error is "feasibility".
_VIOLATED_BY_CODE = {
    "reveal_too_early": "reveal_schedule",
    "milestone_uncovered": "milestone",
    "protection_conflict": "protection",
}
# Intent statuses that can still come due.
_PENDING_INTENT_STATUSES = frozenset({"planned", "due", "held"})
# The knowledge states in which an observer holds a fact as known, as the protections read them.
_KNOWN_STATES = frozenset({"knows", "believes"})
# A failed plan row is planned again, so it no longer counts as a plan.
_DISCARDED_PLAN_STATUS = "failed"
_LOCATION_FIELD = "location_id"


@dataclass(frozen=True)
class _Chapter:
    """What the audit reads of a chapter plan, a draft or a persisted row alike."""

    number: int
    pov_ref: str
    onstage_refs: tuple[str, ...]
    location_refs: tuple[str, ...]
    span_minutes: int | None
    # (kind, ref) of each world binding.
    bindings: frozenset[tuple[str, str]]
    # (fact ref, observer, target state) of each reveal directive.
    reveals: tuple[tuple[str, str, str], ...]
    # (ref, field, value) of each intended change, in order.
    changes: tuple[tuple[str, str, Any], ...]
    milestone_ids: frozenset[str]
    # (field, text) of every text the writer reads verbatim: title, one_line, goals, hook and hint texts.
    writer_texts: tuple[tuple[str, str], ...]

    @classmethod
    def of(cls, plan: ChapterPlanDraft | ChapterPlan, calendar: StoryCalendar | None, lexicon: BookLexicon) -> _Chapter:
        if isinstance(plan, ChapterPlanDraft):
            return cls.of_draft(plan, calendar, lexicon)
        return cls.of_row(plan)

    @classmethod
    def of_draft(cls, draft: ChapterPlanDraft, calendar: StoryCalendar | None, lexicon: BookLexicon) -> _Chapter:
        span: int | None = None
        if calendar is not None:
            try:
                span = expected_span_minutes(draft, calendar, lexicon)
            except StoryClockError:
                span = None
        return cls(
            number=draft.chapter_number, pov_ref=draft.pov_ref, onstage_refs=tuple(draft.onstage_refs),
            location_refs=tuple(draft.location_refs), span_minutes=span,
            bindings=frozenset((item.kind, item.ref) for item in draft.world_bindings),
            reveals=tuple((item.fact_ref, item.observer, item.target_state) for item in draft.reveal_directives),
            changes=tuple((item.ref, item.field, item.value) for item in draft.intended_changes),
            milestone_ids=frozenset(draft.milestone_ids),
            writer_texts=_writer_texts(draft.title, draft.one_line, draft.goals, draft.hook,
                                       [item.hint_text for item in draft.hint_directives]),
        )

    @classmethod
    def of_row(cls, row: ChapterPlan) -> _Chapter:
        return cls(
            number=int(row.chapter_number), pov_ref=str(row.pov_ref or ""),
            onstage_refs=tuple(_strings(row.onstage_refs_json)), location_refs=tuple(_strings(row.location_refs_json)),
            span_minutes=row.expected_span_minutes,
            bindings=frozenset((str(item.get("kind") or ""), str(item.get("ref") or ""))
                               for item in _records(row.world_bindings_json)),
            reveals=tuple((str(item.get("fact_ref") or ""), str(item.get("observer") or ""),
                           str(item.get("target_state") or "")) for item in _records(row.reveal_directives_json)),
            changes=tuple((str(item.get("ref") or ""), str(item.get("field") or ""), item.get("value"))
                          for item in _records(row.intended_changes_json)),
            milestone_ids=frozenset(_strings(row.milestone_ids_json)),
            writer_texts=_writer_texts(
                str(row.title or ""), str(row.one_line or ""), _strings(row.goals_json), str(row.hook or ""),
                [str(item.get("hint_text") or "") for item in _records(row.hint_directives_json)],
            ),
        )


class FeasibilityAuditor:
    """Deterministic and read-only: the planner's feasibility audit, the pre-write check and the plan step's."""

    def audit_plans(
        self,
        session: Session,
        *,
        project_id: str,
        plans: Sequence[ChapterPlanDraft | ChapterPlan],
        beat_sheet: ArcBeatSheetData | None,
        policy: RuntimePolicy,
        runtime: BookStateRuntime | None = None,
    ) -> list[PlanIssue]:
        """Every problem with ``plans``: errors make them infeasible, warnings only inform.

        ``runtime`` is the latest canon's; it is loaded when not given. ``beat_sheet`` is the arc's active sheet;
        without one the milestone and exit-state checks are skipped.
        """
        if not plans:
            return []
        if runtime is None:
            latest = BookStateRepository(session).latest_canon_chapter(project_id) or 0
            runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=latest)
        lexicon = BookLexiconRepository(session).get(project_id)
        calendar = StoryCalendarRepository(session).get(project_id)
        chapters = sorted((_Chapter.of(plan, calendar, lexicon) for plan in plans), key=lambda item: item.number)
        band = _band_range(beat_sheet, chapters)
        low, high = _arc_range(beat_sheet) if beat_sheet is not None else band
        kept = _kept_chapters(session, project_id, low=min(low, band[0]), high=max(high, band[1]),
                              audited={chapter.number for chapter in chapters})
        walk = _ChapterWalk(runtime=runtime, lexicon=lexicon, schedule=earliest_reveal_chapters(session, project_id),
                            tolerance=policy.review.travel_tolerance, reader_known=reader_known_facts(runtime))
        issues = [issue for chapter in chapters for issue in walk.issues(chapter)]
        if beat_sheet is not None:
            issues.extend(_milestone_issues(beat_sheet, chapters, kept))
            issues.extend(_exit_state_issues(beat_sheet, chapters, kept))
        now = BookStateQuery(session).current_story_minute(project_id, as_of_chapter=runtime.as_of_chapter) or 0
        in_band = [chapter for chapter in [*chapters, *kept] if band[0] <= chapter.number <= band[1]]
        issues.extend(_band_world_issues(runtime, in_band, as_of=runtime.as_of_chapter, now=now))
        return issues


@dataclass
class _ChapterWalk:
    """The per-chapter checks, in chapter order; positions and restraint releases carry to later chapters."""

    runtime: BookStateRuntime
    lexicon: BookLexicon
    # fact node id -> earliest reader reveal chapter
    schedule: Mapping[str, int]
    tolerance: float
    # fact node ids the reader's recorded cognition at the latest canon knows or believes
    reader_known: frozenset[str] = frozenset()
    positions: dict[str, str] = field(default_factory=dict)
    released: set[str] = field(default_factory=set)

    def issues(self, chapter: _Chapter) -> list[PlanIssue]:
        issues: list[PlanIssue] = []

        def add(code: str, ref: str, message: str) -> None:
            issues.append(PlanIssue(code=code, severity="error", chapter_number=chapter.number, ref=ref,
                                    message=message))

        self.released.update(ref for ref, name, value in chapter.changes if _frees(name, value))
        for ref in chapter.onstage_refs:
            problem = self._unavailable(ref)
            if problem:
                add("onstage_unavailable", ref, f"onstage ref {ref!r} {problem}")
        for ref in chapter.location_refs:
            if ref not in self.runtime.map.nodes_by_id:
                add("location_unknown", ref, f"location ref {ref!r} is not a map node")
        unreachable = self._unreachable(chapter)
        if unreachable:
            add("location_unreachable", chapter.location_refs[0], unreachable)
        too_early, broken = self._reveal_and_protection_issues(chapter)
        for ref, message in too_early.items():
            add("reveal_too_early", ref, message)
        for ref, message in broken:
            add("protection_conflict", ref, message)
        for ref, fields in self._stated_secrets(chapter).items():
            add("writer_text_states_secret", ref, (
                f"{', '.join(fields)} state the proposition of {ref}, which the reader may not learn in chapter "
                f"{chapter.number}; title, one_line, goals, hook and hint texts reach the writer verbatim, so hint "
                f"at it or reveal it to the reader first"
            ))
        self._advance(chapter)
        return issues

    def _unavailable(self, ref: str) -> str:
        world = self.runtime.world
        node = world.get_node(ref)
        if node is None or not node.is_active:
            return "is not in the world"
        state = world.get_state(ref)
        if state.get("alive_status") == "dead":
            return "is dead"
        restraint = _restraint(state)
        if restraint != "free" and ref not in self.released:
            return f"is {restraint} and no intended change of this chapter or an earlier one frees it"
        return ""

    def _position(self, ref: str) -> str:
        return self.positions.get(ref) or resolve_world_node_location_id(self.runtime.world, ref)

    def _unreachable(self, chapter: _Chapter) -> str:
        """Why the POV cannot reach the chapter's first location within its span; empty when it can."""
        nodes = self.runtime.map.nodes_by_id
        destination = chapter.location_refs[0] if chapter.location_refs else ""
        origin = self._position(chapter.pov_ref)
        if (not destination or destination not in nodes or origin not in nodes or origin == destination
                or chapter.span_minutes is None):
            return ""
        travel = estimate_travel(self.runtime, self.lexicon, mover_id=chapter.pov_ref, from_location_id=origin,
                                 to_location_id=destination)
        limit = chapter.span_minutes * (1 + self.tolerance)
        route = f"POV {chapter.pov_ref} from {origin} to {destination}"
        if not travel.reachable:
            return f"no map path takes {route}: {travel.blocked_reason}"
        if not travel.route_known or travel.minutes is None:
            return f"{route}: the POV knows no route there"
        if travel.minutes > limit:
            return (f"{route} takes {travel.minutes:g} minutes; the chapter spans {chapter.span_minutes} minutes, "
                    f"{limit:g} with the travel tolerance")
        return ""

    def _reveal_and_protection_issues(self, chapter: _Chapter) -> tuple[dict[str, str], list[tuple[str, str]]]:
        """Reader reveals before their schedule or against a reader protection, by fact ref, and the other
        protections the chapter's intended changes, implied moves and reveals break, as (ref, message)."""
        too_early: dict[str, str] = {}
        knowledge: set[tuple[str, str]] = set()
        for fact_ref, observer, state in chapter.reveals:
            fact = _fact_ref(fact_ref)
            earliest = self.schedule.get(fact.removeprefix(FACT_REF_PREFIX))
            if observer == READER_OBSERVER and earliest is not None and chapter.number < earliest:
                too_early.setdefault(fact, (
                    f"the reader learns {fact} in chapter {chapter.number}; its earliest reveal chapter is {earliest}"
                ))
            if state in _KNOWN_STATES:
                knowledge.add((observer, fact))
        broken: list[tuple[str, str]] = []
        violations = narrative_boundary_violations(
            self.runtime, changes=[*self._implied_moves(chapter), *chapter.changes], knowledge=knowledge,
            next_chapter=chapter.number,
        )
        for violation in violations:
            payload = self.runtime.narrative.nodes_by_id[violation.constraint_node_id].payload
            until = payload.get("until_chapter") or "open"
            if violation.kind == "must_not_know" and payload.get("observer_ref") == READER_OBSERVER:
                fact = _fact_ref(str(payload.get("fact_ref") or ""))
                too_early.setdefault(fact, (
                    f"protection {violation.constraint_node_id} keeps {fact} from the reader until chapter {until}"
                ))
                continue
            broken.append((violation.ref, (
                f"the chapter's intended changes, onstage moves or reveals break protection "
                f"{violation.constraint_node_id} ({violation.kind} on {violation.ref}, until chapter {until})"
            )))
        return too_early, broken

    def _implied_moves(self, chapter: _Chapter) -> list[tuple[str, str, Any]]:
        """An onstage actor not at any of the chapter's locations moves to its first one."""
        if not chapter.location_refs:
            return []
        return [(ref, f"state.{_LOCATION_FIELD}", chapter.location_refs[0]) for ref in chapter.onstage_refs
                if self.runtime.world.get_node(ref) is not None and self._position(ref) not in chapter.location_refs]

    def _stated_secrets(self, chapter: _Chapter) -> dict[str, list[str]]:
        """The writer texts stating each fact the chapter may not reveal, by fact ref (Ruling P4a-R21).

        A secret or hidden fact, or one a protection keeps from the reader, stays unrevealable unless the
        reader already knows it, the chapter reveals it to the reader, or its earliest reveal chapter has come.
        """
        protected = {
            protection.fact_ref.removeprefix(FACT_REF_PREFIX)
            for protection in active_protections(self.runtime, chapter=chapter.number)
            if protection.kind == "must_not_know" and protection.observer_ref == READER_OBSERVER
        }
        revealed = {_fact_ref(fact_ref).removeprefix(FACT_REF_PREFIX)
                    for fact_ref, observer, _state in chapter.reveals if observer == READER_OBSERVER}
        unrevealable = {
            f"{FACT_REF_PREFIX}{fact.id}": fact.proposition
            for fact in self.runtime.world.facts_by_id.values()
            if (fact.sensitivity_level in HIDDEN_SENSITIVITY or fact.id in protected) and fact.id not in revealed
            and fact.id not in self.reader_known and self.schedule.get(fact.id, math.inf) > chapter.number
        }
        fields: dict[str, list[str]] = {}
        for name, text in chapter.writer_texts:
            for ref in stated_facts([text], unrevealable):
                if name not in fields.setdefault(ref, []):
                    fields[ref].append(name)
        return dict(sorted(fields.items()))

    def _advance(self, chapter: _Chapter) -> None:
        if chapter.location_refs:
            for ref in chapter.onstage_refs:
                self.positions[ref] = chapter.location_refs[-1]
        for ref, name, value in chapter.changes:
            if name.strip().removeprefix("state.") == _LOCATION_FIELD and isinstance(value, str) and value:
                self.positions[ref] = value


def _milestone_issues(
    beat_sheet: ArcBeatSheetData, chapters: Sequence[_Chapter], kept: Sequence[_Chapter],
) -> list[PlanIssue]:
    """A hard milestone due by the last planned chapter that no plan carries; it is placed at the first audited
    chapter inside its range, the last chance to carry it."""
    planned = [*chapters, *kept]
    last = max(chapter.number for chapter in planned)
    carried = {milestone_id for chapter in planned for milestone_id in chapter.milestone_ids}
    audited = sorted(chapter.number for chapter in chapters)
    issues = []
    for milestone in sorted(beat_sheet.milestones, key=lambda item: (item.target_range, item.id)):
        start, end = milestone.target_range
        if not milestone.hard or end > last or milestone.id in carried:
            continue
        issues.append(PlanIssue(
            code="milestone_uncovered", severity="error",
            chapter_number=next((number for number in audited if number >= start), start), ref=milestone.id,
            message=(f"hard milestone {milestone.id} is due by chapter {end}, but no accepted or planned chapter "
                     f"carries it; list it in milestone_ids of a chapter from {start} on"),
        ))
    return issues


def _exit_state_issues(
    beat_sheet: ArcBeatSheetData, chapters: Sequence[_Chapter], kept: Sequence[_Chapter],
) -> list[PlanIssue]:
    """Exit-state refs no chapter of the arc's last band changes, once the audit reaches that band."""
    if not beat_sheet.band_breakdown:
        return []
    last = max(beat_sheet.band_breakdown, key=lambda band: band.chapter_start)
    if not any(last.chapter_start <= chapter.number <= last.chapter_end for chapter in chapters):
        return []
    touched = {ref for chapter in [*chapters, *kept] if last.chapter_start <= chapter.number <= last.chapter_end
               for ref, _name, _value in chapter.changes}
    issues = []
    for item in beat_sheet.exit_state:
        if item.ref in touched or any(issue.ref == item.ref for issue in issues):
            continue
        issues.append(PlanIssue(
            code="exit_state_unaddressed", severity="warning", chapter_number=last.chapter_end, ref=item.ref,
            message=(f"no chapter of the arc's last band ({last.chapter_start}-{last.chapter_end}) has an intended "
                     f"change of {item.ref}, which the exit state needs ({item.field})"),
        ))
    return issues


def _band_world_issues(
    runtime: BookStateRuntime, band: Sequence[_Chapter], *, as_of: int, now: int,
) -> list[PlanIssue]:
    """Public clocks running out and onstage intents coming due within the band's story window, unbound.

    The window runs from the latest canon's story minute through the expected spans of the band's chapters still
    to write; a clock or intent counts as bound when any chapter of the band binds it.
    """
    ahead = sorted((chapter for chapter in band if chapter.number > as_of), key=lambda chapter: chapter.number)
    if not ahead:
        return []
    ends: list[tuple[int, int]] = []
    cursor = now
    for chapter in ahead:
        cursor += chapter.span_minutes or 0
        ends.append((chapter.number, cursor))

    def chapter_at(minute: int) -> int:
        return next((number for number, end in ends if end >= minute), ends[-1][0])

    bound = {binding for chapter in band for binding in chapter.bindings}
    bound_refs = {ref for _kind, ref in bound}
    world = runtime.world
    issues = []
    for node in sorted(world.nodes_by_id.values(), key=lambda item: item.id):
        if not node.is_active:
            continue
        state = world.get_state(node.id)
        if str(node.node_type) == "clock":
            due = state.get("due_at")
            if (state.get("status") == "running" and node.profile.get("scope") == "public" and type(due) is int
                    and due <= cursor and node.id not in bound_refs):
                issues.append(PlanIssue(
                    code="clock_unanswered", severity="warning", chapter_number=chapter_at(due), ref=node.id,
                    message=f"public clock {node.id} runs out at story minute {due}, inside the band, and no "
                            f"chapter binds it",
                ))
        elif str(node.node_type) == "intent" and node.profile.get("requires_onstage") is True:
            status, at = state.get("status"), state.get("scheduled_at")
            due_now = status == "due"
            if status not in _PENDING_INTENT_STATUSES or not (due_now or (type(at) is int and at <= cursor)):
                continue
            if ("intent", node.id) in bound:
                continue
            issues.append(PlanIssue(
                code="due_onstage_unbound", severity="warning",
                chapter_number=chapter_at(now if due_now or type(at) is not int else at), ref=node.id,
                message=f"intent {node.id} comes due inside the band and needs its owner onstage, but no chapter "
                        f"of the band binds it",
            ))
    return issues


# The plan step's response to an infeasible band.


class _Replanner(Protocol):
    def replan_remaining(
        self, session: Session, *, project_id: str, band_plan_id: str, from_chapter: int, policy: RuntimePolicy,
        notes: Sequence[str], expected_band_plan_id: str | None = None,
    ) -> BandPlanOutcome: ...


@dataclass(frozen=True)
class InfeasibleBandResponse:
    # The band's plan afterwards: the new version when the band was planned again.
    band_plan_id: str
    replanned: bool = False
    conflict_id: str | None = None
    deferred_reason: str = ""


def respond_to_infeasible_band(
    session: Session,
    *,
    band_planner: _Replanner,
    project_id: str,
    band_plan_id: str,
    from_chapter: int,
    issues: Sequence[PlanIssue],
    policy: RuntimePolicy,
) -> InfeasibleBandResponse:
    """Plan the band's chapters from ``from_chapter`` on again while the band is under
    ``planning.max_replans_per_band``; at the cap, or when planning again fails, open a plan_feasibility conflict.

    ``issues`` are the audit's on the band's remaining chapters; without an error nothing happens. Planning again
    calls the model: hold no lock across this call.
    """
    errors = _errors(issues)
    if not errors:
        return InfeasibleBandResponse(band_plan_id=band_plan_id)
    row = session.get(BandPlanRow, band_plan_id)
    if row is None or row.project_id != project_id:
        raise LookupError(f"band plan {band_plan_id} is not a band plan of project {project_id}")
    if row.replan_count < policy.planning.max_replans_per_band:
        outcome = band_planner.replan_remaining(
            session, project_id=project_id, band_plan_id=row.id, from_chapter=from_chapter, policy=policy,
            notes=[_feedback(issue) for issue in errors],
            expected_band_plan_id=row.id,
        )
        if outcome.failure_reason in {"superseded_by_concurrent_plan", "superseded_beat_sheet"}:
            return InfeasibleBandResponse(band_plan_id=outcome.band_plan_id or row.id,
                                          deferred_reason=outcome.failure_reason)
        if outcome.band_plan_id is not None and outcome.band_plan_id != row.id:
            return InfeasibleBandResponse(band_plan_id=outcome.band_plan_id, replanned=True)
        issues = outcome.issues or issues
    # Audit failures and cap decisions also belong to the audited version. Serialize the final
    # check with publication before opening a conflict, without taking a lock across the model.
    session.execute(select(Project.id).where(Project.id == project_id).with_for_update(key_share=True))
    active = session.scalar(select(BandPlanRow.id).where(
        BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == row.arc_plan_id,
        BandPlanRow.band_index == row.band_index, BandPlanRow.status == "active",
    ).order_by(BandPlanRow.version.desc()).limit(1))
    if active != row.id:
        return InfeasibleBandResponse(band_plan_id=active or row.id,
                                      deferred_reason="superseded_by_concurrent_plan")
    conflict = open_plan_feasibility_conflict(
        session, project_id=project_id, band=BandSpan(band_index=row.band_index, chapter_start=row.chapter_start,
                                                      chapter_end=row.chapter_end),
        issues=issues, policy=policy, band_plan_id=row.id,
    )
    return InfeasibleBandResponse(band_plan_id=row.id, conflict_id=conflict.id)


def open_plan_feasibility_conflict(
    session: Session,
    *,
    project_id: str,
    band: BandSpan,
    issues: Sequence[PlanIssue],
    policy: RuntimePolicy,
    band_plan_id: str | None = None,
) -> WorldPlanConflictRow:
    """A plan_feasibility conflict over the band, offered and routed as conflict routing says: human, amend_plan.

    Its first error by chapter gives ``violated`` and the chapter it blocks from; it lists every issue, warnings
    included, since the amender reads them. An open plan conflict over the band that already blocks from that
    chapter or earlier is returned instead. The project row lock (FOR NO KEY UPDATE, taken before the check as the
    band planner takes it) keeps two writers from both opening one; the caller commits soon after.
    """
    errors = _errors(issues)
    if not errors:
        raise ValueError("a plan_feasibility conflict needs at least one error")
    first = errors[0]
    blocks_from = first.chapter_number if first.chapter_number is not None else band.chapter_start
    session.execute(select(Project.id).where(Project.id == project_id).with_for_update(key_share=True))
    existing = _open_plan_conflict(session, project_id, band, blocks_from_chapter=blocks_from)
    if existing is not None:
        return existing
    as_of = BookStateRepository(session).latest_canon_chapter(project_id) or 0
    draft = ConflictDraft(
        project_id=project_id, source=PLAN_FEASIBILITY_SOURCE, detected_after_chapter=as_of,
        story_time=BookStateQuery(session).current_story_minute(project_id, as_of_chapter=as_of) or 0,
        involved_refs=list(dict.fromkeys(issue.ref for issue in errors if issue.ref)), violated=violated_by(errors),
        impact_level="major",
        summary=f"Chapters {band.chapter_start}-{band.chapter_end} are not feasible: {first.message}",
        proposed_change={
            "kind": "plan_issues", "band": [band.chapter_start, band.chapter_end], "band_plan_id": band_plan_id,
            "issues": [issue.model_dump(mode="json") for issue in issues],
        },
        blocks_from_chapter=blocks_from,
    )
    return WorldConflictRepository(session).create(
        draft, required_decider=route_conflict(draft, policy=policy), options=available_options(draft),
    )


def band_for_chapter(session: Session, project_id: str, chapter_number: int) -> tuple[BandSpan, str | None]:
    """The active beat-sheet band holding the chapter and its active band plan's id, if it has one.

    Without a sheet band the chapter stands alone, as a one-chapter band.
    """
    repo = ArcBeatSheetRepository(session)
    for row in repo.active_for_project(project_id):
        arc = session.get(ArcPlanVersion, row.arc_plan_id)
        if arc is None or not arc.chapter_start <= chapter_number <= arc.chapter_end:
            continue
        band = next((item for item in repo.load(row).band_breakdown
                     if item.chapter_start <= chapter_number <= item.chapter_end), None)
        if band is None:
            break
        band_plan_id = session.scalar(
            select(BandPlanRow.id)
            .where(BandPlanRow.project_id == project_id, BandPlanRow.arc_plan_id == arc.id,
                   BandPlanRow.band_index == band.band_index, BandPlanRow.chapter_start == band.chapter_start,
                   BandPlanRow.chapter_end == band.chapter_end, BandPlanRow.status == "active")
            .order_by(BandPlanRow.version.desc()).limit(1)
        )
        return band, band_plan_id
    return BandSpan(band_index=0, chapter_start=chapter_number, chapter_end=chapter_number), None


def reader_known_facts(runtime: BookStateRuntime) -> frozenset[str]:
    """The fact node ids the reader's recorded cognition knows or believes; an unrecorded public fact is not
    known by default."""
    reader = runtime.cognition_by_observer.get((READER_OBSERVER, READER_OBSERVER))
    states = reader.knowledge_by_ref if reader is not None else {}
    return frozenset(ref.removeprefix(FACT_REF_PREFIX) for ref, state in states.items() if state in _KNOWN_STATES)


def violated_by(issues: Sequence[PlanIssue]) -> str:
    """What a conflict over ``issues`` broke: the first error's, by chapter."""
    errors = _errors(issues)
    return _VIOLATED_BY_CODE.get(errors[0].code, "feasibility") if errors else "feasibility"


def _errors(issues: Iterable[PlanIssue]) -> list[PlanIssue]:
    """The errors in chapter order; an error without a chapter goes last."""
    return sorted((issue for issue in issues if issue.severity == "error"),
                  key=lambda issue: (issue.chapter_number is None, issue.chapter_number or 0))


def _open_plan_conflict(
    session: Session, project_id: str, band: BandSpan, *, blocks_from_chapter: int,
) -> WorldPlanConflictRow | None:
    """An open plan conflict over the band that blocks from ``blocks_from_chapter`` or earlier."""
    return session.scalar(
        select(WorldPlanConflictRow)
        .where(WorldPlanConflictRow.project_id == project_id, WorldPlanConflictRow.status == "open",
               WorldPlanConflictRow.source.in_(sorted(PLAN_CONFLICT_SOURCES)),
               WorldPlanConflictRow.blocks_from_chapter.between(band.chapter_start, blocks_from_chapter))
        .order_by(WorldPlanConflictRow.blocks_from_chapter, WorldPlanConflictRow.created_at,
                  WorldPlanConflictRow.id)
        .limit(1)
    )


def _feedback(issue: PlanIssue) -> str:
    where = f"chapter {issue.chapter_number}: " if issue.chapter_number is not None else ""
    return f"{where}[{issue.code}] {issue.message}"


def _band_range(beat_sheet: ArcBeatSheetData | None, chapters: Sequence[_Chapter]) -> tuple[int, int]:
    """The beat-sheet band holding the first audited chapter, else the audited chapters' own range."""
    first, last = chapters[0].number, chapters[-1].number
    bands = beat_sheet.band_breakdown if beat_sheet is not None else []
    band = next((item for item in bands if item.chapter_start <= first <= item.chapter_end), None)
    return (band.chapter_start, band.chapter_end) if band is not None else (first, last)


def _arc_range(beat_sheet: ArcBeatSheetData) -> tuple[int, int]:
    spans = [(band.chapter_start, band.chapter_end) for band in beat_sheet.band_breakdown]
    spans.extend(phase.chapter_range for phase in beat_sheet.phases)
    return min(start for start, _end in spans), max(end for _start, end in spans)


def _kept_chapters(session: Session, project_id: str, *, low: int, high: int, audited: set[int]) -> list[_Chapter]:
    """The plan rows from ``low`` to ``high`` the audit does not replace; a failed row is no plan."""
    rows = session.scalars(select(ChapterPlan).where(
        ChapterPlan.project_id == project_id, ChapterPlan.chapter_number.between(low, high),
        ChapterPlan.status != _DISCARDED_PLAN_STATUS,
    ).order_by(ChapterPlan.chapter_number))
    return [_Chapter.of_row(row) for row in rows if row.chapter_number not in audited]


def _writer_texts(title: str, one_line: str, goals: Sequence[str], hook: str,
                  hints: Sequence[str]) -> tuple[tuple[str, str], ...]:
    return (("title", title), ("one_line", one_line), *(("goals", goal) for goal in goals), ("hook", hook),
            *(("hint_directives", hint) for hint in hints))


def _frees(name: str, value: Any) -> bool:
    """Whether an intended change sets the restraint free."""
    path = name.strip().removeprefix("state.")
    if path == "restraint":
        return value == "free" or (isinstance(value, Mapping) and value.get("state") == "free")
    return path == "restraint.state" and value == "free"


def _restraint(state: Mapping[str, Any]) -> str:
    restraint = state.get("restraint")
    return str((restraint.get("state") if isinstance(restraint, Mapping) else None) or "free")


def _fact_ref(ref: str) -> str:
    return f"{FACT_REF_PREFIX}{ref.removeprefix(FACT_REF_PREFIX)}"


def _json_list(raw: str | None) -> list[Any]:
    try:
        value = json.loads(raw or "[]")
    except ValueError:
        return []
    return value if isinstance(value, list) else []


def _strings(raw: str | None) -> list[str]:
    return [item for item in _json_list(raw) if isinstance(item, str)]


def _records(raw: str | None) -> list[dict[str, Any]]:
    return [item for item in _json_list(raw) if isinstance(item, dict)]


__all__ = [
    "PLAN_FEASIBILITY_SOURCE",
    "FeasibilityAuditor",
    "InfeasibleBandResponse",
    "PlanIssue",
    "band_for_chapter",
    "open_plan_feasibility_conflict",
    "reader_known_facts",
    "respond_to_infeasible_band",
    "violated_by",
]
