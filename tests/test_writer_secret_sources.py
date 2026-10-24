"""Author-side sources stay out of every chapter-1 writer prompt in three real books."""

from __future__ import annotations

import json
from copy import deepcopy
from dataclasses import dataclass
from types import SimpleNamespace

import pytest

from forwin.book_state.lexicon import lexicon_from_payload
from forwin.book_state.story_clock import calendar_from_payload, duration_to_minutes
from forwin.context.assembler_core import ChapterContextAssembler
from forwin.context.providers.experience_provider import ExperienceContextProvider
from forwin.context.providers.genesis_provider import GenesisContextProvider
from forwin.context.providers.map_provider import MapContextProvider
from forwin.context.providers.world_view_provider import WorldViewContextProvider
from forwin.context.providers import world_view_provider
from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.chapter_planner import ChapterExperiencePlanner
from forwin.experience.types import BeatSheetExperienceInput
from forwin.genesis.handoff.commands import StartWritingCommand
from forwin.genesis.handoff.service import GenesisHandoffService
from forwin.genesis.handoff import service as handoff_service
from forwin.planning.beat_sheet.repository import ArcBeatSheetRepository
from forwin.planning.beat_sheet.service import ArcBeatSheetService, beat_sheet_issues
from forwin.planning.band_planner.types import ChapterPlanDraft
from forwin.planning import feasibility
from forwin.runtime.policy import RuntimePolicy
from forwin.world_view.types import ChapterWorldView, ViewEntity
from forwin.context.request import ContextDraft, ContextRequest
from forwin.generation.pipeline_core.repair_patches import _current_chapter_repair_experience_plan
from forwin.models.project import ChapterPlan, Project
from forwin.planning.beat_sheet.types import ArcBeatSheetData
from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.review import ContinuityIssue
from forwin.review.draft_service import DraftReviewService
from forwin.world_view import builder as view_builder
from forwin.writer.prompt_core.world_view import forbidden_topic_text
from tests.test_chapter_delta_multi_genre import _committed_refs
from tests.test_planning_multi_genre import _fixture_runtime, _resolve
from tests.test_world_seed_multi_genre import GENRES, load_fixture
from tests.test_writer_world_view_prompt import BUILDERS, _prompt


PROMISE_FIELDS = (
    "genre_promise", "pleasure_promise", "core_pleasures", "acceptable_drag_level",
    "acceptable_exposition_density", "cliffhanger_aggressiveness", "world_legibility_target",
)


class _Answers:
    def __init__(self, *answers):
        self.answers = iter(answers)
        self.messages = []

    def chat(self, messages, **kwargs):
        self.messages.append(messages)
        return json.dumps(next(self.answers), ensure_ascii=False)


def _handoff(monkeypatch, project, revision, pack):
    """Run the real handoff assignment; substitute only persistence and unrelated stages."""
    @dataclass
    class SeedSummary:
        committed: bool = True

    arc = SimpleNamespace(id="arc-1", status="active", arc_number=1, chapter_start=1, chapter_end=3)
    session = SimpleNamespace(
        get=lambda *_: project,
        execute=lambda *_: SimpleNamespace(scalar_one=lambda: 0),
        add=lambda *_: None, flush=lambda: None,
    )
    updater = SimpleNamespace(save_decision_event=lambda *_: SimpleNamespace(id="handoff"))
    owner = SimpleNamespace(active_revision=lambda *_: revision, load_pack=lambda *_: pack)
    service = GenesisHandoffService(owner, beat_sheets=SimpleNamespace(ensure_for_arc=lambda *a, **k: None))
    service.arc_materializer = SimpleNamespace(materialize_book_arcs=lambda **_: [arc])
    service.map_bootstrap = SimpleNamespace(bootstrap_book_map_from_genesis=lambda **_: {})
    service.world_seed_committer = SimpleNamespace(commit=lambda **_: SeedSummary())
    monkeypatch.setattr(handoff_service, "build_rule_handoff_summary", lambda *a, **k: {})
    monkeypatch.setattr(handoff_service.ProjectPolicyStore, "load", lambda *_: SimpleNamespace(policy=RuntimePolicy.for_profile("standard")))
    result = service.start_writing(session=session, updater=updater, command=StartWritingCommand(project_id=project.id))
    assert result.project_status == project.creation_status == "writing"


