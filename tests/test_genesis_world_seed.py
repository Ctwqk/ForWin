from __future__ import annotations

from copy import deepcopy

import pytest

from forwin.genesis.world_seed import (
    WORLD_SEED_KEYS,
    fallback_world_seed,
    normalize_world_seed,
    story_engine_actor_refs,
    validate_world_seed,
)


@pytest.fixture
def story_engine():
    return {
        "core_cast": [{"id": "cast-1", "name": "甲", "current_base": "node-1", "home_location": "node-2"}],
        "opposition": [{"id": "opp-1", "name": "乙", "base_location": "node-2"}],
        "factions": [{"id": "faction-1", "name": "丙", "base_location": "node-1"}],
    }


@pytest.fixture
def map_atlas():
    return {"nodes": [{"id": "node-1", "name": "A"}, {"id": "node-2", "name": "B"}]}


@pytest.fixture
def seed():
    return {
        "calendar": {"epoch_label": "", "opening_minute": 0, "units": [{"name": "日", "minutes": 1440}]},
        "lexicon": {
            "time_unit_aliases": [], "rank_ladders": [], "currencies": [],
            "appellations": [], "generic_character_refs": [],
            "travel_modes": [{"id": "mode-1", "name": "", "speed_multiplier": 1.0, "allowed_edge_types": ["road"]}],
            "news_speed_multiplier": 1.0,
        },
        "actors": [
            {
                "ref": "cast-1", "actor_tier": "major", "location_ref": "node-1",
                "travel_mode_ids": ["mode-1"], "resources": [],
                "agendas": [{
                    "id": "agenda-1", "goal": "", "motivation": "", "priority": 3,
                    "horizon": "", "means_refs": ["faction-1", "fact-1"],
                    "constraints": [], "secrecy_refs": ["opp-1"],
                }],
                "intents": [{
                    "id": "intent-1", "agenda_id": "agenda-1", "action_type": "move",
                    "summary": "", "target_refs": ["opp-1", "fact-1"], "location_ref": "node-2",
                    "scheduled_in": {"amount": 1, "unit": "日"},
                    "requires_onstage": False, "impact_level": "minor",
                }],
            },
            {"ref": "opp-1", "actor_tier": "minor", "location_ref": "node-2", "travel_mode_ids": [], "resources": [], "agendas": [], "intents": []},
            {"ref": "faction-1", "actor_tier": "minor", "location_ref": "node-1", "travel_mode_ids": [], "resources": [], "agendas": [], "intents": []},
        ],
        "relations": [{"source": "cast-1", "target": "faction-1", "edge_type": "member_of", "note": "", "visibility": "public", "appellation": ""}],
        "facts": [{"id": "fact-1", "proposition": "", "truth_value": True, "sensitivity": "secret", "related_refs": ["cast-1"], "nobody_knows": False}],
        "knowledge": [{"observer_ref": "cast-1", "fact_id": "fact-1", "state": "knows", "channel": "seeded"}],
        "rules": [{"id": "rule-1", "statement": "", "hardness": "hard", "checkable": {"kind": "exclusive_relation", "edge_type": "serves", "max_per_source": 1}}],
        "clocks": [{"id": "clock-1", "label": "", "due_in": {"amount": 2, "unit": "日"}, "owner_ref": "faction-1", "scope": "public", "on_expire": ""}],
        "reader_promises": [{"id": "promise-1", "summary": "", "promise_type": "", "expected_payoff_window": ""}],
        "reveal_candidates": [{"fact_id": "fact-1", "arc_number": 1, "note": ""}],
    }


def validate(seed, story_engine, map_atlas):
    return validate_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas, arc_numbers={1, 2})


def test_valid_seed_has_no_issues(seed, story_engine, map_atlas):
    assert validate(seed, story_engine, map_atlas) == []
    assert story_engine_actor_refs(story_engine) == {"cast-1": "character", "opp-1": "character", "faction-1": "faction"}


def test_unbuildable_node_is_unknown_location_at_lock(seed, story_engine, map_atlas):
    from forwin.genesis.helpers import _world_seed_validation_issues

    map_atlas["nodes"].append({"id": "nameless"})
    seed["actors"][0]["location_ref"] = "nameless"
    seed["actors"][0]["intents"][0]["location_ref"] = "nameless"
    pack = {
        "world": {"map_atlas": map_atlas, "story_engine": story_engine},
        "world_seed": seed,
        "book_arc_blueprint": {"arcs": [{"arc_number": 1}, {"arc_number": 2}]},
        "stage_states": {"map": {"locked": True}},
    }
    assert [(issue.code, issue.path) for issue in _world_seed_validation_issues(pack)] == [
        ("unknown_location_ref", "actors[0].location_ref"),
        ("unknown_location_ref", "actors[0].intents[0].location_ref"),
    ]


