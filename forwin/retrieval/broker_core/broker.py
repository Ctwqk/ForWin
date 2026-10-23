from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from forwin.context import assemble_context
from forwin.llm_kb.retriever import LLMKnowledgeBaseRetriever
from forwin.llm_kb.store import LLMKnowledgeBaseStore
from forwin.protocol.context import (
    ChapterContextPack,
    EntitySnapshot,
    PlotThreadSnapshot,
    RelationSnapshot,
)
from forwin.protocol.world_model import WorldContextPack
from forwin.retrieval.memory_index import ChapterMemoryIndex
from forwin.retrieval.typed_budget import RetrievalBudget, bucket_memory_results
from .helpers import (
    _extract_source_digest,
    _truncate,
)


logger = logging.getLogger(__name__)


def _assemble_context(repo, project_id: str, chapter_plan) -> ChapterContextPack:
    return assemble_context(repo, project_id, chapter_plan)


class RetrievalBroker:
    """Builds a task-specific writer view under a configurable context budget."""

    def __init__(
        self,
        context_budget_chars: int = 6000,
        max_entities: int = 8,
        max_threads: int = 4,
        max_summaries: int = 3,
        max_memories: int = 3,
        max_world_pages: int = 6,
        memory_index: ChapterMemoryIndex | None = None,
        llm_kb_root: Path | None = None,
        llm_kb_qdrant_url: str | None = None,
        llm_kb_qdrant_collection: str | None = None,
        llm_kb_qdrant_client: object | None = None,
        llm_kb_qdrant_models: object | None = None,
        retrieval_budget: RetrievalBudget | None = None,
        memory_index_provider: Callable[[], ChapterMemoryIndex] | None = None,
        llm_kb_retriever_provider: Callable[[], LLMKnowledgeBaseRetriever] | None = None,
    ) -> None:
        if memory_index is not None and memory_index_provider is not None:
            raise ValueError("Pass memory_index or memory_index_provider, not both.")
        self.context_budget_chars = context_budget_chars
        self.max_entities = max_entities
        self.max_threads = max_threads
        self.max_summaries = max_summaries
        self.max_memories = max_memories
        self.max_world_pages = max_world_pages
        self.memory_index = memory_index
        self._owns_memory_index = memory_index_provider is not None
        self.llm_kb_root = llm_kb_root
        self.llm_kb_qdrant_url = llm_kb_qdrant_url
        self.llm_kb_qdrant_collection = llm_kb_qdrant_collection
        self.llm_kb_qdrant_client = llm_kb_qdrant_client
        self.llm_kb_qdrant_models = llm_kb_qdrant_models
        self._memory_index_provider = memory_index_provider
        self._llm_kb_retriever_provider = (
            llm_kb_retriever_provider or self._build_configured_llm_kb_retriever
        )
        self._llm_kb_retriever: LLMKnowledgeBaseRetriever | None = None
        self._owns_llm_kb_retriever = True
        self._closed = False
        self._memory_index_lock = threading.Lock()
        self._llm_kb_retriever_lock = threading.Lock()
        self.retrieval_budget = retrieval_budget or RetrievalBudget()
        self.last_observability_summary: dict[str, object] = {}

    def build_chapter_context(
        self, repo, project_id: str, chapter_plan
    ) -> ChapterContextPack:
        self._ensure_memory_index(repo)
        base_pack = _assemble_context(repo, project_id, chapter_plan)
        summaries = self._pick_summaries(base_pack.previous_chapter_summaries)
        entities = self._pick_entities(base_pack.active_entities)
        threads = self._pick_threads(base_pack.active_threads)
        relations = self._pick_relations(base_pack.active_relations, entities)
        memories = self._pick_memories(base_pack)
        world_context = self._pick_world_context(base_pack.world_context)

        pack = base_pack.model_copy(
            update={
                "previous_chapter_summaries": summaries,
                "active_entities": entities,
                "active_threads": threads,
                "active_relations": relations,
                "retrieved_memories": memories,
                "world_context": world_context,
            }
        )
        pack = self._trim_pack(pack)
        self._finalize_context_summary(
            base_pack=base_pack, pack=pack, memories=memories
        )

        return pack

    def _trim_pack(self, pack: ChapterContextPack) -> ChapterContextPack:
        summaries = self._pick_summaries(list(pack.previous_chapter_summaries))
        entities = self._pick_entities(list(pack.active_entities))
        threads = self._pick_threads(list(pack.active_threads))
        relations = self._pick_relations(list(pack.active_relations), entities)
        pack = pack.model_copy(
            update={
                "previous_chapter_summaries": summaries,
                "active_entities": entities,
                "active_threads": threads,
                "active_relations": relations,
            }
        )
        estimate = self._estimate_pack_with_components(pack)

        while estimate > self.context_budget_chars:
            if pack.active_relations:
                removed = pack.active_relations[-1]
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(
                    update={"active_relations": pack.active_relations[:-1]}
                )
                continue
            memories = list(getattr(pack, "retrieved_memories", []) or [])
            if memories:
                next_memories, removed = self._drop_lowest_priority_memory(memories)
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(update={"retrieved_memories": next_memories})
                continue
            if len(pack.active_entities) > 3:
                removed = pack.active_entities[-1]
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(
                    update={"active_entities": pack.active_entities[:-1]}
                )
                continue
            if len(pack.active_threads) > 1:
                removed = pack.active_threads[-1]
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(
                    update={"active_threads": pack.active_threads[:-1]}
                )
                continue
            if len(pack.previous_chapter_summaries) > 1:
                removed = pack.previous_chapter_summaries[0]
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(
                    update={
                        "previous_chapter_summaries": pack.previous_chapter_summaries[
                            1:
                        ]
                    }
                )
                continue
            world_context = getattr(pack, "world_context", None)
            relevant_pages = getattr(world_context, "relevant_world_pages", []) or []
            if len(relevant_pages) > 1 and hasattr(world_context, "model_copy"):
                next_world = world_context.model_copy(
                    update={
                        "relevant_world_pages": relevant_pages[:-1],
                    }
                )
                pack = pack.model_copy(update={"world_context": next_world})
                estimate = self._estimate_pack_with_components(pack)
                continue
            break
        return pack

    def _finalize_context_summary(
        self,
        *,
        base_pack: ChapterContextPack,
        pack: ChapterContextPack,
        memories: list[object],
    ) -> None:
        before_chars = self._estimate_pack_with_components(base_pack)
        after_chars = self._estimate_pack_with_components(pack)
        memories_before = len(
            getattr(base_pack, "retrieved_memories", []) or memories or []
        )
        memories_after = len(getattr(pack, "retrieved_memories", []) or [])
        self.last_observability_summary = {
            "chapter_number": int(getattr(pack, "chapter_number", 0) or 0),
            "active_entities_count_before": len(base_pack.active_entities),
            "active_entities_count_after": len(pack.active_entities),
            "relations_count_before": len(base_pack.active_relations),
            "relations_count_after": len(pack.active_relations),
            "threads_count_before": len(base_pack.active_threads),
            "threads_count_after": len(pack.active_threads),
            "summaries_count_before": len(base_pack.previous_chapter_summaries),
            "summaries_count_after": len(pack.previous_chapter_summaries),
            "memories_count_before": memories_before,
            "memories_count_after": memories_after,
            "memories_count": memories_after,
            "estimated_context_chars_before": before_chars,
            "estimated_context_chars_after": after_chars,
            "pruned_entities": max(
                0, len(base_pack.active_entities) - len(pack.active_entities)
            ),
            "pruned_threads": max(
                0, len(base_pack.active_threads) - len(pack.active_threads)
            ),
            "pruned_relations": max(
                0, len(base_pack.active_relations) - len(pack.active_relations)
            ),
            "pruned_memories": max(0, memories_before - memories_after),
        }

    @staticmethod
    def _memory_eviction_rank(memory: object) -> tuple[int, int]:
        memory_type = str(getattr(memory, "memory_type", "") or "").strip()
        priority = {
            "recent": 0,
            "relationship": 1,
            "world": 1,
            "enemy": 2,
            "wealth_status": 2,
            "promise": 2,
        }.get(memory_type, 1)
        return priority, int(getattr(memory, "chapter_number", 0) or 0)

    def _drop_lowest_priority_memory(
        self,
        memories: list[object],
    ) -> tuple[list[object], object | None]:
        if not memories:
            return memories, None
        remove_index = min(
            range(len(memories)),
            key=lambda index: (self._memory_eviction_rank(memories[index]), -index),
        )
        removed = memories[remove_index]
        return memories[:remove_index] + memories[remove_index + 1 :], removed


    def _load_llm_kb_context(
        self, project_id: str, *, pack_kind: str, query: str = ""
    ) -> dict[str, object]:
        store = LLMKnowledgeBaseStore(root=self.llm_kb_root)
        files = store.list_files(project_id)
        if not files:
            return {}
        safe_file_keys = [
            "CURRENT_STATE.md",
            "NEXT_CHAPTER_CONTEXT.md",
            "ACTIVE_THREADS.md",
            "CHARACTER_MEMORY.md",
            "MAP_CONTEXT.md",
            "READER_PROMISES.md",
            "KNOWLEDGE_GAPS.md",
            "MUST_NOT_REVEAL.md",
            "RECENT_CHANGES.md",
        ]
        excerpts: dict[str, str] = {}
        source_digest = ""
        for key in safe_file_keys:
            try:
                content = store.read_file(project_id, key)
            except (FileNotFoundError, ValueError):
                continue
            excerpts[key] = _truncate(content, limit=1200)
            if not source_digest:
                source_digest = _extract_source_digest(content)
        search_results: list[dict[str, object]] = []
        if str(query or "").strip():
            role = {
                "writing": "writer",
                "review": "reviewer",
                "planning": "planner",
                "compiler": "compiler",
            }.get(pack_kind, "writer")
            search_results = self._ensure_llm_kb_retriever().search(
                project_id,
                query,
                role=role,
                limit=5,
            )
        return {
            "root_policy": "writer_safe",
            "pack_kind": pack_kind,
            "files": [item["file_key"] for item in files],
            "source_digest": source_digest,
            "excerpts": excerpts,
            "search_results": search_results,
        }

    def _ensure_memory_index(self, _repo=None) -> None:  # noqa: ANN001
        if self._closed:
            raise RuntimeError("RetrievalBroker is closed")
        if self.memory_index is not None:
            return
        if self._memory_index_provider is None:
            raise RuntimeError(
                "memory_index or memory_index_provider is required for chapter retrieval"
            )
        with self._memory_index_lock:
            if self._closed:
                raise RuntimeError("RetrievalBroker is closed")
            if self.memory_index is not None:
                return
            memory_index = self._memory_index_provider()
            if memory_index is None:
                raise RuntimeError("memory_index_provider returned no memory index")
            self.memory_index = memory_index

    def resolve_memory_index(self) -> ChapterMemoryIndex:
        self._ensure_memory_index()
        if self.memory_index is None:  # pragma: no cover
            raise RuntimeError("memory index initialization did not complete")
        return self.memory_index

    def _ensure_llm_kb_retriever(self) -> LLMKnowledgeBaseRetriever:
        if self._closed:
            raise RuntimeError("RetrievalBroker is closed")
        if self._llm_kb_retriever is not None:
            return self._llm_kb_retriever
        with self._llm_kb_retriever_lock:
            if self._closed:
                raise RuntimeError("RetrievalBroker is closed")
            if self._llm_kb_retriever is not None:
                return self._llm_kb_retriever
            retriever = self._llm_kb_retriever_provider()
            if retriever is None:
                raise RuntimeError(
                    "llm_kb_retriever_provider returned no retriever"
                )
            self._llm_kb_retriever = retriever
            return retriever

    def close(self) -> None:
        with self._memory_index_lock, self._llm_kb_retriever_lock:
            if self._closed:
                return
            self._closed = True
            memory_index = self.memory_index if self._owns_memory_index else None
            retriever = (
                self._llm_kb_retriever if self._owns_llm_kb_retriever else None
            )
            self.memory_index = None
            self._llm_kb_retriever = None
        for resource in (memory_index, retriever):
            close = getattr(resource, "close", None)
            if not callable(close):
                continue
            try:
                close()
            except Exception:  # noqa: BLE001
                logger.debug("Retrieval resource close failed.", exc_info=True)

    def _build_configured_llm_kb_retriever(self) -> LLMKnowledgeBaseRetriever:
        if self.llm_kb_qdrant_client is None and not self.llm_kb_qdrant_url:
            raise RuntimeError(
                "llm_kb_retriever_provider or explicit llm_kb_qdrant_url is required"
            )
        if not self.llm_kb_qdrant_collection:
            raise RuntimeError(
                "llm_kb_retriever_provider or explicit llm_kb_qdrant_collection is required"
            )
        return LLMKnowledgeBaseRetriever(
            root=self.llm_kb_root,
            qdrant_url=self.llm_kb_qdrant_url,
            qdrant_collection=self.llm_kb_qdrant_collection,
            qdrant_client=self.llm_kb_qdrant_client,
            qdrant_models=self.llm_kb_qdrant_models,
        )


    def _pick_summaries(self, summaries: list[str]) -> list[str]:
        return summaries[-self.max_summaries :]

    def _pick_entities(self, entities: list[EntitySnapshot]) -> list[EntitySnapshot]:
        ranked = sorted(entities, key=lambda item: (-item.importance, item.name))
        return ranked[: self.max_entities]

    def _pick_threads(
        self, threads: list[PlotThreadSnapshot]
    ) -> list[PlotThreadSnapshot]:
        # Lower numeric priority means more important, matching the DB/order semantics
        # used by thread sampling and phase analyzers.
        ranked = sorted(threads, key=lambda item: (item.priority, item.name))
        return ranked[: self.max_threads]

    def _pick_relations(
        self,
        relations: list[RelationSnapshot],
        entities: Iterable[EntitySnapshot],
    ) -> list[RelationSnapshot]:
        entity_names = {entity.name for entity in entities}
        return [
            relation
            for relation in relations
            if relation.source_name in entity_names
            or relation.target_name in entity_names
        ]

    @staticmethod
    def _estimate_chars(pack: ChapterContextPack) -> int:
        payload = pack.model_dump(mode="json")
        return len(json.dumps(payload, ensure_ascii=False))

    @classmethod
    def _estimate_pack_with_components(cls, pack: ChapterContextPack) -> int:
        empty_pack = pack.model_copy(
            update={
                "previous_chapter_summaries": [],
                "active_entities": [],
                "active_threads": [],
                "active_relations": [],
                "retrieved_memories": [],
                "world_context": WorldContextPack(),
                # The world view keeps to its own budget.
                "world_view": None,
            }
        )
        total = cls._estimate_chars(empty_pack)
        total += sum(
            cls._estimate_component_chars(item)
            for item in getattr(pack, "previous_chapter_summaries", []) or []
        )
        total += sum(
            cls._estimate_component_chars(item)
            for item in getattr(pack, "active_entities", []) or []
        )
        total += sum(
            cls._estimate_component_chars(item)
            for item in getattr(pack, "active_threads", []) or []
        )
        total += sum(
            cls._estimate_component_chars(item)
            for item in getattr(pack, "active_relations", []) or []
        )
        total += sum(
            cls._estimate_component_chars(item)
            for item in getattr(pack, "retrieved_memories", []) or []
        )
        world_context = getattr(pack, "world_context", None)
        if world_context is not None:
            if hasattr(world_context, "model_copy"):
                total += cls._estimate_component_chars(
                    world_context.model_copy(update={"relevant_world_pages": []})
                )
            total += sum(
                cls._estimate_component_chars(item)
                for item in getattr(world_context, "relevant_world_pages", []) or []
            )
        return total

    @staticmethod
    def _estimate_component_chars(item: object) -> int:
        if hasattr(item, "model_dump"):
            payload = item.model_dump(mode="json")
        elif hasattr(item, "__dict__"):
            payload = {
                key: value
                for key, value in vars(item).items()
                if isinstance(value, (str, int, float, bool, list, dict, type(None)))
            }
        else:
            payload = item
        try:
            return len(json.dumps(payload, ensure_ascii=False)) + 1
        except TypeError:
            return len(str(payload)) + 1

    def _pick_memories(self, base_pack: ChapterContextPack):
        query_parts = [
            base_pack.chapter_plan_title,
            base_pack.chapter_plan_one_line,
            *base_pack.chapter_goals,
            *(thread.name for thread in base_pack.active_threads[: self.max_threads]),
            *(entity.name for entity in base_pack.active_entities[: self.max_entities]),
        ]
        query = "\n".join(part for part in query_parts if part)
        if not query.strip():
            return []
        raw_limit = max(
            self.max_memories, sum(self.retrieval_budget.model_dump().values())
        )
        memories = self.memory_index.search(
            project_id=base_pack.project_id,
            query=query,
            limit=raw_limit,
        )
        eligible = [
            memory
            for memory in memories
            if memory.chapter_number < base_pack.chapter_number
        ]
        buckets = bucket_memory_results(eligible, self.retrieval_budget)
        selected = []
        for key in (
            "recent",
            "promise",
            "enemy",
            "wealth_status",
            "relationship",
            "world",
        ):
            selected.extend(buckets[key])
        return selected[:raw_limit]

    def _pick_world_context(self, world_context: WorldContextPack) -> WorldContextPack:
        if not world_context or not world_context.snapshot_id:
            return WorldContextPack()
        conflicts = list(world_context.active_world_conflicts[:8])
        pages = list(world_context.relevant_world_pages)
        priority = {
            "contradiction": 8,
            "secret": 7,
            "promise": 6,
            "character": 5,
            "faction": 4,
            "region": 3,
            "node": 3,
            "overview": 2,
        }
        pages = sorted(
            pages,
            key=lambda page: (priority.get(page.page_type, 1), page.title),
            reverse=True,
        )
        return world_context.model_copy(
            update={
                "relevant_world_pages": pages[: self.max_world_pages],
                "active_world_conflicts": conflicts,
                "active_secrets": [
                    page for page in pages if page.page_type == "secret"
                ][:3],
                "active_promises": [
                    page for page in pages if page.page_type == "promise"
                ][:3],
                "active_resource_constraints": [
                    page for page in pages if page.page_type in {"resource", "currency"}
                ][:3],
                "active_institution_rules": [
                    page for page in pages if page.page_type == "institution"
                ][:3],
            }
        )


__all__ = [
    "RetrievalBroker",
]
