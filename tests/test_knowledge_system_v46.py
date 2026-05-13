from __future__ import annotations

import json
from pathlib import Path

import pytest

from forwin.api_book_state_routes import build_handlers as build_book_state_handlers
from forwin.api_llm_kb_routes import build_handlers as build_llm_kb_handlers
from forwin.api_obsidian_routes import build_handlers as build_obsidian_handlers
from forwin.api_schemas import WorldEditProposalReviewRequest, WorldModelExportRequest, WorldModelImportRequest
from forwin.api_world_model_routes import build_handlers as build_world_model_handlers
from forwin.book_state import BookStateCompiler, BookStateDeltaAdapter, BookStateRepository
from forwin.book_state.reviewer import BookStateReviewGate
from forwin.llm_kb import LLMKnowledgeBaseCompiler, LLMKnowledgeBaseRetriever
from forwin.llm_kb.store import LLMKnowledgeBaseStore
from forwin.models import Project
from forwin.models.base import get_engine, get_session_factory, init_db
from forwin.models.book_state import GraphDeltaRow
from forwin.models.world_model import WorldEditProposalRow, WorldModelPageRow
from forwin.obsidian import ObsidianExporter
from forwin.protocol.book_state import (
    ApprovedGraphDeltaSet,
    CognitionOverlay,
    FactNode,
    FactPatch,
    GraphDelta,
    MapEdge,
    MapNode,
    ReaderPromise,
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
        from forwin.world_studio.search_service import WorldStudioSearchService

        human_results = WorldStudioSearchService(
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        ).search(
            project_id,
            query="旧档案室的怀疑",
            index_kind="obsidian_human",
            role="human",
            limit=5,
        )["results"]
        assert human_results
        assert human_results[0]["canon_status"] == "human_unreviewed"

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

        world_model_handlers = build_world_model_handlers(get_session=Session)
        structured = world_model_handlers["review_project_world_model_proposal"](
            project_id,
            structured_id,
            WorldEditProposalReviewRequest(status="accepted", reason="structured patch reviewed"),
        )
        assert structured.status == "accepted"
        assert structured.graph_delta_id
        with Session() as session:
            nodes = {
                node.id: node
                for node in BookStateRepository(session).list_world_nodes(project_id, as_of_chapter=0)
            }
            node = nodes["char_lin"]
            assert node.summary == "旧城线主角，开始怀疑旧档案室。"
            assert session.query(GraphDeltaRow).filter_by(project_id=project_id).count() == 2
    finally:
        engine.dispose()


def test_obsidian_export_records_projection_cache_metadata_and_skips_unchanged_pages(tmp_path: Path) -> None:
    Session, engine = _session_factory()
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

        vault_root = tmp_path / "vault"
        with Session.begin() as session:
            first = ObsidianExporter(session).export_project(project_id, vault_root=vault_root, as_of_chapter=1)
            page_path = vault_root / "03_Actors" / "Characters" / "林烬_char_lin.md"
            first_mtime_ns = page_path.stat().st_mtime_ns
            row = session.query(WorldModelPageRow).filter_by(project_id=project_id, page_key="character:char_lin").one()
            first_revision = row.revision

        text = page_path.read_text(encoding="utf-8")
        assert "projection_version: obsidian_v2" in text
        assert "source_digest:" in text

        with Session.begin() as session:
            second = ObsidianExporter(session).export_project(project_id, vault_root=vault_root, as_of_chapter=1)
            row = session.query(WorldModelPageRow).filter_by(project_id=project_id, page_key="character:char_lin").one()

        assert first.exported_count == second.exported_count
        assert row.projection_kind == "obsidian"
        assert row.projection_version == "obsidian_v2"
        assert row.source_digest
        assert row.section_digest_json != "{}"
        assert row.revision == first_revision
        assert page_path.stat().st_mtime_ns == first_mtime_ns

        pages = build_world_model_handlers(get_session=Session)["list_project_world_model_pages"](project_id)
        api_page = next(item for item in pages if item.page_key == "character:char_lin")
        assert api_page.projection_kind == "obsidian"
        assert api_page.projection_version == "obsidian_v2"
        assert api_page.source_digest == row.source_digest
        assert api_page.section_digest
        assert api_page.role_scope == "human"
    finally:
        engine.dispose()


def test_projection_cache_migration_is_registered() -> None:
    from forwin.models.base import POSTGRES_BASELINE_MIGRATIONS

    migration = Path("forwin/migrations/versions/0006_projection_cache_fields.py")
    assert migration.exists()
    text = migration.read_text(encoding="utf-8")
    for column_name in (
        "projection_kind",
        "projection_version",
        "source_digest",
        "section_digest_json",
        "role_scope",
    ):
        assert column_name in text
    assert "projection_cache_fields_v1" in POSTGRES_BASELINE_MIGRATIONS


def test_projection_api_refresh_status_and_pages(tmp_path: Path) -> None:
    from forwin.api_projection_routes import build_handlers as build_projection_handlers

    Session, engine = _session_factory()
    qdrant_client = FakeQdrantClient()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                    source_refs=["chapter:1"],
                )
            )

        handlers = build_projection_handlers(
            get_session=Session,
            obsidian_root=tmp_path / "vaults",
            llm_kb_root=tmp_path / "kb",
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        )
        refreshed = handlers["refresh_projection"](
            project_id,
            projection_kind="all",
            as_of_chapter=1,
            force=True,
        )
        assert refreshed["ok"] is True
        assert refreshed["projection_kind"] == "all"
        status = handlers["get_projection_status"](project_id, projection_kind="obsidian")
        assert status["page_count"] >= 1
        pages = handlers["list_projection_pages"](project_id, projection_kind="obsidian")
        page = next(item for item in pages if item.page_key == "character:char_lin")
        assert page.projection_kind == "obsidian"
        fetched = handlers["get_projection_page"](project_id, "character:char_lin")
        assert fetched.source_digest == page.source_digest
    finally:
        engine.dispose()


