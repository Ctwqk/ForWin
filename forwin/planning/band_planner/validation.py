"""Deterministic checks on a ``plan.band`` answer: chapters, refs, directives, spans and plot responses."""

from __future__ import annotations

from collections.abc import Sequence

from forwin.book_state.knowledge import FACT_REF_PREFIX, KNOWLEDGE_STATES
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.runtime import BookStateRuntime
from forwin.book_state.schema import WORLD_NODE_FIELDS
from forwin.book_state.story_clock import StoryCalendar, StoryClockError, duration_to_minutes
from forwin.planning.band_planner.inputs import OfferedRefs, PendingResponse
from forwin.planning.band_planner.types import BandPlanOutput, ChapterPlanDraft, PlanIssue
from forwin.planning.beat_sheet.types import READER_OBSERVER
from forwin.planning.fact_text import normalized_text


def band_plan_issues(
    output: BandPlanOutput,
    *,
    chapter_numbers: Sequence[int],
    offered: OfferedRefs,
    runtime: BookStateRuntime,
    calendar: StoryCalendar | None,
    lexicon: BookLexicon,
) -> list[PlanIssue]:
    """Every deterministic problem with the answer, as error issues; refs must be ones the prompt offered."""
    issues: list[PlanIssue] = []
    numbers = [chapter.chapter_number for chapter in output.chapters]
    if sorted(numbers) != sorted(chapter_numbers):
        issues.append(_issue("chapter_numbers", None, "", (
            f"the chapters are {sorted(numbers)}; plan exactly chapters {sorted(chapter_numbers)}, each once"
        )))
    directive_ids: set[str] = set()
    for chapter in output.chapters:
        issues.extend(_chapter_issues(chapter, offered=offered, runtime=runtime, calendar=calendar, lexicon=lexicon,
                                      directive_ids=directive_ids))
    carriers = response_carriers(output.chapters, offered.pending)
    for response in offered.pending:
        if response.conflict_id not in carriers:
            how = (f"put its goal in a chapter's goals or bind one of {sorted(response.refs)}" if response.refs
                   else "copy its goal text into a chapter's goals")
            issues.append(_issue("pending_response_unaddressed", None, response.conflict_id, (
                f"pending response {response.conflict_id} is carried by no chapter: {how}"
            )))
    return issues


def response_carriers(
    chapters: Sequence[ChapterPlanDraft], pending: Sequence[PendingResponse],
) -> dict[str, ChapterPlanDraft]:
    """The first chapter carrying each pending response: its goals state the response goal, or it binds a response ref."""
    carriers: dict[str, ChapterPlanDraft] = {}
    for response in pending:
        goal = _normalized(response.goal)
        for chapter in sorted(chapters, key=lambda item: item.chapter_number):
            if (any(goal and goal in _normalized(text) for text in chapter.goals)
                    or any(binding.ref in response.refs for binding in chapter.world_bindings)):
                carriers[response.conflict_id] = chapter
                break
    return carriers


def expected_span_minutes(chapter: ChapterPlanDraft, calendar: StoryCalendar, lexicon: BookLexicon) -> int:
    return duration_to_minutes(calendar, lexicon, amount=chapter.expected_span.amount,
                               unit_name=chapter.expected_span.unit)


