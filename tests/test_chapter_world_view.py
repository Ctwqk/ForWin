from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.knowledge import HIDDEN_SENSITIVITY
from forwin.book_state.lexicon import BookLexicon, TravelMode
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.book_state.story_clock import CalendarUnit, StoryCalendar
from forwin.models.project import ChapterPlan
from forwin.protocol.book_state import CognitionOverlay, FactNode, MapEdge, MapNode, WorldNode
from forwin.writer.prompt_core.world_view import forbidden_topic_text


CHAPTER = 6
NOW = 1500
CALENDAR = StoryCalendar("epoch", 0, (CalendarUnit("day", 1440), CalendarUnit("hour", 60)))
# fact id -> (proposition, sensitivity, truth_value, related refs, created_at_chapter)
FACTS = {
    "pov-secret": ("pov-secret-truth", "secret", "true", ["villain"], 3),
    "pov-lie": ("pov-lie-text", "public", "false", [], 2),
    "pov-heard": ("pov-heard-text", "public", "true", [], 1),
    "pov-disbelieved": ("pov-disbelieved-truth", "secret", "true", ["hero"], 0),
    "public-unrecorded": ("public-unrecorded-text", "public", "true", ["ally"], 0),
    "forbidden": ("forbidden-truth", "secret", "true", ["ally", "villain", "poisoning", "reader", "missing"], 0),
    "forbidden-nameless": ("nameless-truth", "hidden", "true", ["poisoning"], 0),
    "reader-secret": ("reader-secret-truth", "secret", "true", ["ally"], 0),
    "due-secret": ("due-truth", "secret", "true", ["ally"], 0),
    "due-pov": ("due-pov-truth", "secret", "true", [], 0),
    "revealed": ("truth-of-revealed", "secret", "true", ["villain"], 0),
    "pov-revealed": ("truth-of-pov-revealed", "secret", "true", [], 0),
    "actor-revealed": ("truth-of-actor-revealed", "must_not_reveal", "true", [], 0),
    "hinted": ("hinted-truth", "secret", "true", ["ally"], 0),
    "reader-other": ("reader-other-text", "public", "true", ["villain"], 0),
    "reader-place": ("reader-place-text", "public", "true", ["chapter-place"], 0),
    "reader-suspected": ("reader-suspected-text", "public", "true", ["ally"], 0),
}
POV_KNOWLEDGE = {
    "pov-secret": "knows", "pov-lie": "believes", "pov-heard": "heard", "pov-disbelieved": "disbelieves",
    "reader-secret": "knows", "due-pov": "knows", "pov-revealed": "knows",
}
READER_KNOWLEDGE = {
    "forbidden": "suspects", "reader-secret": "knows", "reader-other": "knows", "reader-place": "believes",
    "reader-suspected": "suspects",
}
SCHEDULE = {"pov-secret": 9, "forbidden-nameless": 8, "due-secret": 6, "due-pov": 6, "revealed": 9, "pov-revealed": 9}
REVEALS = [
    {"id": "d-reveal", "fact_ref": "fact:revealed", "observer": "reader", "target_state": "knows"},
    {"id": "d-pov", "fact_ref": "fact:pov-revealed", "observer": "reader", "target_state": "suspects"},
    {"id": "d-actor", "fact_ref": "fact:actor-revealed", "observer": "ally", "target_state": "knows"},
    {"id": "d-missing", "fact_ref": "fact:absent", "observer": "reader", "target_state": "knows"},
]
HINTS = [{"id": "d-hint", "fact_ref": "fact:hinted", "hint_text": "hint-value"}]
NEARBY = [f"n{index}" for index in range(10)]


def _node(ref, kind="character", *, profile=None, **state):
    return WorldNode(id=ref, project_id="p", node_type=kind, name=f"{ref}-name", profile=profile or {}, state=state)