def test_structured_patch_sets_personality_loadout_via_proposal(tmp_path: Path) -> None:
    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                    profile={"personality_loadout": {}},
                )
            )

        loadout = {
            "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.75},
            "secondary": [],
            "social_mask": [],
            "stress_modes": [],
            "relationship_patterns": [],
            "overrides": {},
        }
        structured_patch = "```forwin-patch\n" + json.dumps(
            [
                {
                    "op": "set_personality_loadout",
                    "node_id": "char_lin",
                    "old_value": {},
                    "new_value": loadout,
                    "reason": "World Studio loadout edit",
                }
            ],
            ensure_ascii=False,
        ) + "\n```"
        with Session.begin() as session:
            row = WorldEditProposalRow(
                project_id=project_id,
                source="world_studio",
                target_page_key="character:char_lin",
                target_node_id="char_lin",
                target_field="Proposed Correction",
                proposal_type="PersonalityLoadoutProposal",
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
            proposal_id = row.id

        reviewed = build_world_model_handlers(
            get_session=Session,
            qdrant_client=FakeQdrantClient(),
            qdrant_models=FakeQdrantModels,
        )["review_project_world_model_proposal"](
            project_id,
            proposal_id,
            WorldEditProposalReviewRequest(status="accepted", reason="reviewed loadout"),
        )
        assert reviewed.status == "accepted"
        with Session() as session:
            node = BookStateRepository(session).list_world_nodes(project_id, as_of_chapter=0)[0]
            assert node.profile["personality_loadout"]["dominant"]["skill"] == "trait-suspicious-survivor"
            assert node.profile["personality_loadout"]["dominant"]["weight"] == 0.75
    finally:
        engine.dispose()


def test_structured_patch_old_value_mismatch_blocks_without_forced_accept(tmp_path: Path) -> None:
    from fastapi import HTTPException

    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                )
            )
            row = WorldEditProposalRow(
                project_id=project_id,
                source="obsidian",
                target_page_key="character:char_lin",
                target_node_id="char_lin",
                target_field="Proposed Correction",
                proposal_type="CanonCorrectionProposal",
                proposed_patch_json=json.dumps(
                    {
                        "new_value": """```forwin-patch
[
  {
    "op": "set_node_field",
    "node_id": "char_lin",
    "field_path": "summary",
    "old_value": "错误旧值",
    "new_value": "不应写入的摘要"
  }
]
```""",
                        "frontmatter": {"as_of_chapter": 0},
                    },
                    ensure_ascii=False,
                ),
                status="pending",
                created_by="test",
            )
            session.add(row)
            session.flush()
            proposal_id = row.id

        with pytest.raises(HTTPException) as exc:
            build_world_model_handlers(get_session=Session)["review_project_world_model_proposal"](
                project_id,
                proposal_id,
                WorldEditProposalReviewRequest(status="accepted", reason="normal approval"),
            )
        assert exc.value.status_code == 409
        assert "old_value mismatch" in str(exc.value.detail)
        with Session() as session:
            node = BookStateRepository(session).list_world_nodes(project_id, as_of_chapter=0)[0]
            assert node.summary == "旧城线主角。"
    finally:
        engine.dispose()


