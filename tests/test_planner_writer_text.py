from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.types import BeatSheetExperienceInput
from forwin.planning.band_planner.inputs import OfferedRefs, _concealed_names
from forwin.planning.band_planner.types import BandPlanOutput
from forwin.planning.band_planner.validation import band_plan_issues
from forwin.planning.beat_sheet.service import beat_sheet_issues
from forwin.planning.beat_sheet.types import ArcBeatSheetData
from forwin.protocol.book_state import CognitionOverlay, FactNode, WorldNode
from forwin.writer.prompt_core.sections import _experience_overlay_section
from tests.test_arc_beat_sheet import _OFFERED, _READER_PROTECTED, _RUNTIME, _VALID


@pytest.mark.parametrize("field", ["purpose", "description"])
def test_later_phase_and_milestone_propositions_are_checked_from_arc_start(field):
    payload = deepcopy(_VALID)
    payload.update(_READER_PROTECTED)
    payload["phases"] = [
        {"name": "opening", "chapter_range": [1, 5], "purpose": "follow the clue"},
        {"name": "turn", "chapter_range": [6, 12], "purpose": "test the clue"},
    ]
    payload["band_breakdown"] = [
        {"band_index": 0, "chapter_start": 1, "chapter_end": 6},
        {"band_index": 1, "chapter_start": 7, "chapter_end": 12},
    ]
    payload["milestones"][0].update(target_range=[6, 8], description="question the clue")
    payload["reveal_targets"][0]["range"] = [6, 8]
    target = payload["phases"][1] if field == "purpose" else payload["milestones"][0]
    target[field] = "learn the secret-value"
    data = ArcBeatSheetData.model_validate(payload)
    issues = beat_sheet_issues(data, chapter_start=1, chapter_end=12, runtime=_RUNTIME, actor_ids=_OFFERED)
    path = "phases[1].purpose" if field == "purpose" else "milestones[0].description"
    assert any(issue.startswith(f"{path} states the proposition of fact:secret") for issue in issues)

    target[field] = "investigate the clue"
    data = ArcBeatSheetData.model_validate(payload)
    assert beat_sheet_issues(data, chapter_start=1, chapter_end=12, runtime=_RUNTIME, actor_ids=_OFFERED) == []
    band = data.band_breakdown[0]
    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:6", chapter_start=1, chapter_end=6,
        experience=BeatSheetExperienceInput.from_beat_sheet(data, band), active_band=[],
    )
    for chapter in range(1, 6):
        rendered = _experience_overlay_section(SimpleNamespace(
            chapter_number=chapter, band_delight_schedule=schedule,
        ))
        assert rendered
        assert "investigate the clue" in rendered
        assert "secret-value" not in rendered


def _concealed_runtime(*, visibility_source="state", reader_knows=False):
    return BookStateRuntime(
        project_id="p", as_of_chapter=0, map_graph=MapGraph(),
        world=ObjectiveWorldGraph(nodes=[
            *_RUNTIME.world.nodes_by_id.values(),
            WorldNode(id="society", project_id="p", node_type="organization", name="HiddenSociety",
                      **{visibility_source: {"visibility_level": "secret"}}),
        ], facts=[FactNode(id="secret", project_id="p", proposition="secret-value",
                           related_node_refs=["society"])]),
        cognition_by_observer={
            ("reader", "reader"): CognitionView(CognitionOverlay(
                id="reader", project_id="p", observer_type="reader", observer_id="reader",
                knowledge_by_ref={"fact:secret": "knows" if reader_knows else "suspects"},
            )),
        },
    )


def _named_sheet(field):
    payload = deepcopy(_VALID)
    text = "investigate HiddenSociety"
    if field == "purpose":
        payload["phases"][0][field] = text
        path = "phases[0].purpose"
    elif field == "description":
        payload["milestones"][0][field] = text
        path = "milestones[0].description"
    elif field == "ambiguity_constraints":
        payload["arc_payoff_map"] = {field: [text]}
        path = "arc_payoff_map.ambiguity_constraints[0]"
    elif field == "revelation_summary":
        payload["arc_payoff_map"] = {"revelation_layers": [{"summary": text, "chapter_window": "1"}]}
        path = "arc_payoff_map.revelation_layers[0].summary"
    else:
        payload["reader_promise"] = {field: [text] if field == "core_pleasures" else text}
        path = f"reader_promise.{field}" + ("[0]" if field == "core_pleasures" else "")
    return payload, path


