"""Exercise the genre fixtures' extraction and canon reuse without a database."""

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.extraction.chapter_delta_extractor import ensure_chapter_delta
from forwin.book_state.extraction.chapter_input import build_chapter_extraction_input
from forwin.book_state.lexicon import lexicon_from_payload
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.projection import BookStateProjection
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import calendar_from_payload
from forwin.genesis.world_seed import story_engine_actor_refs
from forwin.models.project import ChapterPlan
from forwin.narrative_obligations.repository import NarrativeObligationRepository
from forwin.planning.band_planner.persistence import chapter_plan_fingerprint
from forwin.protocol.book_state import WORLD_EDGE_TYPES_BY_FAMILY, FactNode, MapNode, WorldEdge, WorldNode
from forwin.world_view import builder
from tests.test_chapter_delta_multi_genre import _Book, _ExtractionClient, _committed_refs, _resolved
from tests.test_world_seed_multi_genre import GENRES, entity_names, load_fixture


@pytest.mark.parametrize("genre", GENRES)
def test_genre_extraction_input_is_stable_and_canon_reuses_draft_without_retry(monkeypatch, genre):
    fixture = load_fixture(genre)
    pack, chapter = fixture["pack"], fixture["chapter_1"]
    seed = pack["world_seed"]
    names = entity_names(pack["world"]["story_engine"])
    kinds = story_engine_actor_refs(pack["world"]["story_engine"])
    nodes = [WorldNode(
        id=actor["ref"], project_id="p", node_type=kinds[actor["ref"]], name=names[actor["ref"]],
        state={"location_id": actor["location_ref"]},
    ) for actor in seed["actors"]]
    sites = [MapNode(id=site["id"], project_id="p", node_type="site", name=site["name"])
             for site in pack["world"]["map_atlas"]["nodes"]]
    facts = [FactNode(id="unoffered-b", project_id="p", proposition="B", sensitivity_level="secret"),
             FactNode(id="unoffered-a", project_id="p", proposition="A", sensitivity_level="secret")]
    edges = [WorldEdge(
        id=f"edge-{index}", project_id="p", source_id=relation["source"], target_id=relation["target"],
        edge_type=relation["edge_type"], visibility=relation["visibility"],
        edge_family=next(family for family, types in WORLD_EDGE_TYPES_BY_FAMILY.items()
                         if relation["edge_type"] in types),
    ) for index, relation in enumerate(seed["relations"])]

    def runtime(reverse=False):
        return BookStateRuntime(
            project_id="p", as_of_chapter=0,
            world=ObjectiveWorldGraph(nodes=nodes[::-1] if reverse else nodes,
                                      facts=facts[::-1] if reverse else facts,
                                      edges=edges[::-1] if reverse else edges),
            map_graph=MapGraph(nodes=sites[::-1] if reverse else sites),
        )

    draft_runtime, canon_runtime = runtime(), runtime(reverse=True)
    book = _Book("p", chapter, _committed_refs(draft_runtime))
    planned = _resolved(chapter["plan"], book.refs)
    plan = ChapterPlan(
        id="plan", project_id="p", arc_plan_id="arc", chapter_number=1, status="planned",
        title=planned["title"], one_line=planned["one_line"], goals_json=json.dumps(planned["goals"]),
        pov_ref=planned.get("pov_ref", ""), onstage_refs_json=json.dumps(planned.get("onstage_refs", [])),
        location_refs_json=json.dumps(planned.get("location_refs", [])),
    )
    session = SimpleNamespace(scalar=lambda query: plan, get=lambda *args: None)
    monkeypatch.setattr(StoryCalendarRepository, "get", lambda *args: calendar_from_payload(seed["calendar"]))
    monkeypatch.setattr(BookLexiconRepository, "get", lambda *args: lexicon_from_payload(seed["lexicon"]))
    monkeypatch.setattr(NarrativeObligationRepository, "list_active_for_context", lambda *args, **kwargs: [])
    monkeypatch.setattr(NarrativeObligationRepository, "list_planned_for_chapter", lambda *args, **kwargs: [])
    monkeypatch.setattr(builder.BookStateQuery, "current_story_minute", lambda *args, **kwargs: None)
    monkeypatch.setattr(builder, "earliest_reveal_chapters", lambda *args: {})
    monkeypatch.setattr(BookStateProjection, "load_runtime_as_of", lambda *args, **kwargs: draft_runtime)
    draft_input = build_chapter_extraction_input(session, project_id="p", chapter_number=1, runtime=draft_runtime)
    draft_key = chapter_plan_fingerprint(plan)
    client = _ExtractionClient(book.extraction())
    output, extracted = ensure_chapter_delta(
        session, llm_client=client, project_id="p", chapter_number=1, writer_output=book.draft(),
    )

    plan.status = "drafted"
    canon_input = build_chapter_extraction_input(session, project_id="p", chapter_number=1, runtime=canon_runtime)
    assert canon_input == draft_input
    assert chapter_plan_fingerprint(plan) == draft_key
    assert canon_input.runtime_fact_refs == ("fact:unoffered-a", "fact:unoffered-b")
    reused, result = ensure_chapter_delta(
        session, llm_client=client, project_id="p", chapter_number=1, writer_output=output,
    )
    assert reused is output and result is None
    assert extracted.attempts == 1, extracted.prompt_trace["output_summary"]["business_retry_events"]
    assert client.stage_keys == ["chapter_delta_extraction"]
