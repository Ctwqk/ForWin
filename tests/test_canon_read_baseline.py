from __future__ import annotations

import pytest
from forwin.book_state.query import BookStateQuery
from forwin.book_state.repository import BookStateRepository
from forwin.models import Project
from forwin.protocol.book_state import WorldNode
from forwin.retrieval.memory_index import create_memory_index, HashTextEmbedder
from forwin.retrieval.embedding_cache import memory_embedding_identity, LLM_KB_PREPROCESSING
from tests.test_knowledge_system_v46 import _session_factory, _create_project


def test_book_state_cache_refreshes_same_height_revision():
    Factory, engine = _session_factory()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="person",
                    project_id=project_id,
                    node_type="character",
                    name="A",
                    summary="old",
                )
            )
        with Factory() as session:
            from forwin.models.book_state import WorldNodeRow

            retained_row = session.get(WorldNodeRow, "person")
            assert retained_row.summary == "old"
            query = BookStateQuery(session)
            old = query.runtime(project_id, as_of_chapter=0)
            with Factory.begin() as writer:
                from forwin.models.book_state import WorldNodeRow

                writer.get(WorldNodeRow, "person").summary = "new"
                writer.get(Project, project_id).book_revision += 1
            fresh = query.runtime(project_id, as_of_chapter=0)
            assert fresh is not old
            assert fresh.world.nodes_by_id["person"].summary == "new"
    finally:
        engine.dispose()


def test_unknown_memory_is_not_writer_evidence():
    Factory, engine = _session_factory()
    index = create_memory_index(qdrant_url=":memory:")
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            index.upsert_chapter(
                project_id=project_id,
                chapter_number=1,
                title="old",
                summary="unsupported",
                body="legacy",
            )
            from forwin.retrieval import source_identity

            baseline = source_identity.CanonReadBaseline.capture(
                session, project_id, as_of_chapter=1
            )
            assert (
                index.search(
                    project_id=project_id,
                    query="old",
                    session=session,
                    baseline=baseline,
                )
                == []
            )
    finally:
        index.close()
        engine.dispose()


def test_stale_pages_are_excluded_before_limit_and_secondary_loader(tmp_path):
    from forwin.obsidian import ObsidianExporter
    from forwin.knowledge_system.context import KnowledgeContextQuery
    from forwin.models.book_state import WorldNodeRow
    from forwin.retrieval.broker_core import RetrievalBroker

    Factory, engine = _session_factory()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            for node_id, name in [("stale", "Z"), ("valid", "A")]:
                repo.create_world_node(
                    WorldNode(
                        id=node_id,
                        project_id=project_id,
                        node_type="character",
                        name=name,
                        summary=f"{name} original",
                    )
                )
            ObsidianExporter(session).export_project(project_id, vault_root=tmp_path)
        with Factory.begin() as session:
            session.get(WorldNodeRow, "stale").summary = "Z revised"
            session.get(Project, project_id).book_revision += 1
        with Factory() as session:
            pages = (
                KnowledgeContextQuery(session)
                .build(project_id=project_id, chapter_number=1, max_pages=1)
                .relevant_world_pages
            )
            assert [page.title for page in pages] == ["A"]
            pages = RetrievalBroker()._load_obsidian_page_context(
                session, project_id, include_hidden_truth=False
            )
            assert not any(page["title"] == "Z" for page in pages)
    finally:
        engine.dispose()


def test_llm_kb_secondary_file_and_search_reject_stale_and_forged_text(tmp_path):
    from forwin.llm_kb import LLMKnowledgeBaseCompiler
    from forwin.retrieval.broker_core import RetrievalBroker
    from forwin.retrieval.source_identity import CanonReadBaseline
    from forwin.models.book_state import WorldNodeRow
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels

    Factory, engine = _session_factory()
    qdrant = FakeQdrantClient()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="person",
                    project_id=project_id,
                    node_type="character",
                    name="A",
                    summary="旧宫主人",
                )
            )
            LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path,
                qdrant_client=qdrant,
                qdrant_models=FakeQdrantModels,
                qdrant_collection="kb",
            ).rebuild(project_id)
        with Factory.begin() as session:
            session.get(WorldNodeRow, "person").summary = "新城来客"
            session.get(Project, project_id).book_revision += 1
        with Factory() as session:
            broker = RetrievalBroker(
                llm_kb_root=tmp_path,
                llm_kb_qdrant_client=qdrant,
                llm_kb_qdrant_models=FakeQdrantModels,
                llm_kb_qdrant_collection="kb",
            )
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=0)
            result = broker._load_llm_kb_context(
                project_id,
                pack_kind="writing",
                query="旧宫主人",
                session=session,
                baseline=baseline,
            )
            assert not result.get("excerpts")
            assert not result.get("search_results")
    finally:
        engine.dispose()