def test_validate_reports_unknown_time_unit(seed, story_engine, map_atlas):
    seed["actors"][0]["intents"][0]["scheduled_in"]["unit"] = "天"
    issues = validate(seed, story_engine, map_atlas)
    assert [(i.code, i.path, i.severity) for i in issues] == [
        ("unknown_time_unit", "actors[0].intents[0].scheduled_in.unit", "error")
    ]


def test_duration_aliases_and_clock_units(seed, story_engine, map_atlas):
    seed["clocks"][0]["due_in"]["unit"] = "天"
    assert [(i.code, i.path) for i in validate(seed, story_engine, map_atlas)] == [
        ("unknown_time_unit", "clocks[0].due_in.unit")
    ]
    seed["lexicon"]["time_unit_aliases"] = [{"name": "天", "minutes": 1440}]
    assert validate(seed, story_engine, map_atlas) == []


@pytest.mark.parametrize("field,path", [
    ("scheduled_in", "actors[0].intents[0].scheduled_in.amount"),
    ("due_in", "clocks[0].due_in.amount"),
])
@pytest.mark.parametrize("amount", [-1, "-1", float("nan"), float("inf"), "nan", "inf", "garbage", None])
def test_invalid_duration_amount(seed, story_engine, map_atlas, field, path, amount):
    item = seed["actors"][0]["intents"][0] if field == "scheduled_in" else seed["clocks"][0]
    item[field]["amount"] = amount
    normalized = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    for candidate in (seed, normalized):
        assert [(i.code, i.path, i.severity) for i in validate(candidate, story_engine, map_atlas)] == [
            ("invalid_duration_amount", path, "error")
        ]


@pytest.mark.parametrize("field,path", [
    ("scheduled_in", "actors[0].intents[0].scheduled_in.amount"),
    ("due_in", "clocks[0].due_in.amount"),
])
def test_missing_duration_amount(seed, story_engine, map_atlas, field, path):
    item = seed["actors"][0]["intents"][0] if field == "scheduled_in" else seed["clocks"][0]
    item[field].pop("amount")
    normalized = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    for candidate in (seed, normalized):
        assert [(i.code, i.path, i.severity) for i in validate(candidate, story_engine, map_atlas)] == [
            ("invalid_duration_amount", path, "error")
        ]


@pytest.mark.parametrize("amount", [None, "garbage", "nan", "inf"])
def test_normalize_invalid_duration_amount_to_none(seed, story_engine, map_atlas, amount):
    seed["actors"][0]["intents"][0]["scheduled_in"]["amount"] = amount
    seed["clocks"][0]["due_in"]["amount"] = amount
    result = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    assert result["actors"][0]["intents"][0]["scheduled_in"]["amount"] is None
    assert result["clocks"][0]["due_in"]["amount"] is None
    assert normalize_world_seed(result, story_engine=story_engine, map_atlas=map_atlas) == result


def test_normalize_missing_duration_amount_to_none(seed, story_engine, map_atlas):
    seed["actors"][0]["intents"][0]["scheduled_in"].pop("amount")
    seed["clocks"][0]["due_in"].pop("amount")
    result = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    assert result["actors"][0]["intents"][0]["scheduled_in"]["amount"] is None
    assert result["clocks"][0]["due_in"]["amount"] is None


@pytest.mark.parametrize("amount", [0, 0.5, "1.5"])
def test_valid_duration_amount(seed, story_engine, map_atlas, amount):
    seed["actors"][0]["intents"][0]["scheduled_in"]["amount"] = amount
    seed["clocks"][0]["due_in"]["amount"] = amount
    result = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    assert validate(result, story_engine, map_atlas) == []
    assert result["actors"][0]["intents"][0]["scheduled_in"]["amount"] == float(amount)
    assert result["clocks"][0]["due_in"]["amount"] == float(amount)


def test_invalid_duration_amount_is_independent_of_time_dependencies(seed, story_engine, map_atlas):
    seed["calendar"]["opening_minute"] = -1
    seed["lexicon"]["news_speed_multiplier"] = 0
    seed["actors"][0]["intents"][0]["scheduled_in"] = {"amount": -1, "unit": "unknown"}
    seed["clocks"][0]["due_in"] = {"amount": "garbage", "unit": "unknown"}
    assert [(i.code, i.path) for i in validate(seed, story_engine, map_atlas)] == [
        ("invalid_calendar", "calendar"),
        ("invalid_lexicon", "lexicon"),
        ("invalid_duration_amount", "actors[0].intents[0].scheduled_in.amount"),
        ("invalid_duration_amount", "clocks[0].due_in.amount"),
    ]


def test_missing_and_unknown_actor_refs(seed, story_engine, map_atlas):
    seed["actors"][1]["ref"] = "cast-9"
    issues = validate(seed, story_engine, map_atlas)
    assert {(i.code, i.path) for i in issues} == {
        ("missing_actor", "actors"), ("unknown_actor_ref", "actors[1].ref")
    }
    assert "opp-1" in next(i.message for i in issues if i.code == "missing_actor")