def _chapter_issues(
    chapter: ChapterPlanDraft,
    *,
    offered: OfferedRefs,
    runtime: BookStateRuntime,
    calendar: StoryCalendar | None,
    lexicon: BookLexicon,
    directive_ids: set[str],
) -> list[PlanIssue]:
    number = chapter.chapter_number
    issues: list[PlanIssue] = []

    def add(code: str, ref: str, message: str) -> None:
        issues.append(_issue(code, number, ref, message))

    if not any(goal.strip() for goal in chapter.goals):
        add("empty_goals", "", "goals is empty; give at least one goal")
    if chapter.pov_ref not in offered.character_ids:
        add("pov_not_character", chapter.pov_ref, f"pov_ref {chapter.pov_ref!r} is not an offered character")
    if chapter.pov_ref not in chapter.onstage_refs:
        add("pov_not_onstage", chapter.pov_ref, f"pov_ref {chapter.pov_ref!r} is not in onstage_refs")
    for ref in chapter.onstage_refs:
        if ref not in offered.actor_ids:
            add("unknown_actor", ref, f"onstage ref {ref!r} is not one of the offered actors")
    for ref in chapter.location_refs:
        if ref not in offered.location_ids:
            add("unknown_location", ref, f"location ref {ref!r} is not one of the offered locations")
    for binding in chapter.world_bindings:
        if binding.ref not in offered.binding_refs(binding.kind):
            add("unknown_binding_ref", binding.ref, f"{binding.kind} binding ref {binding.ref!r} is not one the input "
                                                    f"offers for that kind")
    for directive in chapter.reveal_directives:
        _directive_id(directive.id, directive_ids, add)
        _fact(directive.fact_ref, offered, add)
        if directive.observer != READER_OBSERVER and directive.observer not in offered.actor_ids:
            add("unknown_observer", directive.observer,
                f"reveal observer {directive.observer!r} is neither the reader nor an offered actor")
        if directive.target_state not in KNOWLEDGE_STATES:
            add("unknown_target_state", directive.target_state,
                f"reveal target_state {directive.target_state!r} is not an allowed value")
    for directive in chapter.hint_directives:
        _directive_id(directive.id, directive_ids, add)
        truth = offered.facts.get(directive.fact_ref)
        if truth is None:
            _fact(directive.fact_ref, offered, add)
        elif _normalized(truth) and _normalized(truth) in _normalized(directive.hint_text):
            add("hint_states_truth", directive.fact_ref,
                f"hint {directive.id!r} states the proposition of {directive.fact_ref}; hint at it instead")
    for change in chapter.intended_changes:
        node = runtime.world.get_node(change.ref) if change.ref in offered.actor_ids else None
        if node is None:
            add("unknown_change_ref", change.ref, f"intended change ref {change.ref!r} is not one of the offered actors")
        elif not _is_node_field(str(node.node_type), change.field):
            add("unknown_change_field", change.ref,
                f"intended change field {change.field!r} is not a {str(node.node_type)} field")
    for milestone_id in chapter.milestone_ids:
        if milestone_id not in offered.milestone_ids:
            add("unknown_milestone", milestone_id, f"milestone {milestone_id!r} is not in the beat sheet")
    if chapter.expected_span.unit not in offered.time_units or calendar is None:
        add("unknown_time_unit", chapter.expected_span.unit,
            f"expected_span unit {chapter.expected_span.unit!r} is not one of the book's time units")
    else:
        try:
            expected_span_minutes(chapter, calendar, lexicon)
        except StoryClockError as exc:
            add("invalid_span", "", f"expected_span is invalid: {exc}")
    revealed = {ref for directive in chapter.reveal_directives
                if (fact := runtime.world.facts_by_id.get(directive.fact_ref.removeprefix(FACT_REF_PREFIX))) is not None
                for ref in fact.related_node_refs}
    writer_texts = {"title": [chapter.title], "one_line": [chapter.one_line], "goals": chapter.goals,
                    "hook": [chapter.hook]}
    for ref, name in offered.concealed_names.items():
        named = [field for field, texts in writer_texts.items() if any(name in text for text in texts)]
        if named and ref not in revealed:
            add("writer_text_names_concealed", ref, (
                f"{', '.join(named)} name concealed entity {ref} ({name}); title, one_line, goals and hook reach the "
                f"writer verbatim and may name it only in a chapter whose reveal_directives reveal a fact about it"
            ))
    rewards = offered.rewards_by_chapter.get(number, ())
    if chapter.reward_slot and chapter.reward_slot not in {item.category for item in rewards}:
        add("unscheduled_reward", chapter.reward_slot,
            f"reward_slot {chapter.reward_slot!r} is not a reward the schedule gives this chapter")
    for trope_id in chapter.trope_ids:
        if trope_id not in {item.template_id for item in rewards}:
            add("unscheduled_trope", trope_id, f"trope {trope_id!r} is not a template the schedule gives this chapter")
    return issues


def _directive_id(directive_id: str, seen: set[str], add) -> None:
    if not directive_id.strip() or directive_id in seen:
        add("duplicate_directive_id", directive_id, f"directive id {directive_id!r} is empty or repeated in the band")
    seen.add(directive_id)


def _fact(ref: str, offered: OfferedRefs, add) -> None:
    if ref not in offered.facts:
        add("unknown_fact", ref, f"fact ref {ref!r} is not one of the offered facts")


def _is_node_field(node_type: str, field: str) -> bool:
    """Whether ``field`` is a state or profile field of the type, bare or as a ``state.``/``profile.`` path."""
    fields = WORLD_NODE_FIELDS.get(node_type, {})
    section, _, rest = field.strip().partition(".")
    if section in ("state", "profile") and rest:
        return rest.split(".", 1)[0] in fields.get(section, ())
    return section in fields.get("state", set()) | fields.get("profile", set())


# The planner's one normalizer (Ruling P4a-R21): whitespace never hides a proposition or a goal.
_normalized = normalized_text


def _issue(code: str, chapter_number: int | None, ref: str, message: str) -> PlanIssue:
    return PlanIssue(code=code, severity="error", chapter_number=chapter_number, ref=ref, message=message)


__all__ = ["band_plan_issues", "expected_span_minutes", "response_carriers"]