def _cognition(observer_type, observer_id, knowledge, **fields):
    return CognitionView(CognitionOverlay(
        id=f"{observer_id}-view", project_id="p", observer_type=observer_type, observer_id=observer_id,
        knowledge_by_ref={f"fact:{ref}": state for ref, state in knowledge.items()}, **fields,
    ))


def _runtime():
    nodes = [
        _node("hero", profile={"public_identity": "hero-identity"}, location_id="home", alive_status="alive",
              health="hero-health", travel_mode_ids=["fast"]),
        _node("ally", profile={"public_identity": "ally-identity", "true_identity_ref": "ally-true"},
              location_id="home", alive_status="alive", health={"wound": "ally-wound"},
              restraint={"state": "restrained", "by_ref": "villain", "since": 0}),
        _node("villain", location_id="n9", alive_status="alive"),
        _node("blade", "item", holder_id="ally"),
        _node("vial", "item", holder_id="ally", visibility_level="secret"),
        _node("charm", "item", holder_id="hero", visibility_level="secret"),
        _node("coin", "resource", holder_id="hero"),
        _node("poisoning", "event"),
        _node("villain-agenda", "agenda", profile={"owner_ref": "villain", "goal": "agenda-goal"},
              status="active", secrecy_refs=["hero"]),
        _node("public-clock", "clock", profile={"label": "public-clock-label", "scope": "public"},
              status="running", due_at=NOW + 120),
        _node("insider-clock", "clock", profile={"label": "insider-clock-label", "owner_ref": "villain",
                                                 "scope": "secret"}, status="running", due_at=NOW + 1500),
        _node("own-clock", "clock", profile={"label": "own-clock-label", "owner_ref": "hero"}, status="running"),
        _node("outsider-clock", "clock", profile={"label": "outsider-clock-label", "owner_ref": "ally",
                                                  "scope": "secret"}, status="running", due_at=NOW + 60),
        _node("expired-clock", "clock", profile={"label": "expired-clock-label", "scope": "public"},
              status="expired", due_at=NOW - 60),
        _node("overdue-clock", "clock", profile={"label": "overdue-clock-label", "scope": "public"},
              status="running", due_at=NOW - 30),
    ]
    facts = [
        FactNode(id=ref, project_id="p", proposition=proposition, sensitivity_level=sensitivity,
                 truth_value=truth, related_node_refs=related, created_at_chapter=created)
        for ref, (proposition, sensitivity, truth, related, created) in FACTS.items()
    ]
    places = ["home", "chapter-place", "den", "isolated", *NEARBY]
    edges = [
        MapEdge(id=f"road-{ref}", project_id="p", from_node_id="home", to_node_id=ref, edge_type="road",
                bidirectional=True, travel_time=index + 1)
        for index, ref in enumerate(NEARBY)
    ]
    edges.append(MapEdge(id="road-chapter-place", project_id="p", from_node_id="chapter-place", to_node_id="home",
                         edge_type="road", bidirectional=True, travel_time=12))
    edges.append(MapEdge(id="hidden-den", project_id="p", from_node_id="home", to_node_id="den",
                         edge_type="hidden_route", bidirectional=True, travel_time=0.5,
                         discovered_by_default=False))
    return BookStateRuntime(
        project_id="p", as_of_chapter=CHAPTER - 1,
        world=ObjectiveWorldGraph(nodes=nodes, facts=facts),
        map_graph=MapGraph(
            nodes=[MapNode(id=ref, project_id="p", node_type="site", name=f"{ref}-name") for ref in places],
            edges=edges,
        ),
        cognition_by_observer={
            ("reader", "reader"): _cognition("reader", "reader", READER_KNOWLEDGE),
            # The hero knows the hidden route, which the writer still never sees.
            ("character", "hero"): _cognition("character", "hero", POV_KNOWLEDGE,
                                              visible_refs=["map_edge:hidden-den"]),
        },
    )


