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
from forwin.world_model.store import load_json
from forwin.obsidian.frontmatter import parse_sections
from forwin.personality import CharacterPersonalityLibrary, build_active_personality_contexts
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
        llm_kb_root: Path | None = None,
        database_url: str | None = None,
        retrieval_backend: str = "qdrant",
        qdrant_url: str | None = None,
        qdrant_collection: str = "chapter_memories",
        llm_kb_qdrant_url: str | None = None,
        llm_kb_qdrant_collection: str | None = None,
        llm_kb_qdrant_client: object | None = None,
        llm_kb_qdrant_models: object | None = None,
    ) -> None:
        self.context_budget_chars = context_budget_chars
        self.max_entities = max_entities
        self.max_threads = max_threads
        self.max_summaries = max_summaries
        self.max_memories = max_memories
        self.max_world_pages = max_world_pages
        self.memory_index = memory_index
        self.database_url = database_url
        self.retrieval_backend = retrieval_backend
        self.qdrant_url = qdrant_url
        self.qdrant_collection = qdrant_collection
        self.llm_kb_root = llm_kb_root
        self.llm_kb_qdrant_url = llm_kb_qdrant_url
        self.llm_kb_qdrant_collection = llm_kb_qdrant_collection
        self.llm_kb_qdrant_client = llm_kb_qdrant_client
        self.llm_kb_qdrant_models = llm_kb_qdrant_models
        self.last_observability_summary: dict[str, object] = {}

    def build_chapter_context(self, repo, project_id: str, chapter_plan) -> ChapterContextPack:
        self._ensure_memory_index(repo)
        base_pack = assemble_context(repo, project_id, chapter_plan)
        try:
            world_pack = self.build_world_model_pack(
                repo,
                project_id,
                int(getattr(chapter_plan, "chapter_number", 0) or 0),
                "writing",
            )
            base_pack = self._merge_writer_world_model_pack(base_pack, world_pack)
        except Exception:
            pass

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

    def build_world_model_pack(
        self,
        repo,
        project_id: str,
        chapter_number: int,
        pack_kind: str,
        query: str = "",
    ) -> WorldModelRetrievalPack:
        """Build a role-specific v4 world-model retrieval pack.

        Writer-facing packs intentionally omit hidden objective truth. Review and
        compiler packs keep objective truth and planned reveal context so they can
        enforce information-asymmetry contracts.
        """
        if self.database_url is None:
            self.database_url = _database_url_from_repo(repo)
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
        base_pack = pack_cls(
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
        return self._augment_v46_context(
            session=session,
            pack=base_pack,
            project_id=project_id,
            chapter_number=chapter_number,
            pack_kind=pack_kind,
            include_hidden_truth=include_hidden_truth,
            query=query,
        )

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
    ) -> WorldModelRetrievalPack:
        repo = BookStateRepository(session)
        snapshot = repo.latest_world_snapshot(project_id, chapter_number)
        nodes = repo.list_world_nodes(project_id, as_of_chapter=chapter_number)
        edges = repo.list_world_edges(project_id, as_of_chapter=chapter_number)
        facts = repo.list_fact_nodes(project_id, as_of_chapter=chapter_number)
        map_nodes = repo.list_map_nodes(project_id)
        map_edges = repo.list_map_edges(project_id)
        if not include_hidden_truth:
            visible_node_ids = {node.id for node in nodes if not _book_state_node_hidden(node)}
            nodes = [node for node in nodes if node.id in visible_node_ids]
            edges = [
                edge for edge in edges
                if edge.source_id in visible_node_ids
                and edge.target_id in visible_node_ids
                and not _book_state_edge_hidden(edge)
            ]
            facts = [fact for fact in facts if not _book_state_fact_hidden(fact)]
            map_edges = [edge for edge in map_edges if not _map_edge_hidden(edge)]
            map_nodes = [node for node in map_nodes if not _map_node_hidden(node)]

        book_state_snapshot = snapshot.model_dump(mode="json") if snapshot is not None else {}
        book_state_nodes = [_node_context(node) for node in nodes[: self.max_world_pages * 4]]
        book_state_edges = [_edge_context(edge) for edge in edges[: self.max_world_pages * 4]]
        book_state_facts = [_fact_context(fact) for fact in facts[: self.max_world_pages * 4]]
        book_state_map = {
            "nodes": [_map_node_context(node) for node in map_nodes[: self.max_world_pages * 4]],
            "edges": [_map_edge_context(edge) for edge in map_edges[: self.max_world_pages * 4]],
        }
        active_personality_contexts = _active_personality_contexts(nodes)
        obsidian_pages = self._load_obsidian_page_context(
            session,
            project_id,
            include_hidden_truth=include_hidden_truth,
        )
        llm_kb_context = self._load_llm_kb_context(project_id, pack_kind=pack_kind, query=query)
        conflicts = self._load_review_conflicts(session, project_id)
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
    ) -> list[dict[str, object]]:
        rows = list(
            session.execute(
                select(WorldModelPageRow)
                .where(WorldModelPageRow.project_id == project_id, WorldModelPageRow.status == "canon_live")
                .order_by(WorldModelPageRow.as_of_chapter.desc(), WorldModelPageRow.updated_at.desc(), WorldModelPageRow.id.desc())
                .limit(max(self.max_world_pages * 4, 12))
            )
            .scalars()
            .all()
        )
        pages: list[dict[str, object]] = []
        for row in rows:
            frontmatter = load_json(row.frontmatter_json, {})
            if not include_hidden_truth and _frontmatter_hidden(frontmatter):
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
                    "manual_notes_present": bool(sections.get("Manual Notes", "").strip()),
                    "proposed_correction_present": bool(sections.get("Proposed Correction", "").strip()),
                }
            )
        return pages

    def _load_llm_kb_context(self, project_id: str, *, pack_kind: str, query: str = "") -> dict[str, object]:
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
            search_results = LLMKnowledgeBaseRetriever(
                root=self.llm_kb_root,
                qdrant_url=self.llm_kb_qdrant_url,
                qdrant_collection=self.llm_kb_qdrant_collection,
                qdrant_client=self.llm_kb_qdrant_client,
                qdrant_models=self.llm_kb_qdrant_models,
            ).search(
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

    def _ensure_memory_index(self, repo=None) -> None:  # noqa: ANN001
        if self.memory_index is not None:
            return
        database_url = self.database_url or _database_url_from_repo(repo)
        self.memory_index = create_memory_index(
            backend=self.retrieval_backend,
            database_url=database_url,
            qdrant_url=self.qdrant_url or DEFAULT_QDRANT_URL,
            qdrant_collection=self.qdrant_collection,
        )

    @staticmethod
    def _load_review_conflicts(session, project_id: str) -> list[dict[str, object]]:
        rows = list(
            session.execute(
                select(WorldModelConflictRow)
                .where(WorldModelConflictRow.project_id == project_id, WorldModelConflictRow.status == "open")
                .order_by(WorldModelConflictRow.created_at.desc(), WorldModelConflictRow.id.desc())
                .limit(12)
            )
            .scalars()
            .all()
        )
        return [
            {
                "id": row.id,
                "conflict_type": row.conflict_type,
                "severity": row.severity,
                "subject_key": row.subject_key,
                "description": row.description,
                "evidence_refs": load_json(row.evidence_refs_json, []),
            }
            for row in rows
        ]

    def _filter_writer_safe_world_context(self, pack: ChapterContextPack) -> ChapterContextPack:
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
                "active_world_lines": world_pack.active_world_lines or pack.active_world_lines,
                "visible_world_lines": world_pack.visible_world_lines or pack.visible_world_lines,
                "hidden_world_lines": world_pack.hidden_world_lines or pack.hidden_world_lines,
                "recent_world_deltas": world_pack.recent_world_deltas or pack.recent_world_deltas,
                "recent_offscreen_deltas": world_pack.recent_offscreen_deltas,
                "active_knowledge_gaps": world_pack.active_knowledge_gaps or pack.active_knowledge_gaps,
                "planned_reveal_ladder": world_pack.planned_reveal_ladder or pack.planned_reveal_ladder,
                "promise_debts": world_pack.promise_debts or pack.promise_debts,
                "recent_reader_experience_deltas": world_pack.recent_reader_experience_deltas or pack.recent_reader_experience_deltas,
                "must_not_reveal": world_pack.must_not_reveal or pack.must_not_reveal,
                "fair_misdirection_requirements": world_pack.fair_misdirection_requirements or pack.fair_misdirection_requirements,
                "active_personality_contexts": world_pack.active_personality_contexts or pack.active_personality_contexts,
                "map_context": map_context,
                "knowledge_system_context": knowledge_system_context,
                "world_context": pack.world_context.model_copy(
                    update={
                        "world_model_refs": {
                            **pack.world_context.world_model_refs,
                            "knowledge_system_v46": world_pack.source_digest or "book_state",
                        }
                    }
                ),
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


def _book_state_node_hidden(node) -> bool:
    tags = {str(tag).lower() for tag in getattr(node, "tags", []) or []}
    metadata = getattr(node, "metadata", {}) if isinstance(getattr(node, "metadata", {}), dict) else {}
    return (
        str(getattr(node, "status", "") or "").lower() in {"hidden", "secret", "must_not_reveal"}
        or str(metadata.get("visibility", "") or "").lower() in {"hidden", "secret", "must_not_reveal"}
        or bool(tags.intersection({"hidden", "secret", "must_not_reveal"}))
    )


def _book_state_edge_hidden(edge) -> bool:
    metadata = getattr(edge, "metadata", {}) if isinstance(getattr(edge, "metadata", {}), dict) else {}
    return (
        str(getattr(edge, "status", "") or "").lower() in {"hidden", "secret", "must_not_reveal"}
        or str(getattr(edge, "visibility", "") or "").lower() in {"hidden", "secret", "must_not_reveal"}
        or str(getattr(edge, "visibility_default", "") or "").lower() == "hidden"
        or str(metadata.get("visibility", "") or "").lower() in {"hidden", "secret", "must_not_reveal"}
    )


def _book_state_fact_hidden(fact) -> bool:
    return str(getattr(fact, "sensitivity_level", "") or "").lower() in {
        "hidden",
        "secret",
        "must_not_reveal",
    }


def _map_node_hidden(node) -> bool:
    metadata = getattr(node, "metadata", {}) if isinstance(getattr(node, "metadata", {}), dict) else {}
    return (
        str(getattr(node, "status", "") or "").lower() in {"hidden", "secret", "must_not_reveal"}
        or str(metadata.get("visibility", "") or "").lower() in {"hidden", "secret", "must_not_reveal"}
    )


def _map_edge_hidden(edge) -> bool:
    return (
        str(getattr(edge, "status", "") or "").lower() in {"hidden", "secret", "must_not_reveal"}
        or str(getattr(edge, "visibility_default", "") or "").lower() == "hidden"
        or not bool(getattr(edge, "discovered_by_default", True))
    )


def _frontmatter_hidden(frontmatter: dict[str, object]) -> bool:
    visibility = str(frontmatter.get("visibility", "") or "").lower()
    truth = str(frontmatter.get("truth_relation", "") or "").lower()
    status = str(frontmatter.get("status", "") or "").lower()
    node_type = str(frontmatter.get("node_type", "") or "").lower()
    return (
        visibility in {"hidden", "secret", "must_not_reveal"}
        or truth in {"hidden", "secret"}
        or status in {"hidden", "secret"}
        or node_type in {"secret"}
    )


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
