from __future__ import annotations

import json
import logging
import threading
from collections.abc import Callable, Iterable
from pathlib import Path

from sqlalchemy import select

from forwin.book_state.repository import BookStateRepository
from forwin.book_state.query import BookStateQuery
from forwin.retrieval.requirements import hydrate_requirements, RequiredContextError
from forwin.retrieval.source_identity import CanonReadBaseline, CanonBaselineChanged, fresh_orm_reads
from forwin.book_state.visibility import book_state_node_hidden
from forwin.context import assemble_context
from forwin.knowledge_system.page_repository import KnowledgePageRepository
from forwin.knowledge_system.store import load_json
from forwin.llm_kb.retriever import LLMKnowledgeBaseRetriever
from forwin.llm_kb.store import LLMKnowledgeBaseStore
from forwin.models.world_contract import (
    ArcWorldContractRow,
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
    RepairContract,
    RelationSnapshot,
    RevealPack,
    ReviewPack,
    WorldModelRetrievalPack,
    WritingPack,
)
from forwin.protocol.world_model import WorldContextPack
from forwin.protocol.scene import ScenePlan
from forwin.obsidian.frontmatter import frontmatter_hidden, parse_sections
from forwin.retrieval.memory_index import ChapterMemoryIndex
from forwin.retrieval.typed_budget import RetrievalBudget, bucket_memory_results
from forwin.writer.prompt_core.builders import writer_context_chars
from .helpers import (
    _budget_genesis_references,
    _drop_unrelated_genesis_reference,
    _active_personality_contexts,
    _edge_context,
    _extract_source_digest,
    _fact_context,
    _map_edge_context,
    _map_node_context,
    _node_context,
    _truncate,
)

from .visibility import (
    _book_state_edge_hidden,
    _book_state_fact_hidden,
    _map_edge_hidden,
)


logger = logging.getLogger(__name__)