def _plan(**overrides):
    fields = {
        "project_id": "p", "arc_plan_id": "arc", "chapter_number": CHAPTER, "pov_ref": "hero",
        "onstage_refs_json": json.dumps(["hero", "ally", "ghost"]),
        "location_refs_json": json.dumps(["chapter-place"]),
        "reveal_directives_json": json.dumps(REVEALS),
        "hint_directives_json": json.dumps(HINTS),
        "expected_span_minutes": 180,
    }
    return ChapterPlan(**{**fields, **overrides})


@pytest.fixture
def source(monkeypatch):
    from forwin.world_view import builder as module

    runtime = _runtime()
    data = SimpleNamespace(runtime=runtime, now=NOW, schedule=dict(SCHEDULE), calls=[], lexicon=BookLexicon(
        travel_modes=(TravelMode(id="fast", name="fast-name", speed_multiplier=2, allowed_edge_types=("road",)),),
    ))
    monkeypatch.setattr(module.BookLexiconRepository, "get", lambda self, project_id: data.lexicon)
    monkeypatch.setattr(module.StoryCalendarRepository, "get", lambda self, project_id: CALENDAR)
    monkeypatch.setattr(module.BookStateQuery, "current_story_minute",
                        lambda self, project_id, *, as_of_chapter: data.now)
    monkeypatch.setattr(module, "earliest_reveal_chapters", lambda session, project_id: data.schedule)

    def load(self, project_id, *, as_of_chapter, **kwargs):
        data.calls.append((project_id, as_of_chapter))
        return runtime

    monkeypatch.setattr(module.BookStateProjection, "load_runtime_as_of", load)
    return data


def _build(source, *, budget=100_000, supplied=True, plan=None):
    from forwin.world_view.builder import build_chapter_world_view
    return build_chapter_world_view(object(), project_id="p", chapter_plan=plan or _plan(), budget_chars=budget,
                                    runtime=source.runtime if supplied else None)


def _texts(view):
    from forwin.world_view.builder import render_view_text
    return view.model_dump_json(), render_view_text(view)


def test_view_lists_onstage_perceptible_state(source):
    from forwin.world_view.types import OnstageEntity, ViewLocation

    view = _build(source, supplied=False)
    assert source.calls == [("p", CHAPTER - 1)]
    assert (view.pov_ref, view.pov_name) == ("hero", "hero-name")
    assert view.pov_location == ViewLocation(id="home", name="home-name", minutes_from_pov=0)
    assert view.onstage == [
        # The POV carries its own concealed item; an unknown onstage ref is dropped.
        OnstageEntity(ref="hero", name="hero-name", location_name="home-name", alive_status="alive",
                      health="hero-health", held_items=["charm-name", "coin-name"],
                      public_identity="hero-identity"),
        OnstageEntity(ref="ally", name="ally-name", location_name="home-name", alive_status="alive",
                      restraint="restrained", health='{"wound":"ally-wound"}', held_items=["blade-name"],
                      public_identity="ally-identity"),
    ]
    for text in _texts(view):
        assert "vial-name" not in text and "ally-true" not in text and "ghost" not in text


def test_view_lists_pov_clocks_and_story_time(source):
    view = _build(source)
    assert view.now_label == "epoch 1day1hour"
    assert view.expected_span_label == "3hour"
    assert [(clock.label, clock.remaining_label) for clock in view.pov_clocks] == [
        ("overdue-clock-label", "0hour"),
        ("public-clock-label", "2hour"),
        ("insider-clock-label", "1day1hour"),
        ("own-clock-label", ""),
    ]
    source.now = None
    view = _build(source, plan=_plan(expected_span_minutes=None))
    assert view.now_label == "epoch 0hour"
    assert view.expected_span_label == ""
    # Without the POV in the owner's insiders only public clocks remain.
    source.runtime.world.states_by_node_id["villain-agenda"]["secrecy_refs"] = []
    labels = [clock.label for clock in _build(source).pov_clocks]
    assert "insider-clock-label" not in labels and "own-clock-label" in labels