@pytest.mark.parametrize("collection,path", [
    ("facts", "facts[1].id"),
    ("rules", "rules[1].id"),
    ("clocks", "clocks[1].id"),
    ("reader_promises", "reader_promises[1].id"),
])
def test_duplicate_seed_ids(seed, story_engine, map_atlas, collection, path):
    seed["facts"][0]["nobody_knows"] = True
    seed[collection].append(deepcopy(seed[collection][0]))
    assert [(i.code, i.path, i.severity) for i in validate(seed, story_engine, map_atlas)] == [
        ("duplicate_ref", path, "error")
    ]
    seed[collection][1]["id"] += "-other"
    assert validate(seed, story_engine, map_atlas) == []


@pytest.mark.parametrize("collection,path", [
    ("agendas", "actors[0].agendas[1].id"),
    ("intents", "actors[0].intents[1].id"),
])
def test_duplicate_actor_ids(seed, story_engine, map_atlas, collection, path):
    items = seed["actors"][0][collection]
    items.append(deepcopy(items[0]))
    assert [(i.code, i.path, i.severity) for i in validate(seed, story_engine, map_atlas)] == [
        ("duplicate_ref", path, "error")
    ]
    items[1]["id"] += "-other"
    assert validate(seed, story_engine, map_atlas) == []


def test_agenda_and_intent_ids_are_scoped_to_actor(seed, story_engine, map_atlas):
    seed["actors"][1]["agendas"] = deepcopy(seed["actors"][0]["agendas"])
    seed["actors"][1]["intents"] = deepcopy(seed["actors"][0]["intents"])
    assert validate(seed, story_engine, map_atlas) == []


def test_seed_ids_are_scoped_to_collection(seed, story_engine, map_atlas):
    for collection in ("rules", "clocks", "reader_promises"):
        seed[collection][0]["id"] = "fact-1"
    seed["actors"][0]["intents"][0]["id"] = "agenda-1"
    assert validate(seed, story_engine, map_atlas) == []


@pytest.mark.parametrize("collection,path", [
    ("facts", "facts[0].id"),
    ("rules", "rules[0].id"),
    ("clocks", "clocks[0].id"),
    ("reader_promises", "reader_promises[0].id"),
    ("agendas", "actors[0].agendas[0].id"),
    ("intents", "actors[0].intents[0].id"),
])
@pytest.mark.parametrize("missing", ["", None, "absent"])
def test_missing_seed_ids(seed, story_engine, map_atlas, collection, path, missing):
    item = (seed["actors"][0] if collection in ("agendas", "intents") else seed)[collection][0]
    if missing == "absent":
        item.pop("id")
    else:
        item["id"] = missing
    issues = validate(seed, story_engine, map_atlas)
    assert [(i.code, i.path, i.severity) for i in issues if i.code in ("missing_id", "duplicate_ref")] == [
        ("missing_id", path, "error")
    ]


def test_repeated_missing_ids_are_not_duplicates(seed, story_engine, map_atlas):
    seed["rules"].append(deepcopy(seed["rules"][0]))
    for rule in seed["rules"]:
        rule["id"] = ""
    assert [(i.code, i.path) for i in validate(seed, story_engine, map_atlas)] == [
        ("missing_id", "rules[0].id"), ("missing_id", "rules[1].id"),
    ]


def test_duplicate_relation_triple(seed, story_engine, map_atlas):
    seed["relations"].append(dict(seed["relations"][0], note="other", visibility="secret", appellation="other"))
    assert [(i.code, i.path, i.severity) for i in validate(seed, story_engine, map_atlas)] == [
        ("duplicate_ref", "relations[1]", "error")
    ]
    seed["relations"][1]["edge_type"] = "ally_of"
    assert validate(seed, story_engine, map_atlas) == []
    seed["relations"][1].update(source="faction-1", target="cast-1", edge_type="member_of")
    assert validate(seed, story_engine, map_atlas) == []


def test_major_actor_rules(seed, story_engine, map_atlas):
    actor = seed["actors"][0]
    actor.update(agendas=[], intents=[], location_ref="")
    assert {(i.code, i.severity) for i in validate(seed, story_engine, map_atlas)} == {
        ("major_actor_without_agenda", "error"), ("major_actor_without_location", "error")
    }
    actor["actor_tier"] = "minor"
    assert [(i.code, i.severity) for i in validate(seed, story_engine, map_atlas)] == [
        ("minor_actor_without_location", "warning")
    ]
    actor.update(actor_tier="background")
    assert validate(seed, story_engine, map_atlas) == []
    seed["actors"][2].update(actor_tier="major", location_ref="")
    assert [i.code for i in validate(seed, story_engine, map_atlas)] == ["major_actor_without_agenda"]


def test_secret_without_knower(seed, story_engine, map_atlas):
    seed["knowledge"] = []
    assert [(i.code, i.path) for i in validate(seed, story_engine, map_atlas)] == [
        ("secret_without_knower", "facts[0]")
    ]
    seed["facts"][0]["nobody_knows"] = True
    assert validate(seed, story_engine, map_atlas) == []


