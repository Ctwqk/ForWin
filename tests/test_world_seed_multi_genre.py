"""Three genres with different calendars, travel modes and rank ladders take one world-seed path.

Each ``tests/fixtures/world_seed/<genre>.json`` is a complete Genesis pack (design §7.1). The
tests run it through seed validation, the real start-writing handoff and the chapter-0
BookState reads; nothing in the runtime may depend on which genre the book is.
"""

from __future__ import annotations

import json
from collections import Counter
from contextlib import ExitStack
from copy import deepcopy
from functools import wraps
from itertools import combinations
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from sqlalchemy import func, select

from forwin.book_state import BookStateProjection
from forwin.book_state.book_frame_repository import BookLexiconRepository, StoryCalendarRepository
from forwin.book_state.lexicon import lexicon_from_payload
from forwin.book_state.query import BookStateQuery
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.book_state.rule_predicates import RuleRefScope, parse_checkable
from forwin.book_state.schema import ACTOR_NODE_TYPES
from forwin.book_state.story_clock import calendar_from_payload, format_story_time
from forwin.canon import CanonAdmissionService
from forwin.genesis import GENESIS_STAGE_ORDER, BookGenesisService
from forwin.genesis.handoff.commands import StartWritingCommand
from forwin.genesis.handoff.world_seed_committer import (
    build_world_seed_deltas,
    map_node_ids_by_genesis_ref,
    seed_ref_id,
    world_seed_delta_ids,
)
from forwin.genesis.world_seed import story_engine_actor_refs, validate_world_seed
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_frame import BookLexiconRow, StoryCalendarRow
from forwin.models.book_state import GraphDeltaRow
from forwin.models.project import Project
from forwin.protocol.book_state import ApprovedGraphDeltaSet, GraphDelta, NodePatch
from forwin.runtime.policy import RuntimePolicy
from forwin.runtime.policy_store import ProjectPolicyStore
from forwin.state.updater import StateUpdater
from tests.postgres import postgres_test_url


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "world_seed"
GENRES = ("xianxia", "interstellar", "urban_mystery")
KINSHIP_EDGE_TYPES = ("kin_of", "family_of", "master_of", "mentor_of")
HIDDEN_SENSITIVITIES = ("hidden", "secret")
KNOWER_STATES = ("knows", "believes", "suspects")
FALSE_BELIEF_STATES = ("knows", "believes")
# The last review is the gate the arc-1 beat sheet's protections pass.
SHARED_PIPELINE = ["validate_world_seed", "build_world_seed_deltas", "review", "commit_world_seed", "review"]


def load_fixture(genre: str) -> dict[str, Any]:
    return json.loads((FIXTURE_DIR / f"{genre}.json").read_text(encoding="utf-8"))


def arc_numbers(pack: dict[str, Any]) -> set[int]:
    return {arc["arc_number"] for arc in pack["book_arc_blueprint"]["arcs"]}


def entity_names(story_engine: dict[str, Any]) -> dict[str, str]:
    return {
        item["id"]: item["name"]
        for key in ("core_cast", "opposition", "factions")
        for item in story_engine[key]
    }


class _NoApiKeyClient:
    api_key = ""
    codex_enabled = False
    profile_id = ""
    profile_name = ""
    model = "fake-model"
    base_url = ""
    last_call_result = None

    def chat(self, *_args, **_kwargs):  # pragma: no cover - start-writing makes no Genesis call
        raise AssertionError("the multi-genre tests must not call a real LLM")


@pytest.fixture
def session_factory():
    engine = get_engine(postgres_test_url("world-seed-multi-genre"))
    init_db(engine)
    yield get_session_factory(engine)
    engine.dispose()


