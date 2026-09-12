from __future__ import annotations

import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from types import SimpleNamespace

import pytest

from forwin.retrieval import memory_index as memory_index_module
from forwin.retrieval.broker_core.broker import RetrievalBroker
from forwin.retrieval.memory_index import (
    HashTextEmbedder,
    QdrantChapterMemoryIndex,
    create_memory_index,
)
from forwin.llm_kb import vector_index as llm_kb_vector_module
from forwin.llm_kb.vector_index import LLMKBVectorIndex
from forwin.retrieval.embedding_cache import memory_embedding_identity, LLM_KB_PREPROCESSING
from forwin.api_schema import WorldModelExportRequest
from forwin.http.adapters import api_obsidian_routes
from forwin.retrieval import obsidian_human_index as obsidian_index_module
from forwin.retrieval import skill_index as skill_index_module
from forwin.retrieval.obsidian_human_index import ObsidianHumanVectorIndex
from forwin.retrieval.skill_index import SkillVectorIndex
from forwin.world_studio import search_service as world_search_module
from forwin.world_studio.search_service import WorldStudioSearchService
from tests.qdrant import FakeQdrantClient, FakeQdrantModels


class _CountingQdrantClient(FakeQdrantClient):
    def __init__(self, *, delay_seconds: float = 0.0) -> None:
        super().__init__()
        self.delay_seconds = delay_seconds
        self.collection_list_calls = 0
        self.collection_create_calls = 0

    def get_collections(self):
        self.collection_list_calls += 1
        if self.delay_seconds:
            time.sleep(self.delay_seconds)
        return super().get_collections()

    def create_collection(self, *, collection_name: str, vectors_config) -> None:
        self.collection_create_calls += 1
        super().create_collection(
            collection_name=collection_name,
            vectors_config=vectors_config,
        )