@pytest.mark.parametrize("state,expected", [("knows", []), ("believes", []), ("suspects", []), ("heard", ["secret_without_knower"]), ("misled", ["secret_without_knower"]), ("disbelieves", ["secret_without_knower"])])
def test_hidden_fact_requires_awareness(seed, story_engine, map_atlas, state, expected):
    seed["facts"][0]["sensitivity"] = "hidden"
    seed["knowledge"][0]["state"] = state
    assert [i.code for i in validate(seed, story_engine, map_atlas)] == expected


@pytest.mark.parametrize("sensitivity", ["Secret", "must_not_reveal", "", None, []])
def test_invalid_sensitivity(seed, story_engine, map_atlas, sensitivity):
    seed["facts"][0]["sensitivity"] = sensitivity
    seed["knowledge"] = []
    assert [(i.code, i.path, i.severity) for i in validate(seed, story_engine, map_atlas)] == [
        ("invalid_sensitivity", "facts[0].sensitivity", "error")
    ]


@pytest.mark.parametrize("visibility", ["Secret", "hidden", "", None, []])
def test_invalid_visibility(seed, story_engine, map_atlas, visibility):
    seed["relations"][0]["visibility"] = visibility
    assert [(i.code, i.path, i.severity) for i in validate(seed, story_engine, map_atlas)] == [
        ("invalid_visibility", "relations[0].visibility", "error")
    ]


@pytest.mark.parametrize("sensitivity", ["public", "hidden", "secret"])
@pytest.mark.parametrize("visibility", ["public", "secret"])
def test_valid_sensitivity_and_visibility(seed, story_engine, map_atlas, sensitivity, visibility):
    seed["facts"][0]["sensitivity"] = sensitivity
    seed["relations"][0]["visibility"] = visibility
    assert validate(seed, story_engine, map_atlas) == []


def test_enum_and_reference_errors(seed, story_engine, map_atlas):
    seed["facts"][0]["sensitivity"] = "public"
    seed["actors"][0]["intents"][0]["action_type"] = "bad"
    seed["relations"][0]["edge_type"] = "bad"
    seed["knowledge"][0].update(state="bad", channel="told")
    seed["rules"][0]["hardness"] = "bad"
    seed["clocks"][0]["scope"] = "bad"
    seed["reveal_candidates"][0]["arc_number"] = 9
    assert {(i.code, i.path) for i in validate(seed, story_engine, map_atlas)} == {
        ("invalid_action_type", "actors[0].intents[0].action_type"),
        ("invalid_edge_type", "relations[0].edge_type"),
        ("invalid_knowledge_state", "knowledge[0].state"),
        ("invalid_channel", "knowledge[0].channel"),
        ("invalid_hardness", "rules[0].hardness"),
        ("invalid_clock_scope", "clocks[0].scope"),
        ("invalid_arc_number", "reveal_candidates[0].arc_number"),
    }


def test_remaining_reference_errors(seed, story_engine, map_atlas):
    actor = seed["actors"][0]
    actor.update(actor_tier="bad", location_ref="node-9", travel_mode_ids=["mode-9"])
    actor["agendas"][0].update(priority=6, means_refs=["missing"], secrecy_refs=["missing"])
    actor["intents"][0].update(agenda_id="other-actor-agenda", impact_level="bad", location_ref="node-9", target_refs=["missing"])
    seed["actors"][1]["agendas"] = [dict(actor["agendas"][0], id="other-actor-agenda", priority=1, means_refs=[], secrecy_refs=[])]
    seed["actors"].append(deepcopy(seed["actors"][2]))
    seed["relations"][0].update(source="missing", target="fact-1")
    seed["facts"][0].update(related_refs=["missing"], nobody_knows=True)
    seed["knowledge"][0].update(observer_ref="fact-1", fact_id="missing")
    seed["clocks"][0]["owner_ref"] = "missing"
    seed["reveal_candidates"][0]["fact_id"] = "missing"
    actual = {(i.code, i.path) for i in validate(seed, story_engine, map_atlas)}
    assert actual == {
        ("invalid_actor_tier", "actors[0].actor_tier"),
        ("unknown_location_ref", "actors[0].location_ref"),
        ("unknown_travel_mode", "actors[0].travel_mode_ids[0]"),
        ("invalid_priority", "actors[0].agendas[0].priority"),
        ("unknown_ref", "actors[0].agendas[0].means_refs[0]"),
        ("unknown_ref", "actors[0].agendas[0].secrecy_refs[0]"),
        ("unknown_agenda_ref", "actors[0].intents[0].agenda_id"),
        ("invalid_impact_level", "actors[0].intents[0].impact_level"),
        ("unknown_location_ref", "actors[0].intents[0].location_ref"),
        ("unknown_ref", "actors[0].intents[0].target_refs[0]"),
        ("duplicate_ref", "actors[3].ref"),
        ("unknown_relation_ref", "relations[0].source"),
        ("unknown_relation_ref", "relations[0].target"),
        ("unknown_ref", "facts[0].related_refs[0]"),
        ("unknown_observer_ref", "knowledge[0].observer_ref"),
        ("unknown_fact_ref", "knowledge[0].fact_id"),
        ("unknown_ref", "clocks[0].owner_ref"),
        ("unknown_fact_ref", "reveal_candidates[0].fact_id"),
    }