def _accepted_source(
    session, project_id, number, *, plan=None, version=1, text="正式事实"
):
    from forwin.models import ArcPlanVersion, ChapterPlan, ChapterDraft, ChapterReview
    from forwin.models.draft import CandidateDraftRecord
    from forwin.models.canon import CanonCommitRecord
    from forwin.retrieval.source_identity import text_hash

    if plan is None:
        arc = ArcPlanVersion(project_id=project_id, arc_synopsis="arc")
        session.add(arc)
        session.flush()
        plan = ChapterPlan(
            project_id=project_id,
            arc_plan_id=arc.id,
            chapter_number=number,
            title="当前计划",
            status="accepted",
        )
        session.add(plan)
        session.flush()
    draft = ChapterDraft(
        chapter_plan_id=plan.id, body_text=text, summary=text, version=version
    )
    session.add(draft)
    session.flush()
    review = ChapterReview(draft_id=draft.id, verdict="accept")
    session.add(review)
    session.flush()
    candidate = CandidateDraftRecord(
        project_id=project_id,
        chapter_plan_id=plan.id,
        chapter_number=number,
        candidate_draft_id=draft.id,
        review_id=review.id,
        version=version,
        status="accepted",
        body_hash=text_hash(text),
    )
    session.add(candidate)
    session.flush()
    commit = CanonCommitRecord(
        project_id=project_id,
        chapter_plan_id=plan.id,
        chapter_number=number,
        candidate_id=candidate.id,
        chapter_title="正式标题",
        idempotency_key=candidate.id,
        acceptance_revision=version,
    )
    session.add(commit)
    session.flush()
    plan.active_commit_id = commit.id
    session.get(Project, project_id).book_revision += 1
    session.flush()
    return plan, dict(
        project_id=project_id,
        chapter_number=number,
        title=commit.chapter_title,
        summary=draft.summary,
        body=draft.body_text,
        canon_commit_id=commit.id,
        candidate_id=candidate.id,
        draft_id=draft.id,
        body_hash=candidate.body_hash,
    )


def test_memory_active_source_refill_late_write_forgery_and_unchanged_history():
    from forwin.retrieval.source_identity import CanonReadBaseline
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels

    Factory, engine = _session_factory()
    client = FakeQdrantClient()
    index = create_memory_index(
        qdrant_url="unused", qdrant_client=client, qdrant_models=FakeQdrantModels
    )
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            plan, old = _accepted_source(session, project_id, 1, text="旧宫主人")
            _, active = _accepted_source(
                session, project_id, 1, plan=plan, version=2, text="新城来客"
            )
            index.upsert_chapter(**active)
            index.upsert_chapter(**old)  # old worker finishes after revision
            assert len(client.collections[index.collection_name]["points"]) == 2
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=1)
            # Text is reconstructed from the formal draft even if payload claims a different story.
            points = client.collections[index.collection_name]["points"]
            active_point = next(
                p
                for p in points.values()
                if p.payload["canon_commit_id"] == active["canon_commit_id"]
            )
            active_point.payload["summary"] = "伪造世界事实"
            result = index.search(
                project_id=project_id,
                query="旧宫主人",
                session=session,
                baseline=baseline,
            )
            assert [m.summary for m in result] == ["新城来客"]
            for i in range(25):
                index.upsert_chapter(
                    **{**old, "candidate_id": f"unknown-{i}", "summary": "检索词"}
                )
            # Future high hits must be filtered server-side; 25 obsolete high hits require refill.
            _, future = _accepted_source(session, project_id, 2, text="检索词")
            index.upsert_chapter(**future)
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=1)
            result = index.search(
                project_id=project_id,
                query="检索词",
                session=session,
                baseline=baseline,
                limit=1,
            )
            assert [m.summary for m in result] == ["新城来客"]
            assert index.last_search_stats["batches"] == 2
            assert index.last_search_stats["rejected"] >= 25
            active_point.payload["body_hash"] = "forged"
            assert (
                index.search(
                    project_id=project_id,
                    query="新城来客",
                    session=session,
                    baseline=baseline,
                )
                == []
            )
    finally:
        index.close()
        engine.dispose()


