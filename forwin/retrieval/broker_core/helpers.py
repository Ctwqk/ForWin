from __future__ import annotations

import json

from forwin.personality import (
    CharacterPersonalityLibrary,
    build_active_personality_contexts,
)
from forwin.protocol.context import ChapterContextPack


def _node_context(node) -> dict[str, object]:
    return {
        "id": node.id,
        "node_type": str(node.node_type),
        "name": node.name,
        "summary": node.summary or node.description,
        "status": node.status,
        "importance": node.importance,
        "source_refs": list(node.source_refs),
        "state_summary": str(node.state.get("state_summary", ""))
        if isinstance(node.state, dict)
        else "",
    }


def _edge_context(edge) -> dict[str, object]:
    return {
        "id": edge.id,
        "source_id": edge.source_id,
        "target_id": edge.target_id,
        "edge_type": edge.edge_type,
        "edge_family": str(edge.edge_family),
        "status": edge.status,
        "visibility": edge.visibility or edge.visibility_default,
        "truth_relation": edge.truth_relation,
        "source_refs": list(edge.source_refs or edge.evidence_refs),
    }


def _fact_context(fact) -> dict[str, object]:
    return {
        "id": fact.id,
        "proposition": fact.proposition,
        "fact_type": fact.fact_type,
        "truth_value": fact.truth_value,
        "confidence": fact.confidence,
        "source_refs": list(fact.source_refs),
    }


def _map_node_context(node) -> dict[str, object]:
    return {
        "id": node.id,
        "node_type": str(node.node_type),
        "name": node.name,
        "subworld_id": node.subworld_id,
        "region_id": node.region_id,
        "status": node.status,
        "danger_level": node.default_danger_level,
    }


def _map_edge_context(edge) -> dict[str, object]:
    return {
        "id": edge.id,
        "from_node_id": edge.from_node_id,
        "to_node_id": edge.to_node_id,
        "edge_type": str(edge.edge_type),
        "status": edge.status,
        "travel_time": edge.travel_time,
        "risk_level": edge.risk_level,
        "visibility": edge.visibility_default,
    }


def _active_personality_contexts(nodes: list[object]) -> list[dict[str, object]]:
    characters = []
    for node in nodes:
        if str(getattr(node, "node_type", "") or "") != "character":
            continue
        profile = (
            getattr(node, "profile", {})
            if isinstance(getattr(node, "profile", {}), dict)
            else {}
        )
        loadout = (
            profile.get("personality_loadout") if isinstance(profile, dict) else None
        )
        if not loadout:
            continue
        characters.append(
            {
                "character_id": getattr(node, "id", ""),
                "character_name": getattr(node, "name", ""),
                "personality_loadout": loadout,
            }
        )
    if not characters:
        return []
    try:
        return [
            item.model_dump(mode="json")
            for item in build_active_personality_contexts(
                characters,
                library=CharacterPersonalityLibrary(),
                scene_flags=["chapter_generation"],
            )
        ]
    except Exception:
        return []


def _truncate(value: str, *, limit: int = 600) -> str:
    text = str(value or "").strip()
    if len(text) <= limit:
        return text
    return text[: limit - 3].rstrip() + "..."


def _extract_source_digest(content: str) -> str:
    for line in content.splitlines():
        if line.startswith("source_digest:"):
            return line.split(":", 1)[1].strip()
    return ""


__all__ = [
    "_node_context",
    "_edge_context",
    "_fact_context",
    "_map_node_context",
    "_map_edge_context",
    "_active_personality_contexts",
    "_truncate",
    "_extract_source_digest",
]


def _budget_genesis_references(pack: ChapterContextPack, max_chars: int) -> ChapterContextPack:
    """Reserve only a bounded share for whole writing-time source statements."""
    remaining_chars = max(0, max_chars)
    selected = []
    for fact in pack.genesis_reference_facts:
        size = len(json.dumps(fact.model_dump(mode="json"), ensure_ascii=False))
        if size <= remaining_chars:
            selected.append(fact)
            remaining_chars -= size
    return pack.model_copy(update={
        "genesis_reference_facts": selected,
        "genesis_reference_omitted_count": (
            pack.genesis_reference_omitted_count + len(pack.genesis_reference_facts) - len(selected)
        ),
    })


def _drop_unrelated_genesis_reference(pack: ChapterContextPack) -> ChapterContextPack | None:
    """Under pressure, offstage source background precedes current Canon eviction."""
    chapter_text = "\n".join([pack.chapter_plan_title, pack.chapter_plan_one_line, *pack.chapter_goals])
    current_names = {entity.name for entity in pack.active_entities}
    for index in range(len(pack.genesis_reference_facts) - 1, -1, -1):
        fact = pack.genesis_reference_facts[index]
        if (fact.category == "character_secret" and fact.subject not in current_names
                and fact.subject not in chapter_text):
            selected = list(pack.genesis_reference_facts)
            selected.pop(index)
            return pack.model_copy(update={
                "genesis_reference_facts": selected,
                "genesis_reference_omitted_count": pack.genesis_reference_omitted_count + 1,
            })
    return None
