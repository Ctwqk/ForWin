from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from fastapi import HTTPException

from forwin.llm_kb import LLMKnowledgeBaseCompiler, LLMKnowledgeBaseRetriever, LLMKnowledgeBaseStore
from forwin.models.project import Project
from forwin.retrieval.broker import RetrievalBroker
from forwin.state.repo import StateRepository


ROLE_PACK_KIND = {
    "writer": "writing",
    "reviewer": "review",
    "planner": "planning",
    "compiler": "compiler",
}


def _require_project(session, project_id: str) -> Project:
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(status_code=404, detail="project not found")
    return project


def build_handlers(*, get_session: Callable[[], Any], llm_kb_root: Path | None = None) -> dict[str, Callable[..., Any]]:
    def rebuild_llm_kb(project_id: str, as_of_chapter: int = 0) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            result = LLMKnowledgeBaseCompiler(session, root=llm_kb_root).rebuild(project_id, as_of_chapter=as_of_chapter)
            session.commit()
            return {
                "ok": True,
                "project_id": project_id,
                "root": result.root,
                "as_of_chapter": result.as_of_chapter,
                "files": result.files,
                "source_digest": result.source_digest,
                "vector_index": result.vector_index,
            }

    def list_llm_kb_files(project_id: str) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            return {"project_id": project_id, "files": LLMKnowledgeBaseStore(root=llm_kb_root).list_files(project_id)}

    def get_llm_kb_file(project_id: str, file_key: str) -> dict[str, Any]:
        with get_session() as session:
            _require_project(session, project_id)
            try:
                content = LLMKnowledgeBaseStore(root=llm_kb_root).read_file(project_id, file_key)
            except ValueError as exc:
                raise HTTPException(status_code=400, detail=str(exc)) from exc
            except FileNotFoundError as exc:
                raise HTTPException(status_code=404, detail="LLM KB file not found") from exc
            return {"project_id": project_id, "file_key": file_key, "content": content}

    def search_llm_kb(project_id: str, query: str, role: str = "writer", limit: int = 5) -> dict[str, Any]:
        role_key = str(role or "").strip().lower()
        if role_key not in ROLE_PACK_KIND:
            raise HTTPException(status_code=404, detail="search role must be writer, reviewer, planner, or compiler")
        with get_session() as session:
            _require_project(session, project_id)
            results = LLMKnowledgeBaseRetriever(root=llm_kb_root).search(project_id, query, role=role_key, limit=limit)
            return {"project_id": project_id, "role": role_key, "query": query, "results": results}

    def get_context_pack(project_id: str, role: str, chapter_number: int = 0, query: str = "") -> dict[str, Any]:
        role_key = str(role or "").strip().lower()
        if role_key not in ROLE_PACK_KIND:
            raise HTTPException(status_code=404, detail="context pack role must be writer, reviewer, planner, or compiler")
        with get_session() as session:
            _require_project(session, project_id)
            pack_kind = ROLE_PACK_KIND[role_key]
            pack = RetrievalBroker(llm_kb_root=llm_kb_root).build_world_model_pack(
                StateRepository(session),
                project_id,
                chapter_number,
                pack_kind,
                query=query,
            )
            payload = pack.model_dump(mode="json")
            payload["requested_role"] = role_key
            payload["pack_kind"] = pack_kind
            return payload

    return {
        "rebuild_llm_kb": rebuild_llm_kb,
        "list_llm_kb_files": list_llm_kb_files,
        "get_llm_kb_file": get_llm_kb_file,
        "search_llm_kb": search_llm_kb,
        "get_context_pack": get_context_pack,
    }