@pytest.mark.parametrize("keep_changing", [False, True])
def test_broker_rebuilds_whole_actual_writer_messages_once(
    tmp_path, monkeypatch, keep_changing
):
    from forwin.models import ChapterPlan
    from forwin.models.book_state import WorldNodeRow
    from forwin.obsidian import ObsidianExporter
    from forwin.retrieval.broker_core import RetrievalBroker
    from forwin.retrieval.source_identity import CanonBaselineChanged
    from forwin.context.providers.state_provider import StateContextProvider
    from forwin.state.repo import StateRepository
    from forwin.writer.prompt_core import build_single_chapter_draft_prompt

    Factory, engine = _session_factory()
    index = create_memory_index(qdrant_url=":memory:")
    calls = []
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            from forwin.runtime.policy_store import ProjectPolicyStore
            from forwin.runtime.policy import RuntimePolicy

            ProjectPolicyStore(session).initialize(
                session.get(Project, project_id), RuntimePolicy.for_profile("standard")
            )
            plan, old = _accepted_source(session, project_id, 1, text="旧摘要标记")
            index.upsert_chapter(**old)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="hero",
                    project_id=project_id,
                    node_type="location",
                    name="主角",
                    summary="旧状态标记",
                )
            )
            ObsidianExporter(session).export_project(
                project_id, vault_root=tmp_path, as_of_chapter=1
            )
            next_plan = ChapterPlan(
                project_id=project_id,
                arc_plan_id=plan.arc_plan_id,
                chapter_number=2,
                title="主角调查",
                one_line="主角展开调查",
            )
            session.add(next_plan)
            session.flush()
            next_id, old_plan_id = next_plan.id, plan.id
        original = StateContextProvider.contribute

        def concurrent_accept(provider, request, draft):
            original(provider, request, draft)
            calls.append(request.baseline)
            if len(calls) == 1 or keep_changing:
                with Factory.begin() as writer:
                    if len(calls) == 1:
                        _accepted_source(
                            writer,
                            project_id,
                            1,
                            plan=writer.get(ChapterPlan, old_plan_id),
                            version=2,
                            text="新摘要标记",
                        )
                        writer.get(WorldNodeRow, "hero").summary = "新状态标记"
                    else:
                        writer.get(Project, project_id).book_revision += 1

        monkeypatch.setattr(StateContextProvider, "contribute", concurrent_accept)
        with Factory() as session:
            broker = RetrievalBroker(
                memory_index=index,
                llm_kb_root=tmp_path / "kb",
                context_budget_chars=12000,
            )
            if keep_changing:
                with pytest.raises(CanonBaselineChanged):
                    broker.build_chapter_context(
                        StateRepository(session),
                        project_id,
                        session.get(ChapterPlan, next_id),
                    )
            else:
                pack = broker.build_chapter_context(
                    StateRepository(session),
                    project_id,
                    session.get(ChapterPlan, next_id),
                )
                messages = "\n".join(
                    message["content"]
                    for message in build_single_chapter_draft_prompt(pack)
                )
                assert "旧摘要标记" not in messages
                assert "旧状态标记" not in messages
                assert "新摘要标记" in messages
                assert "新状态标记" in messages
                from forwin.protocol.context import RepairContract

                assert pack.canon_read_baseline is calls[-1]
                repair_pack = broker.prepare_repair_context(
                    pack, RepairContract(must_fix=["保留已确认事实"])
                )
                assert repair_pack.canon_read_baseline is calls[-1]
                assert "canon_read_baseline" not in repair_pack.model_dump(mode="json")
            assert len(calls) == 2
            assert calls[0].as_of_chapter == calls[1].as_of_chapter == 1
            assert calls[1].book_revision > calls[0].book_revision
    finally:
        index.close()
        engine.dispose()