def create_ready_project(session, service: BookGenesisService, fixture: dict[str, Any], project_id: str) -> Project:
    """A project whose Genesis stages are all locked, as the UI leaves it before start-writing."""
    updater = StateUpdater(session)
    project = Project(id=project_id, **fixture["project"])
    session.add(project)
    session.flush()
    revision = service.create_initial_revision(session=session, updater=updater, project=project)
    revision = service.patch_pack(
        session=session, updater=updater, project=project, revision=revision,
        patch=deepcopy(fixture["pack"]),
    )
    for stage_key in GENESIS_STAGE_ORDER:
        revision = service.lock_stage(
            session=session, updater=updater, project=project, revision=revision, stage_key=stage_key,
        )
    session.commit()
    assert project.creation_status == "genesis_ready"
    return project


def start_writing(session, service: BookGenesisService, project_id: str):
    # A project from project_create carries a runtime policy, which the arc-1 beat sheet reads.
    ProjectPolicyStore(session).initialize(session.get(Project, project_id), RuntimePolicy.for_profile("standard"))
    result = service.handoff.start_writing(
        session=session, updater=StateUpdater(session),
        command=StartWritingCommand(project_id=project_id, actor_type="manual_ui"),
    )
    session.commit()
    return result


def seed_counts(seed: dict[str, Any]) -> dict[str, int]:
    return {
        "actor_count": len(seed["actors"]),
        "agenda_count": sum(len(actor.get("agendas", [])) for actor in seed["actors"]),
        "intent_count": sum(len(actor.get("intents", [])) for actor in seed["actors"]),
        "clock_count": len(seed["clocks"]),
        "fact_count": len(seed["facts"]),
        "knowledge_count": len(seed["knowledge"]),
        "rule_count": len(seed["rules"]),
        "promise_count": len(seed["reader_promises"]),
        "world_variable_count": len(seed["world_variables"]),
    }


# --- the fixtures themselves ---------------------------------------------------------------


@pytest.mark.parametrize("genre", GENRES)
def test_fixture_world_seed_is_valid(genre: str) -> None:
    pack = load_fixture(genre)["pack"]

    issues = validate_world_seed(
        pack["world_seed"],
        story_engine=pack["world"]["story_engine"],
        map_atlas=pack["world"]["map_atlas"],
        arc_numbers=arc_numbers(pack),
        world_profiles={item["id"]: kind
                        for key, kind in (("institution_profiles", "institution"), ("resource_economy_profiles", "resource_economy"))
                        for item in pack["world"].get(key, [])},
    )

    assert [issue for issue in issues if issue.severity == "error"] == []


@pytest.mark.parametrize("genre", GENRES)
def test_fixture_covers_the_required_seed_shapes(genre: str) -> None:
    pack = load_fixture(genre)["pack"]
    seed = pack["world_seed"]
    kinds = story_engine_actor_refs(pack["world"]["story_engine"])
    facts = {fact["id"]: fact for fact in seed["facts"]}
    major_actors = [actor for actor in seed["actors"] if actor["actor_tier"] == "major"]

    assert seed["world_variables"]
    assert len(seed["actors"]) >= 3 and "faction" in kinds.values()
    assert major_actors and all(actor["agendas"] for actor in major_actors)
    assert sum(len(actor["intents"]) for actor in seed["actors"]) >= 2
    assert any(
        fact["sensitivity"] in HIDDEN_SENSITIVITIES
        and any(item["fact_id"] == fact["id"] and item["state"] in KNOWER_STATES for item in seed["knowledge"])
        for fact in seed["facts"]
    )
    assert any(
        item["state"] in FALSE_BELIEF_STATES and not facts[item["fact_id"]]["truth_value"]
        for item in seed["knowledge"]
    )
    assert seed["clocks"] and seed["reader_promises"]
    assert any(
        relation["edge_type"] in KINSHIP_EDGE_TYPES and relation["appellation"]
        and kinds[relation["source"]] == kinds[relation["target"]] == "character"
        for relation in seed["relations"]
    )


def test_genres_differ_in_calendar_travel_and_rank_vocabulary() -> None:
    seeds = [load_fixture(genre)["pack"]["world_seed"] for genre in GENRES]
    vocabularies = {
        "calendar units": [{unit["name"] for unit in seed["calendar"]["units"]} for seed in seeds],
        "travel modes": [{mode["id"] for mode in seed["lexicon"]["travel_modes"]} for seed in seeds],
        "rank ladders": [{ladder["id"] for ladder in seed["lexicon"]["rank_ladders"]} for seed in seeds],
    }

    for label, names in vocabularies.items():
        assert all(not (first & second) for first, second in combinations(names, 2)), label


