from __future__ import annotations

import json
from datetime import timedelta

import pytest

from forwin.book_state.cognition import CognitionView
from forwin.book_state.map_graph import MapGraph
from forwin.book_state.repository import BookStateRepository
from forwin.book_state.runtime import BookStateRuntime, ObjectiveWorldGraph
from forwin.knowledge_system.dependencies import (
    dependencies_valid,
    page_dependencies,
)
from forwin.knowledge_system.page_repository import KnowledgePageRepository
from forwin.models import Project
from forwin.models.knowledge import KnowledgeProjectionPageRow
from forwin.protocol.book_state import (
    CognitionOverlay,
    FactNode,
    FactPatch,
    MapNode,
    NodePatch,
    WorldNode,
)
from tests.test_knowledge_system_v46 import _create_project, _session_factory


def _runtime(
    project_id: str = "dependency-project",
    *,
    world_count: int = 1,
    map_count: int = 0,
) -> BookStateRuntime:
    nodes = [
        WorldNode(
            id=f"world-{index}",
            project_id=project_id,
            node_type="character",
            name=f"Character {index}",
            state={"created_at": "04:07"},
            profile={"updated_at": "04:08"},
            metadata={
                "owner": "archive",
                "evidence": {"refreshed_at": "04:09"},
            },
        )
        for index in range(world_count)
    ]
    cognition = CognitionView(
        CognitionOverlay(
            id="reader-cognition",
            project_id=project_id,
            observer_type="reader",
            observer_id="reader",
            metadata={"marker": "COGNITION_SENTINEL"},
        )
    )
    runtime = BookStateRuntime(
        project_id=project_id,
        as_of_chapter=0,
        world=ObjectiveWorldGraph(nodes=nodes),
        map_graph=MapGraph(
            nodes=[
                MapNode(
                    id=f"map-{index}",
                    project_id=project_id,
                    node_type="site",
                    name=f"Site {index}",
                )
                for index in range(map_count)
            ]
        ),
        cognition_by_observer={("reader", "reader"): cognition},
    )
    return runtime


@pytest.mark.parametrize(
    ("field_path", "replacement"),
    [
        ("state.created_at", "04:17"),
        ("profile.updated_at", "04:18"),
        ("metadata.evidence.refreshed_at", "04:19"),
        ("metadata.owner", "harbor-master"),
    ],
)
def test_business_timestamps_and_owner_changes_invalidate_node_manifest(
    field_path: str,
    replacement: str,
) -> None:
    runtime = _runtime()
    manifest = page_dependencies(runtime, scope="node", node_id="world-0")

    runtime.world.apply_node_patch(
        NodePatch(
            node_id="world-0",
            node_type="character",
            op="set",
            field_path=field_path,
            new_value=replacement,
        )
    )

    assert not dependencies_valid(manifest, runtime)


def test_old_manifest_version_fails_closed_and_current_version_validates() -> None:
    runtime = _runtime()
    current = page_dependencies(runtime, scope="node", node_id="world-0")
    legacy = {**current, "version": 1}

    assert current["version"] > 1
    assert dependencies_valid(current, runtime)
    assert not dependencies_valid(legacy, runtime)


def test_whole_book_fact_change_invalidates_manifest() -> None:
    runtime = _runtime()
    runtime.world.add_fact(
        FactNode(
            id="fact-1",
            project_id=runtime.project_id,
            proposition="The key is sealed.",
        )
    )
    manifest = page_dependencies(runtime, scope="book")

    runtime.world.apply_fact_patch(
        FactPatch(
            fact_id="fact-1",
            op="set",
            field_path="metadata.owner",
            new_value="harbor-master",
        )
    )

    assert not dependencies_valid(manifest, runtime)


def _projection_row(
    *,
    project_id: str,
    node_id: str,
    page_type: str,
    manifest: dict[str, object],
) -> KnowledgeProjectionPageRow:
    return KnowledgeProjectionPageRow(
        project_id=project_id,
        page_key=f"{page_type}:{node_id}",
        page_type=page_type,
        title=node_id,
        vault_path=f"{node_id}.md",
        markdown=f"# {node_id}",
        frontmatter_json=json.dumps({"node_id": node_id}),
        content_hash=f"hash-{node_id}",
        logical_identity_key=f"{page_type}:name:{node_id}",
        canonical_source_type="book_state_node",
        canonical_source_id=node_id,
        dependency_manifest_json=json.dumps(manifest),
    )


