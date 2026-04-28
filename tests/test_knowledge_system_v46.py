from __future__ import annotations

import json
from pathlib import Path

import pytest

from forwin.api_book_state_routes import build_handlers as build_book_state_handlers
from forwin.api_llm_kb_routes import build_handlers as build_llm_kb_handlers
from forwin.api_obsidian_routes import build_handlers as build_obsidian_handlers
from forwin.api_schemas import WorldEditProposalReviewRequest, WorldModelExportRequest, WorldModelImportRequest
from forwin.book_state import BookStateCompiler, BookStateDeltaAdapter, BookStateRepository
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.llm_kb import LLMKnowledgeBaseCompiler, LLMKnowledgeBaseRetriever
from forwin.llm_kb.store import LLMKnowledgeBaseStore
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow
from forwin.models.world_model import WorldEditProposalRow
from forwin.obsidian import ObsidianExporter
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionOverlay,
    FactNode,
    FactPatch,
    GraphDelta,
    MapEdge,
    MapNode,
    WorldEdge,
    WorldNode,
)
from forwin.protocol.world_v4 import ApprovedWorldChangeSet, ReaderExperienceDelta
from forwin.retrieval.broker import RetrievalBroker
from forwin.state.repo import StateRepository
from tests.qdrant import FakeQdrantClient, FakeQdrantModels


def _session_factory():
    engine = get_engine(postgres_test_url())
    init_db(engine)
    return get_session_factory(engine), engine


def _create_project(session) -> str:
    project = Project(title="V4.6 知识系统", premise="测试 DB canon 投影。", genre="玄幻")
    session.add(project)
    session.flush()
    return project.id


def test_book_state_v46_fields_api_and_gate() -> None:
    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="org_archive",
                    project_id=project_id,
                    node_type="organization",
                    name="问心阁",
                    summary="掌管旧城契约档案的组织。",
                    status="active",
                    scope="old-city",
                    tags=["archive"],
                    valid_from_chapter=1,
                    source_refs=["chapter:1"],
                )
            )
            repo.create_world_node(
                WorldNode(
                    id="promise_truth",
                    project_id=project_id,
                    node_type="reader_promise",
                    name="灵矿真相",
                    summary="读者承诺：灵矿真相会被逐层兑现。",
                )
            )
            repo.create_world_edge(
                WorldEdge(
                    id="edge_alliance",
                    project_id=project_id,
                    source_id="org_archive",
                    target_id="promise_truth",
                    edge_type="promises",
                    edge_family="reader_experience",
                    status="active",
                    truth_relation="true",
                    source_refs=["chapter:1"],
                )
            )
            repo.upsert_cognition_overlay(
                CognitionOverlay(
                    id="cog_reader_1",
                    project_id=project_id,
                    observer_type="reader",
                    observer_id="reader",
                    as_of_chapter=1,
                    false_facts={
                        "fact_false_mine": FactNode(
                            id="fact_false_mine",
                            project_id=project_id,
                            proposition="读者误以为灵矿已经枯竭。",
                            truth_value="false",
                        )
                    },
                )
            )
            repo.append_graph_delta(
                GraphDelta(
                    id="delta_reader_promise",
                    project_id=project_id,
                    chapter_number=1,
                    operation="create_reader_promise",
                    target_type="node",
                    target_id="promise_truth",
                    summary="建立灵矿真相承诺。",
                    evidence_refs=["chapter:1"],
                    review_verdict_id="review_1",
                )
            )

        handlers = build_book_state_handlers(get_session=Session)
        nodes = handlers["list_book_state_nodes"](project_id, as_of_chapter=1)
        edges = handlers["list_book_state_edges"](project_id, as_of_chapter=1)
        deltas = handlers["list_book_state_deltas"](project_id, through_chapter=1)
        cognition = handlers["list_book_state_cognition"](project_id, as_of_chapter=1)
        promises = handlers["list_book_state_reader_promises"](project_id, as_of_chapter=1)

        assert nodes["nodes"][0]["node_type"] == "organization"
        assert nodes["nodes"][0]["summary"] == "掌管旧城契约档案的组织。"
        assert edges["edges"][0]["edge_family"] == "reader_experience"
        assert edges["edges"][0]["truth_relation"] == "true"
        assert deltas["deltas"][0]["operation"] == "create_reader_promise"
        assert cognition["overlays"][0]["false_facts"]["fact_false_mine"]["truth_value"] == "false"
        assert promises["reader_promise_nodes"][0]["id"] == "promise_truth"

        with Session.begin() as session:
            blocked = BookStateReviewGate(session).review(
                ApprovedGraphDeltaSet(
                    project_id=project_id,
                    chapter_number=2,
                    graph_deltas=[
                        GraphDelta(
                            id="delta_unapproved",
                            project_id=project_id,
                            chapter_number=2,
                            allowed_for_canon=False,
                            fact_patches=[
                                FactPatch(
                                    fact_id="fact_unapproved",
                                    op="create",
                                    proposition="未批准事实。",
                                    truth_value="true",
                                )
                            ],
                        )
                    ],
                )
            )
        assert blocked.accepted is False
        assert any(issue.code == "delta_not_allowed_for_canon" for issue in blocked.issues)
    finally:
        engine.dispose()


