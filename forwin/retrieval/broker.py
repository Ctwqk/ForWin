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


class RetrievalBroker:
    """Builds a task-specific writer view under a configurable context budget."""

    def __init__(
        self,
        context_budget_chars: int = 6000,
        max_entities: int = 8,
        max_threads: int = 4,
        max_summaries: int = 3,
    ) -> None:
        self.context_budget_chars = context_budget_chars
        self.max_entities = max_entities
        self.max_threads = max_threads
        self.max_summaries = max_summaries

    def build_chapter_context(self, repo, project_id: str, chapter_plan) -> ChapterContextPack:
        base_pack = assemble_context(repo, project_id, chapter_plan)

        summaries = self._pick_summaries(base_pack.previous_chapter_summaries)
        entities = self._pick_entities(base_pack.active_entities)
        threads = self._pick_threads(base_pack.active_threads)
        relations = self._pick_relations(base_pack.active_relations, entities)

        pack = base_pack.model_copy(
            update={
                "previous_chapter_summaries": summaries,
                "active_entities": entities,
                "active_threads": threads,
                "active_relations": relations,
            }
        )

        while self._estimate_chars(pack) > self.context_budget_chars:
            if pack.active_relations:
                pack = pack.model_copy(update={"active_relations": pack.active_relations[:-1]})
                continue
            if len(pack.active_entities) > 3:
                pack = pack.model_copy(update={"active_entities": pack.active_entities[:-1]})
                continue
            if len(pack.active_threads) > 1:
                pack = pack.model_copy(update={"active_threads": pack.active_threads[:-1]})
                continue
            if len(pack.previous_chapter_summaries) > 1:
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
