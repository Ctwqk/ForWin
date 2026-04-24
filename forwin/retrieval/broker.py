from __future__ import annotations

import json
from typing import Iterable

from sqlalchemy import select

from forwin.context.assembler import assemble_context
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
        pack = self._filter_writer_safe_world_context(pack)
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

    def build_world_model_pack(
        self,
        repo,
        project_id: str,
        chapter_number: int,
        pack_kind: str,
    ) -> WorldModelRetrievalPack:
        """Build a role-specific v4 world-model retrieval pack.

        Writer-facing packs intentionally omit hidden objective truth. Review and
        compiler packs keep objective truth and planned reveal context so they can
        enforce information-asymmetry contracts.
        """
        pack_classes: dict[str, type[WorldModelRetrievalPack]] = {
            "planning": PlanningPack,
            "writing": WritingPack,
            "review": ReviewPack,
            "compiler": CompilerPack,
            "reader_experience": ReaderExperiencePack,
            "cognition": CognitionPack,
            "reveal": RevealPack,
        }
        if pack_kind not in pack_classes:
            raise ValueError(f"unknown world model pack kind: {pack_kind}")

        session = getattr(repo, "session", None)
        if session is None:
            raise TypeError("repo must expose a SQLAlchemy session")

        lines = list(
            session.execute(
                select(WorldLineRow)
                .where(WorldLineRow.project_id == project_id)
                .order_by(WorldLineRow.created_at.asc(), WorldLineRow.id.asc())
            )
            .scalars()
            .all()
        )
        deltas = list(
            session.execute(
                select(WorldDeltaRow)
                .where(
                    WorldDeltaRow.project_id == project_id,
                    WorldDeltaRow.narrative_chapter <= chapter_number,
                )
                .order_by(
                    WorldDeltaRow.narrative_chapter.desc(),
                    WorldDeltaRow.created_at.desc(),
                    WorldDeltaRow.id.desc(),
                )
                .limit(12)
            )
            .scalars()
            .all()
        )
        gaps = list(
            session.execute(
                select(KnowledgeGapRow)
                .where(
                    KnowledgeGapRow.project_id == project_id,
                    KnowledgeGapRow.status.in_(("open", "hinted", "partially_closed")),
                )
                .order_by(KnowledgeGapRow.created_at.asc(), KnowledgeGapRow.id.asc())
            )
            .scalars()
            .all()
        )
        reader_experience = list(
            session.execute(
                select(ReaderExperienceDeltaRow)
                .where(
                    ReaderExperienceDeltaRow.project_id == project_id,
                    ReaderExperienceDeltaRow.chapter_number <= chapter_number,
                )
                .order_by(
                    ReaderExperienceDeltaRow.chapter_number.desc(),
                    ReaderExperienceDeltaRow.created_at.desc(),
                    ReaderExperienceDeltaRow.id.desc(),
                )
                .limit(8)
            )
            .scalars()
            .all()
        )
        beliefs = list(
            session.execute(
                select(BeliefRow)
                .where(BeliefRow.project_id == project_id)
                .order_by(
                    BeliefRow.last_updated_at_chapter.desc(),
                    BeliefRow.created_at.desc(),
                    BeliefRow.id.desc(),
                )
                .limit(24)
            )
            .scalars()
            .all()
        )

        visible_lines = [
            line.world_line_id
            for line in lines
            if bool(line.is_visible_onstage)
        ]
        hidden_lines = [
            line.world_line_id
            for line in lines
            if line.world_line_id not in visible_lines
            or any(token in line.line_type for token in ("hidden", "secret", "antagonist"))
        ]
        active_lines = [
            line.world_line_id
            for line in lines
            if line.world_line_id in visible_lines or line.world_line_id in hidden_lines
        ]
        active_gap_ids = [gap.gap_id for gap in gaps]
        chapter_intent = WorldContractRepository(session).get_chapter_intent(
            project_id,
            chapter_number,
        )
        reveal_ladder = self._load_reveal_ladder(session, project_id)
        include_hidden_truth = pack_kind in {
            "planning",
            "review",
            "compiler",
            "cognition",
            "reveal",
        }
        hidden_objective_truths = (
            [
                gap.objective_truth
                for gap in gaps
                if gap.objective_truth and gap.related_world_line_id in hidden_lines
            ]
            if include_hidden_truth
            else []
        )

        reader_state: dict[str, str] = {}
        character_states: dict[str, dict[str, str]] = {}
        for belief in beliefs:
            entry = {
                "proposition": belief.proposition,
                "truth_relation": belief.truth_relation,
                "belief_status": belief.belief_status,
            }
            if belief.holder_type == "reader":
                reader_state[belief.belief_id] = belief.belief_status
            elif belief.holder_type == "character":
                character_states.setdefault(belief.holder_id, {})[belief.belief_id] = (
                    f"{entry['truth_relation']}:{entry['belief_status']}"
                )

        promise_debts = [
            item.next_desire or item.cognition_transition
            for item in reader_experience
            if int(item.promise_debt_change or 0) > 0
        ]
        recent_reader_exp = [
            item.cognition_transition or item.reader_experience_delta_id
            for item in reader_experience
        ]

        pack_cls = pack_classes[pack_kind]
        return pack_cls(
            project_id=project_id,
            as_of_chapter=chapter_number,
            active_world_lines=active_lines,
            visible_world_lines=visible_lines,
            hidden_world_lines=hidden_lines,
            recent_world_deltas=[delta.summary for delta in deltas],
            recent_offscreen_deltas=[
                delta.summary for delta in deltas if delta.delta_kind == "offscreen"
            ],
            active_knowledge_gaps=active_gap_ids,
            hidden_objective_truths=hidden_objective_truths,
            planned_reveal_ladder=reveal_ladder,
            reader_cognition_state=reader_state,
            character_cognition_states=character_states,
            observer_visibility_states=self._observer_visibility_from_gaps(gaps),
            promise_debts=promise_debts,
            recent_reader_experience_deltas=recent_reader_exp,
            must_not_reveal=list(chapter_intent.must_not_reveal)
            if isinstance(chapter_intent, ChapterWorldDeltaIntent)
            else [],
            fair_misdirection_requirements=self._fairness_requirements_from_gaps(gaps),
            accepted_delta_ids=[
                delta.delta_id for delta in deltas if bool(delta.allowed_for_canon)
            ],
            rejected_delta_ids=[
                delta.delta_id for delta in deltas if not bool(delta.allowed_for_canon)
            ],
            metadata={"hidden_truth_included": include_hidden_truth},
        )

    def _filter_writer_safe_world_context(self, pack: ChapterContextPack) -> ChapterContextPack:
        """Keep writer-facing v4 context to IDs, hints, and explicit reveal guards."""
        intent = pack.chapter_world_delta_intent
        if intent is None:
            return pack
        safe_intent = intent.model_copy(
            update={
                "offscreen_delta_intents": [],
                "reveal_delta_intents": [],
            }
        )
        return pack.model_copy(
            update={
                "chapter_world_delta_intent": safe_intent,
                "recent_offscreen_deltas": [],
                "must_not_reveal": list(intent.must_not_reveal),
            }
        )

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

    @staticmethod
    def _load_reveal_ladder(session, project_id: str) -> list[RevealLadderStep]:
        rows = list(
            session.execute(
                select(ArcWorldContractRow)
                .where(
                    ArcWorldContractRow.project_id == project_id,
                    ArcWorldContractRow.status == "active",
                )
                .order_by(
                    ArcWorldContractRow.arc_number.asc(),
                    ArcWorldContractRow.updated_at.desc(),
                    ArcWorldContractRow.id.desc(),
                )
            )
            .scalars()
            .all()
        )
        steps: list[RevealLadderStep] = []
        for row in rows:
            try:
                contract = ArcWorldContract.model_validate(
                    json.loads(row.contract_json or "{}")
                )
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            steps.extend(contract.reveal_ladder)
        return steps

    @staticmethod
    def _observer_visibility_from_gaps(gaps: list[KnowledgeGapRow]) -> dict[str, str]:
        states: dict[str, str] = {}
        for gap in gaps:
            try:
                payload = json.loads(gap.observer_states_json or "{}")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if not isinstance(payload, dict):
                continue
            for observer_id, state in payload.items():
                if isinstance(state, dict):
                    visibility = str(state.get("visibility", "") or "")
                    if visibility:
                        states[f"{gap.gap_id}:{observer_id}"] = visibility
        return states

    @staticmethod
    def _fairness_requirements_from_gaps(gaps: list[KnowledgeGapRow]) -> list[str]:
        requirements: list[str] = []
        for gap in gaps:
            try:
                payload = json.loads(gap.fairness_requirements_json or "[]")
            except (TypeError, ValueError, json.JSONDecodeError):
                continue
            if isinstance(payload, list):
                requirements.extend(str(item) for item in payload if str(item).strip())
        return requirements