def _sheet_issues(payload, *, runtime=None, reveal_chapters=None):
    return beat_sheet_issues(
        ArcBeatSheetData.model_validate(payload), chapter_start=1, chapter_end=6,
        runtime=runtime or _concealed_runtime(), actor_ids=_OFFERED | {"society"},
        reveal_chapters=reveal_chapters,
    )


@pytest.mark.parametrize("field", [
    "purpose", "description", "genre_promise", "pleasure_promise", "core_pleasures",
    "acceptable_drag_level", "acceptable_exposition_density", "cliffhanger_aggressiveness",
    "world_legibility_target", "ambiguity_constraints", "revelation_summary",
])
@pytest.mark.parametrize("visibility_source", ["state", "profile"])
def test_every_beat_sheet_writer_text_rejects_concealed_names(field, visibility_source):
    payload, path = _named_sheet(field)
    runtime = _concealed_runtime(visibility_source=visibility_source)
    issues = _sheet_issues(payload, runtime=runtime)
    assert len(issues) == 1
    assert issues[0].startswith(f"{path} names concealed entity society")
    # A sheet's future target overrides the older schedule for the same fact.
    assert _sheet_issues(payload, runtime=runtime, reveal_chapters={"secret": 1})
    payload["reveal_targets"][0]["range"] = [1, 6]
    assert _sheet_issues(payload, runtime=runtime) == []
    payload["reveal_targets"] = []
    assert _sheet_issues(payload, runtime=runtime)
    assert _sheet_issues(payload, runtime=runtime, reveal_chapters={"secret": 1}) == []


@pytest.mark.parametrize("field", ["purpose", "description", "revelation_summary"])
@pytest.mark.parametrize(("reveal_start", "allowed"), [(3, True), (4, True), (5, False)])
def test_concealed_name_exception_uses_the_text_window_start(field, reveal_start, allowed):
    payload, _ = _named_sheet(field)
    if field == "purpose":
        payload["phases"][0]["chapter_range"] = [4, 6]
        payload["phases"].insert(0, {"name": "opening", "chapter_range": [1, 3], "purpose": "a clue"})
    elif field == "description":
        payload["milestones"][0]["target_range"] = [4, 6]
    else:
        payload["arc_payoff_map"]["revelation_layers"][0]["chapter_window"] = "4-6"
    payload["reveal_targets"][0]["range"] = [reveal_start, 6]
    assert bool(_sheet_issues(payload)) is not allowed


@pytest.mark.parametrize("field", ["genre_promise", "revelation_summary"])
def test_concealed_names_need_a_related_reader_reveal_or_recorded_knowledge(field):
    payload, _ = _named_sheet(field)
    payload["reveal_targets"][0].update(observer="hero", range=[1, 6])
    assert _sheet_issues(payload)
    assert _sheet_issues(payload, runtime=_concealed_runtime(reader_knows=True)) == []
    runtime = _concealed_runtime()
    runtime.world.facts_by_id["secret"] = FactNode(
        id="secret", project_id="p", proposition="secret-value", related_node_refs=["hero"],
    )
    payload["reveal_targets"][0]["observer"] = "reader"
    assert _sheet_issues(payload, runtime=runtime)


def test_every_revelation_layer_summary_rejects_an_unrevealed_concealed_name():
    payload, _ = _named_sheet("revelation_summary")
    layers = payload["arc_payoff_map"]["revelation_layers"]
    layers[:0] = [{"summary": "follow the clue", "chapter_window": "1"}] * 2
    payload["reveal_targets"] = []
    issues = _sheet_issues(payload)
    assert len(issues) == 1
    assert issues[0].startswith("arc_payoff_map.revelation_layers[2].summary names concealed entity society")