def test_memory_index_constructor_and_status_do_not_initialize_qdrant(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = 0

    def create_client(_url: str):
        nonlocal calls
        calls += 1
        raise RuntimeError("qdrant unavailable")

    monkeypatch.setattr(memory_index_module, "_create_qdrant_client", create_client)

    index = create_memory_index(
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="lazy_chapters",
        qdrant_models=FakeQdrantModels,
    )

    assert calls == 0
    assert index.embedding_status()["kind"] == "hash"
    assert index.collection_vector_size() is None
    assert calls == 0
    with pytest.raises(RuntimeError, match="qdrant unavailable"):
        index.search(project_id="project-1", query="query")
    assert calls == 1


def test_memory_index_failed_initialization_can_retry_and_success_is_cached(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = _CountingQdrantClient()
    calls = 0

    def create_client(_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary qdrant failure")
        return client

    monkeypatch.setattr(memory_index_module, "_create_qdrant_client", create_client)
    index = QdrantChapterMemoryIndex(
        url="http://qdrant.test:6333",
        collection_name="retry_chapters",
        embedder=HashTextEmbedder(),
        qdrant_models=FakeQdrantModels,
    )

    with pytest.raises(RuntimeError, match="temporary qdrant failure"):
        index.search(project_id="project-1", query="first")

    assert index.search(project_id="project-1", query="second") == []
    assert index.search(project_id="project-1", query="third") == []
    assert calls == 2
    assert client.collection_list_calls == 1
    assert client.collection_create_calls == 1


def test_memory_index_initialization_is_thread_safe() -> None:
    client = _CountingQdrantClient(delay_seconds=0.02)
    index = QdrantChapterMemoryIndex(
        url="http://qdrant.test:6333",
        collection_name="threaded_chapters",
        embedder=HashTextEmbedder(),
        client=client,
        qdrant_models=FakeQdrantModels,
    )

    assert client.collection_list_calls == 0
    with ThreadPoolExecutor(max_workers=8) as executor:
        results = list(
            executor.map(
                lambda query: index.search(
                    project_id="project-1",
                    query=query,
                ),
                [f"query-{index}" for index in range(8)],
            )
        )

    assert results == [[]] * 8
    assert client.collection_list_calls == 1
    assert client.collection_create_calls == 1


def test_llm_kb_index_initializes_on_nonempty_search_and_retries_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    client = _CountingQdrantClient()
    calls = 0
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    client.close = close  # type: ignore[attr-defined]

    def create_client(_url: str):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("temporary llm-kb qdrant failure")
        return client

    monkeypatch.setattr(llm_kb_vector_module, "_create_qdrant_client", create_client)
    index = LLMKBVectorIndex(
        tmp_path,
        qdrant_url="http://qdrant.test:6333",
        collection_name="lazy_llm_kb",
        qdrant_models=FakeQdrantModels,
    )

    assert calls == 0
    assert index.search("project-1", "") == []
    assert calls == 0
    with pytest.raises(RuntimeError, match="temporary llm-kb qdrant failure"):
        index.search("project-1", "query")
    assert index.search("project-1", "query") == []
    assert index.search("project-1", "query again") == []
    assert calls == 2
    assert client.collection_list_calls == 1
    assert client.collection_create_calls == 1
    index.close()
    index.close()
    assert close_calls == 1


def test_llm_kb_rebuild_is_the_first_initializing_operation(tmp_path: Path) -> None:
    client = _CountingQdrantClient()
    index = LLMKBVectorIndex(
        tmp_path,
        qdrant_url="http://qdrant.test:6333",
        collection_name="rebuild_llm_kb",
        qdrant_client=client,
        qdrant_models=FakeQdrantModels,
    )

    assert client.collection_list_calls == 0
    result = index.rebuild_project("missing-project")

    assert result["section_count"] == 0
    assert client.collection_list_calls == 1
    assert client.collection_create_calls == 1


def test_llm_kb_existing_collection_dimension_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    client = FakeQdrantClient()
    client.create_collection(
        collection_name=f"llm_kb_wrong_dims_{memory_embedding_identity(HashTextEmbedder(dims=96), preprocessing=LLM_KB_PREPROCESSING)}",
        vectors_config=FakeQdrantModels.VectorParams(
            size=95,
            distance=FakeQdrantModels.Distance.COSINE,
        ),
    )
    index = LLMKBVectorIndex(
        tmp_path,
        collection_name="llm_kb_wrong_dims",
        qdrant_client=client,
        qdrant_models=FakeQdrantModels,
    )

    with pytest.raises(ValueError, match="vector size 95, expected 96"):
        index.search("project-1", "query")


def test_llm_kb_create_race_dimension_mismatch_fails_closed(
    tmp_path: Path,
) -> None:
    class RacingClient(FakeQdrantClient):
        def create_collection(self, *, collection_name: str, vectors_config) -> None:
            super().create_collection(
                collection_name=collection_name,
                vectors_config=FakeQdrantModels.VectorParams(
                    size=95,
                    distance=FakeQdrantModels.Distance.COSINE,
                ),
            )
            raise RuntimeError("collection already exists")

    index = LLMKBVectorIndex(
        tmp_path,
        collection_name="llm_kb_raced_wrong_dims",
        qdrant_client=RacingClient(),
        qdrant_models=FakeQdrantModels,
    )

    with pytest.raises(ValueError, match="vector size 95, expected 96"):
        index.search("project-1", "query")


def test_broker_resolves_each_injected_provider_once(tmp_path: Path) -> None:
    project_root = tmp_path / "project-1"
    project_root.mkdir()
    (project_root / "CURRENT_STATE.md").write_text("# Current State\n", encoding="utf-8")
    memory_index = SimpleNamespace(search=lambda **_kwargs: [])
    retriever = SimpleNamespace(
        search=lambda *_args, **_kwargs: [{"file_key": "CURRENT_STATE.md"}]
    )
    memory_provider_calls = 0
    retriever_provider_calls = 0

    def memory_provider():
        nonlocal memory_provider_calls
        memory_provider_calls += 1
        return memory_index

    def retriever_provider():
        nonlocal retriever_provider_calls
        retriever_provider_calls += 1
        return retriever

    broker = RetrievalBroker(
        llm_kb_root=tmp_path,
        memory_index_provider=memory_provider,
        llm_kb_retriever_provider=retriever_provider,
    )

    assert memory_provider_calls == 0
    assert retriever_provider_calls == 0
    broker._ensure_memory_index()
    broker._ensure_memory_index()
    from forwin.llm_kb import LLMKnowledgeBaseCompiler
    from forwin.models import Project
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.retrieval.source_identity import CanonReadBaseline
    from tests.postgres import postgres_test_url
    engine = get_engine(postgres_test_url("lazy-kb-provider"))
    init_db(engine)
    sessions = get_session_factory(engine)
    try:
        with sessions.begin() as session:
            session.add(Project(id="project-1", title="Valid lazy provider", premise="Source validation"))
            session.flush()
            LLMKnowledgeBaseCompiler(session, root=tmp_path, qdrant_client=FakeQdrantClient(), qdrant_models=FakeQdrantModels).rebuild("project-1")
        with sessions() as session:
            baseline = CanonReadBaseline.capture(session, "project-1", as_of_chapter=0)
            first = broker._load_llm_kb_context("project-1", pack_kind="writing", query="first", session=session, baseline=baseline)
            second = broker._load_llm_kb_context("project-1", pack_kind="writing", query="second", session=session, baseline=baseline)
        assert broker.memory_index is memory_index
        assert first["search_results"] == [{"file_key": "CURRENT_STATE.md"}]
        assert second["search_results"] == [{"file_key": "CURRENT_STATE.md"}]
        assert memory_provider_calls == 1
        assert retriever_provider_calls == 1
    finally:
        engine.dispose()


def test_broker_provider_failure_is_not_cached() -> None:
    calls = 0
    memory_index = SimpleNamespace(search=lambda **_kwargs: [])

    def provider():
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("provider unavailable")
        return memory_index

    broker = RetrievalBroker(memory_index_provider=provider)

    with pytest.raises(RuntimeError, match="provider unavailable"):
        broker._ensure_memory_index()
    broker._ensure_memory_index()

    assert broker.memory_index is memory_index
    assert calls == 2


def test_owned_lazy_indexes_close_without_resolving_unused_providers() -> None:
    calls: list[str] = []
    memory_index = SimpleNamespace(close=lambda: calls.append("memory.close"))
    retriever = SimpleNamespace(close=lambda: calls.append("retriever.close"))
    broker = RetrievalBroker(
        memory_index_provider=lambda: memory_index,
        llm_kb_retriever_provider=lambda: retriever,
    )

    broker.close()
    assert calls == []

    broker = RetrievalBroker(
        memory_index_provider=lambda: memory_index,
        llm_kb_retriever_provider=lambda: retriever,
    )
    broker._ensure_memory_index()
    broker._ensure_llm_kb_retriever()
    broker.close()

    assert calls == ["memory.close", "retriever.close"]
    with pytest.raises(RuntimeError, match="closed"):
        broker._ensure_memory_index()


def test_owned_qdrant_client_is_closed_once(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _CountingQdrantClient()
    close_calls = 0

    def close() -> None:
        nonlocal close_calls
        close_calls += 1

    client.close = close  # type: ignore[attr-defined]
    monkeypatch.setattr(
        memory_index_module,
        "_create_qdrant_client",
        lambda _url: client,
    )
    index = QdrantChapterMemoryIndex(
        url="http://qdrant.test:6333",
        collection_name="owned_client",
        embedder=HashTextEmbedder(),
        qdrant_models=FakeQdrantModels,
    )

    index.search(project_id="project-1", query="query")
    index.close()
    index.close()

    assert close_calls == 1
    with pytest.raises(RuntimeError, match="closed"):
        index.search(project_id="project-1", query="again")


def test_injected_embedder_is_not_closed_but_factory_embedder_is_owned(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class SharedEmbedder(HashTextEmbedder):
        def close(self) -> None:
            calls.append("shared.close")

    shared = SharedEmbedder()
    memory_index = QdrantChapterMemoryIndex(
        url="http://qdrant.test:6333",
        collection_name="shared_memory",
        embedder=shared,
        client=FakeQdrantClient(),
        qdrant_models=FakeQdrantModels,
    )
    llm_kb_index = LLMKBVectorIndex(
        tmp_path,
        embedder=shared,
        qdrant_client=FakeQdrantClient(),
        qdrant_models=FakeQdrantModels,
    )

    memory_index.close()
    llm_kb_index.close()
    assert calls == []

    monkeypatch.setattr(
        HashTextEmbedder,
        "close",
        lambda _self: calls.append("factory.close"),
    )
    factory_index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="factory_memory",
        qdrant_client=FakeQdrantClient(),
        qdrant_models=FakeQdrantModels,
        embedding_backend="hash",
    )
    factory_index.close()  # type: ignore[attr-defined]

    assert calls == ["factory.close"]


@pytest.mark.parametrize(
    ("module", "index_type"),
    [
        (obsidian_index_module, ObsidianHumanVectorIndex),
        (skill_index_module, SkillVectorIndex),
    ],
)
def test_world_studio_indexes_close_only_owned_qdrant_clients(
    monkeypatch: pytest.MonkeyPatch,
    module,
    index_type,
) -> None:
    calls: list[str] = []
    owned = FakeQdrantClient()
    owned.close = lambda: calls.append("owned")  # type: ignore[attr-defined]
    monkeypatch.setattr(module, "_create_qdrant_client", lambda _url: owned)

    owned_index = index_type(
        qdrant_url="http://qdrant.test:6333",
        qdrant_models=FakeQdrantModels,
    )
    owned_index.close()
    owned_index.close()

    injected = FakeQdrantClient()
    injected.close = lambda: calls.append("injected")  # type: ignore[attr-defined]
    injected_index = index_type(
        qdrant_client=injected,
        qdrant_models=FakeQdrantModels,
    )
    injected_index.close()

    assert calls == ["owned"]


def test_world_studio_search_closes_each_temporary_vector_index(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class TemporaryIndex:
        def __init__(self, *args, **kwargs) -> None:
            del args, kwargs
            self.name = self.__class__.__name__

        def search(self, *args, **kwargs):
            del args, kwargs
            return [{"index_kind": self.name, "score": 1.0}]

        def close(self) -> None:
            calls.append(self.name)

    class ObsidianIndex(TemporaryIndex):
        pass

    class LLMKBIndex(TemporaryIndex):
        pass

    class SkillIndex(TemporaryIndex):
        pass

    monkeypatch.setattr(world_search_module, "ObsidianHumanVectorIndex", ObsidianIndex)
    monkeypatch.setattr(world_search_module, "LLMKnowledgeBaseRetriever", LLMKBIndex)
    monkeypatch.setattr(world_search_module, "SkillVectorIndex", SkillIndex)

    result = WorldStudioSearchService(llm_kb_root=tmp_path).search(
        "project-1",
        query="query",
        index_kind="all",
    )

    assert len(result["results"]) == 3
    assert calls == ["ObsidianIndex", "LLMKBIndex", "SkillIndex"]


def test_obsidian_export_closes_temporary_human_index_on_rebuild_failure(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    calls: list[str] = []

    class Context:
        def __enter__(self):
            return self

        def __exit__(self, *_args) -> None:
            return None

    class Session(Context):
        def begin_nested(self):
            return Context()

        def commit(self) -> None:
            calls.append("commit")

    class Exporter:
        def __init__(self, _session) -> None:
            return None

        def export_project(self, _project_id: str, *, vault_root: Path | None):
            return SimpleNamespace(
                vault_root=str(vault_root or tmp_path),
                exported_count=1,
                as_of_chapter=1,
            )

    class Index:
        def __init__(self, **_kwargs) -> None:
            return None

        def rebuild_project(self, *_args, **_kwargs) -> None:
            calls.append("rebuild")
            raise RuntimeError("qdrant unavailable")

        def close(self) -> None:
            calls.append("close")

    monkeypatch.setattr(api_obsidian_routes, "require_project", lambda *_args: None)
    monkeypatch.setattr(api_obsidian_routes, "ObsidianExporter", Exporter)
    monkeypatch.setattr(api_obsidian_routes, "ObsidianHumanVectorIndex", Index)
    handler = api_obsidian_routes.build_handlers(
        get_session=Session,
    )["export_obsidian"]

    response = handler(
        "project-1",
        WorldModelExportRequest(vault_root=str(tmp_path)),
    )

    assert response.ok is True
    assert calls == ["commit", "rebuild", "close"]