@pytest.mark.parametrize("value", [None, -1, 0, 6, 2.5, True, "3"])
def test_priority_requires_integer_in_range(seed, story_engine, map_atlas, value):
    seed["actors"][0]["agendas"][0]["priority"] = value
    assert [i.code for i in validate(seed, story_engine, map_atlas)] == ["invalid_priority"]


@pytest.mark.parametrize("checkable,valid", [(None, True), ({"kind": "x", "data": {"a": [1]}}, False), ({}, False), ({"kind": " "}, False), ({"kind": 3}, False), ([], False), ("x", False)])
def test_checkable_shape(seed, story_engine, map_atlas, checkable, valid):
    seed["rules"][0]["checkable"] = checkable
    assert [i.code for i in validate(seed, story_engine, map_atlas)] == ([] if valid else ["invalid_checkable"])


@pytest.mark.parametrize("arc_number", [True, "1", 1.0, 0, 3])
def test_arc_number_requires_known_integer(seed, story_engine, map_atlas, arc_number):
    seed["reveal_candidates"][0]["arc_number"] = arc_number
    assert [i.code for i in validate(seed, story_engine, map_atlas)] == ["invalid_arc_number"]


@pytest.mark.parametrize("broken", ["calendar", "lexicon", "both"])
def test_invalid_time_dependencies_do_not_cascade(seed, story_engine, map_atlas, broken):
    expected = []
    if broken in ("calendar", "both"):
        seed["calendar"]["opening_minute"] = -1
        expected.append(("invalid_calendar", "calendar", "opening_minute must be a nonnegative integer"))
    if broken in ("lexicon", "both"):
        seed["lexicon"]["news_speed_multiplier"] = 0
        expected.append(("invalid_lexicon", "lexicon", "news_speed_multiplier must be positive"))
    seed["actors"][0]["intents"][0]["scheduled_in"]["unit"] = "unknown"
    seed["clocks"][0]["due_in"]["unit"] = "unknown"
    assert [(i.code, i.path, i.message) for i in validate(seed, story_engine, map_atlas)] == expected


def test_normalize_coerces_shapes_without_judging_refs(seed, story_engine, map_atlas):
    seed.pop("rules")
    seed["actors"][0]["ref"] = "cast-9"
    seed["actors"][0]["agendas"][0]["priority"] = "3"
    seed["actors"][0]["intents"][0].update(requires_onstage="false", target_refs=["unknown", 7])
    seed["actors"][0]["intents"][0]["scheduled_in"]["amount"] = "1.5"
    seed["facts"][0].update(truth_value="false", nobody_knows="true")
    seed["calendar"].update(epoch_label=7, opening_minute="0")
    seed["calendar"]["units"][0]["minutes"] = "1440"
    seed["lexicon"]["travel_modes"][0]["speed_multiplier"] = "2.5"
    seed["lexicon"]["rank_ladders"] = [{"id": 2, "name": 3, "ranks": [1, "甲"]}, None]
    seed["lexicon"]["currencies"] = [{"id": 4, "name": 5}, "bad"]
    seed["actors"].extend([None, "bad"])
    seed["relations"].append(1)
    original = deepcopy(seed)
    result = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    assert tuple(result) == WORLD_SEED_KEYS == (
        "calendar", "lexicon", "actors", "relations", "facts", "knowledge",
        "rules", "clocks", "reader_promises", "reveal_candidates", "world_variables",
    )
    assert result["rules"] == []
    assert len(result["actors"]) == 3 and len(result["relations"]) == 1
    actor = result["actors"][0]
    assert actor["ref"] == "cast-9" and actor["agendas"][0]["priority"] == 3
    assert actor["intents"][0]["target_refs"] == ["unknown", "7"]
    assert actor["intents"][0]["requires_onstage"] is False
    assert actor["intents"][0]["scheduled_in"]["amount"] == 1.5
    assert result["facts"][0]["truth_value"] is False
    assert result["facts"][0]["nobody_knows"] is True
    assert result["calendar"] == {"epoch_label": "7", "opening_minute": 0, "units": [{"name": "日", "minutes": 1440}]}
    assert result["lexicon"]["travel_modes"][0]["speed_multiplier"] == 2.5
    assert result["lexicon"]["rank_ladders"] == [{"id": "2", "name": "3", "ranks": ["1", "甲"]}]
    assert result["lexicon"]["currencies"] == [{"id": "4", "name": "5"}]
    assert normalize_world_seed(result, story_engine=story_engine, map_atlas=map_atlas) == result
    assert seed == original


