from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from forwin.retrieval.memory_index import HashTextEmbedder
from forwin.retrieval.embedding_cache import memory_embedding_identity, LLM_KB_PREPROCESSING
from forwin.llm_kb.vector_index import (
    LLMKBVectorIndex,
    _existing_points_by_normalized_id,
    _collect_project_sections,
    LLM_KB_PROJECTION_VERSION,
)
from tests.qdrant import FakeQdrantClient, FakeQdrantModels


BASE_COLLECTION = "llm-kb-convergence"
COLLECTION = f"{BASE_COLLECTION}_{memory_embedding_identity(HashTextEmbedder(dims=96), preprocessing=LLM_KB_PREPROCESSING)}"


def _write_current_state(root: Path, project_id: str, text: str) -> None:
    project_root = root / project_id
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "CURRENT_STATE.md").write_text(text, encoding="utf-8")


def _vector_index(root: Path, client: FakeQdrantClient) -> LLMKBVectorIndex:
    return LLMKBVectorIndex(
        root,
        collection_name=BASE_COLLECTION,
        embedder=HashTextEmbedder(dims=96),
        owns_embedder=True,
        qdrant_client=client,
        qdrant_models=FakeQdrantModels,
    )


def _search_current(index, root, source_digest, as_of_chapter):
    sections = _collect_project_sections(root / "project-1", source_digest=source_digest,
        as_of_chapter=as_of_chapter, projection_version=LLM_KB_PROJECTION_VERSION)
    return index.search("project-1", "state", as_of_chapter=as_of_chapter,
        source_sections={(section["file_key"], section["section_key"]): section for section in sections})


def _owned_points(client: FakeQdrantClient, project_id: str) -> dict[str, object]:
    points = client.collections[COLLECTION]["points"]
    return {
        point_id: point
        for point_id, point in points.items()
        if point.payload.get("project_id") == project_id
        and point.payload.get("index_kind") == "llm_kb"
    }


def _insert_foreign_point(
    client: FakeQdrantClient,
    *,
    project_id: str,
    index_kind: str,
) -> str:
    point_id = str(uuid4())
    client.collections[COLLECTION]["points"][point_id] = FakeQdrantModels.PointStruct(
        id=point_id,
        vector=[0.0] * 96,
        payload={
            "project_id": project_id,
            "index_kind": index_kind,
            "section_digest": "foreign",
        },
    )
    return point_id


def test_rebuild_retains_other_generations_and_replays_current_points(tmp_path: Path) -> None:
    client = FakeQdrantClient()
    _write_current_state(
        tmp_path,
        "project-1",
        "# Current\nold state\n\n## Removed\nstale section\n",
    )
    index = _vector_index(tmp_path, client)
    try:
        first = index.rebuild_project(
            "project-1",
            source_digest="digest-1",
            as_of_chapter=1,
        )
        assert first["section_count"] == 2
        assert first["upserted_section_count"] == 2
        assert first["deleted_section_count"] == 0

        other_project_id = _insert_foreign_point(
            client,
            project_id="project-2",
            index_kind="llm_kb",
        )
        other_index_id = _insert_foreign_point(
            client,
            project_id="project-1",
            index_kind="chapter_memory",
        )
        _write_current_state(tmp_path, "project-1", "# Current\nnew state\n")

        second = index.rebuild_project(
            "project-1",
            source_digest="digest-2",
            as_of_chapter=2,
        )

        assert second["section_count"] == 1
        assert second["upserted_section_count"] == 1
        assert second["deleted_section_count"] == 0
        assert second["retained_other_generation_count"] == 2
        owned = _owned_points(client, "project-1")
        assert len(owned) == 3
        current = [point for point in owned.values() if point.payload["source_digest"] == "digest-2"]
        assert len(current) == 1
        payload = current[0].payload
        records = _search_current(index, tmp_path, "digest-2", 2)
        assert [record.text for record in records] == ["# Current\nnew state"]
        assert payload["text"] == "# Current\nnew state"
        assert payload["as_of_chapter"] == 2
        assert payload["source_digest"] == "digest-2"
        assert other_project_id in client.collections[COLLECTION]["points"]
        assert other_index_id in client.collections[COLLECTION]["points"]

        writes_before_replay = client.upsert_calls
        deletes_before_replay = client.delete_calls
        replay = index.rebuild_project(
            "project-1",
            source_digest="digest-2",
            as_of_chapter=2,
        )

        assert replay["upserted_section_count"] == 0
        assert replay["skipped_section_count"] == 1
        assert replay["deleted_section_count"] == 0
        assert client.upsert_calls == writes_before_replay
        assert client.delete_calls == deletes_before_replay
    finally:
        index.close()