def test_view_marks_false_belief_and_pov_only_secret(source):
    from forwin.world_view.types import KnownFact

    view = _build(source)
    facts = {fact.fact_ref: fact for fact in view.pov_knowledge}
    # Recorded held states only: a disbelieved secret and unrecorded public facts stay out.
    assert list(facts) == [
        "fact:pov-secret", "fact:pov-lie", "fact:pov-heard", "fact:due-pov", "fact:pov-revealed",
        "fact:reader-secret",
    ]
    assert facts["fact:pov-secret"] == KnownFact(
        fact_ref="fact:pov-secret", proposition="pov-secret-truth", state="knows",
        pov_believes_false=False, pov_only_secret=True,
    )
    assert facts["fact:pov-lie"] == KnownFact(
        fact_ref="fact:pov-lie", proposition="pov-lie-text", state="believes",
        pov_believes_false=True, pov_only_secret=False,
    )
    assert facts["fact:pov-heard"].state == "heard"
    # The reader already knows it, its reveal is due, or a reader directive permits it.
    for ref in ("fact:reader-secret", "fact:due-pov", "fact:pov-revealed", "fact:pov-heard"):
        assert facts[ref].pov_only_secret is False
        assert facts[ref].pov_believes_false is False
    # A POV without a cognition overlay holds no recorded knowledge.
    del source.runtime.cognition_by_observer[("character", "hero")]
    assert _build(source).pov_knowledge == []


def test_forbidden_topic_has_no_truth_text(source):
    from forwin.world_view.types import ForbiddenTopic

    view = _build(source)
    assert view.forbidden_topics == [
        # Only entity names stand for the subjects; event and unknown refs do not, nor does the reader.
        ForbiddenTopic(fact_ref="fact:forbidden", topic=forbidden_topic_text(["ally-name", "villain-name"])),
        ForbiddenTopic(fact_ref="fact:forbidden-nameless", topic=forbidden_topic_text(["forbidden-nameless"])),
        ForbiddenTopic(fact_ref="fact:hinted", topic=forbidden_topic_text(["ally-name"])),
        ForbiddenTopic(fact_ref="fact:pov-disbelieved", topic=forbidden_topic_text(["hero-name"])),
    ]
    for text in _texts(view):
        for truth in ("forbidden-truth", "nameless-truth", "hinted-truth", "pov-disbelieved-truth", "due-truth",
                      "poisoning-name"):
            assert truth not in text
    # Without the POV's knowledge its secrets become topics too, still without their truth.
    del source.runtime.cognition_by_observer[("character", "hero")]
    view = _build(source)
    assert {"fact:pov-secret", "fact:pov-disbelieved"} <= {topic.fact_ref for topic in view.forbidden_topics}
    assert all("pov-secret-truth" not in text for text in _texts(view))


def test_reveal_directive_gives_truth_for_that_fact_only(source, caplog):
    from forwin.world_view.types import AllowedReveal, ViewHint

    view = _build(source)
    assert view.allowed_reveals == [
        AllowedReveal(directive_id="d-reveal", fact_ref="fact:revealed", proposition="truth-of-revealed",
                      observer="reader", target_state="knows"),
        AllowedReveal(directive_id="d-pov", fact_ref="fact:pov-revealed", proposition="truth-of-pov-revealed",
                      observer="reader", target_state="suspects"),
        AllowedReveal(directive_id="d-actor", fact_ref="fact:actor-revealed", proposition="truth-of-actor-revealed",
                      observer="ally", target_state="knows"),
    ]
    assert any("d-missing" in record.message for record in caplog.records)
    assert view.hints == [ViewHint(directive_id="d-hint", hint_text="hint-value")]
    # Every secret whose truth the view carries is held by the POV, known to the reader or permitted.
    permitted = {"revealed", "pov-revealed", "actor-revealed"}
    held = {ref for ref, state in POV_KNOWLEDGE.items() if state != "disbelieves"}
    reader_known = {ref for ref, state in READER_KNOWLEDGE.items() if state in {"knows", "believes"}}
    secrets = {
        ref for ref, (_, sensitivity, truth, _, _) in FACTS.items()
        if truth == "true" and (sensitivity in HIDDEN_SENSITIVITY or ref in SCHEDULE)
    }
    for text in _texts(view):
        carried = {ref for ref in secrets if FACTS[ref][0] in text}
        assert carried == (permitted | held | reader_known) & secrets
    # A hint that states its fact's truth is dropped.
    leaky = {"id": "d-leak", "fact_ref": "fact:hinted", "hint_text": "so\n hinted-truth  indeed"}
    view = _build(source, plan=_plan(hint_directives_json=json.dumps([*HINTS, leaky])))
    assert [hint.directive_id for hint in view.hints] == ["d-hint"]
    assert all("hinted-truth" not in text for text in _texts(view))
    # The directive is what permits it: without one the fact is a topic.
    view = _build(source, plan=_plan(reveal_directives_json="[]"))
    assert "fact:revealed" in {topic.fact_ref for topic in view.forbidden_topics}
    assert all("truth-of-revealed" not in text for text in _texts(view))