def test_normalize_preserves_checkable_extensions_and_invalid_values(seed, story_engine, map_atlas):
    seed["rules"][0]["checkable"] = {"kind": "custom", "nested": {"refs": ["cast-9"]}}
    seed["calendar"]["opening_minute"] = "-1"
    seed["actors"][0]["agendas"][0]["priority"] = "9"
    result = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    assert result["rules"] == seed["rules"]
    result["rules"][0]["checkable"]["nested"]["refs"].append("other")
    assert seed["rules"][0]["checkable"]["nested"]["refs"] == ["cast-9"]
    assert result["calendar"]["opening_minute"] == -1
    assert result["actors"][0]["agendas"][0]["priority"] == 9


def test_fallback_is_structural_shell(story_engine, map_atlas):
    original = deepcopy(story_engine)
    result = fallback_world_seed(story_engine)
    assert result["calendar"] == {"epoch_label": "", "opening_minute": 0, "units": []}
    assert result["lexicon"] == {"time_unit_aliases": [], "rank_ladders": [], "currencies": [], "appellations": [], "generic_character_refs": [], "travel_modes": [], "news_speed_multiplier": 1.0}
    assert result["actors"] == [
        {"ref": ref, "actor_tier": "minor", "location_ref": location, "travel_mode_ids": [], "resources": [], "agendas": [], "intents": []}
        for ref, location in [("cast-1", "node-1"), ("opp-1", "node-2"), ("faction-1", "node-1")]
    ]
    assert all(result[key] == [] for key in ("relations", "facts", "knowledge", "rules", "clocks", "reader_promises", "reveal_candidates"))
    assert not [i for i in validate(result, story_engine, map_atlas) if i.severity == "error"]
    assert story_engine == original
    story_engine["core_cast"][0]["current_base"] = ""
    assert fallback_world_seed(story_engine)["actors"][0]["location_ref"] == "node-2"
    story_engine["core_cast"][0]["home_location"] = ""
    result = fallback_world_seed(story_engine)
    assert [(i.code, i.severity) for i in validate(result, story_engine, map_atlas)] == [("minor_actor_without_location", "warning")]
    result["lexicon"]["appellations"].append("new")
    assert fallback_world_seed(story_engine)["lexicon"]["appellations"] == []


def test_validation_is_deterministic_and_does_not_mutate(seed, story_engine, map_atlas):
    seed["actors"] = []
    original = deepcopy((seed, story_engine, map_atlas))
    issues = validate(seed, story_engine, map_atlas)
    assert issues == validate(seed, story_engine, map_atlas)
    assert (seed, story_engine, map_atlas) == original
    assert [i.message for i in issues if i.code == "missing_actor"] == [
        "Missing actor: cast-1", "Missing actor: opp-1", "Missing actor: faction-1"
    ]


@pytest.mark.parametrize("checkable", [
    {"kind": "invented"},
    {"kind": "actor_cannot_enter", "location_ref": "missing"},
    {"kind": "actor_cannot_enter", "location_ref": "node-1", "except_refs": ["missing"]},
    {"kind": "blocked_channel", "fact_ref": "missing", "source_refs": ["cast-1"], "channels": ["told"]},
    {"kind": "requires_travel_mode", "edge_types": ["road"], "mode_ids": ["missing"]},
    {"kind": "exclusive_relation", "edge_type": "serves", "max_per_source": 0},
    "malformed",
])
def test_seed_invalid_checkable_is_an_issue(seed, story_engine, map_atlas, checkable):
    seed["rules"][0]["checkable"] = checkable
    for candidate in (seed, normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)):
        issues = validate(candidate, story_engine, map_atlas)
        assert [(i.code, i.path, i.severity) for i in issues] == [
            ("invalid_checkable", "rules[0].checkable", "error"),
        ]


@pytest.mark.parametrize("hardness,modes,detour,expected", [
    ("hard", [], False, True),
    ("soft", [], False, False),
    ("hard", ["mode-1"], False, False),
    ("hard", [], True, False),
])
def test_seed_predicate_isolating_major_actor_warns(seed, story_engine, map_atlas, hardness, modes, detour, expected):
    seed["rules"][0].update(hardness=hardness, checkable={
        "kind": "requires_travel_mode", "edge_types": ["road"], "mode_ids": ["mode-1"],
    })
    seed["actors"][0]["travel_mode_ids"] = modes
    seed["actors"][1].update(actor_tier="major", agendas=deepcopy(seed["actors"][0]["agendas"]), travel_mode_ids=modes)
    map_atlas["edges"] = [{"id": "edge-1", "from_node_id": "node-1", "to_node_id": "node-2", "kind": "road", "bidirectional": True}]
    if detour:
        map_atlas["edges"].append({**map_atlas["edges"][0], "id": "edge-2", "kind": "path"})
    before = deepcopy(seed)
    issues = validate(seed, story_engine, map_atlas)
    assert [(i.code, i.path, i.severity) for i in issues] == (
        [("rule_isolates_major_actor", "actors[1].location_ref", "warning")] if expected else []
    )
    assert seed == before