def test_structured_patch_cognition_ops_commit_through_proposal(tmp_path: Path) -> None:
    Session, engine = _session_factory()
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
                )
            )
            repo.upsert_cognition_overlay(
                CognitionOverlay(
                    id="cog_reader_initial",
                    project_id=project_id,
                    observer_type="reader",
                    observer_id="reader",
                    as_of_chapter=0,
                    hidden_refs=["fact:fact_hidden"],
                    field_overrides={},
                )
            )
            patch = [
                {
                    "op": "append_cognition_ref",
                    "observer_type": "reader",
                    "observer_id": "reader",
                    "field_path": "visible_refs",
                    "ref": "fact:fact_public",
                    "old_value": [],
                },
                {
                    "op": "remove_cognition_ref",
                    "observer_type": "reader",
                    "observer_id": "reader",
                    "field_path": "hidden_refs",
                    "ref": "fact:fact_hidden",
                    "old_value": ["fact:fact_hidden"],
                },
                {
                    "op": "set_cognition_field",
                    "observer_type": "reader",
                    "observer_id": "reader",
                    "field_path": "field_overrides",
                    "old_value": {},
                    "new_value": {"field:char_lin:summary": "读者误以为林烬已经离城。"},
                },
            ]
            row = WorldEditProposalRow(
                project_id=project_id,
                source="world_studio",
                target_page_key="character:char_lin",
                target_node_id="char_lin",
                target_field="Proposed Correction",
                proposal_type="CognitionCorrectionProposal",
                proposed_patch_json=json.dumps(
                    {
                        "new_value": "```forwin-patch\n" + json.dumps(patch, ensure_ascii=False) + "\n```",
                        "frontmatter": {"as_of_chapter": 1},
                    },
                    ensure_ascii=False,
                ),
                status="pending",
                created_by="test",
            )
            session.add(row)
            session.flush()
            proposal_id = row.id

        reviewed = build_world_model_handlers(get_session=Session)["review_project_world_model_proposal"](
            project_id,
            proposal_id,
            WorldEditProposalReviewRequest(status="accepted", reason="reviewed cognition patch"),
        )
        assert reviewed.status == "accepted"

        cognition = build_book_state_handlers(get_session=Session)["list_book_state_cognition"](
            project_id,
            as_of_chapter=1,
        )
        overlay = next(item for item in cognition["overlays"] if item["observer_id"] == "reader")
        assert overlay["visible_refs"] == ["fact:fact_public"]
        assert overlay["hidden_refs"] == []
        assert overlay["field_overrides"]["field:char_lin:summary"] == "读者误以为林烬已经离城。"
    finally:
        engine.dispose()