def test_non_ascii_and_repeated_headings_have_distinct_stable_points(
    tmp_path: Path,
) -> None:
    client = FakeQdrantClient()
    _write_current_state(
        tmp_path,
        "project-1",
        "# 当前状态\n总览\n\n## 闻澄\n第一次\n\n## 林烬\n第二次\n"
        "\n## 闻澄\n第三次\n",
    )
    index = _vector_index(tmp_path, client)
    try:
        first = index.rebuild_project("project-1", as_of_chapter=1)
        first_points = _owned_points(client, "project-1")
        first_ids = set(first_points)
        section_keys = {
            point.payload["section_key"] for point in first_points.values()
        }
        replay = index.rebuild_project("project-1", as_of_chapter=1)
    finally:
        index.close()

    assert first["section_count"] == 4
    assert len(first_points) == first["section_count"]
    assert len(section_keys) == first["section_count"]
    assert all(key.startswith("section-") for key in section_keys)
    assert set(_owned_points(client, "project-1")) == first_ids
    assert replay["skipped_section_count"] == first["section_count"]
    assert replay["upserted_section_count"] == 0


def test_target_metadata_change_creates_new_identity_and_repairs_its_payload(tmp_path: Path) -> None:
    client = FakeQdrantClient()
    _write_current_state(tmp_path, "project-1", "# Current\nstable state\n")
    index = _vector_index(tmp_path, client)
    try:
        index.rebuild_project(
            "project-1",
            source_digest="digest-1",
            as_of_chapter=1,
        )
        result = index.rebuild_project(
            "project-1",
            source_digest="digest-2",
            as_of_chapter=2,
        )
        point = next(point for point in _owned_points(client, "project-1").values() if point.payload["source_digest"] == "digest-2")
        point.payload["obsolete_field"] = "remove-me"
        obsolete_result = index.rebuild_project(
            "project-1",
            source_digest="digest-2",
            as_of_chapter=2,
        )
    finally:
        index.close()

    assert result["upserted_section_count"] == 1
    assert result["skipped_section_count"] == 0
    payload = next(point for point in _owned_points(client, "project-1").values() if point.payload["source_digest"] == "digest-2").payload
    assert len(_owned_points(client, "project-1")) == 2
    assert payload["as_of_chapter"] == 2
    assert payload["source_digest"] == "digest-2"
    assert obsolete_result["upserted_section_count"] == 1
    assert "obsolete_field" not in payload


def test_rebuild_does_not_require_delete_and_preserves_other_content_versions(
    tmp_path: Path,
) -> None:
    class DeleteFails(FakeQdrantClient):
        fail_delete = False

        def delete(self, **kwargs) -> None:
            if self.fail_delete:
                self.delete_calls += 1
                raise RuntimeError("qdrant delete unavailable")
            super().delete(**kwargs)

    client = DeleteFails()
    _write_current_state(
        tmp_path,
        "project-1",
        "# Current\nstate\n\n## Removed\nstale section\n",
    )
    index = _vector_index(tmp_path, client)
    try:
        index.rebuild_project("project-1", as_of_chapter=1)
        assert len(_owned_points(client, "project-1")) == 2
        _write_current_state(tmp_path, "project-1", "# Current\nstate\n")
        client.fail_delete = True

        result = index.rebuild_project("project-1", as_of_chapter=1)
        assert result["deleted_section_count"] == 0
        assert result["retained_other_generation_count"] == 1
        assert client.delete_calls == 0
        assert len(_owned_points(client, "project-1")) == 2
        records = _search_current(index, tmp_path, "", 1)
        assert [record.text for record in records] == ["# Current\nstate"]
    finally:
        index.close()


def test_existing_owned_point_enumeration_scrolls_every_page() -> None:
    class ScrollClient:
        def __init__(self) -> None:
            self.offsets: list[object] = []

        def scroll(self, *, offset=None, **_kwargs):
            self.offsets.append(offset)
            if offset is None:
                return (
                    [SimpleNamespace(id="point-1", payload={"chapter": 1})],
                    "page-2",
                )
            return (
                [SimpleNamespace(id="point-2", payload={"chapter": 2})],
                None,
            )

    client = ScrollClient()
    existing = _existing_points_by_normalized_id(
        client,
        COLLECTION,
        "project-1",
        index_kind="llm_kb",
        project_filter=object(),
    )

    assert client.offsets == [None, "page-2"]
    assert {key: point.payload for key, point in existing.items()} == {
        "point-1": {"chapter": 1},
        "point-2": {"chapter": 2},
    }


def test_unknown_legacy_native_integer_id_is_preserved(
    tmp_path: Path,
) -> None:
    client = FakeQdrantClient()
    index = _vector_index(tmp_path, client)
    try:
        index.rebuild_project("project-1")
        client.collections[COLLECTION]["points"]["42"] = (
            FakeQdrantModels.PointStruct(
                id=42,
                vector=[0.0] * 96,
                payload={
                    "project_id": "project-1",
                    "index_kind": "llm_kb",
                },
            )
        )

        result = index.rebuild_project("project-1")
    finally:
        index.close()

    assert result["deleted_section_count"] == 0
    assert result["retained_other_generation_count"] == 1
    assert client.deleted_point_ids == []
    assert client.collections[COLLECTION]["points"]["42"].id == 42