def _assemble_context(repo, project_id: str, chapter_plan, *, baseline=None) -> ChapterContextPack:
    return assemble_context(repo, project_id, chapter_plan, baseline=baseline)


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
        self, repo, project_id: str, chapter_plan, *, baseline: CanonReadBaseline | None = None
    ) -> ChapterContextPack:
        session = getattr(repo, "session", None)
        fixed_baseline = baseline
        for attempt in range(1 if fixed_baseline is not None else 2):
            baseline = fixed_baseline or (CanonReadBaseline.capture(session, project_id, as_of_chapter=max(0, chapter_plan.chapter_number - 1)) if session is not None else None)
            try:
                if baseline is not None:
                    baseline.assert_current(session)
                with fresh_orm_reads(session):
                    pack = self._build_chapter_context(repo, project_id, chapter_plan, baseline=baseline)
                if baseline is not None:
                    baseline.assert_current(session)
                return pack
            except CanonBaselineChanged:
                if fixed_baseline is not None or attempt:
                    raise
        raise CanonBaselineChanged("Canon context rebuild exhausted")

    def _build_chapter_context(self, repo, project_id, chapter_plan, *, baseline):
        self._ensure_memory_index(repo)
        base_pack = _assemble_context(repo, project_id, chapter_plan, **({"baseline": baseline} if baseline is not None else {}))
        try:
            world_pack = self.build_world_model_pack(
                repo,
                project_id,
                int(getattr(chapter_plan, "chapter_number", 0) or 0),
                "writing",
                baseline=baseline,
            )
            base_pack = self._merge_writer_world_model_pack(base_pack, world_pack)
        except CanonBaselineChanged:
            raise
        except Exception:
            logger.warning("World model context unavailable", exc_info=True)

        base_pack = self.hydrate_required_context(repo, base_pack, trim=False)
        summaries = self._pick_summaries(base_pack.previous_chapter_summaries)
        entities = self._pick_entities(base_pack.active_entities, base_pack.required_entity_ids)
        threads = self._pick_threads(base_pack.active_threads)
        relations = self._pick_relations(base_pack.active_relations, entities)
        memories = self._pick_memories(base_pack, session=getattr(repo, "session", None), baseline=baseline)
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

    def hydrate_required_context(
        self, repo, pack: ChapterContextPack, *,
        scene_plans: Iterable[ScenePlan] = (), trim: bool = True,
    ) -> ChapterContextPack:
        session = getattr(repo, "session", None)
        baseline = pack.canon_read_baseline
        hydrated = hydrate_requirements(pack, session=session, scene_plans=scene_plans)
        if baseline is not None and session is not None:
            read_transaction = session.get_transaction()

            def hydrate_later(current: ChapterContextPack, scenes: list[ScenePlan]) -> ChapterContextPack:
                if current.canon_read_baseline != baseline:
                    raise RequiredContextError("required_context: hydration baseline mismatch")
                if (
                    read_transaction is None
                    or not read_transaction.is_active
                    or session.get_transaction() is not read_transaction
                ):
                    raise RequiredContextError(
                        "required_context: reassemble after hydration transaction ended"
                    )
                baseline.assert_current(session)
                return self.hydrate_required_context(repo, current, scene_plans=scenes)
            hydrated = hydrated.model_copy(update={"required_context_hydrator": hydrate_later})
        if not trim:
            return hydrated
        trimmed = self._trim_pack(hydrated)
        self._finalize_context_summary(base_pack=hydrated, pack=trimmed, memories=hydrated.retrieved_memories)
        return trimmed

    def prepare_repair_context(
        self, pack: ChapterContextPack, contract: RepairContract
    ) -> ChapterContextPack:
        base = pack.model_copy(update={
            "repair_contract": RepairContract.model_validate(
                contract.model_dump(include=set(RepairContract.model_fields))
            ),
        })
        if base.required_context_hydrator is not None:
            base = base.required_context_hydrator(base, [])
        else:
            base = hydrate_requirements(base)
        trimmed = self._trim_pack(base)
        self._finalize_context_summary(
            base_pack=base, pack=trimmed, memories=base.retrieved_memories
        )
        return trimmed

    def _trim_pack(self, pack: ChapterContextPack) -> ChapterContextPack:
        pack = _budget_genesis_references(pack, self.context_budget_chars // 4)
        summaries = self._pick_summaries(list(pack.previous_chapter_summaries))
        entities = self._pick_entities(list(pack.active_entities), pack.required_entity_ids)
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
        pack = self._filter_writer_safe_world_context(pack)
        estimate = self._estimate_chars(pack)
        current_names = {entity.name for entity in pack.active_entities}

        while estimate > self.context_budget_chars:
            without_unrelated = _drop_unrelated_genesis_reference(pack, current_names)
            if without_unrelated is not None:
                pack = without_unrelated
                estimate = self._estimate_chars(pack)
                continue
            optional_relations = [r for r in pack.active_relations if r.relation_id not in pack.required_relation_ids]
            if optional_relations:
                drop = optional_relations[-1]
                pack = pack.model_copy(
                    update={"active_relations": [r for r in pack.active_relations if r is not drop]}
                )
                estimate = self._estimate_chars(pack)
                continue
            memories = list(getattr(pack, "retrieved_memories", []) or [])
            if memories:
                next_memories, _ = self._drop_lowest_priority_memory(memories)
                pack = pack.model_copy(update={"retrieved_memories": next_memories})
                estimate = self._estimate_chars(pack)
                continue
            optional_entities = [e for e in pack.active_entities if e.entity_id not in pack.required_entity_ids]
            if optional_entities and (pack.required_entity_ids or len(optional_entities) > 3):
                drop = optional_entities[-1]
                pack = pack.model_copy(
                    update={"active_entities": [e for e in pack.active_entities if e is not drop]}
                )
                estimate = self._estimate_chars(pack)
                continue
            if len(pack.active_threads) > 1:
                pack = pack.model_copy(
                    update={"active_threads": pack.active_threads[:-1]}
                )
                estimate = self._estimate_chars(pack)
                continue
            if len(pack.previous_chapter_summaries) > 1:
                pack = pack.model_copy(
                    update={
                        "previous_chapter_summaries": pack.previous_chapter_summaries[
                            1:
                        ]
                    }
                )
                estimate = self._estimate_chars(pack)
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
                estimate = self._estimate_chars(pack)
                continue
            break
        return pack.model_copy(update={"context_budget_summary": {
            "rendered_context_chars": estimate,
            "soft_budget_chars": self.context_budget_chars,
            "soft_budget_exceeded": estimate > self.context_budget_chars,
            "soft_budget_overflow_chars": max(0, estimate - self.context_budget_chars),
        }})

    def _finalize_context_summary(
        self,
        *,
        base_pack: ChapterContextPack,
        pack: ChapterContextPack,
        memories: list[object],
    ) -> None:
        before_chars = self._estimate_chars(base_pack)
        after_chars = self._estimate_chars(pack)
        memories_before = len(
            getattr(base_pack, "retrieved_memories", []) or memories or []
        )
        memories_after = len(getattr(pack, "retrieved_memories", []) or [])
        self.last_observability_summary = {
            **pack.context_budget_summary,
            "required_entity_count": len(pack.required_entity_ids),
            "required_relation_count": len(pack.required_relation_ids),
            "memory_source_validation": dict(getattr(self.memory_index, "last_search_stats", {}) or {}),
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

    def build_world_model_pack(
        self,
        repo,
        project_id: str,
        chapter_number: int,
        pack_kind: str,
        query: str = "",
        *, baseline=None, include_secondary=True,
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

        baseline = baseline or CanonReadBaseline.capture(session, project_id, as_of_chapter=max(0, chapter_number - 1))
        baseline.assert_current(session, project_id=project_id, as_of_chapter=max(0, chapter_number - 1))
        lines = []
        deltas = []
        gaps = []
        reader_experience = []

        visible_lines = [
            line.world_line_id for line in lines if bool(line.is_visible_onstage)
        ]
        hidden_lines = [
            line.world_line_id
            for line in lines
            if line.world_line_id not in visible_lines
            or any(
                token in line.line_type for token in ("hidden", "secret", "antagonist")
            )
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

        accepted_cognition = BookStateQuery(session, baseline=baseline).accepted_cognition(
            project_id, as_of_chapter=baseline.as_of_chapter,
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
        base_pack = pack_cls(
            project_id=project_id,
            as_of_chapter=baseline.as_of_chapter,
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
            accepted_cognition=accepted_cognition,
            observer_visibility_states={},
            promise_debts=promise_debts,
            recent_reader_experience_deltas=recent_reader_exp,
            must_not_reveal=list(chapter_intent.must_not_reveal)
            if isinstance(chapter_intent, ChapterWorldDeltaIntent)
            else [],
            fair_misdirection_requirements=[],
            accepted_delta_ids=[
                delta.delta_id for delta in deltas if bool(delta.allowed_for_canon)
            ],
            rejected_delta_ids=[
                delta.delta_id for delta in deltas if not bool(delta.allowed_for_canon)
            ],
            metadata={
                "hidden_truth_included": include_hidden_truth,
                "retrieval_source": "book_state",
            },
        )
        result = self._augment_v46_context(
            session=session,
            pack=base_pack,
            project_id=project_id,
            chapter_number=chapter_number,
            pack_kind=pack_kind,
            include_hidden_truth=include_hidden_truth,
            query=query,
            baseline=baseline, include_secondary=include_secondary,
        )
        baseline.assert_current(session)
        return result

    def _augment_v46_context(
        self,
        *,
        session,
        pack: WorldModelRetrievalPack,
        project_id: str,
        chapter_number: int,
        pack_kind: str,
        include_hidden_truth: bool,
        query: str = "",
        baseline=None, include_secondary=True,
    ) -> WorldModelRetrievalPack:
        repo = BookStateRepository(session)
        runtime = BookStateQuery(session, baseline=baseline).runtime(project_id, as_of_chapter=baseline.as_of_chapter)
        snapshot = repo.latest_world_snapshot(project_id, baseline.as_of_chapter)
        nodes = [node.model_copy(update={"state": runtime.world.get_state(node.id)}) for node in runtime.world.nodes_by_id.values()]
        edges = list(runtime.world.edges_by_id.values())
        facts = list(runtime.world.facts_by_id.values())
        map_nodes = list(runtime.map.nodes_by_id.values())
        map_edges = list(runtime.map.edges_by_id.values())
        if not include_hidden_truth:
            visible_node_ids = {
                node.id for node in nodes if not book_state_node_hidden(node)
            }
            nodes = [node for node in nodes if node.id in visible_node_ids]
            edges = [
                edge
                for edge in edges
                if edge.source_id in visible_node_ids
                and edge.target_id in visible_node_ids
                and not _book_state_edge_hidden(edge)
            ]
            facts = [fact for fact in facts if not _book_state_fact_hidden(fact)]
            map_edges = [edge for edge in map_edges if not _map_edge_hidden(edge)]
            map_nodes = [node for node in map_nodes if not book_state_node_hidden(node)]

        book_state_snapshot = (
            snapshot.model_dump(mode="json") if snapshot is not None else {}
        )
        book_state_nodes = [
            _node_context(node) for node in nodes[: self.max_world_pages * 4]
        ]
        book_state_edges = [
            _edge_context(edge) for edge in edges[: self.max_world_pages * 4]
        ]
        book_state_facts = [
            _fact_context(fact) for fact in facts[: self.max_world_pages * 4]
        ]
        book_state_map = {
            "nodes": [
                _map_node_context(node)
                for node in map_nodes[: self.max_world_pages * 4]
            ],
            "edges": [
                _map_edge_context(edge)
                for edge in map_edges[: self.max_world_pages * 4]
            ],
        }
        active_personality_contexts = _active_personality_contexts(nodes)
        obsidian_pages = self._load_obsidian_page_context(
            session,
            project_id,
            include_hidden_truth=include_hidden_truth, baseline=baseline,
        ) if include_secondary else []
        llm_kb_context = self._load_llm_kb_context(
            project_id, pack_kind=pack_kind, query=query, session=session, baseline=baseline
        ) if include_secondary else {}
        conflicts: list[dict[str, object]] = []
        source_refs = [
            *list(pack.source_refs),
            *([f"book_state:snapshot:{snapshot.id}"] if snapshot is not None else []),
            *[f"book_state:node:{item['id']}" for item in book_state_nodes[:8]],
            *[f"book_state:fact:{item['id']}" for item in book_state_facts[:8]],
        ]
        source_digest = (
            book_state_snapshot.get("objective_graph_digest")
            or llm_kb_context.get("source_digest")
            or pack.source_digest
        )
        metadata = {
            **pack.metadata,
            "knowledge_system_v46": True,
            "book_state_node_count": len(book_state_nodes),
            "book_state_fact_count": len(book_state_facts),
            "obsidian_page_count": len(obsidian_pages),
            "llm_kb_files": sorted(llm_kb_context.get("files", [])),
            "active_personality_context_count": len(active_personality_contexts),
        }
        return pack.model_copy(
            update={
                "book_state_snapshot": book_state_snapshot,
                "book_state_nodes": book_state_nodes,
                "book_state_edges": book_state_edges,
                "book_state_facts": book_state_facts,
                "book_state_map": book_state_map,
                "obsidian_pages": obsidian_pages[: self.max_world_pages],
                "llm_kb_context": llm_kb_context,
                "review_conflicts": conflicts,
                "active_personality_contexts": active_personality_contexts,
                "source_refs": list(dict.fromkeys(source_refs)),
                "source_digest": str(source_digest or ""),
                "metadata": metadata,
            }
        )

    def _load_obsidian_page_context(
        self,
        session,
        project_id: str,
        *,
        include_hidden_truth: bool,
        baseline=None,
    ) -> list[dict[str, object]]:
        baseline = baseline or CanonReadBaseline.capture(session, project_id, as_of_chapter=BookStateRepository(session).latest_available_chapter(project_id))
        runtime = BookStateQuery(session, baseline=baseline).runtime(project_id, as_of_chapter=baseline.as_of_chapter)
        rows = KnowledgePageRepository(session).list_valid_rows(project_id, runtime=runtime, as_of_chapter=baseline.as_of_chapter)
        rows = sorted(
            rows,
            key=lambda row: (
                int(row.as_of_chapter or 0),
                str(row.updated_at or ""),
                str(row.id or ""),
            ),
            reverse=True,
        )[: max(self.max_world_pages * 4, 12)]
        pages: list[dict[str, object]] = []
        for row in rows:
            frontmatter = load_json(row.frontmatter_json, {})
            if not include_hidden_truth and frontmatter_hidden(frontmatter):
                continue
            sections = parse_sections(row.markdown or "")
            pages.append(
                {
                    "page_key": row.page_key,
                    "page_type": row.page_type,
                    "title": row.title,
                    "vault_path": row.vault_path,
                    "content_hash": row.content_hash,
                    "visibility": frontmatter.get("visibility", ""),
                    "truth_relation": frontmatter.get("truth_relation", ""),
                    "source_refs": frontmatter.get("source_refs", []),
                    "canon_summary": _truncate(sections.get("Canon Summary", "")),
                    "manual_notes_present": bool(
                        sections.get("Manual Notes", "").strip()
                    ),
                    "proposed_correction_present": bool(
                        sections.get("Proposed Correction", "").strip()
                    ),
                }
            )
        return pages

    def _load_llm_kb_context(
        self, project_id: str, *, pack_kind: str, query: str = "", session=None, baseline=None
    ) -> dict[str, object]:
        from forwin.llm_kb.source_validation import validated_manifest, validated_file
        store = LLMKnowledgeBaseStore(root=self.llm_kb_root)
        manifest = validated_manifest(store.root, project_id, session, baseline)
        if not manifest:
            return {}
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
                content = validated_file(store.root, project_id, key, manifest)
                if content is None:
                    continue
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
                session=session, baseline=baseline,
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

    def _filter_writer_safe_world_context(
        self, pack: ChapterContextPack
    ) -> ChapterContextPack:
        """Keep writer-facing v4 context to IDs, hints, and explicit reveal guards."""
        intent = getattr(pack, "chapter_world_delta_intent", None)
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

    def _merge_writer_world_model_pack(
        self,
        pack: ChapterContextPack,
        world_pack: WorldModelRetrievalPack,
    ) -> ChapterContextPack:
        metadata = {
            "knowledge_system_v46": {
                "source_digest": world_pack.source_digest,
                "source_refs": list(world_pack.source_refs),
                "book_state_snapshot": world_pack.book_state_snapshot,
                "book_state_nodes": world_pack.book_state_nodes,
                "book_state_edges": world_pack.book_state_edges,
                "book_state_facts": world_pack.book_state_facts,
                "book_state_map": world_pack.book_state_map,
                "obsidian_pages": world_pack.obsidian_pages,
                "llm_kb_context": world_pack.llm_kb_context,
                "review_conflicts": world_pack.review_conflicts,
                "active_personality_contexts": world_pack.active_personality_contexts,
            }
        }
        map_context = dict(pack.map_context or {})
        if world_pack.book_state_map:
            map_context["book_state"] = world_pack.book_state_map
        knowledge_system_context = {
            **dict(getattr(pack, "knowledge_system_context", {}) or {}),
            **metadata,
        }
        return pack.model_copy(
            update={
                "active_world_lines": world_pack.active_world_lines
                or pack.active_world_lines,
                "visible_world_lines": world_pack.visible_world_lines
                or pack.visible_world_lines,
                "hidden_world_lines": world_pack.hidden_world_lines
                or pack.hidden_world_lines,
                "recent_world_deltas": world_pack.recent_world_deltas
                or pack.recent_world_deltas,
                "recent_offscreen_deltas": world_pack.recent_offscreen_deltas,
                "active_knowledge_gaps": world_pack.active_knowledge_gaps
                or pack.active_knowledge_gaps,
                "planned_reveal_ladder": world_pack.planned_reveal_ladder
                or pack.planned_reveal_ladder,
                "accepted_cognition": list(world_pack.accepted_cognition),
                "reader_cognition_state": "",
                "character_cognition_states": dict(world_pack.character_cognition_states),
                "observer_visibility_states": dict(world_pack.observer_visibility_states),
                "promise_debts": world_pack.promise_debts or pack.promise_debts,
                "recent_reader_experience_deltas": world_pack.recent_reader_experience_deltas
                or pack.recent_reader_experience_deltas,
                "must_not_reveal": world_pack.must_not_reveal or pack.must_not_reveal,
                "fair_misdirection_requirements": world_pack.fair_misdirection_requirements
                or pack.fair_misdirection_requirements,
                "active_personality_contexts": world_pack.active_personality_contexts
                or pack.active_personality_contexts,
                "map_context": map_context,
                "knowledge_system_context": knowledge_system_context,
                "world_context": pack.world_context.model_copy(
                    update={
                        "world_model_refs": {
                            **pack.world_context.world_model_refs,
                            "knowledge_system_v46": world_pack.source_digest
                            or "book_state",
                        }
                    }
                ),
            }
        )

    def _pick_summaries(self, summaries: list[str]) -> list[str]:
        return summaries[-self.max_summaries :]

    def _pick_entities(self, entities: list[EntitySnapshot], required_ids=()) -> list[EntitySnapshot]:
        required = sorted((e for e in entities if e.entity_id in required_ids), key=lambda e: e.entity_id)
        ranked = sorted((e for e in entities if e.entity_id not in required_ids), key=lambda item: (-item.importance, item.name))
        return [*required, *ranked[: self.max_entities]]

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
        return writer_context_chars(pack)

    def _pick_memories(self, base_pack: ChapterContextPack, *, session=None, baseline=None):
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
            **({"session": session, "baseline": baseline} if baseline is not None else {}),
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


__all__ = [
    "RetrievalBroker",
]