@pytest.mark.parametrize("genre", GENRES)
def test_fixture_predicates_use_book_state_refs(genre: str) -> None:
    pack = load_fixture(genre)["pack"]
    seed = pack["world_seed"]
    project_id = f"proj-predicates-{genre}"
    map_ids = {node["id"]: f"map-{node['id']}" for node in pack["world"]["map_atlas"]["nodes"]}
    lexicon = lexicon_from_payload(seed["lexicon"])
    world, _, _ = build_world_seed_deltas(
        project_id=project_id, revision_id="revision", seed=seed,
        story_engine=pack["world"]["story_engine"], map_node_ids=map_ids,
        calendar=calendar_from_payload(seed["calendar"]), lexicon=lexicon,
    )
    scope = RuleRefScope(
        locations=frozenset(map_ids.values()),
        actors=frozenset(p.node_id for p in world.node_patches if p.node_type in ACTOR_NODE_TYPES),
        facts=frozenset(f"fact:{p.fact_id}" for p in world.fact_patches),
        modes=frozenset(mode.id for mode in lexicon.travel_modes),
    )
    for patch in world.node_patches:
        if patch.node_type == "rule" and (checkable := patch.new_value["profile"]["checkable"]) is not None:
            predicate, issues = parse_checkable(checkable, scope=scope)
            assert predicate is not None and issues == [], (patch.node_id, issues)


# --- start-writing into chapter-0 BookState -----------------------------------------------------