def test_list_valid_rows_prepares_world_and_cognition_once(monkeypatch) -> None:
    from forwin.knowledge_system import dependencies as dependency_module
    from forwin.knowledge_system import page_repository as repository_module

    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            runtime = _runtime(project_id, world_count=100)
            manifests = {
                node_id: page_dependencies(runtime, scope="node", node_id=node_id)
                for node_id in runtime.world.nodes_by_id
            }
            session.add_all(
                [
                    _projection_row(
                        project_id=project_id,
                        node_id=node_id,
                        page_type="character",
                        manifest=manifest,
                    )
                    for node_id, manifest in manifests.items()
                ]
            )

        state_reads = 0
        original_get_state = runtime.world.get_state

        def counted_get_state(node_id: str):
            nonlocal state_reads
            state_reads += 1
            return original_get_state(node_id)

        manifest_loads = 0
        manifest_json = {json.dumps(item) for item in manifests.values()}
        original_load_json = repository_module._load_json

        def counted_load_json(raw):
            nonlocal manifest_loads
            if raw in manifest_json:
                manifest_loads += 1
            return original_load_json(raw)

        cognition_serializations = 0
        original_dumps = dependency_module.json.dumps

        def counted_dumps(value, *args, **kwargs):
            nonlocal cognition_serializations
            if "COGNITION_SENTINEL" in repr(value):
                cognition_serializations += 1
            return original_dumps(value, *args, **kwargs)

        monkeypatch.setattr(runtime.world, "get_state", counted_get_state)
        monkeypatch.setattr(repository_module, "_load_json", counted_load_json)
        monkeypatch.setattr(dependency_module.json, "dumps", counted_dumps)

        with Session() as session:
            rows = KnowledgePageRepository(session).list_valid_rows(
                project_id,
                runtime=runtime,
                as_of_chapter=0,
            )

        assert len(rows) == 100
        assert state_reads == 100
        assert manifest_loads == 100
        assert cognition_serializations == 1
    finally:
        engine.dispose()


def test_map_only_validation_does_not_materialize_unrelated_world_nodes(
    monkeypatch,
) -> None:
    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            runtime = _runtime(project_id, world_count=100, map_count=200)
            session.add_all(
                [
                    _projection_row(
                        project_id=project_id,
                        node_id=node_id,
                        page_type="map_node",
                        manifest=page_dependencies(
                            runtime,
                            scope="map_node",
                            node_id=node_id,
                        ),
                    )
                    for node_id in runtime.map.nodes_by_id
                ]
            )

        state_reads = 0
        original_get_state = runtime.world.get_state

        def counted_get_state(node_id: str):
            nonlocal state_reads
            state_reads += 1
            return original_get_state(node_id)

        monkeypatch.setattr(runtime.world, "get_state", counted_get_state)
        with Session() as session:
            rows = KnowledgePageRepository(session).list_valid_rows(
                project_id,
                runtime=runtime,
                as_of_chapter=0,
            )

        assert len(rows) == 200
        assert state_reads == 0
    finally:
        engine.dispose()


def test_writer_rejects_legacy_and_changed_business_timestamp_until_reexport(
    tmp_path,
) -> None:
    from forwin.knowledge_system.context import KnowledgeContextQuery
    from forwin.obsidian import ObsidianExporter

    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            repo.create_world_node(
                WorldNode(
                    id="evidence",
                    project_id=project_id,
                    node_type="character",
                    name="Evidence Keeper",
                    metadata={"created_at": "04:07"},
                )
            )
            exporter = ObsidianExporter(session)
            exporter.export_project(project_id, vault_root=tmp_path)
            row = (
                session.query(KnowledgeProjectionPageRow)
                .filter_by(
                    project_id=project_id,
                    page_key="character:evidence",
                )
                .one()
            )
            manifest = json.loads(row.dependency_manifest_json)
            row.dependency_manifest_json = json.dumps({**manifest, "version": 1})

        with Session() as session:
            row = (
                session.query(KnowledgeProjectionPageRow)
                .filter_by(
                    project_id=project_id,
                    page_key="character:evidence",
                )
                .one()
            )
            original = (row.markdown, row.content_hash, row.updated_at)
            legacy = KnowledgeContextQuery(session).build(
                project_id=project_id,
                chapter_number=1,
                max_pages=20,
            )
            assert all(
                page.page_key != "character:evidence"
                for page in legacy.relevant_world_pages
            )
            session.refresh(row)
            assert (row.markdown, row.content_hash, row.updated_at) == original

        with Session.begin() as session:
            exporter = ObsidianExporter(session)
            exporter.export_project(project_id, vault_root=tmp_path)
            current = KnowledgeContextQuery(session).build(
                project_id=project_id,
                chapter_number=1,
                max_pages=20,
            )
            assert any(
                page.page_key == "character:evidence"
                for page in current.relevant_world_pages
            )

            repo = BookStateRepository(session)
            node = repo.get_world_node("evidence")
            assert node is not None
            repo.create_world_node(
                node.model_copy(
                    update={"metadata": {**node.metadata, "created_at": "04:17"}}
                )
            )
            session.get(Project, project_id).book_revision += 1

        with Session() as session:
            changed = KnowledgeContextQuery(session).build(
                project_id=project_id,
                chapter_number=1,
                max_pages=20,
            )
            assert all(
                page.page_key != "character:evidence"
                for page in changed.relevant_world_pages
            )

        with Session.begin() as session:
            ObsidianExporter(session).export_project(project_id, vault_root=tmp_path)
            rebuilt = KnowledgeContextQuery(session).build(
                project_id=project_id,
                chapter_number=1,
                max_pages=20,
            )
            assert any(
                page.page_key == "character:evidence"
                for page in rebuilt.relevant_world_pages
            )
    finally:
        engine.dispose()