@pytest.mark.parametrize("genre", ["xianxia", "interstellar", "urban_mystery"])
def test_fixture_checkable_rule_parses(genre):
    import json
    from pathlib import Path
    from forwin.book_state.rule_predicates import RuleRefScope, parse_checkable
    from forwin.map.genesis_adapter import buildable_atlas_node_ids

    pack = json.loads((Path(__file__).parent / "fixtures" / "world_seed" / f"{genre}.json").read_text())["pack"]
    seed = pack["world_seed"]
    scope = RuleRefScope(
        locations=buildable_atlas_node_ids(pack["world"]["map_atlas"]),
        actors=frozenset(story_engine_actor_refs(pack["world"]["story_engine"])),
        facts=frozenset(f["id"] for f in seed["facts"]),
        modes=frozenset(m["id"] for m in seed["lexicon"]["travel_modes"]),
    )
    rules = [r for r in seed["rules"] if r.get("checkable") is not None]
    assert rules
    for rule in rules:
        predicate, issues = parse_checkable(rule["checkable"], scope=scope)
        assert predicate is not None and issues == [], issues


def test_seed_predicate_normalization_exposes_parsed_defaults(seed, story_engine, map_atlas):
    seed["rules"][0]["checkable"] = {"kind": "actor_cannot_enter", "location_ref": "node-2"}
    normalized = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    assert normalized["rules"][0]["checkable"] == {
        "kind": "actor_cannot_enter", "location_ref": "node-2", "include_children": True,
        "except_refs": [], "except_member_of": [],
    }
    assert "include_children" not in seed["rules"][0]["checkable"]


def test_seed_prompt_lists_all_predicate_shapes():
    from forwin.book_state.rule_predicates import RULE_PREDICATE_KINDS
    from forwin.genesis.messages import _world_seed_output_requirement
    from forwin.writer.prompt_core.rule_predicates import RULE_PREDICATE_DESCRIPTIONS

    prompt = _world_seed_output_requirement()
    for kind in RULE_PREDICATE_KINDS:
        assert f"{kind}: {RULE_PREDICATE_DESCRIPTIONS[kind]}" in prompt


@pytest.mark.parametrize("exception", [None, "actor", "member"])
def test_seed_entry_rules_respect_actor_and_membership_exceptions(seed, story_engine, map_atlas, exception):
    seed["actors"][1].update(actor_tier="major", agendas=deepcopy(seed["actors"][0]["agendas"]))
    map_atlas["edges"] = [{"id": "edge-1", "from_node_id": "node-1", "to_node_id": "node-2", "kind": "road"}]
    seed["rules"] = [{"id": f"rule-{i}", "hardness": "hard", "checkable": {
        "kind": "actor_cannot_enter", "location_ref": f"node-{i}",
    }} for i in (1, 2)]
    if exception == "actor":
        seed["rules"][1]["checkable"]["except_refs"] = ["cast-1"]
    elif exception == "member":
        seed["rules"][1]["checkable"]["except_member_of"] = ["faction-1"]
    issues = validate(seed, story_engine, map_atlas)
    assert [i.code for i in issues] == (["rule_isolates_major_actor"] if exception is None else [])


def test_seed_disconnected_map_does_not_blame_predicates(seed, story_engine, map_atlas):
    seed["actors"][1].update(actor_tier="major", agendas=deepcopy(seed["actors"][0]["agendas"]))
    seed["rules"][0]["checkable"] = {"kind": "requires_travel_mode", "edge_types": ["road"], "mode_ids": ["mode-1"]}
    assert validate(seed, story_engine, map_atlas) == []


def test_seed_review_panel_names_refs_and_shows_invalid_checkable(seed, story_engine, map_atlas):
    import json
    import shutil
    import subprocess
    from pathlib import Path

    node = shutil.which("node")
    if not node:
        pytest.skip("Node.js is required for the review-panel renderer check")
    source = (Path(__file__).parents[1] / "forwin/ui_assets/home/app_genesis.js").read_text()
    renderer = source[source.index("    function genesisWorldSeedRuleLines("):source.index("    function genesisWorldSeedOverviewLines(")]
    seed["rules"][0]["checkable"] = {"kind": "actor_cannot_enter", "location_ref": "node-2", "except_refs": ["cast-1"]}
    seed["rules"].append({"id": "rule-bad", "hardness": "soft", "checkable": {"kind": "invented"}})
    payload = normalize_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas)
    detail = {"pack": {"world": {"story_engine": story_engine, "map_atlas": map_atlas}}}
    issues = [{"code": "invalid_checkable", "path": "rules[1].checkable", "message": "unknown_rule_predicate"}]
    script = renderer + '\nconsole.log(JSON.stringify(genesisWorldSeedRuleLines(...' + json.dumps([detail, payload, issues]) + ')));'
    result = subprocess.run([node, "-e", script], check=True, capture_output=True, text=True)
    lines = json.loads(result.stdout)
    assert "actor_cannot_enter" in lines[0] and "B (node-2)" in lines[0] and "甲 (cast-1)" in lines[0]
    assert "include_children=true" in lines[0]
    assert "invalid_checkable" in lines[1] and "unknown_rule_predicate" in lines[1]
    assert "proposition" not in result.stdout