@pytest.mark.parametrize("genre", GENRES)
def test_fixture_flows_from_genesis_to_chapter_zero_book_state(genre: str, session_factory) -> None:
    fixture = load_fixture(genre)
    seed = fixture["pack"]["world_seed"]
    story_engine = fixture["pack"]["world"]["story_engine"]
    actor_kinds = story_engine_actor_refs(story_engine)
    names = entity_names(story_engine)
    project_id = f"proj-multi-genre-{genre}"
    calendar = calendar_from_payload(seed["calendar"])
    opening = calendar.opening_minute
    service = BookGenesisService(llm_client=_NoApiKeyClient())

    with session_factory() as session:
        create_ready_project(session, service, fixture, project_id)
        result = start_writing(session, service, project_id)

        first_arc = fixture["pack"]["book_arc_blueprint"]["arcs"][0]
        assert result.project_status == "writing"
        assert result.active_arc_chapter_count == first_arc["chapter_end"] - first_arc["chapter_start"] + 1
        assert result.world_seed_summary["skipped"] is False
        assert {key: result.world_seed_summary[key] for key in seed_counts(seed)} == seed_counts(seed)

        query = BookStateQuery(session)
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        node_types = Counter(str(node.node_type) for node in runtime.world.nodes_by_id.values())
        counts = seed_counts(seed)
        assert node_types["world_variable"] == counts["world_variable_count"]

        # Every seed section arrives in chapter-0 BookState with the fixture's own cardinality.
        assert sum(node_types[kind] for kind in ACTOR_NODE_TYPES) == counts["actor_count"]
        assert node_types["agenda"] == counts["agenda_count"]
        assert node_types["intent"] == counts["intent_count"]
        assert node_types["clock"] == counts["clock_count"]
        assert node_types["rule"] == counts["rule_count"]
        assert len(runtime.world.facts_by_id) == counts["fact_count"]
        assert len(runtime.world.edges_by_id) == len(seed["relations"])
        assert sum(len(view.knowledge_by_ref) for view in runtime.cognition_by_observer.values()) == counts["knowledge_count"]
        assert len(query.reader_promises(project_id, as_of_chapter=0)) == counts["promise_count"]

        # Actors stand on the book map nodes their Genesis location refs name.
        map_node_ids = map_node_ids_by_genesis_ref(session, project_id)
        for actor in seed["actors"]:
            node = runtime.world.nodes_by_id[seed_ref_id(project_id, actor_kinds[actor["ref"]], actor["ref"])]
            assert node.name == names[actor["ref"]]
            assert runtime.world.get_state(node.id)["location_id"] == map_node_ids[actor["location_ref"]]

        # Secret facts have a knower; whoever holds a false fact as true is recorded as misled.
        fact_nodes = {fact["id"]: runtime.world.facts_by_id[seed_ref_id(project_id, "fact", fact["id"])] for fact in seed["facts"]}
        views = list(runtime.cognition_by_observer.values())
        for fact in seed["facts"]:
            if fact["sensitivity"] in HIDDEN_SENSITIVITIES and not fact["nobody_knows"]:
                assert any(view.knowledge_state(fact_nodes[fact["id"]]) in KNOWER_STATES for view in views), fact["id"]
        false_refs = {f"fact:{fact_nodes[fact['id']].id}" for fact in seed["facts"] if not fact["truth_value"]}
        expected_misled = {
            seed_ref_id(project_id, actor_kinds[item["observer_ref"]], item["observer_ref"])
            for item in seed["knowledge"]
            if item["state"] in FALSE_BELIEF_STATES and f"fact:{fact_nodes[item['fact_id']].id}" in false_refs
        }
        assert expected_misled
        misled = {view.observer_id: view.false_beliefs(runtime.world.facts_by_id) for view in views}
        assert {observer_id for observer_id, refs in misled.items() if refs} == expected_misled
        assert all(set(refs) <= false_refs for refs in misled.values())

        # The seed deltas carry the opening time in this book's own calendar units.
        label = format_story_time(calendar, opening)
        assert label and all(unit.name in label for unit in calendar.units)
        # The seed's deltas; arc 1's beat sheet adds its protections in one more.
        deltas = session.execute(select(GraphDeltaRow).where(
            GraphDeltaRow.project_id == project_id, GraphDeltaRow.source_type != "plan_constraint",
        )).scalars().all()
        assert sorted(delta.id for delta in deltas) == sorted(world_seed_delta_ids(project_id))
        assert {(delta.story_time, delta.story_time_start, delta.story_time_end) for delta in deltas} == {(label, opening, opening)}
        assert query.current_story_minute(project_id, as_of_chapter=0) == opening

        # Kinship appellations become canon name anchors.
        expected_anchors = sorted(
            (f"{names[relation['source']]}的{relation['appellation']}", names[relation["target"]])
            for relation in seed["relations"]
            if relation["edge_type"] in KINSHIP_EDGE_TYPES and relation["appellation"]
            and relation["visibility"] != "secret"
        )
        anchors = query.canon_name_anchors(project_id, as_of_chapter=0)
        assert anchors
        assert sorted((anchor.role_label, anchor.canonical_name) for anchor in anchors) == expected_anchors

        # One calendar and one lexicon per book, holding this book's vocabulary.
        for model in (StoryCalendarRow, BookLexiconRow):
            assert session.scalar(select(func.count(model.id)).where(model.project_id == project_id)) == 1
        assert StoryCalendarRepository(session).get(project_id) == calendar
        assert BookLexiconRepository(session).get(project_id) == lexicon_from_payload(seed["lexicon"])


