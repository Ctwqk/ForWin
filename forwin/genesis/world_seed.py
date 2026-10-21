"""Pure shape normalization and deterministic validation for Genesis world seeds."""

from __future__ import annotations

from collections.abc import Collection, Mapping
from copy import deepcopy
from dataclasses import dataclass
from math import isfinite
from typing import Any

from forwin.book_state.knowledge import KNOWLEDGE_STATES
from forwin.book_state.lexicon import (
    EMPTY_LEXICON,
    LexiconError,
    lexicon_from_payload,
    lexicon_to_payload,
)
from forwin.book_state.schema import (
    ACTOR_TIERS,
    CLOCK_SCOPES,
    IMPACT_LEVELS,
    INTENT_ACTION_TYPES,
    RULE_HARDNESS,
)
from forwin.book_state.story_clock import (
    StoryClockError,
    UnknownTimeUnit,
    calendar_from_payload,
    unit_minutes,
)
from forwin.protocol.book_state import WORLD_EDGE_TYPES


WORLD_SEED_KEYS: tuple[str, ...] = (
    "calendar", "lexicon", "actors", "relations", "facts", "knowledge",
    "rules", "clocks", "reader_promises", "reveal_candidates",
)


@dataclass(frozen=True)
class WorldSeedIssue:
    severity: str
    code: str
    path: str
    message: str


def _object(value: Any) -> Mapping[str, Any]:
    return value if isinstance(value, Mapping) else {}


def _list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _objects(value: Any) -> list[Mapping[str, Any]]:
    return [item for item in _list(value) if isinstance(item, Mapping)]


def _text(value: Any) -> str:
    return "" if value is None else str(value)


def _texts(item: Mapping, *keys: str) -> dict[str, str]:
    return {key: _text(item.get(key)) for key in keys}


def _strings(value: Any) -> list[str]:
    return [_text(item) for item in _list(value)]


def _integer(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError, OverflowError):
        return default


def _number(value: Any, default: float = 0.0) -> float:
    try:
        number = float(value)
        return number if isfinite(number) else default
    except (TypeError, ValueError, OverflowError):
        return default


def _boolean(value: Any) -> bool:
    if isinstance(value, str):
        value = value.strip().lower()
        if value in ("false", "0", "no", "off", ""):
            return False
        if value in ("true", "1", "yes", "on"):
            return True
    return bool(value)


def _duration(value: Any) -> dict[str, Any]:
    item = _object(value)
    return {"amount": _number(item.get("amount")), "unit": _text(item.get("unit"))}


def _normalize_actor(item: Mapping) -> dict[str, Any]:
    return {
        **_texts(item, "ref", "actor_tier", "location_ref"),
        "travel_mode_ids": _strings(item.get("travel_mode_ids")),
        "resources": _strings(item.get("resources")),
        "agendas": [{
            **_texts(agenda, "id", "goal", "motivation", "horizon"),
            "priority": _integer(agenda.get("priority")),
            "means_refs": _strings(agenda.get("means_refs")),
            "constraints": _strings(agenda.get("constraints")),
            "secrecy_refs": _strings(agenda.get("secrecy_refs")),
        } for agenda in _objects(item.get("agendas"))],
        "intents": [{
            **_texts(intent, "id", "action_type", "summary", "location_ref", "impact_level"),
            "agenda_id": _text(intent.get("agenda_id")),
            "target_refs": _strings(intent.get("target_refs")),
            "scheduled_in": _duration(intent.get("scheduled_in")),
            "requires_onstage": _boolean(intent.get("requires_onstage")),
        } for intent in _objects(item.get("intents"))],
    }


def _checkable(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, Mapping):
        return None
    return {**deepcopy(dict(value)), "kind": _text(value.get("kind"))}


def story_engine_actor_refs(story_engine: Mapping) -> dict[str, str]:
    return {
        _text(item["id"]): kind
        for key, kind in (("core_cast", "character"), ("opposition", "character"), ("factions", "faction"))
        for item in _objects(story_engine.get(key))
        if item.get("id")
    }