def test_reader_known_uses_recorded_states_only(source, monkeypatch):
    def forbidden(*args):
        pytest.fail("reader knowledge must not default unrecorded public facts to known")

    monkeypatch.setattr(CognitionView, "knowledge_state", forbidden)
    view = _build(source)
    # Recorded knows/believes facts about onstage refs or chapter locations; not suspicions,
    # unrecorded public facts or facts about offstage refs.
    assert view.reader_known == [
        "fact:reader-place: reader-place-text",
        "fact:reader-secret: reader-secret-truth",
    ]
    del source.runtime.cognition_by_observer[("reader", "reader")]
    assert _build(source).reader_known == []


def test_view_locations_cover_chapter_refs_and_nearest_reachable(source):
    from forwin.world_view.builder import VIEW_NEARBY_LOCATIONS
    from forwin.world_view.types import ViewLocation

    assert VIEW_NEARBY_LOCATIONS == 8
    view = _build(source)
    # The POV's travel mode halves road times; the hidden route it knows stays off the writer's map.
    assert view.locations == [
        ViewLocation(id="home", name="home-name", minutes_from_pov=0),
        ViewLocation(id="chapter-place", name="chapter-place-name", minutes_from_pov=360),
        *[ViewLocation(id=f"n{index}", name=f"n{index}-name", minutes_from_pov=30 * (index + 1))
          for index in range(8)],
    ]
    source.runtime.world.states_by_node_id["hero"].pop("location_id")
    view = _build(source)
    assert view.pov_location is None
    assert view.locations == [ViewLocation(id="chapter-place", name="chapter-place-name", minutes_from_pov=None)]