def test_handoff_seed_entry_rule_is_enforced_with_book_state_ids(session_factory) -> None:
    fixture = load_fixture("urban_mystery")
    project_id = "proj-handoff-entry-rule"
    service = BookGenesisService(llm_client=_NoApiKeyClient())

    with session_factory() as session:
        create_ready_project(session, service, fixture, project_id)
        start_writing(session, service, project_id)
        runtime = BookStateProjection(session).load_runtime_as_of(project_id, as_of_chapter=0)
        map_ids = map_node_ids_by_genesis_ref(session, project_id)
        rule_id = seed_ref_id(project_id, "rule", "rule-precinct-entry")
        assert runtime.world.get_node(rule_id).profile["checkable"] == {
            "kind": "actor_cannot_enter", "location_ref": map_ids["node-precinct"],
            "include_children": True,
            "except_refs": [seed_ref_id(project_id, "character", "cast-archivist")],
            "except_member_of": [seed_ref_id(project_id, "faction", "faction-precinct")],
        }

        for actor_ref, allowed in (("opp-director", False), ("cast-archivist", True), ("cast-inspector", True)):
            actor_id = seed_ref_id(project_id, "character", actor_ref)
            start_id = runtime.world.get_state(actor_id)["location_id"]
            station_id, precinct_id = map_ids["node-station"], map_ids["node-precinct"]
            assert start_id != station_id
            delta = GraphDelta(
                id=f"entry-{actor_id}", project_id=project_id, chapter_number=1, source_type="chapter_extraction",
                node_patches=[
                    NodePatch(node_id=actor_id, node_type="character", op="set", field_path="state.location_id",
                              old_value=start_id, new_value=station_id),
                    NodePatch(node_id=actor_id, node_type="character", op="set", field_path="state.location_id",
                              old_value=station_id, new_value=precinct_id),
                ],
            )
            verdict = BookStateReviewGate(session).review(
                ApprovedGraphDeltaSet(project_id=project_id, chapter_number=1, graph_deltas=[delta]),
                base_chapter=0,
            )
            assert verdict.accepted is allowed, verdict.issues
            assert [(issue.severity, issue.code, issue.target_ref) for issue in verdict.issues] == (
                [] if allowed else [("error", "rule_violated", f"rule:{rule_id}")]
            )


def spy(label: str, real, calls: list[str]):
    @wraps(real)
    def wrapper(*args, **kwargs):
        calls.append(label)
        return real(*args, **kwargs)

    return wrapper


def test_three_genres_share_one_code_path(session_factory) -> None:
    service = BookGenesisService(llm_client=_NoApiKeyClient())
    sequences: dict[str, list[str]] = {}

    for genre in GENRES:
        fixture = load_fixture(genre)
        project_id = f"proj-code-path-{genre}"
        calls: list[str] = []
        with session_factory() as session:
            create_ready_project(session, service, fixture, project_id)
            # Spies go on after setup: lock-time validation is a different call site.
            with ExitStack() as spies:
                spies.enter_context(patch(
                    "forwin.genesis.helpers.validate_world_seed",
                    spy("validate_world_seed", validate_world_seed, calls),
                ))
                spies.enter_context(patch(
                    "forwin.genesis.handoff.world_seed_committer.build_world_seed_deltas",
                    spy("build_world_seed_deltas", build_world_seed_deltas, calls),
                ))
                spies.enter_context(patch.object(
                    BookStateReviewGate, "review", spy("review", BookStateReviewGate.review, calls),
                ))
                spies.enter_context(patch.object(
                    CanonAdmissionService, "commit_world_seed",
                    spy("commit_world_seed", CanonAdmissionService.commit_world_seed, calls),
                ))
                start_writing(session, service, project_id)
        sequences[genre] = calls

    assert sequences["xianxia"] == SHARED_PIPELINE
    assert sequences == {genre: SHARED_PIPELINE for genre in GENRES}


def test_secret_kinship_relation_never_becomes_a_name_anchor(session_factory) -> None:
    fixture = load_fixture("interstellar")
    relations = fixture["pack"]["world_seed"]["relations"]
    kin = next(relation for relation in relations if relation["edge_type"] in KINSHIP_EDGE_TYPES and relation["appellation"])
    kin["visibility"] = "secret"
    names = entity_names(fixture["pack"]["world"]["story_engine"])
    project_id = "proj-secret-kin"
    service = BookGenesisService(llm_client=_NoApiKeyClient())

    with session_factory() as session:
        create_ready_project(session, service, fixture, project_id)
        start_writing(session, service, project_id)
        anchors = BookStateQuery(session).canon_name_anchors(project_id, as_of_chapter=0)

    secret_anchor = (f"{names[kin['source']]}的{kin['appellation']}", names[kin["target"]])
    assert secret_anchor not in [(anchor.role_label, anchor.canonical_name) for anchor in anchors]