def normalize_world_seed(
    payload: Mapping, *, story_engine: Mapping, map_atlas: Mapping,
) -> dict[str, Any]:
    """Coerce shapes without resolving, replacing, or inventing references."""
    calendar = _object(payload.get("calendar"))
    lexicon = _object(payload.get("lexicon"))
    return {
        "calendar": {
            "epoch_label": _text(calendar.get("epoch_label")),
            "opening_minute": _integer(calendar.get("opening_minute")),
            "units": [{"name": _text(item.get("name")), "minutes": _integer(item.get("minutes"))}
                      for item in _objects(calendar.get("units"))],
        },
        "lexicon": {
            "time_unit_aliases": [{"name": _text(item.get("name")), "minutes": _integer(item.get("minutes"))}
                                  for item in _objects(lexicon.get("time_unit_aliases"))],
            "rank_ladders": [{**_texts(item, "id", "name"), "ranks": _strings(item.get("ranks"))}
                             for item in _objects(lexicon.get("rank_ladders"))],
            "currencies": [_texts(item, "id", "name") for item in _objects(lexicon.get("currencies"))],
            "appellations": _strings(lexicon.get("appellations")),
            "generic_character_refs": _strings(lexicon.get("generic_character_refs")),
            "travel_modes": [{
                **_texts(item, "id", "name"),
                "speed_multiplier": _number(item.get("speed_multiplier"), 1.0),
                "allowed_edge_types": _strings(item.get("allowed_edge_types")),
            } for item in _objects(lexicon.get("travel_modes"))],
            "news_speed_multiplier": _number(lexicon.get("news_speed_multiplier"), 1.0),
        },
        "actors": [_normalize_actor(item) for item in _objects(payload.get("actors"))],
        "relations": [_texts(item, "source", "target", "edge_type", "note", "visibility", "appellation")
                      for item in _objects(payload.get("relations"))],
        "facts": [{
            **_texts(item, "id", "proposition", "sensitivity"),
            "truth_value": _boolean(item.get("truth_value")),
            "related_refs": _strings(item.get("related_refs")),
            "nobody_knows": _boolean(item.get("nobody_knows")),
        } for item in _objects(payload.get("facts"))],
        "knowledge": [_texts(item, "observer_ref", "fact_id", "state", "channel")
                      for item in _objects(payload.get("knowledge"))],
        "rules": [{**_texts(item, "id", "statement", "hardness"), "checkable": _checkable(item.get("checkable"))}
                  for item in _objects(payload.get("rules"))],
        "clocks": [{**_texts(item, "id", "label", "owner_ref", "scope", "on_expire"), "due_in": _duration(item.get("due_in"))}
                   for item in _objects(payload.get("clocks"))],
        "reader_promises": [_texts(item, "id", "summary", "promise_type", "expected_payoff_window")
                            for item in _objects(payload.get("reader_promises"))],
        "reveal_candidates": [{**_texts(item, "fact_id", "note"), "arc_number": _integer(item.get("arc_number"))}
                              for item in _objects(payload.get("reveal_candidates"))],
    }