def test_structured_patch_reader_promise_ops_commit_native_promises(tmp_path: Path) -> None:
    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)

        def add_patch_proposal(patch: list[dict[str, object]], chapter: int) -> str:
            with Session.begin() as session:
                row = WorldEditProposalRow(
                    project_id=project_id,
                    source="world_studio",
                    target_page_key="reader_promise:promise_truth",
                    target_node_id="promise_truth",
                    target_field="Proposed Correction",
                    proposal_type="ReaderPromiseProposal",
                    proposed_patch_json=json.dumps(
                        {
                            "new_value": "```forwin-patch\n" + json.dumps(patch, ensure_ascii=False) + "\n```",
                            "frontmatter": {"as_of_chapter": chapter},
                        },
                        ensure_ascii=False,
                    ),
                    status="pending",
                    created_by="test",
                )
                session.add(row)
                session.flush()
                return row.id

        handlers = build_world_model_handlers(get_session=Session)
        create_id = add_patch_proposal(
            [
                {
                    "op": "create_reader_promise",
                    "promise_id": "promise_truth",
                    "new_value": {
                        "promise_type": "mystery",
                        "summary": "矿难真相会兑现。",
                        "created_at_chapter": 1,
                        "current_debt_level": 2,
                        "reward_tags": ["mystery"],
                        "status": "open",
                    },
                }
            ],
            1,
        )
        created = handlers["review_project_world_model_proposal"](
            project_id,
            create_id,
            WorldEditProposalReviewRequest(status="accepted", reason="create promise"),
        )
        assert created.status == "accepted"
        promises = build_book_state_handlers(get_session=Session)["list_book_state_reader_promises"](
            project_id,
            as_of_chapter=1,
        )
        promise = next(item for item in promises["reader_promises"] if item["promise_id"] == "promise_truth")
        assert promise["summary"] == "矿难真相会兑现。"
        assert promise["current_debt_level"] == 2

        set_id = add_patch_proposal(
            [
                {
                    "op": "set_reader_promise_field",
                    "promise_id": "promise_truth",
                    "field_path": "summary",
                    "old_value": "矿难真相会兑现。",
                    "new_value": "矿难真相会由旧档案兑现。",
                }
            ],
            2,
        )
        updated = handlers["review_project_world_model_proposal"](
            project_id,
            set_id,
            WorldEditProposalReviewRequest(status="accepted", reason="set promise summary"),
        )
        assert updated.status == "accepted"
        promises = build_book_state_handlers(get_session=Session)["list_book_state_reader_promises"](
            project_id,
            as_of_chapter=2,
        )
        promise = next(item for item in promises["reader_promises"] if item["promise_id"] == "promise_truth")
        assert promise["summary"] == "矿难真相会由旧档案兑现。"

        resolve_id = add_patch_proposal(
            [
                {
                    "op": "resolve_reader_promise",
                    "promise_id": "promise_truth",
                    "old_value": "open",
                }
            ],
            3,
        )
        resolved = handlers["review_project_world_model_proposal"](
            project_id,
            resolve_id,
            WorldEditProposalReviewRequest(status="accepted", reason="resolve promise"),
        )
        assert resolved.status == "accepted"
        promises = build_book_state_handlers(get_session=Session)["list_book_state_reader_promises"](
            project_id,
            as_of_chapter=3,
        )
        promise = next(item for item in promises["reader_promises"] if item["promise_id"] == "promise_truth")
        assert promise["status"] == "resolved"
        assert promise["current_debt_level"] == 0
    finally:
        engine.dispose()


def test_reader_promise_old_value_mismatch_requires_forced_accept(tmp_path: Path) -> None:
    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            repo.upsert_reader_promise(
                ReaderPromise(
                    promise_id="promise_truth",
                    project_id=project_id,
                    promise_type="mystery",
                    summary="矿难真相会兑现。",
                    created_at_chapter=0,
                    status="open",
                )
            )
            blocked = BookStateCompiler(session).compile(
                ApprovedGraphDeltaSet(
                    project_id=project_id,
                    chapter_number=1,
                    graph_deltas=[
                        GraphDelta(
                            id="delta_reader_promise_mismatch",
                            project_id=project_id,
                            chapter_number=1,
                            delta_type="repair",
                            operation="structured_proposal_patch",
                            metadata={
                                "reader_promise_patches": [
                                    {
                                        "op": "set",
                                        "promise_id": "promise_truth",
                                        "field_path": "summary",
                                        "old_value": "错误旧值",
                                        "new_value": "强制修正后的兑现口径。",
                                    }
                                ]
                            },
                        )
                    ],
                )
            )
            assert blocked.committed is False
            assert "old_value mismatch" in blocked.blocked_reasons[0]
            forced = BookStateCompiler(session).compile(
                ApprovedGraphDeltaSet(
                    project_id=project_id,
                    chapter_number=1,
                    forced_accept_reason="人工确认 reader promise repair",
                    graph_deltas=[
                        GraphDelta(
                            id="delta_reader_promise_forced",
                            project_id=project_id,
                            chapter_number=1,
                            delta_type="repair",
                            operation="structured_proposal_patch",
                            metadata={
                                "reader_promise_patches": [
                                    {
                                        "op": "set",
                                        "promise_id": "promise_truth",
                                        "field_path": "summary",
                                        "old_value": "错误旧值",
                                        "new_value": "强制修正后的兑现口径。",
                                    }
                                ]
                            },
                        )
                    ],
                )
            )
            assert forced.committed is True

        promises = build_book_state_handlers(get_session=Session)["list_book_state_reader_promises"](
            project_id,
            as_of_chapter=1,
        )
        promise = next(item for item in promises["reader_promises"] if item["promise_id"] == "promise_truth")
        assert promise["summary"] == "强制修正后的兑现口径。"
    finally:
        engine.dispose()