def test_old_worker_starting_after_new_points_does_not_delete_current_generation(tmp_path):
    client = FakeQdrantClient()
    current_root, old_root = tmp_path / 'current', tmp_path / 'old-worker'
    _write_current_state(current_root, 'project-1', '# Current\nNEW_VERSION\n')
    _write_current_state(old_root, 'project-1', '# Current\nOLD_VERSION\n')
    current = _vector_index(current_root, client)
    old = _vector_index(old_root, client)
    try:
        current.rebuild_project('project-1', source_digest='new-source', as_of_chapter=2)
        current_ids = set(_owned_points(client, 'project-1'))
        late = old.rebuild_project('project-1', source_digest='old-source', as_of_chapter=1)
        assert late['deleted_section_count'] == 0
        assert current_ids < set(_owned_points(client, 'project-1'))
        assert [record.text for record in _search_current(current, current_root, 'new-source', 2)] == ['# Current\nNEW_VERSION']
    finally:
        current.close()
        old.close()


@pytest.mark.parametrize('change', ['source', 'content', 'embedding'])
def test_point_identity_separates_each_source_content_and_embedding_generation(tmp_path, change):
    client = FakeQdrantClient()
    _write_current_state(tmp_path, 'project-1', '# Current\noriginal\n')
    index = _vector_index(tmp_path, client)
    try:
        index.rebuild_project('project-1', source_digest='source-1', as_of_chapter=1)
        original_ids = set(_owned_points(client, 'project-1'))
        original_point = next(iter(_owned_points(client, 'project-1').values()))
        original_identity = original_point.payload['embedding_identity']
        digest = 'source-2' if change == 'source' else 'source-1'
        if change == 'content':
            _write_current_state(tmp_path, 'project-1', '# Current\nchanged\n')
        if change == 'embedding':
            index.embedder.model = 'different-model-same-96-dimensions'
        result = index.rebuild_project('project-1', source_digest=digest, as_of_chapter=1)
        points = {key: point for collection in client.collections.values() for key, point in collection['points'].items()}
        assert original_ids < set(points)
        assert len(points) == 2
        assert result['upserted_section_count'] == 1
        assert result['deleted_section_count'] == 0
        assert original_point.payload['embedding_identity'] == original_identity
        if change == 'embedding':
            assert len({point.payload['embedding_identity'] for point in points.values()}) == 2
        replay = index.rebuild_project('project-1', source_digest=digest, as_of_chapter=1)
        assert replay['upserted_section_count'] == 0
        assert replay['skipped_section_count'] == 1
    finally:
        index.close()


def test_same_dimension_model_namespace_disposes_duplicate_section_limit(tmp_path):
    client = FakeQdrantClient()
    _write_current_state(tmp_path, "project-1", "# First\nstate\n## Second\nstate\n")
    first = _vector_index(tmp_path, client)
    first.rebuild_project("project-1", source_digest="current", as_of_chapter=1)
    second = _vector_index(tmp_path, client)
    second.embedder.model = "other-model"
    assert second.search("project-1", "state") == []
    second.rebuild_project("project-1", source_digest="current", as_of_chapter=1)
    assert first.collection_name != second.collection_name
    sections = _collect_project_sections(tmp_path / "project-1", source_digest="current", as_of_chapter=1, projection_version=LLM_KB_PROJECTION_VERSION)
    sources = {(item["file_key"], item["section_key"]): item for item in sections}
    hits = second.search("project-1", "state", limit=2, source_sections=sources)
    assert {hit.section_key for hit in hits} == {"First", "Second"}
    assert len(client.collections[first.collection_name]["points"]) == 2


def test_llm_kb_cache_reuses_unchanged_sections_across_restart_and_sources(tmp_path):
    from forwin.models.base import get_engine, get_session_factory, init_db
    from tests.postgres import postgres_test_url
    engine = get_engine(postgres_test_url("llm-kb-cache"))
    init_db(engine)
    sessions = get_session_factory(engine)
    calls = []
    class CountingEmbedder(HashTextEmbedder):
        def embed(self, texts):
            calls.extend(texts)
            return super().embed(texts)
    try:
        _write_current_state(tmp_path, "project-1", "# First\nstate\n## Second\nstate\n")
        for revision in (1, 2):
            index = LLMKBVectorIndex(tmp_path, collection_name="cache", embedder=CountingEmbedder(dims=96), qdrant_client=FakeQdrantClient(), qdrant_models=FakeQdrantModels)
            index.session_factory = sessions
            index.rebuild_project("project-1", source_digest=str(revision), as_of_chapter=revision)
        assert len(calls) == 2
    finally:
        engine.dispose()
