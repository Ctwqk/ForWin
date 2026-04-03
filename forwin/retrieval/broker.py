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
        memory_index: ChapterMemoryIndex | None = None,
    ) -> None:
        self.context_budget_chars = context_budget_chars
        self.max_entities = max_entities
        self.max_threads = max_threads
        self.max_summaries = max_summaries
        self.max_memories = max_memories
        self.memory_index = memory_index or create_memory_index()

    def build_chapter_context(self, repo, project_id: str, chapter_plan) -> ChapterContextPack:
        base_pack = assemble_context(repo, project_id, chapter_plan)

        summaries = self._pick_summaries(base_pack.previous_chapter_summaries)
        entities = self._pick_entities(base_pack.active_entities)
        threads = self._pick_threads(base_pack.active_threads)
        relations = self._pick_relations(base_pack.active_relations, entities)
        memories = self._pick_memories(base_pack)

        pack = base_pack.model_copy(
            update={
                "previous_chapter_summaries": summaries,
                "active_entities": entities,
                "active_threads": threads,
                "active_relations": relations,
                "retrieved_memories": memories,
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
            break

        return pack

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
            }
        )
        total = cls._estimate_chars(empty_pack)
        total += sum(cls._estimate_component_chars(item) for item in pack.previous_chapter_summaries)
        total += sum(cls._estimate_component_chars(item) for item in pack.active_entities)
        total += sum(cls._estimate_component_chars(item) for item in pack.active_threads)
        total += sum(cls._estimate_component_chars(item) for item in pack.active_relations)
        total += sum(cls._estimate_component_chars(item) for item in pack.retrieved_memories)
        return total

    @staticmethod
    def _estimate_component_chars(item: object) -> int:
        if hasattr(item, "model_dump"):
            payload = item.model_dump(mode="json")
        else:
            payload = item
        return len(json.dumps(payload, ensure_ascii=False)) + 1

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