def test_reader_experience_syncs_into_book_state_native_tables() -> None:
    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            approved = ApprovedWorldChangeSet(
                project_id=project_id,
                chapter_number=3,
                reader_experience_deltas=[
                    ReaderExperienceDelta(
                        reader_experience_delta_id="reader_exp_ch3",
                        project_id=project_id,
                        chapter_number=3,
                        reader_state_before="读者等待矿难真相。",
                        reader_state_after="读者确认有人隐瞒矿难线索。",
                        cognition_transition="hinted -> suspected",
                        payoff_type="mid_term_delight",
                        reward_tags=["mystery", "payoff"],
                        emotional_effect="期待增强",
                        promise_debt_change=2,
                        next_desire="想看林烬追问档案。",
                        fairness_evidence=["chapter:3"],
                        source_refs=["chapter:3"],
                    )
                ],
                approved_by=["test"],
                review_verdict_id="review_reader_exp",
            )
            graph_changes = BookStateDeltaAdapter().from_world_change_set(approved)
            result = BookStateCompiler(session).compile(graph_changes)
            assert result.committed is True

        handlers = build_book_state_handlers(get_session=Session)
        promises = handlers["list_book_state_reader_promises"](project_id, as_of_chapter=3)
        assert promises["reader_promises"][0]["promise_id"] == "promise:reader_exp_ch3"
        assert promises["reader_promises"][0]["current_debt_level"] == 2
        assert promises["reader_experience_deltas"][0]["reader_experience_delta_id"] == "reader_exp_ch3"
    finally:
        engine.dispose()


