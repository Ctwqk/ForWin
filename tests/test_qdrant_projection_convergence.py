from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest

from forwin.retrieval.memory_index import HashTextEmbedder
from forwin.llm_kb.vector_index import (
    LLMKBVectorIndex,
    _existing_points_by_normalized_id,
)
from tests.qdrant import FakeQdrantClient, FakeQdrantModels


COLLECTION = "llm-kb-convergence"


def _write_current_state(root: Path, project_id: str, text: str) -> None:
    project_root = root / project_id
    project_root.mkdir(parents=True, exist_ok=True)
    (project_root / "CURRENT_STATE.md").write_text(text, encoding="utf-8")


def _vector_index(root: Path, client: FakeQdrantClient) -> LLMKBVectorIndex:
    return LLMKBVectorIndex(
        root,
        collection_name=COLLECTION,
        embedder=HashTextEmbedder(dims=96),
        owns_embedder=True,
        qdrant_client=client,
        qdrant_models=FakeQdrantModels,
    )


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


def test_rebuild_updates_and_deletes_only_owned_stale_points(tmp_path: Path) -> None:
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
        assert second["deleted_section_count"] == 1
        owned = _owned_points(client, "project-1")
        assert len(owned) == 1
        payload = next(iter(owned.values())).payload
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


def test_target_metadata_change_updates_existing_payload(tmp_path: Path) -> None:
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
        point = next(iter(_owned_points(client, "project-1").values()))
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
    payload = next(iter(_owned_points(client, "project-1").values())).payload
    assert payload["as_of_chapter"] == 2
    assert payload["source_digest"] == "digest-2"
    assert obsolete_result["upserted_section_count"] == 1
    assert "obsolete_field" not in payload


def test_delete_failure_fails_rebuild_and_preserves_stale_point(
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

        with pytest.raises(RuntimeError, match="delete unavailable"):
            index.rebuild_project("project-1", as_of_chapter=1)

        assert len(_owned_points(client, "project-1")) == 2
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


def test_stale_native_integer_id_is_not_stringified_for_delete(
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

    assert result["deleted_section_count"] == 1
    assert client.deleted_point_ids == [42]
    assert "42" not in client.collections[COLLECTION]["points"]