def validate_world_seed(
    seed: Mapping, *, story_engine: Mapping, map_atlas: Mapping,
    arc_numbers: Collection[int],
) -> list[WorldSeedIssue]:
    """Return issues in input order; validation never repairs or mutates the seed."""
    issues: list[WorldSeedIssue] = []

    def issue(code: str, path: str, message: str, severity: str = "error") -> None:
        issues.append(WorldSeedIssue(severity, code, path, message))

    calendar = lexicon = None
    try:
        raw_calendar = seed.get("calendar", {})
        if not isinstance(raw_calendar, Mapping):
            raise StoryClockError("calendar must be an object")
        calendar = calendar_from_payload(raw_calendar)
    except StoryClockError as exc:
        issue("invalid_calendar", "calendar", str(exc))
    try:
        raw_lexicon = seed.get("lexicon", {})
        if not isinstance(raw_lexicon, Mapping):
            raise LexiconError("lexicon must be an object")
        lexicon = lexicon_from_payload(raw_lexicon)
    except LexiconError as exc:
        issue("invalid_lexicon", "lexicon", str(exc))

    actor_refs = story_engine_actor_refs(story_engine)
    location_refs = {_text(item.get("id")) for item in _objects(map_atlas.get("nodes")) if item.get("id")}
    facts = _objects(seed.get("facts"))
    fact_refs = {_text(item.get("id")) for item in facts if item.get("id")}
    all_refs = set(actor_refs) | fact_refs
    travel_modes = {_text(item.get("id")) for item in _objects(_object(seed.get("lexicon")).get("travel_modes")) if item.get("id")}

    def reference(value: Any, known: Collection[str], code: str, path: str) -> None:
        if not isinstance(value, str) or value not in known:
            issue(code, path, f"Unknown reference: {value}")

    def references(item: Mapping, key: str, path: str) -> None:
        for index, value in enumerate(_list(item.get(key))):
            reference(value, all_refs, "unknown_ref", f"{path}.{key}[{index}]")

    def enum(item: Mapping, key: str, values: Collection[str], code: str, path: str) -> None:
        value = item.get(key)
        if not isinstance(value, str) or value not in values:
            issue(code, f"{path}.{key}", f"Invalid {key}: {value}")

    def location(item: Mapping, path: str) -> None:
        if item.get("location_ref"):
            reference(item["location_ref"], location_refs, "unknown_location_ref", f"{path}.location_ref")

    def duration(value: Any, path: str) -> None:
        # An invalid dependency already has one diagnostic; avoid cascading unit errors.
        if calendar is None or lexicon is None:
            return
        try:
            unit_minutes(calendar, lexicon, _object(value).get("unit", ""))
        except UnknownTimeUnit as exc:
            issue("unknown_time_unit", f"{path}.unit", str(exc))

    seen: set[str] = set()
    for index, actor in enumerate(_objects(seed.get("actors"))):
        path = f"actors[{index}]"
        ref = _text(actor.get("ref"))
        reference(ref, actor_refs, "unknown_actor_ref", f"{path}.ref")
        if ref in seen:
            issue("duplicate_ref", f"{path}.ref", f"Duplicate actor reference: {ref}")
        seen.add(ref)
        enum(actor, "actor_tier", ACTOR_TIERS, "invalid_actor_tier", path)
        location(actor, path)
        tier = actor.get("actor_tier")
        if actor_refs.get(ref) == "character" and not actor.get("location_ref"):
            if tier == "major":
                issue("major_actor_without_location", f"{path}.location_ref", "Major character must have a location")
            elif tier == "minor":
                issue("minor_actor_without_location", f"{path}.location_ref", "Minor character has no location", "warning")
        agendas = _objects(actor.get("agendas"))
        if tier == "major" and not agendas:
            issue("major_actor_without_agenda", f"{path}.agendas", "Major actor must have an agenda")
        for mode_index, mode in enumerate(_list(actor.get("travel_mode_ids"))):
            reference(mode, travel_modes, "unknown_travel_mode", f"{path}.travel_mode_ids[{mode_index}]")
        agenda_ids = {_text(agenda.get("id")) for agenda in agendas if agenda.get("id")}
        for agenda_index, agenda in enumerate(agendas):
            agenda_path = f"{path}.agendas[{agenda_index}]"
            priority = agenda.get("priority")
            if type(priority) is not int or not 1 <= priority <= 5:
                issue("invalid_priority", f"{agenda_path}.priority", "Priority must be an integer from 1 to 5")
            references(agenda, "means_refs", agenda_path)
            references(agenda, "secrecy_refs", agenda_path)
        for intent_index, intent in enumerate(_objects(actor.get("intents"))):
            intent_path = f"{path}.intents[{intent_index}]"
            enum(intent, "action_type", INTENT_ACTION_TYPES, "invalid_action_type", intent_path)
            enum(intent, "impact_level", IMPACT_LEVELS, "invalid_impact_level", intent_path)
            reference(intent.get("agenda_id"), agenda_ids, "unknown_agenda_ref", f"{intent_path}.agenda_id")
            references(intent, "target_refs", intent_path)
            location(intent, intent_path)
            duration(intent.get("scheduled_in"), f"{intent_path}.scheduled_in")
    for ref in actor_refs:
        if ref not in seen:
            issue("missing_actor", "actors", f"Missing actor: {ref}")

    for index, relation in enumerate(_objects(seed.get("relations"))):
        path = f"relations[{index}]"
        enum(relation, "edge_type", WORLD_EDGE_TYPES, "invalid_edge_type", path)
        for key in ("source", "target"):
            reference(relation.get(key), actor_refs, "unknown_relation_ref", f"{path}.{key}")

    knowledge = _objects(seed.get("knowledge"))
    known_facts = {
        _text(item.get("fact_id")) for item in knowledge
        if item.get("state") in ("knows", "believes", "suspects")
    }
    for index, fact in enumerate(facts):
        path = f"facts[{index}]"
        references(fact, "related_refs", path)
        if (fact.get("sensitivity") in ("hidden", "secret")
                and not fact.get("nobody_knows") and fact.get("id") not in known_facts):
            issue("secret_without_knower", path, "Hidden or secret fact must have a knower or nobody_knows")
    for index, item in enumerate(knowledge):
        path = f"knowledge[{index}]"
        reference(item.get("observer_ref"), actor_refs, "unknown_observer_ref", f"{path}.observer_ref")
        reference(item.get("fact_id"), fact_refs, "unknown_fact_ref", f"{path}.fact_id")
        enum(item, "state", KNOWLEDGE_STATES, "invalid_knowledge_state", path)
        enum(item, "channel", ("seeded",), "invalid_channel", path)

    for index, rule in enumerate(_objects(seed.get("rules"))):
        path = f"rules[{index}]"
        enum(rule, "hardness", RULE_HARDNESS, "invalid_hardness", path)
        checkable = rule.get("checkable")
        if checkable is not None and (
            not isinstance(checkable, Mapping)
            or not isinstance(checkable.get("kind"), str) or not checkable["kind"].strip()
        ):
            issue("invalid_checkable", f"{path}.checkable", "checkable must be null or an object with a nonempty string kind")
    for index, clock in enumerate(_objects(seed.get("clocks"))):
        path = f"clocks[{index}]"
        enum(clock, "scope", CLOCK_SCOPES, "invalid_clock_scope", path)
        if clock.get("owner_ref"):
            reference(clock["owner_ref"], actor_refs, "unknown_ref", f"{path}.owner_ref")
        duration(clock.get("due_in"), f"{path}.due_in")
    for index, candidate in enumerate(_objects(seed.get("reveal_candidates"))):
        path = f"reveal_candidates[{index}]"
        reference(candidate.get("fact_id"), fact_refs, "unknown_fact_ref", f"{path}.fact_id")
        number = candidate.get("arc_number")
        if type(number) is not int or number not in arc_numbers:
            issue("invalid_arc_number", f"{path}.arc_number", f"Unknown arc number: {number}")
    return issues


def fallback_world_seed(story_engine: Mapping) -> dict[str, Any]:
    """Build structural actor shells using only supplied entity and location IDs."""
    seed = normalize_world_seed({}, story_engine=story_engine, map_atlas={})
    seed["lexicon"] = lexicon_to_payload(EMPTY_LEXICON)
    for key in ("core_cast", "opposition", "factions"):
        for item in _objects(story_engine.get(key)):
            if not item.get("id"):
                continue
            location = (item.get("current_base") or item.get("home_location") or ""
                        if key == "core_cast" else item.get("base_location") or "")
            seed["actors"].append({
                "ref": item["id"], "actor_tier": "minor", "location_ref": location,
                "travel_mode_ids": [], "resources": [], "agendas": [], "intents": [],
            })
    return seed