@pytest.mark.parametrize(("window", "start", "reveal_start"), [
    ("1", 1, 1), ("4-6", 4, 3), ([4, 6], 4, 4), (" 4 – 6 ", 4, 4), ("4—6", 4, 4),
])
def test_revelation_summary_with_on_time_related_reveal_validates_but_never_reaches_writer(window, start, reveal_start):
    payload, _ = _named_sheet("revelation_summary")
    payload["arc_payoff_map"]["revelation_layers"][0]["chapter_window"] = window
    payload["reveal_targets"][0]["range"] = [reveal_start, 6]
    assert _sheet_issues(payload) == []

    data = ArcBeatSheetData.model_validate(payload)
    experience = BeatSheetExperienceInput.from_beat_sheet(data, data.band_breakdown[0])
    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:6", chapter_start=1, chapter_end=6, experience=experience, active_band=[],
    )
    for chapter in {1, start}:
        plan = ChapterExperiencePlanner().derive_chapter_experience_plan(
            chapter_number=chapter, experience=experience, schedule=schedule,
            chapter_plan=SimpleNamespace(title="title", one_line="follow the clue", goals_json="[]"),
        )
        rendered = _experience_overlay_section(SimpleNamespace(
            chapter_number=chapter, band_delight_schedule=schedule, chapter_experience_plan=plan,
        ))
        # Ruling FU-R48: revelation summaries are author-only; reveals reach the writer via directives.
        assert "HiddenSociety" not in rendered


@pytest.mark.parametrize("field", ["title", "one_line", "goals", "hook", "hint_text"])
@pytest.mark.parametrize("reveal", [False, True])
def test_band_writer_text_checks_concealed_names_including_every_hint(field, reveal):
    runtime = _concealed_runtime()
    offered = OfferedRefs(
        actor_ids=frozenset(_OFFERED | {"society"}), character_ids=frozenset({"hero", "rival"}),
        location_ids=frozenset(), facts={"fact:secret": "secret-value"}, intent_ids=frozenset(),
        event_ids=frozenset(), clock_ids=frozenset(), relation_ids=frozenset(), news_fact_refs=frozenset(),
        milestone_ids=frozenset(), time_units=frozenset({"minute"}),
        concealed_names=_concealed_names(runtime), rewards_by_chapter={},
    )
    chapter = {
        "chapter_number": 1, "title": "title", "one_line": "a clue", "goals": ["investigate"],
        "pov_ref": "hero", "onstage_refs": ["hero"], "expected_span": {"amount": 1, "unit": "minute"},
    }
    text = "investigate HiddenSociety"
    if field == "hint_text":
        chapter["hint_directives"] = [
            {"id": "hint-safe", "fact_ref": "fact:secret", "hint_text": "a clue"},
            {"id": "hint-hidden", "fact_ref": "fact:secret", "hint_text": text},
        ]
    else:
        chapter[field] = [text] if field == "goals" else text
    if reveal:
        chapter["reveal_directives"] = [{
            "id": "reveal", "fact_ref": "fact:secret", "observer": "reader", "target_state": "suspects",
        }]
    issues = band_plan_issues(
        BandPlanOutput.model_validate({"chapters": [chapter]}), chapter_numbers=[1], offered=offered,
        runtime=runtime, calendar=StoryCalendar("epoch", 0, (CalendarUnit("minute", 1),)), lexicon=BookLexicon(),
    )
    assert [(issue.code, issue.ref) for issue in issues] == (
        [] if reveal else [("writer_text_names_concealed", "society")]
    )
    if issues:
        assert field in issues[0].message


@pytest.mark.parametrize("visibility_source", ["state", "profile"])
def test_beat_sheet_actor_payload_marks_concealed_actors(visibility_source):
    from forwin.planning.beat_sheet.service import _actors

    actors = {item["id"]: item for item in _actors(_concealed_runtime(visibility_source=visibility_source))}
    assert actors["society"]["concealed"] is True
    assert actors["hero"]["concealed"] is False