def test_view_map_excludes_hidden_edges_for_pov_without_overlay():
    from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
    from forwin.book_state.projection import BookStateProjection
    from forwin.book_state.story_clock import format_story_time
    from forwin.world_engine.travel import estimate_travel
    from forwin.world_view.builder import build_chapter_world_view
    from forwin.world_view.types import ViewLocation
    from tests.test_world_gate_v6 import OPENING, _commit, _delta, _seed_world, _session_factory, _time

    engine, Session = _session_factory()
    try:
        world = _seed_world(Session, reveal_candidates=[{"fact_id": "fact-secret", "arc_number": 2}])
        _commit(Session, world, _delta(Session, world, 1, _time(1, "日")))

        def plan(pov):
            return ChapterPlan(
                project_id=world.project_id, arc_plan_id="arc", chapter_number=2, pov_ref=pov,
                onstage_refs_json=json.dumps([world.rival, world.hero]),
                location_refs_json=json.dumps([world.loc(3)]),
            )

        with Session() as session:
            runtime = BookStateProjection(session).load_runtime_as_of(world.project_id, as_of_chapter=1)
            lexicon = BookLexiconRepository(session).get(world.project_id)
            # The rival has no cognition overlay, so the objective map reaches loc 3 by the hidden route.
            assert estimate_travel(runtime, lexicon, mover_id=world.rival, from_location_id=world.loc(2),
                                   to_location_id=world.loc(3)).minutes == 60
            view = build_chapter_world_view(session, project_id=world.project_id, chapter_plan=plan(world.rival),
                                            budget_chars=6000)
            calendar = StoryCalendarRepository(session).get(world.project_id)
            hero_view = build_chapter_world_view(session, project_id=world.project_id,
                                                 chapter_plan=plan(world.hero), budget_chars=6000)

        assert view.pov_location == ViewLocation(id=world.loc(2), name="site-2", minutes_from_pov=0)
        assert view.locations == [
            ViewLocation(id=world.loc(2), name="site-2", minutes_from_pov=0),
            ViewLocation(id=world.loc(3), name="site-3", minutes_from_pov=None),
            ViewLocation(id=world.loc(1), name="site-1", minutes_from_pov=120),
            ViewLocation(id=world.loc(4), name="site-4", minutes_from_pov=240),
        ]
        assert [location.id for location in hero_view.locations] == [
            world.loc(1), world.loc(3), world.loc(2), world.loc(4),
        ]
        assert view.now_label == format_story_time(calendar, OPENING + 1440)
        # The rival does not know the scheduled secret; the hero does and keeps it from the reader.
        topics = {topic.fact_ref: topic.topic for topic in view.forbidden_topics}
        assert topics[world.fact("fact-secret")] == forbidden_topic_text(["甲"])
        assert topics[world.fact("fact-unscheduled")] == forbidden_topic_text([world.node("fact", "fact-unscheduled")])
        assert all("proposition-secret" not in text for text in _texts(view))
        known = {fact.fact_ref: fact for fact in hero_view.pov_knowledge}
        assert known[world.fact("fact-secret")].pov_only_secret is True
        assert world.fact("fact-secret") not in {topic.fact_ref for topic in hero_view.forbidden_topics}
    finally:
        engine.dispose()


def test_view_trims_to_budget_but_keeps_untrimmable_parts(source, caplog):
    from forwin.world_view.builder import render_view_text

    full = _build(source)
    text = render_view_text(full)
    assert full.trimmed == [] and len(text) <= 100_000

    one_less = _build(source, budget=len(text) - 1)
    assert one_less.trimmed == ["reader_known"]
    assert one_less.reader_known == full.reader_known[:-1]
    assert len(render_view_text(one_less)) <= len(text) - 1

    empty = _build(source, budget=0)
    assert empty.trimmed == ["reader_known", "locations", "pov_knowledge", "held_items"]
    assert empty.reader_known == []
    assert [location.id for location in empty.locations] == ["chapter-place"]
    # Secrets and false beliefs stay; plain knowledge goes.
    assert [fact.fact_ref for fact in empty.pov_knowledge] == [
        "fact:pov-secret", "fact:pov-lie", "fact:due-pov", "fact:pov-revealed", "fact:reader-secret",
    ]
    assert all(entity.held_items == [] for entity in empty.onstage)
    for key in ("allowed_reveals", "hints", "forbidden_topics", "pov_location", "pov_clocks", "now_label",
                "expected_span_label"):
        assert getattr(empty, key) == getattr(full, key)
    assert [entity.ref for entity in empty.onstage] == [entity.ref for entity in full.onstage]
    assert any(record.levelname == "WARNING" and "budget" in record.message for record in caplog.records)

    with pytest.raises(ValueError, match="nonnegative"):
        _build(source, budget=-1)


def test_view_types_load_without_the_context_protocol():
    import subprocess
    import sys

    # protocol.context may then import the view types without an import cycle.
    script = "import sys, forwin.world_view.types; print('forwin.protocol.context' in sys.modules)"
    result = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True, check=True)
    assert result.stdout.strip() == "False"