def test_reused_exporter_keeps_dependency_work_linear_and_refreshes_preparation(
    tmp_path,
    monkeypatch,
) -> None:
    from forwin.obsidian import ObsidianExporter

    Session, engine = _session_factory()
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            repo = BookStateRepository(session)
            for index in range(100):
                repo.create_world_node(
                    WorldNode(
                        id=f"node-{index}",
                        project_id=project_id,
                        node_type="character",
                        name=f"Character {index}",
                    )
                )

            calls = 0
            original_get_state = ObjectiveWorldGraph.get_state

            def counted_get_state(graph, node_id: str):
                nonlocal calls
                calls += 1
                return original_get_state(graph, node_id)

            monkeypatch.setattr(ObjectiveWorldGraph, "get_state", counted_get_state)
            exporter = ObsidianExporter(session)
            exporter.export_project(project_id, vault_root=tmp_path)
            first_calls = calls
            first_row = (
                session.query(KnowledgeProjectionPageRow)
                .filter_by(
                    project_id=project_id,
                    page_key="character:node-0",
                )
                .one()
            )
            first_fingerprint = json.loads(first_row.dependency_manifest_json)[
                "fingerprint"
            ]

            node = repo.get_world_node("node-0")
            assert node is not None
            repo.create_world_node(
                node.model_copy(update={"profile": {"refreshed_at": "05:00"}})
            )
            for index in range(100, 200):
                repo.create_world_node(
                    WorldNode(
                        id=f"node-{index}",
                        project_id=project_id,
                        node_type="character",
                        name=f"Character {index}",
                    )
                )
            session.get(Project, project_id).book_revision += 1
            calls = 0
            exporter.export_project(project_id, vault_root=tmp_path)
            second_calls = calls
            session.refresh(first_row)
            second_fingerprint = json.loads(first_row.dependency_manifest_json)[
                "fingerprint"
            ]

        assert first_calls <= 500
        assert second_calls <= 1_000
        assert second_calls <= first_calls * 3
        assert second_fingerprint != first_fingerprint
    finally:
        engine.dispose()


def test_llm_kb_read_ignores_row_audit_time_but_tracks_business_payload(
    tmp_path,
) -> None:
    from forwin.llm_kb import LLMKnowledgeBaseCompiler
    from forwin.models.world_contract import ArcWorldContractRow
    from forwin.retrieval.broker_core import RetrievalBroker
    from forwin.retrieval.source_identity import CanonReadBaseline
    from tests.qdrant import FakeQdrantClient, FakeQdrantModels

    Session, engine = _session_factory()
    qdrant = FakeQdrantClient()
    collection = "knowledge-dependency-audit"
    broker = None
    try:
        with Session.begin() as session:
            project_id = _create_project(session)
            session.add(
                ArcWorldContractRow(
                    project_id=project_id,
                    arc_id="arc-1",
                    arc_number=1,
                    contract_json=json.dumps(
                        {"owner": "planner", "updated_at": "04:08"}
                    ),
                )
            )
            LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path,
                qdrant_client=qdrant,
                qdrant_models=FakeQdrantModels,
                qdrant_collection=collection,
            ).rebuild(project_id)

        broker = RetrievalBroker(
            llm_kb_root=tmp_path,
            llm_kb_qdrant_client=qdrant,
            llm_kb_qdrant_models=FakeQdrantModels,
            llm_kb_qdrant_collection=collection,
        )
        with Session.begin() as session:
            row = (
                session.query(ArcWorldContractRow)
                .filter_by(project_id=project_id)
                .one()
            )
            assert row.created_at is not None
            assert row.updated_at is not None
            row.created_at += timedelta(minutes=1)
            row.updated_at += timedelta(minutes=1)

        with Session() as session:
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=0)
            audit_only = broker._load_llm_kb_context(
                project_id,
                pack_kind="writing",
                session=session,
                baseline=baseline,
            )
            assert audit_only.get("excerpts")

        with Session.begin() as session:
            row = (
                session.query(ArcWorldContractRow)
                .filter_by(project_id=project_id)
                .one()
            )
            row.contract_json = json.dumps(
                {"owner": "harbor-master", "updated_at": "04:18"}
            )
            session.get(Project, project_id).book_revision += 1

        with Session() as session:
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=0)
            changed = broker._load_llm_kb_context(
                project_id,
                pack_kind="writing",
                session=session,
                baseline=baseline,
            )
            assert changed == {}

        with Session.begin() as session:
            LLMKnowledgeBaseCompiler(
                session,
                root=tmp_path,
                qdrant_client=qdrant,
                qdrant_models=FakeQdrantModels,
                qdrant_collection=collection,
            ).rebuild(project_id)
            baseline = CanonReadBaseline.capture(session, project_id, as_of_chapter=0)
            rebuilt = broker._load_llm_kb_context(
                project_id,
                pack_kind="writing",
                session=session,
                baseline=baseline,
            )
            assert rebuilt.get("excerpts")
    finally:
        if broker is not None:
            broker.close()
        engine.dispose()
