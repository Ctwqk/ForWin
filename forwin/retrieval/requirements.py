"""Resolve Writer requirements against one accepted graph (or supplied snapshots).

This read-side owner never infers objective state from authored plans. The broker
owns budget selection; the Writer only requests hydration after scene breakdown.
"""

from __future__ import annotations

import re
from collections import defaultdict
from collections.abc import Iterable
from typing import Any

from forwin.book_state.query import BookStateQuery
from forwin.protocol.book_state import WorldNodeType
from forwin.protocol.context import ChapterContextPack, EntitySnapshot, RelationSnapshot
from forwin.protocol.scene import ScenePlan


class RequiredContextError(ValueError):
    """Required accepted input is missing/ambiguous; do not generate a fallback."""


def hydrate_requirements(
    pack: ChapterContextPack,
    *,
    session=None,
    scene_plans: Iterable[ScenePlan] = (),
) -> ChapterContextPack:
    baseline = pack.canon_read_baseline
    if session is not None and baseline is None:
        raise RequiredContextError(
            "required_context: reassemble context before accepted reads"
        )
    if baseline is not None and (
        baseline.project_id != pack.project_id
        or baseline.as_of_chapter != max(0, pack.chapter_number - 1)
    ):
        raise RequiredContextError(
            "required_context: chapter/project does not match accepted baseline"
        )
    runtime = None
    entities = {e.entity_id: e for e in pack.active_entities}
    relations = {e.relation_id: e for e in pack.active_relations if e.relation_id}
    facts = dict(pack.required_facts)
    if session is not None:
        query = BookStateQuery(session, baseline=baseline)
        runtime = query.runtime(pack.project_id, as_of_chapter=baseline.as_of_chapter)
        # Include explicitly referenced retired and non-character objects too: their
        # current inactive state matters more than their optional retrieval rank.
        entities = {
            node.id: EntitySnapshot(
                entity_id=node.id,
                kind=str(node.node_type),
                name=node.name or node.id,
                aliases=list(node.aliases),
                importance=node.importance,
                status=node.status,
                is_active=node.is_active,
                description=node.description or node.summary,
                current_state={**runtime.world.get_state(node.id)},
            )
            for node in runtime.world.nodes_by_id.values()
        }
        for node in runtime.map.nodes_by_id.values():
            if node.id not in entities:
                entities[node.id] = EntitySnapshot(
                    entity_id=node.id,
                    kind="location",
                    name=node.name or node.id,
                    aliases=list(node.aliases),
                    description=node.description,
                    current_state={
                        "status": node.status,
                        "access_level": node.access_level,
                    },
                )
        relations = {
            edge.id: RelationSnapshot(
                relation_id=edge.id,
                source_id=edge.source_id,
                target_id=edge.target_id,
                source_name=entities[edge.source_id].name
                if edge.source_id in entities
                else edge.source_id,
                target_name=entities[edge.target_id].name
                if edge.target_id in entities
                else edge.target_id,
                relation_type=edge.edge_type,
                description=str(edge.metadata.get("description") or ""),
                current_state=dict(edge.state),
                status=edge.status,
            )
            for edge in runtime.world.edges_by_id.values()
        }
        facts = {
            key: fact.model_dump(mode="json")
            for key, fact in runtime.world.facts_by_id.items()
        }
    aliases: dict[str, set[str]] = defaultdict(set)
    for e in entities.values():
        for name in (e.name, *e.aliases):
            if name:
                aliases[name].add(e.entity_id)
    identity_snapshot = (
        {name: sorted(ids) for name, ids in aliases.items()}
        if session is not None
        else pack.entity_name_candidates
    )
    for name, ids in identity_snapshot.items():
        aliases[name].update(ids)
    required_nodes: set[str] = set()
    required_edges: set[str] = set()
    required_facts: dict[str, Any] = {}
    sources: dict[str, set[str]] = defaultdict(set)
    proposed = {t.entity_name for t in pack.chapter_entry_targets if t.entity_name}
    visited: set[tuple[str, str]] = set()

    def fail(ref: str, source: str, reason="missing"):
        raise RequiredContextError(f"required_context: {reason} {ref!r} from {source}")

    def resolve(ref: str, source: str, *, strict=True):
        ref = str(ref or "").strip()
        if not ref or (ref, source) in visited:
            return
        visited.add((ref, source))
        prefix, _, value = ref.partition(":")
        if prefix == "fact" or ref in facts:
            value = value if prefix == "fact" else ref
            if value not in facts:
                fail(ref, source)
            fact = facts[value]
            required_facts[value] = fact
            sources[ref].add(source)
            for related in [
                *fact.get("related_node_refs", []),
                *fact.get("related_edge_refs", []),
            ]:
                resolve(related, source)
            return
        if prefix == "edge" or ref in relations:
            key = value if prefix == "edge" else ref
            if key not in relations:
                fail(ref, source)
            edge = relations[key]
            required_edges.add(key)
            sources[f"edge:{key}"].add(source)
            resolve(f"node:{edge.source_id}", source)
            resolve(f"node:{edge.target_id}", source)
            return
        field = ""
        if prefix == "field":
            owners = [
                key for key in [*entities, *aliases] if value.startswith(key + ":")
            ]
            if owners:
                owner = max(owners, key=len)
                value, field = owner, value[len(owner) + 1 :]
            if not owners or not field:
                fail(ref, source)
        node_prefixes = {
            "node",
            "field",
            "map",
            "entity",
            *[kind.value for kind in WorldNodeType],
        }
        token = ref if ref in entities else value if prefix in node_prefixes else ref
        candidates = {token} if token in entities else aliases.get(token, set())
        if len(candidates) > 1:
            fail(ref, source, "ambiguous")
        if not candidates:
            if prefix not in node_prefixes and (not strict or token in proposed):
                return
            fail(ref, source)
        key = next(iter(candidates))
        if session is None and token not in entities and token not in identity_snapshot:
            fail(ref, source, "reassemble to establish name uniqueness for")
        if key not in entities:
            fail(ref, source, "reassemble to load accepted entity for")
        if field:
            node = runtime.world.nodes_by_id.get(key) if runtime else None
            state = entities[key].current_state
            values = [{**(node.model_dump() if node else {}), **state, "state": state}]
            for part in field.split("."):
                values = [v[part] for v in values if isinstance(v, dict) and part in v]
            if not values:
                fail(ref, source, "missing field")
            # Profile/metadata fields explicitly required by a field ref must also
            # render even when they aren't represented in the current state map.
            required_facts[ref] = {"ref": ref, "value": values[0]}
        required_nodes.add(key)
        sources[f"node:{key}"].add(source)
        if runtime and key in runtime.map.nodes_by_id:
            for site in runtime.world.nodes_by_id.values():
                if (
                    str(site.node_type) == "site_state"
                    and site.profile.get("map_node_id") == key
                ):
                    resolve(f"node:{site.id}", source)

    def structured(payload, source):
        if not isinstance(payload, dict):
            return
        for key, value in payload.items():
            if key in {
                "subject_refs",
                "entity_refs",
                "node_refs",
                "edge_refs",
                "fact_refs",
                "required_refs",
            } and isinstance(value, list):
                for ref in value:
                    resolve(ref, source)
            elif key in {
                "subject_ref",
                "entity_ref",
                "node_ref",
                "edge_ref",
                "fact_ref",
                "target_ref",
                "entity_name",
                "character_name",
                "location_name",
                "source_name",
                "target_name",
                "node_id",
                "source_id",
                "target_id",
                "entity_id",
            } and isinstance(value, str):
                resolve(value, source)
            elif key == "edge_id" and value:
                resolve(f"edge:{value}", source)
            elif key == "fact_id" and value:
                resolve(f"fact:{value}", source)
            elif isinstance(value, dict):
                structured(value, source)

    text_parts = [
        pack.chapter_plan_title,
        pack.chapter_plan_one_line,
        *pack.chapter_goals,
    ]
    for origin, tasks in (
        ("chapter_task", pack.chapter_task_contract),
        ("band_task", pack.band_task_contract),
    ):
        for index, task in enumerate(tasks):
            resolve(
                task.target_name,
                f"{origin}:{index}",
                strict=task.task_type != "experience_delivery",
            )
            text_parts.append(task.description)
    for target in pack.chapter_entry_targets:
        resolve(target.entity_name, "entry_target", strict=False)
    for constraint in pack.active_future_constraints:
        resolve(constraint.subject_name, f"constraint:{constraint.id}")
        structured(constraint.payload, f"constraint:{constraint.id}")
    for obligation in [
        *pack.active_narrative_obligations,
        *pack.canon_quality_context.get("active_narrative_obligations", []),
    ]:
        structured(obligation, f"obligation:{obligation.get('id', '')}")
    for key in ("character_state_constraints", "invariant_constraints"):
        for constraint in pack.canon_quality_context.get(key, []):
            structured(constraint, key)
            subject = str(constraint.get("subject_key") or "")
            resolve(subject, key, strict=False)
    if pack.chapter_experience_plan:
        text_parts.append(str(pack.chapter_experience_plan.model_dump(mode="json")))
    if pack.repair_contract:
        text_parts.extend(
            [*pack.repair_contract.must_fix, *pack.repair_contract.must_preserve]
        )
    for scene in scene_plans:
        source = f"scene:{scene.scene_no}"
        for ref in scene.involved_entities:
            resolve(ref, source)
        resolve(scene.location_hint, source, strict=False)
        text_parts.extend(
            [scene.objective, scene.location_hint, *scene.must_progress_points]
        )
    # Exact known names in authored text are requirements, not a character-name
    # guesser. Unknown prose is not mistaken for an absent entity.
    for name in aliases:
        if len(name) < 2:
            continue
        pattern = re.escape(name)
        if name.isascii():
            pattern = rf"(?<![\w-]){pattern}(?![\w-])"
        if any(re.search(pattern, text) for text in text_parts):
            resolve(name, "plan_mention")
    # Retain earlier scene requirements when scenes are consumed sequentially.
    for prefix, keys in (
        ("node", pack.required_entity_ids),
        ("edge", pack.required_relation_ids),
    ):
        for key in keys:
            for source in pack.required_selection_sources.get(
                f"{prefix}:{key}", ["retained_requirement"]
            ):
                resolve(f"{prefix}:{key}", source)
    # Implicit relations only connect independently required participants. A hub
    # must not turn its entire peripheral cast into required input. Explicit
    # edge/fact refs above always preserve their actual endpoints.
    original_nodes = {
        key
        for key in required_nodes
        if sources[f"node:{key}"] - {"required_entity_relation"}
    }
    for edge in relations.values():
        if edge.source_id in original_nodes and edge.target_id in original_nodes:
            resolve(f"edge:{edge.relation_id}", "required_entity_relation")
    selected = {e.entity_id: e for e in pack.active_entities}
    selected.update({key: entities[key] for key in required_nodes})
    selected_relations = {e.relation_id: e for e in pack.active_relations}
    selected_relations.update({key: relations[key] for key in required_edges})
    if session is not None:
        baseline.assert_current(session)
    return pack.model_copy(
        update={
            "entity_name_candidates": identity_snapshot,
            "active_entities": list(selected.values()),
            "active_relations": list(selected_relations.values()),
            "required_entity_ids": sorted(required_nodes),
            "required_relation_ids": sorted(required_edges),
            "required_facts": {**pack.required_facts, **required_facts},
            "required_selection_sources": {
                key: sorted(value | set(pack.required_selection_sources.get(key, [])))
                for key, value in sources.items()
            },
            "allowed_entities": list(
                dict.fromkeys(
                    [
                        *pack.allowed_entities,
                        *(
                            entities[key].name
                            for key in sorted(required_nodes)
                            if entities[key].kind == "character"
                        ),
                    ]
                )
            ),
        }
    )