def test_unified_proposal_api_creates_reviews_and_updates_loadout(tmp_path: Path) -> None:
    from forwin.api_proposal_routes import build_handlers as build_proposal_handlers
    from forwin.api_schemas import WorldEditProposalCreateRequest

    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                    profile={"personality_loadout": {}},
                )
            )

        loadout = {
            "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.8},
            "secondary": [],
            "social_mask": [],
            "stress_modes": [],
            "relationship_patterns": [],
            "overrides": {},
        }
        structured_patch = "```forwin-patch\n" + json.dumps(
            [
                {
                    "op": "set_personality_loadout",
                    "node_id": "char_lin",
                    "old_value": {},
                    "new_value": loadout,
                }
            ],
            ensure_ascii=False,
        ) + "\n```"
        handlers = build_proposal_handlers(
            get_session=Session,
            qdrant_client=FakeQdrantClient(),
            qdrant_models=FakeQdrantModels,
        )
        created = handlers["create_project_proposal"](
            project_id,
            WorldEditProposalCreateRequest(
                source="world_studio",
                target_page_key="character:char_lin",
                target_node_id="char_lin",
                target_field="Proposed Correction",
                proposal_type="PersonalityLoadoutProposal",
                proposed_patch={
                    "new_value": structured_patch,
                    "frontmatter": {"as_of_chapter": 1},
                },
                reason="World Studio loadout edit",
                created_by="world-studio-test",
            ),
        )
        assert created.status == "pending"
        assert created.proposal_type == "PersonalityLoadoutProposal"
        with Session() as session:
            node = BookStateRepository(session).list_world_nodes(project_id, as_of_chapter=1)[0]
            assert node.profile["personality_loadout"] == {}

        fetched = handlers["get_project_proposal"](project_id, created.id)
        listed = handlers["list_project_proposals"](project_id)
        assert fetched.id == created.id
        assert any(item.id == created.id for item in listed)

        approved = handlers["approve_project_proposal"](
            project_id,
            created.id,
            WorldEditProposalReviewRequest(status="accepted", reason="approved loadout"),
        )
        assert approved.status == "accepted"
        assert approved.graph_delta_id
        with Session() as session:
            node = BookStateRepository(session).list_world_nodes(project_id, as_of_chapter=1)[0]
            assert node.profile["personality_loadout"]["dominant"]["skill"] == "trait-suspicious-survivor"

        rejected = handlers["create_project_proposal"](
            project_id,
            WorldEditProposalCreateRequest(
                source="world_studio",
                target_page_key="character:char_lin",
                target_node_id="char_lin",
                target_field="Manual Notes",
                proposal_type="NoteOnlyProposal",
                proposed_patch={"new_value": "只应留在 proposal 审核队列。"},
                reason="human note",
            ),
        )
        before_count = len(build_book_state_handlers(get_session=Session)["list_book_state_deltas"](project_id)["deltas"])
        rejected_info = handlers["reject_project_proposal"](
            project_id,
            rejected.id,
            WorldEditProposalReviewRequest(status="rejected", reason="not canon"),
        )
        after_count = len(build_book_state_handlers(get_session=Session)["list_book_state_deltas"](project_id)["deltas"])
        assert rejected_info.status == "rejected"
        assert after_count == before_count

        world_model_proposals = build_world_model_handlers(get_session=Session)["list_project_world_model_proposals"](
            project_id
        )
        assert {item.id for item in world_model_proposals} >= {created.id, rejected.id}
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


