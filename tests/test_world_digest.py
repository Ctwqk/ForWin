from __future__ import annotations

from types import SimpleNamespace

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.lexicon import BookLexicon, TravelMode
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.narrative import NarrativeControlGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.book_state import CognitionOverlay, FactNode, MapEdge, MapNode, NarrativeNode, WorldEdge, WorldNode
from forwin.world_engine.types import NewsItem


KEYS = [
    "protagonist", "actors", "news_in_transit", "offscreen_events", "secrets_progress",
    "tensions", "clocks", "stalled_threads_obligations",
]


def _node(ref, kind="character", *, profile=None, **state):
    return WorldNode(id=ref, project_id="p", node_type=kind, name=f"{ref}-name", profile=profile or {}, state=state)


def _runtime():
    nodes = [
        _node("hero", location_id="home", resources=["resource-value"]),
        _node("z-target", location_id="near"),
        _node("a-plain", location_id="near"),
        _node("far-linked", location_id="far"),
        _node("two-hops", location_id="home"),
        _node("unlinked", location_id="home"),
        *[_node(f"extra-{i}", location_id="far") for i in range(6)],
        _node("related-item", "item"),
        _node("hidden-intent", "intent", profile={
            "owner_ref": "z-target", "summary": "hidden-intent-value", "target_refs": ["related-item"],
            "visibility_level": "secret",
        }, status="planned", scheduled_at=120),
        _node("visible-intent", "intent", profile={
            "owner_ref": "a-plain", "summary": "visible-intent-value", "target_refs": [],
        }, status="due", scheduled_at=60),
        _node("agenda", "agenda", profile={"owner_ref": "hero", "goal": "goal-value"}, status="active"),
        _node("clock", "clock", profile={"label": "clock-value", "scope": "secret"}, status="running", due_at=180),
        _node("tension", "conflict", profile={"stakes": "stakes-value"}, status="active"),
    ]
    edges = [WorldEdge(id=f"edge-{i}", project_id="p", source_id=a, target_id=b, edge_family="social",
                       edge_type="ally_of", is_active=active)
             for i, (a, b, active) in enumerate([
                 ("z-target", "hero", True), ("hero", "a-plain", True), ("hero", "far-linked", True),
                 ("far-linked", "two-hops", True), ("hero", "related-item", True),
                 ("hero", "unlinked", False),
             ])]
    for ref, chapter, source in [("recent", 3, "world_engine"), ("old", 2, "world_engine"),
                                  ("future", 6, "world_engine"), ("onstage", 5, "chapter_extraction")]:
        nodes.append(_node(ref, "event", actual_chapter=chapter, actual_story_time=chapter * 60).model_copy(
            update={"created_at_chapter": chapter, "metadata": {"source": source}}))
    facts = [FactNode(id=ref, project_id="p", proposition=f"{ref}-value", sensitivity_level=sensitivity)
             for ref, sensitivity in [("recorded", "secret"), ("unrecorded", "public"), ("news", "public")]]
    return BookStateRuntime(
        project_id="p", as_of_chapter=5, world=ObjectiveWorldGraph(nodes=nodes, edges=edges, facts=facts),
        map_graph=MapGraph(
            nodes=[MapNode(id=ref, project_id="p", node_type="site", name=f"{ref}-name")
                   for ref in ("home", "near", "far")],
            edges=[MapEdge(id=f"road-{ref}", project_id="p", from_node_id="home", to_node_id=ref,
                           edge_type="road", bidirectional=True, travel_time=hours)
                   for ref, hours in [("near", 1), ("far", 3)]],
        ),
        cognition_by_observer={("reader", "reader"): CognitionView(CognitionOverlay(
            id="reader-view", project_id="p", observer_type="reader", observer_id="reader",
            knowledge_by_ref={"fact:recorded": "suspects"},
        ))},
        narrative=NarrativeControlGraph(nodes=[
            NarrativeNode(id="stalled", project_id="p", node_type="plot_thread", title="stalled-value",
                          payload={"beats": [{"chapter_number": 2, "description": "beat-value"}]}),
            NarrativeNode(id="fresh", project_id="p", node_type="plot_thread", title="fresh-value",
                          payload={"beats": [{"chapter_number": 5}]}),
        ]),
    )