@pytest.mark.parametrize(
    "mutation",
    ["edge_add", "edge_delete", "state", "map", "visibility", "cognition", "aggregate"],
)
def test_semantic_dependencies_cover_membership_and_actual_render_inputs(mutation):
    from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
    from forwin.book_state.map_graph import MapGraph
    from forwin.book_state.cognition import CognitionView
    from forwin.protocol.book_state import WorldEdge, MapNode, CognitionOverlay
    from forwin.knowledge_system.dependencies import (
        page_dependencies,
        dependencies_valid,
    )

    a = WorldNode(
        id="a", project_id="p", node_type="character", name="A", state={"place": "old"}
    )
    edge = WorldEdge(
        id="e",
        project_id="p",
        source_id="a",
        target_id="b",
        edge_type="knows",
        edge_family="knowledge_visibility",
    )
    runtime = BookStateRuntime(
        project_id="p",
        as_of_chapter=0,
        world=ObjectiveWorldGraph(nodes=[a], edges=[edge]),
        map_graph=MapGraph(),
    )
    runtime.map.nodes_by_id["m"] = MapNode(
        id="m", project_id="p", name="map", node_type="site"
    )
    runtime.cognition_by_observer[("reader", "reader")] = CognitionView(
        CognitionOverlay(
            id="cog", project_id="p", observer_type="reader", observer_id="reader"
        )
    )
    scope = (
        "map_node"
        if mutation == "map"
        else "book"
        if mutation == "aggregate"
        else "node"
    )
    node_id = "m" if scope == "map_node" else "" if scope == "book" else "a"
    manifest = page_dependencies(runtime, scope=scope, node_id=node_id)
    assert dependencies_valid(manifest, runtime)
    if mutation == "edge_add":
        runtime.world.add_edge(edge.model_copy(update={"id": "e2"}))
    elif mutation == "edge_delete":
        del runtime.world.edges_by_id["e"]
    elif mutation == "state":
        runtime.world.states_by_node_id["a"] = {"place": "new"}
        assert a.state == {"place": "old"}
    elif mutation == "map":
        runtime.map.nodes_by_id["m"].description = "changed topology"
    elif mutation == "visibility":
        a.metadata["visibility"] = "hidden"
    elif mutation == "cognition":
        runtime.cognition_by_observer[("reader", "reader")].confirmed_refs.add("node:a")
    else:
        runtime.world.add_node(a.model_copy(update={"id": "b"}))
    assert not dependencies_valid(manifest, runtime)


def test_projection_render_uses_runtime_state_not_stale_node_copy(
    tmp_path, monkeypatch
):
    from forwin.book_state.projection import BookStateProjection
    from forwin.obsidian import ObsidianExporter
    from forwin.llm_kb import LLMKnowledgeBaseCompiler
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels

    Factory, engine = _session_factory()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="a",
                    project_id=project_id,
                    node_type="character",
                    name="A",
                    state={"state_summary": "旧状态标记"},
                )
            )
            runtime = BookStateProjection(session).load_runtime_as_of(
                project_id, as_of_chapter=0
            )
            runtime.world.states_by_node_id["a"] = {"state_summary": "当前状态标记"}
            monkeypatch.setattr(
                BookStateProjection,
                "load_runtime_as_of",
                lambda *args, **kwargs: runtime,
            )
            export = ObsidianExporter(session).export_project(
                project_id, vault_root=tmp_path / "vault"
            )
            compiled = LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path / "kb",
                qdrant_client=FakeQdrantClient(),
                qdrant_models=FakeQdrantModels,
                qdrant_collection="kb",
            ).rebuild(project_id)
            page = (
                tmp_path
                / "vault"
                / next(path for path in export.pages if "Characters/" in path)
            ).read_text()
            assert "当前状态标记" in page and "旧状态标记" not in page
            text = (tmp_path / "kb" / project_id / "CHARACTER_MEMORY.md").read_text()
            assert "当前状态标记" in text and "旧状态标记" not in text
            assert compiled.source_digest
    finally:
        engine.dispose()