def _author_context(monkeypatch, genre, setting_source="handoff"):
    fixture = load_fixture(genre)
    world = fixture["pack"]["world"]
    summaries = [arc["summary"] for arc in world["story_engine"]["long_arcs"]]
    markers = {source: f"AUTHOR_ONLY_{genre}_{source}_7f3c91" for source in (
        "long_arc", "world", "map", "fact", "beat_sheet", "band_plan", "premise", "setting",
        "phase", "milestone", "ambiguity", "title", "one_line", "goals", "hook", "hint_text",
        *(f"promise_{field}" for field in PROMISE_FIELDS),
    )}
    for arc in world["story_engine"]["long_arcs"]:
        arc["summary"] = "主角：玄霁，身份仍未揭晓。 " + arc["summary"] + " " + markers["long_arc"]
    markers["identity"] = "玄霁"
    world["world_bible"]["overview"] += " " + markers["world"] + " " + markers["setting"]
    world["map_atlas"]["overview"] = markers["map"]
    seed = fixture["pack"]["world_seed"]
    # This fact is deliberately unheld by both POV and reader, with no reveal directive.
    for source, marker in markers.items():
        seed["facts"].append({
            "id": f"unrevealed-{source}", "proposition": marker, "truth_value": True,
            "sensitivity": "hidden", "related_refs": [world["story_engine"]["core_cast"][0]["id"]],
            "nobody_knows": True,
        })
    promise = fixture["beat_sheet"]["reader_promise"]
    for field in PROMISE_FIELDS:
        value = f"PERMITTED_{field}"
        promise[field] = [value] if field == "core_pleasures" else value
    fixture["beat_sheet"]["arc_payoff_map"]["ambiguity_constraints"] = ["PERMITTED_AMBIGUITY"]
    fixture["beat_sheet"]["arc_payoff_map"]["revelation_layers"] = [{
        "layer_id": "future-canary", "summary": markers["beat_sheet"], "chapter_window": "3",
    }]
    fixture["band_plan"]["chapters"][0]["world_bindings"][0]["how"] = markers["band_plan"]
    project_id = f"writer-sources-{genre}"
    runtime = _fixture_runtime(fixture, project_id)
    refs = _committed_refs(runtime)
    chapter = _resolve(fixture["band_plan"]["chapters"][0], refs, project_id)
    sheet = ArcBeatSheetData.model_validate(_resolve(fixture["beat_sheet"], refs, project_id))
    calendar, lexicon = calendar_from_payload(seed["calendar"]), lexicon_from_payload(seed["lexicon"])
    actors = [node.id for node in runtime.world.nodes_by_id.values() if node.node_type == "character"]
    poisoned = sheet.model_dump(mode="json")
    paths = {"phase": "phases[0].purpose", "milestone": "milestones[0].description",
             "ambiguity": "arc_payoff_map.ambiguity_constraints[0]"}
    poisoned["phases"][0]["purpose"] += " " + markers["phase"]
    poisoned["milestones"][0]["description"] += " " + markers["milestone"]
    poisoned["arc_payoff_map"]["ambiguity_constraints"][0] += " " + markers["ambiguity"]
    for field in PROMISE_FIELDS:
        source = f"promise_{field}"
        paths[source] = f"reader_promise.{field}" + ("[0]" if field == "core_pleasures" else "")
        if field == "core_pleasures":
            poisoned["reader_promise"][field][0] += " " + markers[source]
        else:
            poisoned["reader_promise"][field] += " " + markers[source]
    poisoned_sheet = ArcBeatSheetData.model_validate(poisoned)
    experience = BeatSheetExperienceInput.from_beat_sheet(poisoned_sheet)
    assert markers["phase"] in experience.key_beats[0]
    assert any(markers["milestone"] in beat for beat in experience.key_beats)
    assert markers["ambiguity"] in experience.arc_payoff_map.ambiguity_constraints[0]
    for field in PROMISE_FIELDS:
        assert markers[f"promise_{field}"] in str(getattr(experience.reader_promise, field))
    issues = beat_sheet_issues(poisoned_sheet, chapter_start=1, chapter_end=3,
                               runtime=runtime, actor_ids=actors)
    for source, path in paths.items():
        fact = next(fact for fact in runtime.world.facts_by_id.values() if fact.proposition == markers[source])
        assert any(path in issue and f"fact:{fact.id}" in issue for issue in issues), (source, issues)
    # The production retry rejects the poisoned answer and accepts an independent valid answer.
    client = _Answers(poisoned, sheet.model_dump(mode="json"))
    call = ArcBeatSheetService(client, save_trace=lambda *a: "trace")._call(
        {}, "", start=1, end=3, runtime=runtime, actor_ids=actors, reveal_chapters={},
    )
    assert len(client.messages) == 2 and call.data is not None and call.issues == []
    sheet = call.data
    assert all(path in str(client.messages[1]) for path in paths.values())
    monkeypatch.setattr(feasibility, "_kept_chapters", lambda *a, **k: [])
    monkeypatch.setattr(feasibility, "earliest_reveal_chapters", lambda *a: {})
    monkeypatch.setattr(feasibility.StoryCalendarRepository, "get", lambda *_: calendar)
    monkeypatch.setattr(feasibility.BookLexiconRepository, "get", lambda *_: lexicon)
    monkeypatch.setattr(feasibility.BookStateQuery, "current_story_minute", lambda *a, **k: calendar.opening_minute)
    # Exercise the real pre-write/planner audit for every writer-facing band field separately.
    auditor = feasibility.FeasibilityAuditor()
    for source in ("title", "one_line", "goals", "hook", "hint_text"):
        poisoned_chapter = deepcopy(chapter)
        if source == "goals":
            poisoned_chapter[source][0] += " " + markers[source]
        elif source == "hint_text":
            poisoned_chapter["hint_directives"][0][source] += " " + markers[source]
        else:
            poisoned_chapter[source] += " " + markers[source]
        candidate = ChapterPlanDraft.model_validate(poisoned_chapter)
        source_text = (candidate.goals[0] if source == "goals" else
                       candidate.hint_directives[0].hint_text if source == "hint_text" else getattr(candidate, source))
        assert markers[source] in source_text
        issues = auditor.audit_plans(object(), project_id=project_id, plans=[candidate], beat_sheet=None,
                                     policy=RuntimePolicy.for_profile("standard"), runtime=runtime)
        fact = next(fact for fact in runtime.world.facts_by_id.values() if fact.proposition == markers[source])
        assert any(issue.code == "writer_text_states_secret" and issue.ref == f"fact:{fact.id}"
                   for issue in issues), (source, issues)
    valid = ChapterPlanDraft.model_validate(chapter)
    assert not [issue for issue in auditor.audit_plans(
        object(), project_id=project_id, plans=[valid], beat_sheet=None, policy=RuntimePolicy.for_profile("standard"), runtime=runtime,
    ) if issue.severity == "error"]
    plan = ChapterPlan(
        project_id=project_id, arc_plan_id="arc-1", chapter_number=1,
        title=chapter["title"], one_line=chapter["one_line"], hook=chapter["hook"],
        goals_json=json.dumps(chapter["goals"]), pov_ref=chapter["pov_ref"],
        expected_span_minutes=duration_to_minutes(calendar, lexicon, amount=chapter["expected_span"]["amount"],
                                                unit_name=chapter["expected_span"]["unit"]),
        **{f"{key}_json": json.dumps(chapter[key]) for key in (
            "onstage_refs", "location_refs", "world_bindings", "reveal_directives", "hint_directives",
        )},
    )
    monkeypatch.setattr(view_builder.StoryCalendarRepository, "get", lambda *_: calendar)
    monkeypatch.setattr(view_builder.BookLexiconRepository, "get", lambda *_: lexicon)
    monkeypatch.setattr(view_builder.BookStateQuery, "current_story_minute", lambda *_, **__: calendar.opening_minute)
    monkeypatch.setattr(view_builder, "earliest_reveal_chapters", lambda *_: {
        target.fact_ref.removeprefix("fact:"): target.range[0] for target in sheet.reveal_targets
    })
    experience = BeatSheetExperienceInput.from_beat_sheet(sheet)
    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band:1:3", chapter_start=1, chapter_end=3, experience=experience, active_band=[plan],
    )
    chapter_experience = ChapterExperiencePlanner().derive_chapter_experience_plan(
        chapter_number=1, experience=experience, schedule=schedule, chapter_plan=plan,
    )
    revision = SimpleNamespace(id="genesis-source-regression", revision=1, pack_json=json.dumps(fixture["pack"]))
    project = Project(id=project_id, setting_summary="", creation_status="genesis_ready", **fixture["project"])
    # Keep the real premise, including interstellar's early revelation; it has no name-pattern trigger.
    project.premise += " " + markers["premise"]
    if setting_source == "populated":
        project.setting_summary = "Existing author setting " + markers["setting"]
    _handoff(monkeypatch, project, revision, fixture["pack"])
    assert markers["setting"] in project.setting_summary
    if setting_source == "handoff":
        assert project.setting_summary == world["world_bible"]["overview"].strip()
    repo = SimpleNamespace(
        get_active_genesis_revision=lambda _: revision,
        get_arc_payoff_map=lambda *_, **__: sheet.arc_payoff_map,
        get_reader_promise=lambda *_, **__: sheet.reader_promise,
        get_band_experience_plan_for_chapter=lambda *_: schedule,
        get_chapter_experience_plan=lambda *_: chapter_experience,
    )
    monkeypatch.setattr(world_view_provider, "_budget_chars", lambda *_: 6000)
    monkeypatch.setattr(ArcBeatSheetRepository, "active_for_arc", lambda *_: SimpleNamespace(id="sheet"))
    monkeypatch.setattr(ArcBeatSheetRepository, "load", lambda *_: sheet)
    request = ContextRequest(project_id=project_id, chapter_plan=plan, repo=repo)
    draft = ContextDraft(data={"project": project, "book_state_runtime": runtime})
    for provider in (GenesisContextProvider(), ExperienceContextProvider(), MapContextProvider()):
        provider.contribute(request, draft)
    request.session = object()
    WorldViewContextProvider().contribute(request, draft)
    view = draft.data["world_view"]
    context = ChapterContextAssembler()._build_pack(project_id=project_id, chapter_plan=plan, draft=draft)
    # Prove the raw inputs reached their real providers; the reviewer retains the overviews.
    assert markers["identity"] in context.genesis_story_engine_summary
    assert markers["identity"] not in view.model_dump_json()
    assert markers["premise"] in context.premise
    assert markers["setting"] in context.setting_summary
    assert context.reader_promise == sheet.reader_promise
    assert context.band_delight_schedule == schedule
    assert context.chapter_experience_plan == chapter_experience
    assert context.arc_phase_purpose == sheet.phases[0].purpose
    assert any(sheet.milestones[0].description in item.escalated_question for item in schedule.curiosity_beats)
    assert markers["long_arc"] in context.genesis_story_engine_summary
    assert markers["world"] in context.genesis_world_overview
    assert markers["map"] in context.genesis_map_overview
    assert markers["fact"] in {fact.proposition for fact in runtime.world.facts_by_id.values()}
    hidden = next(fact for fact in runtime.world.facts_by_id.values() if fact.proposition == markers["fact"])
    assert f"fact:{hidden.id}" in {topic.fact_ref for topic in view.forbidden_topics}
    assert markers["beat_sheet"] in sheet.arc_payoff_map.model_dump_json()
    assert markers["band_plan"] in plan.world_bindings_json
    assert context.arc_payoff_map.revelation_layers == []
    return context, markers, summaries