@pytest.fixture
def variable_seed(seed):
    seed["world_variables"] = [{
        "id": "variable-1", "label": "Supply", "source_kind": "resource_economy",
        "source_profile_id": "economy-1", "scale": ["low", "normal", "high"], "level": 1,
        "scope_refs": ["node-1", "faction-1"], "holder_refs": ["cast-1"],
        "visibility": "public", "drift_tendency": "volatile", "summary": "Supply condition",
    }]
    return seed


def test_seed_world_variables_normalize_and_validate(variable_seed, story_engine, map_atlas):
    normalized = normalize_world_seed(variable_seed, story_engine=story_engine, map_atlas=map_atlas)
    assert normalized["world_variables"] == variable_seed["world_variables"]
    assert validate_world_seed(normalized, story_engine=story_engine, map_atlas=map_atlas,
                               arc_numbers={1}, world_profiles={"economy-1": "resource_economy"}) == []
    assert fallback_world_seed(story_engine)["world_variables"] == []


@pytest.mark.parametrize("change,code", [
    ({"scale": ["low"]}, "invalid_world_variable_scale"),
    ({"scale": list("abcdefgh")}, "invalid_world_variable_scale"),
    ({"scale": ["low", "low"]}, "invalid_world_variable_scale"),
    ({"scale": ["low", " "]}, "invalid_world_variable_scale"),
    ({"level": 3}, "invalid_world_variable_level"),
    ({"level": -1}, "invalid_world_variable_level"),
    ({"level": True}, "invalid_world_variable_level"),
    ({"level": 1.5}, "invalid_world_variable_level"),
    ({"source_profile_id": "missing"}, "unknown_world_profile"),
    ({"source_kind": "institution"}, "invalid_source_kind"),
    ({"scope_refs": ["fact-1"]}, "unknown_scope_ref"),
    ({"holder_refs": ["node-1"]}, "unknown_holder_ref"),
    ({"visibility": "hidden"}, "invalid_visibility"),
    ({"drift_tendency": "unknown"}, "invalid_drift_tendency"),
])
def test_seed_world_variables_reject_invalid(variable_seed, story_engine, map_atlas, change, code):
    variable_seed["world_variables"][0].update(change)
    normalized = normalize_world_seed(variable_seed, story_engine=story_engine, map_atlas=map_atlas)
    for seed in (variable_seed, normalized):
        issues = validate_world_seed(seed, story_engine=story_engine, map_atlas=map_atlas,
                                     arc_numbers={1}, world_profiles={"economy-1": "resource_economy"})
        assert code in [issue.code for issue in issues if issue.severity == "error"]


def test_seed_world_variables_unique_profile_and_missing_warning(variable_seed, story_engine, map_atlas):
    from forwin.genesis.helpers import _world_seed_validation_issues

    pack = {"world": {"story_engine": story_engine, "map_atlas": map_atlas,
                       "resource_economy_profiles": [{"id": "economy-1"}],
                       "institution_profiles": [{"id": "institution-1"}]},
            "world_seed": variable_seed, "book_arc_blueprint": {"arcs": [{"arc_number": 1}]}}
    assert [(i.code, i.severity) for i in _world_seed_validation_issues(pack)] == [
        ("missing_world_variable", "warning")]
    variable_seed["world_variables"].append(dict(variable_seed["world_variables"][0], id="variable-2"))
    assert "duplicate_world_variable_profile" in [i.code for i in _world_seed_validation_issues(pack)]
    variable_seed["world_variables"][1]["id"] = "variable-1"
    assert "duplicate_ref" in [i.code for i in _world_seed_validation_issues(pack)]


def test_world_variable_prompt_receives_profiles(variable_seed, story_engine, map_atlas):
    from forwin.genesis.messages import _build_stage_generation_messages
    from forwin.models.project import Project

    pack = {"world": {"story_engine": story_engine, "map_atlas": map_atlas,
                       "institution_profiles": [{"id": "institution-input"}],
                       "resource_economy_profiles": [{"id": "economy-input"}]},
            "world_seed": variable_seed}
    content = _build_stage_generation_messages(
        None, project=Project(id="p", title="t", premise="p", genre="g"),
        pack=pack, stage_key="world_seed", fallback={})[-1]["content"]
    assert "institution-input" in content and "economy-input" in content
    for field in ("world_variables", "source_profile_id", "scale", "level", "scope_refs", "holder_refs", "drift_tendency"):
        assert field in content