def test_memory_refill_is_bounded_and_embeds_query_once():
    from forwin.retrieval.source_identity import CanonReadBaseline
    from forwin.retrieval.memory_index import HashTextEmbedder, QdrantChapterMemoryIndex
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels

    class CountingEmbedder(HashTextEmbedder):
        calls = 0

        def embed(self, texts):
            self.calls += 1
            return super().embed(texts)

    embedder = CountingEmbedder()
    index = QdrantChapterMemoryIndex(
        url="unused",
        collection_name="cap",
        embedder=embedder,
        client=FakeQdrantClient(),
        qdrant_models=FakeQdrantModels,
    )
    Factory, engine = _session_factory()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            for i in range(105):
                index.upsert_chapter(
                    project_id=project_id,
                    chapter_number=1,
                    title=f"legacy-{i}",
                    summary="query",
                    body="query",
                )
            before = embedder.calls
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=1)
            assert (
                index.search(
                    project_id=project_id,
                    query="query",
                    session=session,
                    baseline=baseline,
                )
                == []
            )
            assert embedder.calls == before + 1
            assert index.last_search_stats == {
                "rejected": 100,
                "batches": 5,
                "cap_reached": True,
            }
    finally:
        index.close()
        engine.dispose()


def test_llm_kb_validated_search_reconstructs_payload_and_checks_file_bytes(tmp_path):
    from forwin.llm_kb import LLMKnowledgeBaseCompiler, LLMKnowledgeBaseRetriever
    from forwin.retrieval.source_identity import CanonReadBaseline
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels

    Factory, engine = _session_factory()
    client = FakeQdrantClient()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="a",
                    project_id=project_id,
                    node_type="location",
                    name="港口",
                    summary="真实港口标记",
                )
            )
            LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path,
                qdrant_client=client,
                qdrant_models=FakeQdrantModels,
                qdrant_collection="kb",
            ).rebuild(project_id)
        with Factory() as session:
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=0)
            for point in client.collections[f"kb_{memory_embedding_identity(HashTextEmbedder(dims=96), preprocessing=LLM_KB_PREPROCESSING)}"]["points"].values():
                point.payload["text"] = "伪造文本标记"
            retriever = LLMKnowledgeBaseRetriever(
                root=tmp_path,
                qdrant_client=client,
                qdrant_models=FakeQdrantModels,
                qdrant_collection="kb",
            )
            result = retriever.search(
                project_id, "真实港口标记", session=session, baseline=baseline, limit=10
            )
            assert any("真实港口标记" in record["text"] for record in result)
            assert all("伪造文本标记" not in record["text"] for record in result)
            files = {record["file_key"] for record in result}
            for key in files:
                (tmp_path / project_id / key).write_text("文件被改写")
            assert all(
                record["file_key"] not in files
                for record in retriever.search(
                    project_id,
                    "真实港口标记",
                    session=session,
                    baseline=baseline,
                    limit=10,
                )
            )
    finally:
        engine.dispose()