@pytest.mark.parametrize("setting_source", ("handoff", "populated"))
@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("genre", GENRES)
def test_no_author_side_text_reaches_any_writer_builder(monkeypatch, genre, builder, setting_source):
    context, markers, summaries = _author_context(monkeypatch, genre, setting_source)
    markers["gate"] = f"AUTHOR_ONLY_{genre}_gate_7f3c91"
    # R44's reviewer boundary: the issue names a topic; the raw gate message stays in audit data.
    issue = ContinuityIssue(
        rule_name="premature_reveal", severity="error", reviewer="world_consistency",
        issue_type="world_consistency", target_scope="draft", blocking=True,
        description=forbidden_topic_text([context.world_view.pov_name]),
        original_result={"code": "premature_reveal", "message": markers["gate"]},
    )
    instruction = DraftReviewService._continuity_repair_instruction(continuity_issues=[issue], context=context)
    overlay = _current_chapter_repair_experience_plan(context.chapter_experience_plan, instruction)
    repaired = context.model_copy(update={"chapter_experience_plan": overlay})
    assert markers["gate"] in issue.original_result["message"]
    assert instruction.must_fix and overlay.rule_anchors

    for candidate in (context, repaired):
        text = _prompt(builder, candidate)
        leaked = [source for source, marker in markers.items() if marker in text]
        assert leaked == [], (genre, builder, leaked)
        assert "Genesis 根层版本：genesis-source-regression" in text
        assert context.world_view.pov_name in text
        assert context.chapter_plan_title in text
        assert f"主角姓名：{context.world_view.pov_name}" in text
        assert context.genre in text
        assert context.arc_phase_purpose in text
        assert context.chapter_plan_one_line in text
        assert context.chapter_plan_hook in text
        assert all(goal in text for goal in context.chapter_goals)
        assert all(hint.hint_text in text for hint in context.world_view.hints)
        for field in PROMISE_FIELDS:
            assert f"PERMITTED_{field}" in text
        assert "PERMITTED_AMBIGUITY" in text
        assert not any(heading in text for heading in ("Genesis 世界总览", "Genesis 地图总览", "Genesis 长线引擎"))
        if candidate is repaired:
            assert "repair must fix:" in text
        if genre == "interstellar":
            assert "父亲还活在沉船墓场" in context.premise
            assert "父亲还活在沉船墓场" not in text
            assert context.chapter_number == 1
            assert summaries == ["旗舰沉没的真相与父亲的归来"]
            assert all(summary not in text for summary in summaries)


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("identity", ("pov", "known", "absent", "no_view"))
@pytest.mark.parametrize("name_source", ("summary", "premise"))
def test_protagonist_identity_uses_only_permitted_structured_names(builder, identity, name_source):
    from forwin.protocol.context import ChapterContextPack

    view = ChapterWorldView(pov_ref="hero")
    if identity == "pov":
        view.pov_name = "PERMITTED_NAME"
    elif identity == "known":
        view.known_entities = [ViewEntity(ref="hero", name="PERMITTED_NAME")]
    context = ChapterContextPack(
        project_id="identity", project_title="Book", genre="mystery", chapter_number=1,
        premise="主角：玄霁，身份仍未揭晓。" if name_source == "premise" else "A neutral premise",
        setting_summary="Author setup",
        genesis_story_engine_summary="主角：玄霁，身份仍未揭晓。",
        chapter_plan_title="Plan", chapter_plan_one_line="Goal", chapter_goals=["Permitted goal"],
        world_view=None if identity == "no_view" else view,
    )
    for candidate in (context, context.model_copy(update={"chapter_experience_plan": ChapterExperiencePlan(
        rule_anchors=["repair must fix: permitted action"],
    )})):
        text = _prompt(builder, candidate)
        assert "玄霁" not in text
        if identity in {"pov", "known"}:
            assert "主角姓名：PERMITTED_NAME" in text
        else:
            assert "主角姓名：" not in text