@pytest.fixture
def source(monkeypatch):
    from forwin.planning import world_digest as module

    runtime = _runtime()
    data = SimpleNamespace(runtime=runtime, news=[], reveals={"recorded": 7, "unrecorded": 8}, calls=[])
    monkeypatch.setattr(module.BookLexiconRepository, "get", lambda *args: BookLexicon())
    monkeypatch.setattr(module.StoryCalendarRepository, "get", lambda *args: StoryCalendar(
        "epoch", 0, (CalendarUnit("minute", 1),)))
    monkeypatch.setattr(module, "latest_news_in_transit", lambda *args: data.news)
    monkeypatch.setattr(module, "earliest_reveal_chapters", lambda *args: data.reveals)
    monkeypatch.setattr(module.NarrativeObligationRepository, "list_active_for_context", lambda *args, **kw: [
        NarrativeObligation(id="obligation", project_id="p", origin_chapter_number=5,
                            obligation_type="world_change_response", status="active", summary="obligation-value",
                            deadline_chapter=6, payoff_test="payoff-value"),
    ])

    def load(self, project_id, *, as_of_chapter, **kwargs):
        data.calls.append((project_id, as_of_chapter))
        return runtime

    monkeypatch.setattr(module.BookStateProjection, "load_runtime_as_of", load)
    return data


def _build(source, *, budget=100_000, supplied=True):
    from forwin.planning.world_digest import build_world_digest
    return build_world_digest(object(), project_id="p", as_of_chapter=5, protagonist_ref="hero",
                              window_end_minute=180, budget_chars=budget,
                              runtime=source.runtime if supplied else None)


def _lines(digest, key):
    return next(section.lines for section in digest.sections if section.key == key)


def test_digest_orders_sections_and_ranks_actors_by_relevance(source):
    from forwin.planning.world_digest import DIGEST_TOP_ACTORS
    digest = _build(source, supplied=False)
    assert [section.key for section in digest.sections] == KEYS
    assert DIGEST_TOP_ACTORS == 8
    actors = _lines(digest, "actors")
    assert len(actors) == 8
    assert [line.split(" | ")[0] for line in actors[:4]] == [
        "z-target-name", "a-plain-name", "far-linked-name", "two-hops-name",
    ]
    assert "hidden-intent-value" in actors[0] and "120" in actors[0]
    assert "visible-intent-value" in actors[1] and "60" in actors[1]
    assert "goal-value" in "\n".join(_lines(digest, "protagonist"))
    assert ("p", 5) in source.calls
    source.runtime.world.nodes_by_id = dict(reversed(list(source.runtime.world.nodes_by_id.items())))
    source.runtime.world.edges_by_id = dict(reversed(list(source.runtime.world.edges_by_id.items())))
    assert _build(source).text == digest.text


def test_digest_trims_from_the_end_under_budget(source):
    full = _build(source)
    prefix = "\n\n".join("\n".join([section.title, *section.lines]) for section in full.sections[:3])
    trimmed = _build(source, budget=len(prefix))
    assert trimmed.text == prefix
    assert [section.key for section in trimmed.sections] == KEYS[:3]
    assert trimmed.trimmed_keys == list(reversed(KEYS[3:]))
    assert _build(source, budget=len(full.text)).text == full.text
    empty = _build(source, budget=0)
    assert empty.text == "" and empty.sections == []
    assert empty.trimmed_keys == list(reversed(KEYS))


def test_digest_lists_only_news_arriving_within_window(source):
    source.news = [
        NewsItem(fact_ref="fact:news", observer_ref="hero", arrive_at=180),
        NewsItem(fact_ref="fact:unrecorded", observer_ref="hero", arrive_at=181),
        NewsItem(fact_ref="fact:recorded", observer_ref="a-plain", arrive_at=100),
        NewsItem(fact_ref="fact:recorded", observer_ref="two-hops", arrive_at=170),
        NewsItem(fact_ref="fact:news", observer_ref="missing", arrive_at=1),
    ]
    lines = _lines(_build(source), "news_in_transit")
    assert len(lines) == 2
    assert "two-hops" in lines[0] and "170" in lines[0]
    assert "hero" in lines[1] and "180" in lines[1]
    assert "181" not in "\n".join(lines) and "a-plain" not in "\n".join(lines)
    source.runtime.world.states_by_node_id["hero"].pop("location_id")
    assert _lines(_build(source), "news_in_transit") == []


