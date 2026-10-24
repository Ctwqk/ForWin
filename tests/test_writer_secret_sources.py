"""Author-side sources stay out of every chapter-1 writer prompt in three real books."""

from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.lexicon import lexicon_from_payload
from forwin.book_state.story_clock import calendar_from_payload, duration_to_minutes
from forwin.context.assembler_core import ChapterContextAssembler
from forwin.context.providers.experience_provider import ExperienceContextProvider
from forwin.context.providers.genesis_provider import GenesisContextProvider
from forwin.context.providers.map_provider import MapContextProvider
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


def _author_context(monkeypatch, genre):
    fixture = load_fixture(genre)
    world = fixture["pack"]["world"]
    summaries = [arc["summary"] for arc in world["story_engine"]["long_arcs"]]
    markers = {source: f"AUTHOR_ONLY_{genre}_{source}_7f3c91" for source in (
        "long_arc", "world", "map", "fact", "beat_sheet", "band_plan",
    )}
    for arc in world["story_engine"]["long_arcs"]:
        arc["summary"] += " " + markers["long_arc"]
    world["world_bible"]["overview"] += " " + markers["world"]
    world["map_atlas"]["overview"] = markers["map"]
    seed = fixture["pack"]["world_seed"]
    # This fact is deliberately unheld by both POV and reader, with no reveal directive.
    seed["facts"].append({
        "id": "unrevealed-canary", "proposition": markers["fact"], "truth_value": True,
        "sensitivity": "hidden", "related_refs": [world["story_engine"]["core_cast"][0]["id"]],
        "nobody_knows": True,
    })
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
    view = view_builder.build_chapter_world_view(
        object(), project_id=project_id, chapter_plan=plan, budget_chars=6000, runtime=runtime,
    )
    revision = SimpleNamespace(id="genesis-source-regression", revision=1, pack_json=json.dumps(fixture["pack"]))
    repo = SimpleNamespace(
        get_active_genesis_revision=lambda _: revision,
        get_arc_payoff_map=lambda *_, **__: sheet.arc_payoff_map,
    )
    request = ContextRequest(project_id=project_id, chapter_plan=plan, repo=repo)
    draft = ContextDraft(data={
        "project": Project(id=project_id, setting_summary="", **fixture["project"]),
        "world_view": view, "arc_phase_purpose": sheet.phases[0].purpose,
    })
    for provider in (GenesisContextProvider(), ExperienceContextProvider(), MapContextProvider()):
        provider.contribute(request, draft)
    context = ChapterContextAssembler()._build_pack(project_id=project_id, chapter_plan=plan, draft=draft)
    # Prove the raw inputs reached their real providers; the reviewer retains the overviews.
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


@pytest.mark.parametrize("builder", BUILDERS)
@pytest.mark.parametrize("genre", GENRES)
def test_no_author_side_text_reaches_any_writer_builder(monkeypatch, genre, builder):
    context, markers, summaries = _author_context(monkeypatch, genre)
    markers["gate"] = f"AUTHOR_ONLY_{genre}_gate_7f3c91"
    # R44's reviewer boundary: the issue names a topic; the raw gate message stays in audit data.
    issue = ContinuityIssue(
        rule_name="premature_reveal", severity="error", reviewer="world_consistency",
        issue_type="world_consistency", target_scope="draft", blocking=True,
        description=forbidden_topic_text([context.world_view.pov_name]),
        original_result={"code": "premature_reveal", "message": markers["gate"]},
    )
    instruction = DraftReviewService._continuity_repair_instruction(continuity_issues=[issue], context=context)
    overlay = _current_chapter_repair_experience_plan(ChapterExperiencePlan(), instruction)
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
        assert not any(heading in text for heading in ("Genesis 世界总览", "Genesis 地图总览", "Genesis 长线引擎"))
        if candidate is repaired:
            assert "repair must fix:" in text
        if genre == "interstellar":
            assert context.chapter_number == 1
            assert summaries == ["旗舰沉没的真相与父亲的归来"]
            assert all(summary not in text for summary in summaries)