def test_llm_kb_vector_payloads_enforce_role_visibility_scopes(tmp_path: Path) -> None:
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

            LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path / "kb",
                qdrant_client=qdrant_client,
                qdrant_models=FakeQdrantModels,
            ).rebuild(project_id, as_of_chapter=1)

        payloads = [
            point.payload
            for point in qdrant_client.collections["llm_kb_vectors"]["points"].values()
        ]
        assert payloads
        assert all(payload["index_kind"] == "llm_kb" for payload in payloads)
        assert all(payload["as_of_chapter"] == 1 for payload in payloads)
        assert all(payload["projection_version"] == "llm_kb_v2" for payload in payloads)
        assert all(payload["canon_status"] == "canon_projection" for payload in payloads)
        assert all(isinstance(payload["node_refs"], list) for payload in payloads)
        assert all(isinstance(payload["edge_refs"], list) for payload in payloads)
        assert all(isinstance(payload["fact_refs"], list) for payload in payloads)
        assert all(isinstance(payload["map_refs"], list) for payload in payloads)
        assert all(isinstance(payload["chapter_refs"], list) for payload in payloads)
        assert any(
            payload["role_scope"] == "writer"
            and payload["visibility_scope"] == "writer_safe"
            for payload in payloads
        )
        assert any(
            payload["role_scope"] == "reviewer"
            and payload["visibility_scope"] == "reviewer_only"
            for payload in payloads
        )

        retriever = LLMKnowledgeBaseRetriever(
            root=tmp_path / "kb",
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        )
        writer_results = retriever.search(project_id, "context", role="writer", limit=200)
        assert writer_results
        assert all(item["index_kind"] == "llm_kb" for item in writer_results)
        assert all(item["visibility_scope"] == "writer_safe" for item in writer_results)
        assert all(item["role_scope"] == "writer" for item in writer_results)

        reviewer_results = retriever.search(project_id, "context", role="reviewer", limit=200)
        assert any(
            item["role_scope"] == "reviewer"
            and item["visibility_scope"] == "reviewer_only"
            for item in reviewer_results
        )
    finally:
        engine.dispose()


def test_obsidian_human_index_searches_manual_notes_without_writer_context(tmp_path: Path) -> None:
    from forwin.retrieval.obsidian_human_index import ObsidianHumanVectorIndex
    from forwin.world_studio.search_service import WorldStudioSearchService

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
            ObsidianExporter(session).export_project(project_id, vault_root=tmp_path / "vault", as_of_chapter=1)

        page_path = tmp_path / "vault" / "03_Actors" / "Characters" / "林烬_char_lin.md"
        page_text = page_path.read_text(encoding="utf-8")
        page_path.write_text(
            page_text.replace("## Manual Notes\n_empty_", "## Manual Notes\n只给编辑看的伏笔线索。"),
            encoding="utf-8",
        )
        with Session.begin() as session:
            from forwin.obsidian import ObsidianImporter

            imported = ObsidianImporter(session).import_project(project_id, vault_root=tmp_path / "vault")
            assert imported.proposal_count == 1

        ObsidianHumanVectorIndex(
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        ).rebuild_project(project_id, vault_root=tmp_path / "vault")

        service_results = WorldStudioSearchService(
            llm_kb_root=tmp_path / "kb",
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        ).search(
            project_id,
            query="伏笔线索",
            index_kind="obsidian_human",
            role="human",
            limit=5,
        )["results"]
        assert service_results
        assert service_results[0]["index_kind"] == "obsidian_human"
        assert service_results[0]["section_name"] == "Manual Notes"
        assert service_results[0]["section_type"] == "editable"
        assert service_results[0]["canon_status"] == "human_unreviewed"

        api_results = build_world_model_handlers(
            get_session=Session,
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        )["search_project_world_studio"](
            project_id,
            query="伏笔线索",
            index_kind="obsidian_human",
            role="human",
            limit=5,
        )
        assert api_results["results"][0]["index_kind"] == "obsidian_human"
        assert api_results["results"][0]["canon_status"] == "human_unreviewed"

        canon_results = build_world_model_handlers(
            get_session=Session,
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        )["search_project_world_studio"](
            project_id,
            query="旧城线主角",
            index_kind="canon",
            role="human",
            limit=5,
        )
        assert canon_results["results"]
        assert canon_results["results"][0]["index_kind"] == "canon"
        assert canon_results["results"][0]["canon_status"] == "canon_projection"

        with Session() as session:
            writer_pack = RetrievalBroker().build_world_model_pack(
                StateRepository(session),
                project_id,
                1,
                "writing",
            )
        assert "只给编辑看的伏笔线索" not in str(writer_pack.model_dump(mode="json"))
    finally:
        engine.dispose()