def test_obsidian_export_import_and_proposal_review(tmp_path: Path) -> None:
    Session, engine = _session_factory()
    qdrant_client = FakeQdrantClient()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                    state={"location_id": "loc_gate", "status": "investigating"},
                    source_refs=["chapter:1"],
                )
            )
            repo.create_world_node(
                WorldNode(
                    id="secret_mine",
                    project_id=project_id,
                    node_type="secret",
                    name="矿难真相",
                    summary="隐藏秘密。",
                    status="hidden",
                    tags=["hidden"],
                )
            )
            repo.create_map_node(MapNode(id="loc_gate", project_id=project_id, node_type="settlement", name="矿门"))
            repo.create_map_node(MapNode(id="loc_archive", project_id=project_id, node_type="site", name="旧档案室"))
            repo.create_map_edge(
                MapEdge(
                    id="route_gate_archive",
                    project_id=project_id,
                    from_node_id="loc_gate",
                    to_node_id="loc_archive",
                    edge_type="road",
                    travel_time=1,
                )
            )

        vault_root = tmp_path / "vault"
        handlers = build_obsidian_handlers(
            get_session=Session,
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        )
        export = handlers["export_obsidian"](project_id, WorldModelExportRequest(vault_root=str(vault_root)))

        assert export.exported_count >= 4
        assert (vault_root / "00_Index.md").exists()
        assert (vault_root / "02_Map" / "Map_Canvas.canvas").exists()
        assert (vault_root / "03_Actors" / "Relationship_Canvas.canvas").exists()
        page_path = next((vault_root / "03_Actors" / "Characters").glob("*char_lin.md"))
        text = page_path.read_text(encoding="utf-8")
        assert "node_id: char_lin" in text
        assert "locked_fields:" in text
        assert "editable_fields:" in text

        text = text.replace("## Manual Notes\n_empty_", "## Manual Notes\n需要补充他对旧档案室的怀疑。")
        text = text.replace("## Proposed Correction\n_empty_", "## Proposed Correction\n关系：林烬应与旧档案室建立 located_in/route_to 关联。")
        page_path.write_text(text, encoding="utf-8")

        imported = handlers["import_obsidian"](project_id, WorldModelImportRequest(vault_root=str(vault_root)))
        assert imported.proposal_count == 2

        with Session() as session:
            proposals = session.query(WorldEditProposalRow).filter_by(project_id=project_id).order_by(WorldEditProposalRow.created_at.asc()).all()
            assert {row.proposal_type for row in proposals} == {"NoteOnlyProposal", "RelationshipCorrectionProposal"}
            approve_id = proposals[0].id
            reject_id = proposals[1].id

        approved = handlers["approve_obsidian_proposal"](
            project_id,
            approve_id,
            WorldEditProposalReviewRequest(status="accepted", reason="human reviewed"),
        )
        rejected = handlers["reject_obsidian_proposal"](
            project_id,
            reject_id,
            WorldEditProposalReviewRequest(status="rejected", reason="needs rewrite"),
        )

        assert approved.status == "accepted"
        assert approved.graph_delta_id
        assert approved.projection_refresh["ok"] is True
        assert rejected.status == "rejected"
        with Session() as session:
            assert session.query(GraphDeltaRow).filter_by(project_id=project_id).count() == 1

        structured_patch = """```forwin-patch
[
  {
    "op": "set_node_field",
    "node_id": "char_lin",
    "field_path": "summary",
    "old_value": "旧城线主角。",
    "new_value": "旧城线主角，开始怀疑旧档案室。"
  }
]
```"""
        with Session.begin() as session:
            row = WorldEditProposalRow(
                project_id=project_id,
                source="obsidian",
                target_page_key="03_Actors/Characters/林烬_char_lin.md",
                target_node_id="char_lin",
                target_field="Proposed Correction",
                proposal_type="CanonCorrectionProposal",
                proposed_patch_json=json.dumps(
                    {
                        "new_value": structured_patch,
                        "frontmatter": {"as_of_chapter": 0},
                    },
                    ensure_ascii=False,
                ),
                status="pending",
                created_by="test",
            )
            session.add(row)
            session.flush()
            structured_id = row.id

        structured = handlers["approve_obsidian_proposal"](
            project_id,
            structured_id,
            WorldEditProposalReviewRequest(status="accepted", reason="structured patch reviewed"),
        )
        assert structured.status == "accepted"
        with Session() as session:
            nodes = {
                node.id: node
                for node in BookStateRepository(session).list_world_nodes(project_id, as_of_chapter=0)
            }
            node = nodes["char_lin"]
            assert node.summary == "旧城线主角，开始怀疑旧档案室。"
    finally:
        engine.dispose()


