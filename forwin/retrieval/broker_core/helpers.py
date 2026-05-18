from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable

from sqlalchemy import select

from forwin.book_state.repository import BookStateRepository
from forwin.config import DEFAULT_QDRANT_URL
from forwin.context.assembler import assemble_context
from forwin.llm_kb.retriever import LLMKnowledgeBaseRetriever
from forwin.llm_kb.store import LLMKnowledgeBaseStore
from forwin.models.world_model import WorldModelConflictRow, WorldModelPageRow
from forwin.models.world_v4 import (
    ArcWorldContractRow,
    BeliefRow,
    KnowledgeGapRow,
    ReaderExperienceDeltaRow,
    WorldDeltaRow,
    WorldLineRow,
)
from forwin.planning.world_contracts import (
    ArcWorldContract,
    ChapterWorldDeltaIntent,
    RevealLadderStep,
    WorldContractRepository,
)
from forwin.protocol.context import (
    ChapterContextPack,
    CognitionPack,
    CompilerPack,
    EntitySnapshot,
    PlanningPack,
    PlotThreadSnapshot,
    ReaderExperiencePack,
    RelationSnapshot,
    RevealPack,
    ReviewPack,
    WorldModelRetrievalPack,
    WritingPack,
)
from forwin.protocol.world_model import WorldContextPack
from forwin.world_model.page_repository import WorldModelPageRepository
from forwin.world_model.store import load_json
from forwin.obsidian.frontmatter import parse_sections
from forwin.personality import CharacterPersonalityLibrary, build_active_personality_contexts
from forwin.retrieval.memory_index import ChapterMemoryIndex, create_memory_index


def _node_context(node) -> dict[str, object]:
    return {
        "id": node.id,
        "node_type": str(node.node_type),
        "name": node.name,
        "summary": node.summary or node.description,
        "status": node.status,
        "importance": node.importance,
        "source_refs": list(node.source_refs),
        "state_summary": str(node.state.get("state_summary", "")) if isinstance(node.state, dict) else "",
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
        profile = getattr(node, "profile", {}) if isinstance(getattr(node, "profile", {}), dict) else {}
        loadout = profile.get("personality_loadout") if isinstance(profile, dict) else None
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


def _database_url_from_repo(repo) -> str | None:  # noqa: ANN001
    session = getattr(repo, "session", None)
    if session is None:
        return None
    try:
        bind = session.get_bind()
    except Exception:  # noqa: BLE001
        return None
    url = getattr(bind, "url", None)
    if url is None:
        return None
    return url.render_as_string(hide_password=False)


__all__ = [
    '_node_context',
    '_edge_context',
    '_fact_context',
    '_map_node_context',
    '_map_edge_context',
    '_active_personality_contexts',
    '_truncate',
    '_extract_source_digest',
    '_database_url_from_repo',
]