@pytest.mark.parametrize("fail", [False, True])
def test_scoped_fresh_reads_preserve_autoflush_and_remove_listener(fail):
    from sqlalchemy import select
    from forwin.models.book_state import WorldNodeRow
    from forwin.retrieval.source_identity import fresh_orm_reads

    Factory, engine = _session_factory()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="a",
                    project_id=project_id,
                    node_type="location",
                    name="A",
                    summary="original",
                )
            )
        with Factory() as session:
            row = session.get(WorldNodeRow, "a")
            listeners = len(session.dispatch.do_orm_execute)
            row.summary = "pending write"
            try:
                with fresh_orm_reads(session):
                    selected = session.scalar(
                        select(WorldNodeRow).where(WorldNodeRow.id == "a")
                    )
                    assert selected.summary == "pending write"
                    assert (
                        session.scalar(
                            select(WorldNodeRow.summary).where(WorldNodeRow.id == "a")
                        )
                        == "pending write"
                    )
                    if fail:
                        raise RuntimeError("deliberate failure")
            except RuntimeError as exc:
                assert str(exc) == "deliberate failure"
            assert len(session.dispatch.do_orm_execute) == listeners
            session.commit()
            row = session.get(WorldNodeRow, "a")
            with Factory.begin() as writer:
                writer.get(WorldNodeRow, "a").summary = "external later write"
            # Ordinary later ORM behavior is unchanged after the scoped operation.
            assert (
                session.scalar(
                    select(WorldNodeRow).where(WorldNodeRow.id == "a")
                ).summary
                == "pending write"
            )
    finally:
        engine.dispose()


def test_cross_chapter_source_chain_is_rejected_even_with_matching_text():
    from forwin.models import ChapterDraft
    from forwin.models.draft import CandidateDraftRecord
    from forwin.retrieval.source_identity import CanonReadBaseline, active_sources

    Factory, engine = _session_factory()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            _, source = _accepted_source(session, project_id, 1)
            other_plan, _ = _accepted_source(session, project_id, 2)
            crossed = ChapterDraft(
                chapter_plan_id=other_plan.id,
                body_text=source["body"],
                summary=source["summary"],
                version=99,
            )
            session.add(crossed)
            session.flush()
            session.get(
                CandidateDraftRecord, source["candidate_id"]
            ).candidate_draft_id = crossed.id
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=2)
            assert source["canon_commit_id"] not in active_sources(session, baseline)
    finally:
        engine.dispose()


def test_memory_projection_producer_supplies_active_immutable_source_ids():
    from forwin.knowledge_system.canon_projection import CanonProjectionService
    from forwin.knowledge_system.checkpoints import ProjectionTarget
    from forwin.retrieval.source_identity import CanonReadBaseline

    Factory, engine = _session_factory()
    index = create_memory_index(qdrant_url=":memory:")
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            plan, _ = _accepted_source(session, project_id, 1, text="old")
            _, active = _accepted_source(
                session, project_id, 1, plan=plan, version=2, text="active formal text"
            )
        result = CanonProjectionService(
            Factory, memory_index_provider=lambda: index
        )._run_chapter_memory(
            ProjectionTarget(
                project_id=project_id,
                chapter_number=1,
                canon_commit_id=active["canon_commit_id"],
                book_revision=2,
            )
        )
        assert result["chapter_count"] == 1
        with Factory() as session:
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=1)
            memories = index.search(
                project_id=project_id,
                query="active formal text",
                session=session,
                baseline=baseline,
            )
            assert len(memories) == 1
            assert memories[0].canon_commit_id == active["canon_commit_id"]
            assert memories[0].candidate_id == active["candidate_id"]
            assert memories[0].draft_id == active["draft_id"]
            assert memories[0].summary == active["summary"]
    finally:
        index.close()
        engine.dispose()


@pytest.mark.parametrize('pending_title', [False, True])
def test_obsidian_index_renders_the_same_fresh_project_title_it_certifies(tmp_path, pending_title):
    from forwin.obsidian import ObsidianExporter
    from forwin.knowledge_system.page_repository import KnowledgePageRepository
    from forwin.retrieval.broker_core import RetrievalBroker
    from forwin.retrieval.source_identity import CanonReadBaseline

    Factory, engine = _session_factory()
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            session.get(Project, project_id).title = 'OLD_TITLE_SENTINEL'
        with Factory() as session:
            retained = session.get(Project, project_id)
            assert retained.title == 'OLD_TITLE_SENTINEL'
            with Factory.begin() as writer:
                writer.get(Project, project_id).title = 'NEW_TITLE_SENTINEL'
                writer.get(Project, project_id).book_revision += 1
            expected_title = 'NEW_TITLE_SENTINEL'
            if pending_title:
                retained.title = 'PENDING_TITLE_SENTINEL'
                expected_title = retained.title
            ObsidianExporter(session).export_project(project_id, vault_root=tmp_path)
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=0)
            runtime = BookStateQuery(session, baseline=baseline).runtime(project_id, as_of_chapter=0)
            rows = KnowledgePageRepository(session).list_valid_rows(project_id, runtime=runtime, as_of_chapter=0)
            index = next(row for row in rows if row.vault_path == '00_Index.md')
            assert expected_title in index.markdown
            assert 'OLD_TITLE_SENTINEL' not in index.markdown
            pages = RetrievalBroker(max_world_pages=20)._load_obsidian_page_context(session, project_id, include_hidden_truth=False, baseline=baseline)
            secondary_index = next(page for page in pages if page['vault_path'] == '00_Index.md')
            assert expected_title in secondary_index['canon_summary']
            assert 'OLD_TITLE_SENTINEL' not in secondary_index['canon_summary']
            session.commit()
        with Factory() as session:
            assert session.get(Project, project_id).title == expected_title
    finally:
        engine.dispose()