def test_llm_kb_rebuild_is_writer_safe_and_allowlisted(tmp_path: Path) -> None:
    Session, engine = _session_factory()
    qdrant_client = FakeQdrantClient()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                    source_refs=["chapter:1"],
                )
            )
            repo.create_fact_node(
                FactNode(
                    id="fact_public",
                    project_id=project_id,
                    proposition="林烬抵达矿门。",
                    source_refs=["chapter:1"],
                    created_at_chapter=1,
                )
            )
            repo.create_fact_node(
                FactNode(
                    id="fact_hidden_truth",
                    project_id=project_id,
                    proposition="矿难由王都密令导致。",
                    source_refs=["chapter:1"],
                    created_at_chapter=1,
                    sensitivity_level="hidden",
                )
            )

            result = LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path / "kb",
                qdrant_client=qdrant_client,
                qdrant_models=FakeQdrantModels,
            ).rebuild(project_id, as_of_chapter=1)

        root = Path(result.root)
        current_state = (root / "CURRENT_STATE.md").read_text(encoding="utf-8")
        must_not_reveal = (root / "MUST_NOT_REVEAL.md").read_text(encoding="utf-8")
        facts = (root / "facts.jsonl").read_text(encoding="utf-8")

        assert "source_digest:" in current_state
        assert "source_refs:" in current_state
        assert "林烬抵达矿门" in current_state
        assert "矿难由王都密令导致" not in current_state
        assert "fact:fact_hidden_truth" in must_not_reveal
        assert "矿难由王都密令导致" not in must_not_reveal
        assert '"source_refs"' in facts
        assert '"source_digest"' in facts
        assert '"as_of_chapter": 1' in facts
        assert result.vector_index["section_count"] > 0
        search_results = LLMKnowledgeBaseRetriever(
            root=tmp_path / "kb",
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        ).search(
            project_id,
            "矿门",
            role="writer",
            limit=3,
        )
        assert search_results
        assert all(item["role_scope"] == "writer" for item in search_results)
        api_search = build_llm_kb_handlers(
            get_session=Session,
            llm_kb_root=tmp_path / "kb",
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        )["search_llm_kb"](
            project_id,
            query="矿门",
            role="writer",
            limit=2,
        )
        assert api_search["results"]

        store = LLMKnowledgeBaseStore(root=tmp_path / "kb")
        assert store.read_file(project_id, "CURRENT_STATE.md").startswith("# Current State")
        with pytest.raises(ValueError):
            store.read_file(project_id, "../CURRENT_STATE.md")
    finally:
        engine.dispose()


def test_retrieval_pack_merges_v46_sources_without_writer_hidden_leak(tmp_path: Path) -> None:
    Session, engine = _session_factory()
    qdrant_client = FakeQdrantClient()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                    source_refs=["chapter:1"],
                )
            )
            repo.create_world_node(
                WorldNode(
                    id="secret_edict",
                    project_id=project_id,
                    node_type="secret",
                    name="王都密令",
                    summary="隐藏真相：王都密令导致矿难。",
                    status="hidden",
                    tags=["hidden"],
                )
            )
            repo.create_fact_node(
                FactNode(
                    id="fact_hidden_edict",
                    project_id=project_id,
                    proposition="王都密令导致矿难。",
                    sensitivity_level="hidden",
                    source_refs=["chapter:1"],
                    created_at_chapter=1,
                )
            )
            repo.create_fact_node(
                FactNode(
                    id="fact_public_gate",
                    project_id=project_id,
                    proposition="林烬抵达矿门。",
                    source_refs=["chapter:1"],
                    created_at_chapter=1,
                )
            )
            ObsidianExporter(session).export_project(project_id, vault_root=tmp_path / "vault", as_of_chapter=1)
            LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path / "kb",
                qdrant_client=qdrant_client,
                qdrant_models=FakeQdrantModels,
            ).rebuild(project_id, as_of_chapter=1)

        with Session() as session:
            broker = RetrievalBroker(
                llm_kb_root=tmp_path / "kb",
                llm_kb_qdrant_client=qdrant_client,
                llm_kb_qdrant_models=FakeQdrantModels,
            )
            state_repo = StateRepository(session)
            writer_pack = broker.build_world_model_pack(state_repo, project_id, 1, "writing")
            review_pack = broker.build_world_model_pack(state_repo, project_id, 1, "review")

        writer_dump = str(writer_pack.model_dump(mode="json"))
        review_dump = str(review_pack.model_dump(mode="json"))
        assert "王都密令导致矿难" not in writer_dump
        assert any(item["proposition"] == "林烬抵达矿门。" for item in writer_pack.book_state_facts)
        assert "CURRENT_STATE.md" in writer_pack.llm_kb_context["files"]
        assert writer_pack.obsidian_pages
        assert writer_pack.metadata["knowledge_system_v46"] is True
        assert "王都密令导致矿难" in review_dump
        assert any(item["proposition"] == "王都密令导致矿难。" for item in review_pack.book_state_facts)
    finally:
        engine.dispose()
