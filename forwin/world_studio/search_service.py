from __future__ import annotations

from pathlib import Path
from typing import Any

from forwin.book_state.repository import BookStateRepository
from forwin.llm_kb.retriever import LLMKnowledgeBaseRetriever
from forwin.llm_kb.store import DEFAULT_LLM_KB_ROOT
from forwin.retrieval.obsidian_human_index import ObsidianHumanVectorIndex
from forwin.retrieval.skill_index import SkillVectorIndex


class WorldStudioSearchService:
    def __init__(
        self,
        *,
        llm_kb_root: Path | None = None,
        skill_root: Path | None = None,
        qdrant_url: str | None = None,
        llm_kb_collection: str | None = None,
        obsidian_human_collection: str | None = None,
        skill_collection: str | None = None,
        qdrant_client: Any | None = None,
        qdrant_models: Any | None = None,
        session: Any | None = None,
    ) -> None:
        self.llm_kb_root = llm_kb_root or DEFAULT_LLM_KB_ROOT
        self.skill_root = skill_root or Path("forwin_skills")
        self.qdrant_url = qdrant_url
        self.llm_kb_collection = llm_kb_collection
        self.obsidian_human_collection = obsidian_human_collection
        self.skill_collection = skill_collection
        self.qdrant_client = qdrant_client
        self.qdrant_models = qdrant_models
        self.session = session

    def search(
        self,
        project_id: str,
        *,
        query: str,
        index_kind: str = "all",
        role: str = "human",
        as_of_chapter: int = 0,
        section_type: str = "",
        limit: int = 10,
    ) -> dict[str, Any]:
        kinds = _normalize_index_kinds(index_kind)
        kinds = _role_allowed_index_kinds(kinds, role)
        results: list[dict[str, Any]] = []
        per_index_limit = max(1, int(limit or 10))
        if "canon" in kinds and self.session is not None:
            results.extend(
                _canon_search(
                    self.session,
                    project_id,
                    query=query,
                    as_of_chapter=as_of_chapter,
                    limit=per_index_limit,
                )
            )
        if "obsidian_human" in kinds:
            results.extend(
                ObsidianHumanVectorIndex(
                    qdrant_url=self.qdrant_url,
                    collection_name=self.obsidian_human_collection,
                    qdrant_client=self.qdrant_client,
                    qdrant_models=self.qdrant_models,
                ).search(
                    project_id,
                    query,
                    limit=per_index_limit,
                    as_of_chapter=as_of_chapter,
                    section_type=section_type,
                )
            )
        if "llm_kb" in kinds:
            results.extend(
                LLMKnowledgeBaseRetriever(
                    root=self.llm_kb_root,
                    qdrant_url=self.qdrant_url,
                    qdrant_collection=self.llm_kb_collection,
                    qdrant_client=self.qdrant_client,
                    qdrant_models=self.qdrant_models,
                ).search(
                    project_id,
                    query,
                    role=_llm_role(role),
                    limit=per_index_limit,
                    as_of_chapter=as_of_chapter,
                )
            )
        if "skill" in kinds:
            results.extend(
                SkillVectorIndex(
                    qdrant_url=self.qdrant_url,
                    collection_name=self.skill_collection,
                    qdrant_client=self.qdrant_client,
                    qdrant_models=self.qdrant_models,
                ).search(query, limit=per_index_limit)
            )
        results.sort(key=lambda item: float(item.get("score") or 0.0), reverse=True)
        return {
            "project_id": project_id,
            "query": query,
            "index_kind": index_kind,
            "role": role,
            "results": results[: max(1, int(limit or 10))],
        }


def _normalize_index_kinds(index_kind: str) -> set[str]:
    normalized = str(index_kind or "all").strip().lower()
    if normalized == "all":
        return {"canon", "obsidian_human", "llm_kb", "skill"}
    if normalized in {"canon", "book_state"}:
        return {"canon"}
    if normalized in {"obsidian_human", "llm_kb", "skill"}:
        return {normalized}
    return set()


def _role_allowed_index_kinds(kinds: set[str], role: str) -> set[str]:
    normalized = str(role or "human").strip().lower()
    if normalized == "writer":
        return {kind for kind in kinds if kind in {"canon", "llm_kb"}}
    if normalized in {"planner", "compiler"}:
        return {kind for kind in kinds if kind in {"canon", "llm_kb"}}
    if normalized == "reviewer":
        return {kind for kind in kinds if kind in {"canon", "llm_kb", "skill"}}
    return kinds


def _canon_search(
    session,
    project_id: str,
    *,
    query: str,
    as_of_chapter: int = 0,
    limit: int = 10,
) -> list[dict[str, Any]]:
    query_text = str(query or "").strip().lower()
    if not query_text:
        return []
    repo = BookStateRepository(session)
    rows: list[dict[str, Any]] = []
    for node in repo.list_world_nodes(project_id, as_of_chapter=as_of_chapter or None):
        haystack = " ".join(
            str(value or "")
            for value in [
                node.id,
                node.node_type,
                node.name,
                node.summary,
                node.description,
                node.status,
            ]
        ).lower()
        if query_text in haystack:
            rows.append(
                {
                    "project_id": project_id,
                    "index_kind": "canon",
                    "source_type": "book_state_node",
                    "canon_status": "canon_projection",
                    "node_id": node.id,
                    "node_type": str(node.node_type),
                    "title": node.name or node.id,
                    "text": node.summary or node.description or node.name,
                    "as_of_chapter": as_of_chapter,
                    "source_refs": [f"book_state:node:{node.id}", *list(node.source_refs)],
                    "score": 1.0,
                }
            )
    for fact in repo.list_fact_nodes(project_id, as_of_chapter=as_of_chapter or None):
        haystack = " ".join(str(value or "") for value in [fact.id, fact.fact_type, fact.proposition]).lower()
        if query_text in haystack:
            rows.append(
                {
                    "project_id": project_id,
                    "index_kind": "canon",
                    "source_type": "book_state_fact",
                    "canon_status": "canon_projection",
                    "fact_id": fact.id,
                    "title": fact.fact_type or fact.id,
                    "text": fact.proposition,
                    "as_of_chapter": as_of_chapter,
                    "source_refs": [f"book_state:fact:{fact.id}", *list(fact.source_refs)],
                    "score": 1.0,
                }
            )
    for map_node in repo.list_map_nodes(project_id):
        haystack = " ".join(str(value or "") for value in [map_node.id, map_node.node_type, map_node.name]).lower()
        if query_text in haystack:
            rows.append(
                {
                    "project_id": project_id,
                    "index_kind": "canon",
                    "source_type": "book_state_map_node",
                    "canon_status": "canon_projection",
                    "map_refs": [f"map_node:{map_node.id}"],
                    "title": map_node.name or map_node.id,
                    "text": f"{map_node.name} ({map_node.node_type})",
                    "as_of_chapter": as_of_chapter,
                    "source_refs": [f"book_state:map_node:{map_node.id}"],
                    "score": 1.0,
                }
            )
    return rows[: max(1, int(limit or 10))]


def _llm_role(role: str) -> str:
    normalized = str(role or "human").strip().lower()
    if normalized in {"writer", "reviewer", "planner", "compiler"}:
        return normalized
    return "writer"
