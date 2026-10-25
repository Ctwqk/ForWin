from __future__ import annotations

import hashlib
import json
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
    "tensions", "clocks", "world_conditions", "stalled_threads_obligations",
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
        }, status="due", scheduled_at=1379),
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


def _build(source, *, budget=100_000, supplied=True, as_of_chapter=5):
    from forwin.planning.world_digest import build_world_digest
    return build_world_digest(object(), project_id="p", as_of_chapter=as_of_chapter, protagonist_ref="hero",
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
    assert "visible-intent-value" in actors[1] and "1379 (epoch 1379minute)" in actors[1]
    assert "goal-value" in "\n".join(_lines(digest, "protagonist"))
    assert ("p", 5) in source.calls
    source.runtime.world.nodes_by_id = dict(reversed(list(source.runtime.world.nodes_by_id.items())))
    source.runtime.world.edges_by_id = dict(reversed(list(source.runtime.world.edges_by_id.items())))
    assert _build(source).text == digest.text


def test_digest_trims_lines_and_preserves_section_diversity(source, caplog):
    full = _build(source)
    trimmed = _build(source, budget=700)
    assert len(trimmed.text) <= 700
    assert len([section for section in trimmed.sections if section.lines]) >= 4
    assert trimmed.sections[0].key == "protagonist"
    assert len(trimmed.sections[0].lines) == 1
    for section in trimmed.sections:
        original = next(item for item in full.sections if item.key == section.key)
        assert section.lines == original.lines[:len(section.lines)]
    assert _build(source, budget=700) == trimmed
    assert _build(source, budget=len(full.text)).text == full.text
    empty = _build(source, budget=0)
    assert empty.text == "" and empty.sections == []
    assert set(empty.trimmed_keys) == set(KEYS)
    assert any("protagonist" in record.message for record in caplog.records)


def test_digest_lists_only_news_arriving_within_window(source):
    source.news = [
        NewsItem(fact_ref="fact:news", observer_ref="hero", arrive_at=180),
        NewsItem(fact_ref="fact:unrecorded", observer_ref="hero", arrive_at=181),
        NewsItem(fact_ref="fact:recorded", observer_ref="a-plain", arrive_at=100),
        NewsItem(fact_ref="fact:recorded", observer_ref="two-hops", arrive_at=170),
        NewsItem(fact_ref="fact:news", observer_ref="missing", arrive_at=1),
        NewsItem(fact_ref="fact:absent", observer_ref="hero", arrive_at=1),
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
    recorded = next(line for line in lines if line.startswith("recorded-value |"))
    unrecorded = next(line for line in lines if line.startswith("unrecorded-value |"))
    assert "reader_state=suspects" in recorded and "reveal_target=7" in recorded
    assert "reader_state=unknown" in unrecorded and "reveal_target=8" in unrecorded
    assert "knows" not in unrecorded
    source.runtime.cognition_by_observer.clear()
    assert "reader_state=unknown" in "\n".join(_lines(_build(source), "secrets_progress"))


def test_digest_includes_recent_world_events_tensions_clocks_and_stalled_obligations(source):
    digest = _build(source)
    events = _lines(digest, "offscreen_events")
    assert len(events) == 1 and "recent-name" in events[0]
    world = source.runtime.world
    world.nodes_by_id["recent"] = world.nodes_by_id["recent"].model_copy(update={
        "summary": "event-summary-value", "profile": {"location_id": "near", "unused": "omit-me"},
    })
    assert _lines(_build(source), "offscreen_events") == ["event-summary-value | location=near-name | chapter=3"]
    assert "stakes-value" not in "\n".join(_lines(digest, "tensions"))
    assert _lines(digest, "clocks") == ["clock-value | remaining=0 (0minute) | scope=secret"]
    source.runtime.world.states_by_node_id["clock"]["due_at"] = 457
    assert _lines(_build(source), "clocks") == ["clock-value | remaining=277 (277minute) | scope=secret"]
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


def test_digest_postgres_reads_canon_projection_and_news(monkeypatch):
    from forwin.planning.world_digest import build_world_digest
    from forwin.narrative_obligations.repository import NarrativeObligationRepository
    from tests import world_gate_helpers as fixtures
    from tests.test_world_gate_v6 import _commit, _delta, _knowledge, _seed_world, _session_factory, _time
    from tests.test_world_tick_delta import _built, _candidate, _chapter_one, _commit_tick, _context

    original_pack = fixtures._pack

    def relation_pack(**kwargs):
        pack = original_pack(**kwargs)
        pack["world_seed"]["relations"] = [
            {"source": "cast-1", "target": "opp-1", "edge_type": "enemy_of", "note": "", "visibility": "public", "appellation": ""},
            {"source": "faction-1", "target": "cast-1", "edge_type": "owes_debt_to", "note": "", "visibility": "public", "appellation": ""},
        ]
        return pack

    monkeypatch.setattr(fixtures, "_pack", relation_pack)
    engine, Session = _session_factory()
    try:
        world = _seed_world(Session, reveal_candidates=[{"fact_id": "fact-secret", "arc_number": 2}])
        _chapter_one(Session, world, {
            "knowledge_events": [_knowledge("reader", world.fact("fact-public"), channel="inferred")],
            "thread_beats": [{"new_thread_title": "digest-thread-value", "beat_type": "setup", "evidence": "thread-evidence"}],
        })
        for number in range(2, 5):
            chapter = _delta(Session, world, number, _time(1, "日"))
            _commit(Session, world, chapter)
        candidate = _candidate("event", effects={"events": [{
            "summary": "digest-event-value", "participant_refs": [world.rival],
            "location_id": world.loc(2), "visibility": "public", "at": chapter.story_time_end,
        }]})
        from forwin.world_engine.delta_builder import tick_event_fact_ref
        fact_ref = tick_event_fact_ref(world.project_id, "digest-tick", "event", 0)
        tick = _built(Session, _context(world, chapter), [candidate], tick_id="digest-tick", news=[
            NewsItem(fact_ref=fact_ref, observer_ref=world.hero, arrive_at=chapter.story_time_end + 60),
        ])
        _commit_tick(Session, world, tick, tick_id="digest-tick", chapter=4)
        with Session.begin() as session:
            NarrativeObligationRepository(session).create_obligation(NarrativeObligation(
                project_id=world.project_id, origin_chapter_number=1, obligation_type="world_change_response",
                status="active", summary="digest-obligation-value", deadline_chapter=5,
                payoff_test="digest-payoff-value", subject_refs=[world.rival],
            ))
        with Session() as session:
            digest = build_world_digest(session, project_id=world.project_id, as_of_chapter=4,
                                        protagonist_ref=world.hero, window_end_minute=chapter.story_time_end + 60,
                                        budget_chars=100_000)
            assert [section.key for section in digest.sections] == KEYS
            assert "digest-event-value" in "\n".join(_lines(digest, "offscreen_events"))
            assert "digest-event-value" in "\n".join(_lines(digest, "news_in_transit"))
            assert "location=site-2" in "\n".join(_lines(digest, "offscreen_events"))
            actors = _lines(digest, "actors")
            assert len(actors) == 2
            assert any(line.startswith("乙 |") and "location=site-2" in line for line in actors)
            assert any(line.startswith("丙 |") for line in actors)
            tensions = _lines(digest, "tensions")
            assert "甲 — enemy_of — 乙" in tensions
            assert "丙 — owes_debt_to — 甲" in tensions
            secrets = _lines(digest, "secrets_progress")
            assert any(line.startswith("proposition-secret |") and "reveal_target=6" in line for line in secrets)
            assert any(line.startswith("proposition-public |") and "reader_state=knows" in line for line in secrets)
            stalled = "\n".join(_lines(digest, "stalled_threads_obligations"))
            assert "digest-thread-value" in stalled
            assert "digest-obligation-value" in stalled and "digest-payoff-value" in stalled
    finally:
        engine.dispose()



def test_digest_section_keys_match_titles(source):
    from forwin.planning.world_digest import DIGEST_SECTION_KEYS
    from forwin.writer.prompt_core.planning import WORLD_DIGEST_TITLES
    assert DIGEST_SECTION_KEYS == tuple(KEYS)
    assert set(WORLD_DIGEST_TITLES) == set(DIGEST_SECTION_KEYS)


@pytest.mark.parametrize("supplied, expected", [(True, 0), (False, 1)])
def test_digest_loads_runtime_once(source, supplied, expected):
    _build(source, supplied=supplied)
    assert len(source.calls) == expected


@pytest.mark.parametrize("supplied, expected", [(True, 0), (False, 1)])
def test_thread_sampling_reuses_runtime(source, supplied, expected):
    from forwin.book_state.thread_sampling import sample_active_threads
    result = sample_active_threads(session=object(), project_id="p", chapter_number=5,
                                   limit=20, stale_window=3,
                                   runtime=source.runtime if supplied else None)
    assert {thread.id for thread in result.threads} == {"stalled", "fresh"}
    assert len(source.calls) == expected


def test_digest_excludes_dead_and_inactive_actors_and_events(source):
    world = source.runtime.world
    world.states_by_node_id["z-target"]["alive_status"] = "dead"
    world.nodes_by_id["a-plain"] = world.nodes_by_id["a-plain"].model_copy(update={"is_active": False})
    world.nodes_by_id["recent"] = world.nodes_by_id["recent"].model_copy(update={"is_active": False})
    digest = _build(source)
    actors = _lines(digest, "actors")
    assert not any(line.startswith(("z-target-name |", "a-plain-name |")) for line in actors)
    assert _lines(digest, "offscreen_events") == []


def test_digest_rejects_non_character_protagonist_and_negative_budget(source):
    with pytest.raises(ValueError, match="nonnegative"):
        _build(source, budget=-1)
    source.runtime.world.nodes_by_id["hero"] = _node("hero", "faction")
    with pytest.raises(ValueError, match="character"):
        _build(source)


def test_digest_filters_orders_and_caps_secrets(source):
    from forwin.planning.world_digest import DIGEST_SECRETS_LIMIT
    world = source.runtime.world
    facts = [FactNode(id=f"public-{i:02}", project_id="p", proposition=f"public-value-{i:02}") for i in range(50)]
    facts += [FactNode(id=f"hidden-{i}", project_id="p", proposition=f"hidden-value-{i}",
                       sensitivity_level=sensitivity) for i, sensitivity in enumerate(("hidden", "secret", "must_not_reveal"))]
    world.facts_by_id = {fact.id: fact for fact in facts}
    source.reveals = {"hidden-2": 4, "hidden-0": 4}
    reader = source.runtime.cognition_by_observer[("reader", "reader")]
    reader.knowledge_by_ref = {}
    lines = _lines(_build(source), "secrets_progress")
    assert [line.split(" | ")[0] for line in lines] == ["hidden-value-0", "hidden-value-2", "hidden-value-1"]
    reader.knowledge_by_ref["fact:public-20"] = "suspects"
    source.reveals["public-30"] = 2
    lines = _lines(_build(source), "secrets_progress")
    assert len(lines) == 5 and lines[0].startswith("public-value-30 |")
    assert any(line.startswith("public-value-20 |") for line in lines)
    for fact in facts[:20]:
        reader.knowledge_by_ref[f"fact:{fact.id}"] = "knows"
    assert DIGEST_SECRETS_LIMIT == 12
    assert len(_lines(_build(source), "secrets_progress")) == 12


def test_digest_relations_are_named_ordered_active_and_capped(source):
    from forwin.planning.world_digest import DIGEST_TENSIONS_LIMIT
    world = source.runtime.world
    world.edges_by_id = {
        f"relation-{i:02}": WorldEdge(id=f"relation-{i:02}", project_id="p", source_id="a-plain",
                                    target_id="hero" if i > 2 else "z-target", edge_family="social",
                                    edge_type="enemy_of" if i % 2 else "owes_debt_to", is_active=i != 4)
        for i in range(15)
    }
    state = world.states_by_node_id["hero"]
    state.update(rank_title="rank-value", power_level=4, resource_summary="resource-summary")
    digest = _build(source)
    tensions = _lines(digest, "tensions")
    assert DIGEST_TENSIONS_LIMIT == 10 and len(tensions) == 10
    assert all("hero-name" in line for line in tensions)
    assert tensions[0] == "a-plain-name — enemy_of — hero-name"
    protagonist = _lines(digest, "protagonist")
    assert len(protagonist) == 7
    assert "resources=[\"resource-value\"]" in protagonist[0]
    assert "rank_title=rank-value" in protagonist[0] and "power_level=4" in protagonist[0]
    assert "resource_summary=resource-summary" in protagonist[0]
    assert all("relation-" not in line for line in protagonist)
    world.edges_by_id["relation-00"] = world.edges_by_id["relation-00"].model_copy(update={"edge_family": "loyalty_conflict", "edge_type": "opposes"})
    for key in list(world.edges_by_id)[3:]:
        world.edges_by_id.pop(key)
    tensions = _lines(_build(source), "tensions")
    assert tensions[0] == "a-plain-name — opposes — z-target-name"
    world.nodes_by_id["z-target"] = world.nodes_by_id["z-target"].model_copy(update={"name": ""})
    assert _lines(_build(source), "tensions")[0] == "a-plain-name — opposes — z-target"


def test_digest_bounds_intents_and_uses_names(source):
    from forwin.planning.world_digest import DIGEST_INTENTS_PER_ACTOR
    world = source.runtime.world
    for i, scheduled_at in enumerate((400, 200, 300, 100)):
        node = _node(f"intent-id-{i}", "intent", profile={"owner_ref": "hero", "action_type": "investigate",
                     "summary": f"summary-{i}", "target_refs": ["related-item"]}, status="planned", scheduled_at=scheduled_at)
        world.nodes_by_id[node.id] = node
        world.states_by_node_id[node.id] = node.state
    line = _lines(_build(source), "protagonist")[0]
    assert DIGEST_INTENTS_PER_ACTOR == 3
    assert line.index("summary-3") < line.index("summary-1") < line.index("summary-2")
    assert "summary-0" not in line and "intent-id-" not in line
    assert "related-item-name" in line and "action_type" in line
    assert "owner_ref" not in line and "profile=" not in line and "state=" not in line


def test_realistic_digest_fits_default_budget(source):
    world = source.runtime.world
    for ref, node in list(world.nodes_by_id.items()):
        if str(node.node_type) in {"agenda", "intent"}:
            del world.nodes_by_id[ref]
            world.states_by_node_id.pop(ref, None)
    actors = [node for node in world.nodes_by_id.values() if str(node.node_type) == "character"][:9]
    for actor in actors:
        world.states_by_node_id[actor.id].update(alive_status="alive", restraint={"state": "free", "by_ref": "", "since": None},
                                               actor_tier="major", state_summary="unused-detail " * 40)
        agenda = _node(f"agenda-{actor.id}", "agenda", profile={"owner_ref": actor.id,
                       "goal": "Secure passage through the guarded district", "motivation": "unused-detail " * 40}, status="active")
        intents = [_node(f"intent-{actor.id}-{i}", "intent", profile={"owner_ref": actor.id, "agenda_ref": agenda.id,
                         "action_type": "investigate", "summary": "Find the missing courier before the meeting",
                         "target_refs": ["hero", "a-plain"], "expected_effects": {"unused": "detail " * 40}},
                         status="planned", scheduled_at=1500 + i * 60) for i in range(2)]
        for node in [agenda, *intents]:
            world.nodes_by_id[node.id] = node
            world.states_by_node_id[node.id] = node.state
    for i in range(3):
        node = _node(f"clock-{i}", "clock", profile={"label": f"Deadline {i}", "scope": "public"}, status="running", due_at=300 + i)
        world.nodes_by_id[node.id] = node
        world.states_by_node_id[node.id] = node.state
    source.news = [NewsItem(fact_ref="fact:news", observer_ref="hero", arrive_at=180)]
    digest = _build(source, budget=8000)
    assert len(digest.text) <= 8000
    assert {"protagonist", "actors", "news_in_transit", "offscreen_events"} <= {section.key for section in digest.sections}
    assert 1 <= len(_lines(digest, "actors")) <= 8
    assert _lines(digest, "news_in_transit") and _lines(digest, "offscreen_events")


def test_digest_tension_types_are_explicit_schema_members():
    from forwin.planning.world_digest import DIGEST_TENSION_EDGE_TYPES
    from forwin.protocol.book_state import WORLD_EDGE_TYPES

    assert DIGEST_TENSION_EDGE_TYPES == frozenset({"enemy_of", "rival_of", "distrusts", "owes_debt_to", "opposes"})
    assert DIGEST_TENSION_EDGE_TYPES <= WORLD_EDGE_TYPES


@pytest.mark.parametrize("edge_type", ["ally_of", "family_of", "romantic_with", "trusts", "serves", "allied_with", "mentor_of"])
def test_digest_excludes_friendly_tensions(source, edge_type):
    family = "loyalty_conflict" if edge_type in {"serves", "allied_with"} else "social"
    edge = WorldEdge(id="friendly", project_id="p", source_id="hero", target_id="a-plain",
                     edge_family=family, edge_type=edge_type)
    source.runtime.world.edges_by_id = {edge.id: edge}
    assert _lines(_build(source), "tensions") == []


def test_digest_friendly_edges_do_not_crowd_out_hostile_edges(source):
    types = ["ally_of", "family_of", "romantic_with", "trusts", "serves", "allied_with"] * 2
    edges = [WorldEdge(id=f"edge-{i:02}", project_id="p", source_id="hero", target_id="a-plain",
                       edge_family="loyalty_conflict" if kind in {"serves", "allied_with"} else "social",
                       edge_type=kind) for i, kind in enumerate([*types, "enemy_of", "rival_of"])]
    world = source.runtime.world
    world.edges_by_id = {edge.id: edge for edge in edges}
    lines = _lines(_build(source), "tensions")
    assert lines == ["hero-name — enemy_of — a-plain-name", "hero-name — rival_of — a-plain-name"]
    world.edges_by_id = dict(reversed(list(world.edges_by_id.items())))
    assert _lines(_build(source), "tensions") == lines


@pytest.mark.parametrize("known_scheduled", [15, 23])
def test_digest_prioritizes_unrevealed_secrets_in_long_books(source, monkeypatch, known_scheduled):
    def forbidden(*args):
        pytest.fail("Reader progress must use recorded states only")

    monkeypatch.setattr(CognitionView, "knowledge_state", forbidden)
    scheduled = [FactNode(id=f"scheduled-{i:02}", project_id="p", proposition=f"scheduled-secret-{i:02}",
                          sensitivity_level="secret") for i in range(30)]
    hidden = [FactNode(id=f"hidden-{i}", project_id="p", proposition=f"hidden-unscheduled-{i}",
                       sensitivity_level="secret") for i in range(5)]
    public = [FactNode(id=f"public-{i:03}", project_id="p", proposition=f"public-known-{i:03}") for i in range(150)]
    world = source.runtime.world
    world.facts_by_id = {fact.id: fact for fact in [*scheduled, *hidden, *public]}
    source.reveals = {fact.id: 2 + 2 * i for i, fact in enumerate(scheduled)}
    reader = source.runtime.cognition_by_observer[("reader", "reader")]
    reader.knowledge_by_ref = {f"fact:{fact.id}": "knows" for fact in [*scheduled[:known_scheduled], *public]}
    reader.knowledge_by_ref[f"fact:{scheduled[known_scheduled].id}"] = "suspects"
    lines = _lines(_build(source, as_of_chapter=40), "secrets_progress")
    expected = [*scheduled[known_scheduled:], *hidden][:12]
    assert len(lines) == 12
    assert [line.split(" | ")[0] for line in lines] == [fact.proposition for fact in expected]
    assert all("reader_state=knows" not in line for line in lines)
    assert "reader_state=suspects" in lines[0]
    world.facts_by_id = dict(reversed(list(world.facts_by_id.items())))
    assert _lines(_build(source, as_of_chapter=40), "secrets_progress") == lines


@pytest.mark.parametrize("length", [59, 60, 61, 118])
def test_digest_bounds_free_text_fields(source, length):
    from forwin.planning.world_digest import DIGEST_TEXT_FIELD_LIMIT

    assert DIGEST_TEXT_FIELD_LIMIT == 60
    text = "x" * length
    expected = text if length <= 60 else text[:59] + "…"
    world = source.runtime.world
    world.nodes_by_id["agenda"].profile["goal"] = text
    world.nodes_by_id["hidden-intent"].profile["summary"] = text
    world.nodes_by_id["visible-intent"].profile.pop("summary")
    world.nodes_by_id["visible-intent"] = world.nodes_by_id["visible-intent"].model_copy(update={"summary": text})
    world.nodes_by_id["recent"] = world.nodes_by_id["recent"].model_copy(update={"summary": text})
    world.nodes_by_id["clock"].profile["label"] = text
    digest = _build(source)
    assert f'agendas={json.dumps([expected], ensure_ascii=False)}' in _lines(digest, "protagonist")[0]
    for actor in _lines(digest, "actors")[:2]:
        intents = json.loads(actor.split(" | intents=", 1)[1].split(" | ", 1)[0])
        assert intents[0]["summary"] == expected
    assert _lines(digest, "offscreen_events")[0].split(" | ")[0] == expected
    assert _lines(digest, "clocks")[0].split(" | ")[0] == expected
    assert world.nodes_by_id["agenda"].profile["goal"] == text


def test_digest_renders_unknown_locations(source):
    world = source.runtime.world
    for ref in ("hero", "far-linked"):
        world.states_by_node_id[ref].pop("location_id")
    digest = _build(source)
    assert " | location=unknown | " in _lines(digest, "protagonist")[0]
    actor = next(line for line in _lines(digest, "actors") if line.startswith("far-linked-name |"))
    assert " | location=unknown | " in actor
    assert " | location=unknown | " in _lines(digest, "offscreen_events")[0]


def test_digest_long_english_prose_keeps_core_sections(source, monkeypatch):
    from forwin.planning import world_digest as module

    def hid(kind, index):
        return f"{kind}-{hashlib.sha256(f'{kind}{index}'.encode()).hexdigest()[:20]}"

    goal = "Uncover the truth behind her father's disappearance before the clan tournament and keep her seat in the family council"
    summary = "Slip into the scripture archive at night to search the entry ledgers from three years ago and bribe the elder on guard"
    assert len(goal) == 118
    locations = [hid("loc", i) for i in range(5)]
    actors = [hid("character", "hero"), *[hid("character", i) for i in range(12)]]
    nodes = []
    for i, ref in enumerate(actors):
        nodes.append(_node(ref, location_id=locations[i % 5], alive_status="alive", restraint={"state": "free"},
                           actor_tier="major").model_copy(update={"name": f"Character {i} {'Lin Xuewei' if i % 2 else 'Lu Chengyuan'}"}))
        nodes.append(_node(hid("agenda", i), "agenda", profile={"owner_ref": ref, "goal": goal}, status="active"))
        for j in range(2):
            nodes.append(_node(hid("intent", f"{i}-{j}"), "intent", profile={
                "owner_ref": ref, "action_type": "investigate", "summary": summary,
                "target_refs": [actors[(i + 1) % len(actors)], actors[(i + 2) % len(actors)]],
            }, status="planned", scheduled_at=100000 + i * 600 + j * 1440))
    for i in range(3):
        nodes.append(_node(hid("clock", i), "clock", profile={
            "label": f"Opening of the sect tournament round {i}", "scope": "public",
        }, status="running", due_at=100000 + 3000 * (i + 1)))
        nodes.append(_node(hid("event", i), "event", profile={"location_id": locations[i]}).model_copy(update={
            "created_at_chapter": 4, "metadata": {"source": "world_engine"},
            "summary": "Enforcement hall disciples stopped a night caravan outside the mountain gate and seized all of its cargo",
        }))
    types = ["ally_of", "family_of", "mentor_of", "trusts", "romantic_with", "serves", "allied_with",
             "enemy_of", "owes_debt_to", "rival_of", "distrusts", "opposes"]
    edges = [WorldEdge(id=hid("edge", i), project_id="p", source_id=actors[0], target_id=actors[i + 1],
                       edge_family="loyalty_conflict" if kind in {"serves", "allied_with", "opposes"} else "social",
                       edge_type=kind) for i, kind in enumerate(types)]
    facts = [FactNode(id=hid("fact", i), project_id="p",
                     proposition=f"The sect leader has long known the true origin of the object hidden in secret chamber number {i}",
                     sensitivity_level="secret" if i < 4 else "public") for i in range(30)]
    source.runtime.world = ObjectiveWorldGraph(nodes=nodes, edges=edges, facts=facts)
    source.runtime.map = MapGraph(
        nodes=[MapNode(id=ref, project_id="p", node_type="site", name=f"Mountain Gate Outpost {i}")
               for i, ref in enumerate(locations)],
        edges=[MapEdge(id=hid("road", i), project_id="p", from_node_id=locations[0], to_node_id=locations[i],
                       edge_type="road", bidirectional=True, travel_time=i) for i in range(1, 5)],
    )
    source.runtime.cognition_by_observer[("reader", "reader")].knowledge_by_ref = {f"fact:{facts[5].id}": "knows"}
    source.reveals = {facts[i].id: 3 + i for i in range(4)}
    source.news = [NewsItem(fact_ref=f"fact:{facts[8 + i].id}", observer_ref=actors[0], arrive_at=100100 + i) for i in range(3)]
    monkeypatch.setattr(module.StoryCalendarRepository, "get", lambda *args: StoryCalendar(
        "Year of Origin", 0, (CalendarUnit("d", 1440), CalendarUnit("h", 60))))
    digest = module.build_world_digest(object(), project_id="p", as_of_chapter=5, protagonist_ref=actors[0],
                                       window_end_minute=101000, budget_chars=8000, runtime=source.runtime)
    assert len(digest.text) <= 8000
    assert [section.key for section in digest.sections[:4]] == KEYS[:4]
    assert 1 <= len(_lines(digest, "actors")) <= 8
    assert len(_lines(digest, "news_in_transit")) == len(_lines(digest, "offscreen_events")) == 3


def test_digest_lists_world_conditions(source):
    from tests.test_world_drift import variable

    nodes = [variable(), variable("secret")]
    nodes[1].profile.update(label="secret-condition", visibility="secret")
    for node in nodes:
        source.runtime.world.nodes_by_id[node.id] = node
        source.runtime.world.states_by_node_id[node.id] = {**node.state, "level": 2}
    digest = _build(source)
    assert _lines(digest, "world_conditions") == [
        "condition-label | level=high", "secret-condition | level=high",
    ]


def test_digest_keeps_a_short_companion_when_the_top_actor_cannot_fit(source):
    from forwin.planning.world_digest import DigestSection, render_digest_sections

    full = _build(source)
    hero = full.sections[0]
    clock = next(section for section in full.sections if section.key == "clocks")
    cap = len(render_digest_sections([DigestSection(hero.key, hero.title, hero.lines[:1]), clock]))
    trimmed = _build(source, budget=cap)
    assert len(trimmed.text) <= cap
    assert trimmed.sections[0].key == "protagonist"
    assert len([section for section in trimmed.sections if section.lines]) >= 2
