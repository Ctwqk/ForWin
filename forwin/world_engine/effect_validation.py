"""Shared structural reference validation for world-engine effects."""

from __future__ import annotations

from typing import Any

from forwin.book_state.knowledge import FACT_REF_PREFIX
from forwin.book_state.lexicon import BookLexicon
from forwin.book_state.runtime import BookStateRuntime
from forwin.world_engine.types import NEW_FACT_REF_PREFIX, WorldEffects


def effect_reference_issues(effects: WorldEffects, runtime: BookStateRuntime, lexicon: BookLexicon) -> list[str]:
    allowed = (set(runtime.world.nodes_by_id) | set(runtime.map.nodes_by_id)
               | {f"{FACT_REF_PREFIX}{ref}" for ref in runtime.world.facts_by_id}) - {"reader"}
    new_facts = {f"{NEW_FACT_REF_PREFIX}{fact.key}" for fact in effects.new_facts}
    modes = {mode.id for mode in lexicon.travel_modes}
    issues: list[str] = []

    def check(ref: str, path: str, *, refs: set[str] = allowed, optional: bool = False) -> None:
        if optional and not ref:
            return
        if ref not in refs:
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
        check(item.to_location_id, f"movements[{index}].to_location_id", refs=set(runtime.map.nodes_by_id))
        check(item.travel_mode_id, f"movements[{index}].travel_mode_id", refs=modes, optional=True)
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
        check(item.fact_ref, f"knowledge[{index}].fact_ref", refs=allowed | new_facts)
        check(item.source_ref, f"knowledge[{index}].source_ref", optional=True)
    return issues

