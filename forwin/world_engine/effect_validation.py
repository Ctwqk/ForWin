"""Shared structural reference validation for world-engine effects."""

from __future__ import annotations

from collections.abc import Collection
from typing import Any

from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.names import normalized_name
from forwin.book_state.runtime import BookStateRuntime
from forwin.world_engine.types import NEW_ACTOR_REF_PREFIX, NEW_FACT_REF_PREFIX, WorldEffects


def new_actor_name_taken(
    name: str, runtime: BookStateRuntime, reserved_names: Collection[str] = (),
    *, ignored_node_ids: Collection[str] = (),
) -> bool:
    names = set(reserved_names)
    for node in [*runtime.world.nodes_by_id.values(), *runtime.map.nodes_by_id.values()]:
        if node.id not in ignored_node_ids and getattr(node, "is_active", True):
            names.add(node.name)
            names.update(getattr(node, "aliases", ()))
    return normalized_name(name) in {normalized_name(value) for value in names}


def effect_reference_issues(effects: WorldEffects, runtime: BookStateRuntime, lexicon: BookLexicon) -> list[str]:
    return scoped_effect_reference_issues(
        effects,
        refs=(set(runtime.world.nodes_by_id) | set(runtime.map.nodes_by_id)
              | {f"{FACT_REF_PREFIX}{ref}" for ref in runtime.world.facts_by_id}),
        location_ids=set(runtime.map.nodes_by_id),
        travel_mode_ids={mode.id for mode in lexicon.travel_modes},
        affiliation_ids={node.id for node in runtime.world.nodes_by_id.values()
                         if node.is_active and node.node_type in {"group", "faction", "organization"}},
    )


def scoped_effect_reference_issues(
    effects: WorldEffects,
    *,
    refs: Collection[str],
    location_ids: Collection[str],
    travel_mode_ids: Collection[str],
    affiliation_ids: Collection[str] = (),
) -> list[str]:
    """The same checks within given references: node, map node and ``fact:<id>`` refs, and movement destinations."""
    allowed = (set(refs) | {f"{NEW_ACTOR_REF_PREFIX}{actor.key}" for actor in effects.new_actors}) - {"reader"}
    locations = set(location_ids)
    new_facts = {f"{NEW_FACT_REF_PREFIX}{fact.key}" for fact in effects.new_facts}
    modes = set(travel_mode_ids)
    issues: list[str] = []

    def check(ref: str, path: str, *, within: set[str] = allowed, optional: bool = False) -> None:
        if optional and not ref:
            return
        if ref not in within:
            issues.append(f"unknown_ref at {path}: {ref!r}")

    def value_refs(value: Any, path: str, *, is_ref: bool = False) -> None:
        if isinstance(value, str) and is_ref:
            check(value, path, optional=True)
        elif isinstance(value, dict):
            for key, item in value.items():
                value_refs(item, f"{path}.{key}", is_ref=key.endswith(("_id", "_ids", "_ref", "_refs")))
        elif isinstance(value, list):
            for index, item in enumerate(value):
                value_refs(item, f"{path}[{index}]", is_ref=is_ref)

    for index, item in enumerate(effects.new_actors):
        check(item.location_id, f"new_actors[{index}].location_id", within=locations)
        for ref in item.affiliation_refs:
            check(ref, f"new_actors[{index}].affiliation_refs", within=set(affiliation_ids))

    for index, item in enumerate(effects.entity_changes):
        check(item.ref, f"entity_changes[{index}].ref")
        value_refs(item.new_value, f"entity_changes[{index}].new_value",
                   is_ref=item.field.endswith(("_id", "_ids", "_ref", "_refs")))
    for index, item in enumerate(effects.relation_changes):
        check(item.source_ref, f"relation_changes[{index}].source_ref")
        check(item.target_ref, f"relation_changes[{index}].target_ref")
        value_refs(item.new_value, f"relation_changes[{index}].new_value",
                   is_ref=item.field.endswith(("_id", "_ids", "_ref", "_refs")))
    for index, item in enumerate(effects.movements):
        check(item.entity_ref, f"movements[{index}].entity_ref")
        check(item.to_location_id, f"movements[{index}].to_location_id", within=locations)
        check(item.travel_mode_id, f"movements[{index}].travel_mode_id", within=modes, optional=True)
    for index, item in enumerate(effects.item_transfers):
        check(item.item_ref, f"item_transfers[{index}].item_ref")
        check(item.from_ref, f"item_transfers[{index}].from_ref", optional=True)
        check(item.to_ref, f"item_transfers[{index}].to_ref", optional=True)
    for index, item in enumerate(effects.events):
        check(item.location_id, f"events[{index}].location_id", optional=True)
        for ref in item.participant_refs:
            check(ref, f"events[{index}].participant_refs")
    for index, item in enumerate(effects.new_facts):
        for ref in item.known_by:
            check(ref, f"new_facts[{index}].known_by")
    for index, item in enumerate(effects.knowledge):
        check(item.observer_ref, f"knowledge[{index}].observer_ref")
        check(item.fact_ref, f"knowledge[{index}].fact_ref", within=allowed | new_facts)
        check(item.source_ref, f"knowledge[{index}].source_ref", optional=True)
    return issues