def test_skill_vector_index_is_world_studio_only_and_not_writer_context(tmp_path: Path) -> None:
    from forwin.retrieval.skill_index import SkillVectorIndex
    from forwin.world_studio.search_service import WorldStudioSearchService

    Session, engine = _session_factory()
    qdrant_client = FakeQdrantClient()
    skill_root = tmp_path / "skills"
    skill_path = skill_root / "character_personality" / "skills" / "traits" / "trait-suspicious-survivor" / "SKILL.md"
    skill_path.parent.mkdir(parents=True)
    skill_path.write_text(
        """---
name: trait-suspicious-survivor
version: 1.0.0
description: 疑心幸存者人格约束。
forwin_scope: character_personality
stage_keys:
  - chapter_draft
task_families:
  - write_chapter
mode: instruction_only
---
# Trait Suspicious Survivor

疑心幸存者只相信可验证线索，压力下会控制信息流。
""",
        encoding="utf-8",
    )
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                )
            )

        SkillVectorIndex(
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        ).rebuild(skill_root)
        service_results = WorldStudioSearchService(
            skill_root=skill_root,
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        ).search(
            project_id,
            query="疑心幸存者",
            index_kind="skill",
            role="human",
            limit=5,
        )["results"]
        assert service_results
        assert service_results[0]["index_kind"] == "skill"
        assert service_results[0]["skill_id"] == "trait-suspicious-survivor"
        assert service_results[0]["role_scope"] == "skill_maintenance"

        with Session() as session:
            writer_pack = RetrievalBroker().build_world_model_pack(
                StateRepository(session),
                project_id,
                1,
                "writing",
            )
        assert "疑心幸存者只相信可验证线索" not in str(writer_pack.model_dump(mode="json"))
    finally:
        engine.dispose()


def test_active_personality_context_enters_world_model_and_llm_kb_role_packs(tmp_path: Path) -> None:
    Session, engine = _session_factory()
    qdrant_client = FakeQdrantClient()
    try:
        loadout = {
            "dominant": {"skill": "trait-suspicious-survivor", "weight": 0.75},
            "secondary": [],
            "social_mask": [],
            "stress_modes": [],
            "relationship_patterns": [],
            "overrides": {},
        }
        with Session.begin() as session:
            project_id = _create_project(session)
            BookStateRepository(session).create_world_node(
                WorldNode(
                    id="char_lin",
                    project_id=project_id,
                    node_type="character",
                    name="林烬",
                    summary="旧城线主角。",
                    profile={"personality_loadout": loadout},
                )
            )

        with Session() as session:
            writer_pack = RetrievalBroker().build_world_model_pack(
                StateRepository(session),
                project_id,
                1,
                "writing",
            )
        assert writer_pack.active_personality_contexts
        assert writer_pack.active_personality_contexts[0]["character_id"] == "char_lin"
        assert "trait-suspicious-survivor" in writer_pack.active_personality_contexts[0]["active_skills"]["dominant"]

        with Session.begin() as session:
            result = LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path / "kb",
                qdrant_client=qdrant_client,
                qdrant_models=FakeQdrantModels,
            ).rebuild(project_id, as_of_chapter=1)

        root = Path(result.root)
        writer_context = json.loads((root / "packs" / "writer" / "context.json").read_text(encoding="utf-8"))
        reviewer_context = json.loads((root / "packs" / "reviewer" / "context.json").read_text(encoding="utf-8"))
        assert writer_context["active_personality_contexts"][0]["character_id"] == "char_lin"
        assert reviewer_context["active_personality_contexts"][0]["character_id"] == "char_lin"

        reviewer_results = LLMKnowledgeBaseRetriever(
            root=tmp_path / "kb",
            qdrant_client=qdrant_client,
            qdrant_models=FakeQdrantModels,
        ).search(project_id, "trait-suspicious-survivor", role="reviewer", limit=100)
        assert any("trait-suspicious-survivor" in item["text"] for item in reviewer_results)
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