def test_digest_reader_progress_uses_recorded_reader_states_only(source, monkeypatch):
    def forbidden(*args):
        pytest.fail("Reader progress must not call knowledge_state")
    monkeypatch.setattr(CognitionView, "knowledge_state", forbidden)
    lines = _lines(_build(source), "secrets_progress")
    recorded = next(line for line in lines if "fact:recorded" in line)
    unrecorded = next(line for line in lines if "fact:unrecorded" in line)
    assert "reader_state=suspects" in recorded and "reveal_target=7" in recorded
    assert "reader_state=unknown" in unrecorded and "reveal_target=8" in unrecorded
    assert "knows" not in unrecorded
    source.runtime.cognition_by_observer.clear()
    assert "reader_state=unknown" in "\n".join(_lines(_build(source), "secrets_progress"))


def test_digest_includes_recent_world_events_tensions_clocks_and_stalled_obligations(source):
    digest = _build(source)
    events = _lines(digest, "offscreen_events")
    assert len(events) == 1 and "recent-name" in events[0]
    assert "stakes-value" in "\n".join(_lines(digest, "tensions"))
    assert "clock-value" in "\n".join(_lines(digest, "clocks"))
    last = "\n".join(_lines(digest, "stalled_threads_obligations"))
    assert "stalled-value" in last and "fresh-value" not in last
    assert "obligation-value" in last and "payoff-value" in last


def test_digest_ranking_uses_mover_travel_modes_and_handles_unknown_locations(source, monkeypatch):
    from forwin.planning import world_digest as module

    monkeypatch.setattr(module.BookLexiconRepository, "get", lambda *args: BookLexicon(travel_modes=(
        TravelMode(id="fast", name="fast-value", speed_multiplier=6, allowed_edge_types=("road",)),
    )))
    source.runtime.world.states_by_node_id["far-linked"]["travel_mode_ids"] = ["fast"]
    actors = _lines(_build(source), "actors")
    assert actors[0].startswith("far-linked-name | ")
    assert "travel_minutes=30.0" in actors[0]
    source.runtime.world.states_by_node_id["far-linked"].pop("location_id")
    actors = _lines(_build(source), "actors")
    assert actors[0].startswith("z-target-name | ")
    assert actors[2].startswith("far-linked-name | ") and "travel_minutes=unknown" in actors[2]


def test_digest_postgres_reads_canon_projection_and_news():
    from forwin.planning.world_digest import build_world_digest
    from tests.test_world_gate_v6 import _seed_world, _session_factory
    from tests.test_world_tick_delta import _built, _candidate, _chapter_one, _commit_tick, _context

    engine, Session = _session_factory()
    try:
        world = _seed_world(Session)
        chapter = _chapter_one(Session, world)
        candidate = _candidate("event", effects={"events": [{
            "summary": "digest-event-value", "participant_refs": [world.rival],
            "location_id": world.loc(2), "visibility": "public", "at": chapter.story_time_end,
        }]})
        from forwin.world_engine.delta_builder import tick_event_fact_ref
        fact_ref = tick_event_fact_ref(world.project_id, "digest-tick", "event", 0)
        tick = _built(Session, _context(world, chapter), [candidate], tick_id="digest-tick", news=[
            NewsItem(fact_ref=fact_ref, observer_ref=world.hero, arrive_at=chapter.story_time_end + 60),
        ])
        _commit_tick(Session, world, tick, tick_id="digest-tick")
        with Session() as session:
            digest = build_world_digest(session, project_id=world.project_id, as_of_chapter=1,
                                        protagonist_ref=world.hero, window_end_minute=chapter.story_time_end + 60,
                                        budget_chars=100_000)
            assert [section.key for section in digest.sections] == KEYS
            assert "digest-event-value" in "\n".join(_lines(digest, "offscreen_events"))
            assert fact_ref in "\n".join(_lines(digest, "news_in_transit"))
    finally:
        engine.dispose()
