from __future__ import annotations

import json
from typing import Iterable

from forwin.context.assembler import assemble_context
from forwin.protocol.context import (
    ChapterContextPack,
    EntitySnapshot,
    PlotThreadSnapshot,
    RelationSnapshot,
)
from forwin.protocol.world_model import WorldContextPack
from .memory_index import ChapterMemoryIndex, create_memory_index


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
    ) -> None:
        self.context_budget_chars = context_budget_chars
        self.max_entities = max_entities
        self.max_threads = max_threads
        self.max_summaries = max_summaries
        self.max_memories = max_memories
        self.max_world_pages = max_world_pages
        self.memory_index = memory_index or create_memory_index()
        self.last_observability_summary: dict[str, object] = {}

    def build_chapter_context(self, repo, project_id: str, chapter_plan) -> ChapterContextPack:
        base_pack = assemble_context(repo, project_id, chapter_plan)

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
        self._finalize_context_summary(base_pack=base_pack, pack=pack, memories=memories)

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
                pack = pack.model_copy(update={"active_relations": pack.active_relations[:-1]})
                continue
            if len(pack.active_entities) > 3:
                removed = pack.active_entities[-1]
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(update={"active_entities": pack.active_entities[:-1]})
                continue
            if len(pack.active_threads) > 1:
                removed = pack.active_threads[-1]
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(update={"active_threads": pack.active_threads[:-1]})
                continue
            if len(pack.previous_chapter_summaries) > 1:
                removed = pack.previous_chapter_summaries[0]
                estimate -= self._estimate_component_chars(removed)
                pack = pack.model_copy(
                    update={"previous_chapter_summaries": pack.previous_chapter_summaries[1:]}
                )
                continue
            if len(pack.world_context.relevant_world_pages) > 1:
                next_world = pack.world_context.model_copy(
                    update={
                        "relevant_world_pages": pack.world_context.relevant_world_pages[:-1],
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
            "memories_count": len(getattr(pack, "retrieved_memories", []) or memories or []),
            "estimated_context_chars_before": before_chars,
            "estimated_context_chars_after": after_chars,
            "pruned_entities": max(0, len(base_pack.active_entities) - len(pack.active_entities)),
            "pruned_threads": max(0, len(base_pack.active_threads) - len(pack.active_threads)),
            "pruned_relations": max(0, len(base_pack.active_relations) - len(pack.active_relations)),
        }

    def _pick_summaries(self, summaries: list[str]) -> list[str]:
        return summaries[-self.max_summaries :]

    def _pick_entities(self, entities: list[EntitySnapshot]) -> list[EntitySnapshot]:
        ranked = sorted(entities, key=lambda item: (-item.importance, item.name))
        return ranked[: self.max_entities]

    def _pick_threads(self, threads: list[PlotThreadSnapshot]) -> list[PlotThreadSnapshot]:
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
            if relation.source_name in entity_names or relation.target_name in entity_names
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
            }
        )
        total = cls._estimate_chars(empty_pack)
        total += sum(cls._estimate_component_chars(item) for item in getattr(pack, "previous_chapter_summaries", []) or [])
        total += sum(cls._estimate_component_chars(item) for item in getattr(pack, "active_entities", []) or [])
        total += sum(cls._estimate_component_chars(item) for item in getattr(pack, "active_threads", []) or [])
        total += sum(cls._estimate_component_chars(item) for item in getattr(pack, "active_relations", []) or [])
        total += sum(cls._estimate_component_chars(item) for item in getattr(pack, "retrieved_memories", []) or [])
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
        memories = self.memory_index.search(
            project_id=base_pack.project_id,
            query=query,
            limit=self.max_memories,
        )
        return [
            memory
            for memory in memories
            if memory.chapter_number < base_pack.chapter_number
        ]

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
        pages = sorted(pages, key=lambda page: (priority.get(page.page_type, 1), page.title), reverse=True)
        return world_context.model_copy(
            update={
                "relevant_world_pages": pages[: self.max_world_pages],
                "active_world_conflicts": conflicts,
                "active_secrets": [page for page in pages if page.page_type == "secret"][:3],
                "active_promises": [page for page in pages if page.page_type == "promise"][:3],
                "active_resource_constraints": [page for page in pages if page.page_type in {"resource", "currency"}][:3],
                "active_institution_rules": [page for page in pages if page.page_type == "institution"][:3],
            }
        )