def test_llm_kb_late_compiler_preserves_current_points_and_manifest(tmp_path, monkeypatch):
    import forwin.llm_kb.compiler as compiler_module
    from forwin.llm_kb import LLMKnowledgeBaseCompiler, LLMKnowledgeBaseRetriever
    from forwin.llm_kb.vector_index import LLMKBVectorIndex
    from forwin.models.book_state import WorldNodeRow
    from forwin.retrieval.memory_index import HashTextEmbedder
    from forwin.retrieval.source_identity import CanonReadBaseline, CanonBaselineChanged
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels

    Factory, engine = _session_factory()
    client = FakeQdrantClient()
    index_options = dict(qdrant_client=client, qdrant_models=FakeQdrantModels, qdrant_collection='late-kb')
    created = []
    current_result = []
    try:
        with Factory.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(WorldNode(id='location', project_id=project_id, node_type='location', name='矿门', summary='OLD_VERSION_SENTINEL'))
        def run_new_compiler():
            with Factory.begin() as writer:
                writer.get(WorldNodeRow, 'location').summary = 'NEW_VERSION_SENTINEL'
                writer.get(Project, project_id).book_revision += 1
            with Factory() as current:
                current_result.append(LLMKnowledgeBaseCompiler(current, root=tmp_path, **index_options).rebuild(project_id))
        class PausingEmbedder(HashTextEmbedder):
            def embed(self, texts):
                run_new_compiler()
                return super().embed(texts)
        def index_factory(*args, **kwargs):
            embedder = PausingEmbedder(dims=96) if not created else HashTextEmbedder(dims=96)
            index = LLMKBVectorIndex(*args, embedder=embedder, **kwargs)
            created.append(index)
            return index
        monkeypatch.setattr(compiler_module, 'LLMKBVectorIndex', index_factory)
        stale_error = None
        with Factory() as session:
            try:
                LLMKnowledgeBaseCompiler(session, root=tmp_path, **index_options).rebuild(project_id)
            except CanonBaselineChanged as exc:
                stale_error = exc
        points = list(client.collections[created[0].collection_name]['points'].values())
        assert any('NEW_VERSION_SENTINEL' in point.payload['text'] for point in points)
        assert any('OLD_VERSION_SENTINEL' in point.payload['text'] for point in points)
        assert isinstance(stale_error, CanonBaselineChanged)
        with Factory() as session:
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=0)
            retriever = LLMKnowledgeBaseRetriever(root=tmp_path, **index_options)
            current = retriever.search(project_id, '矿门', session=session, baseline=baseline, limit=10)
            assert any('NEW_VERSION_SENTINEL' in record['text'] for record in current)
            assert all('OLD_VERSION_SENTINEL' not in record['text'] for record in current)
            assert all(record['source_digest'] == current_result[0].source_digest for record in current)
            human = retriever.search(project_id, 'OLD_VERSION_SENTINEL 矿门', as_of_chapter=0, limit=10)
            assert any('NEW_VERSION_SENTINEL' in record['text'] for record in human)
            assert all('OLD_VERSION_SENTINEL' not in record['text'] for record in human)
    finally:
        engine.dispose()
