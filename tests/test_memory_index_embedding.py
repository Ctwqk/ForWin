from __future__ import annotations

import json
import time

import httpx
import pytest

from forwin.retrieval.memory_index import GatewayTextEmbedder, create_memory_index, HashTextEmbedder, memory_embedding_identity
from tests.qdrant import FakeQdrantClient, FakeQdrantModels


def test_gateway_embedder_reads_metadata_and_embeds_without_api_key() -> None:
    requests: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        if request.url.path == "/metadata":
            return httpx.Response(
                200,
                json={
                    "status": "ok",
                    "model": "all-MiniLM-L6-v2",
                    "dimension": 3,
                },
            )
        if request.url.path == "/embed":
            payload = json.loads(request.content.decode("utf-8"))
            assert payload == {"texts": ["末班车", "旧仓库"]}
            return httpx.Response(
                200,
                json={
                    "model": "all-MiniLM-L6-v2",
                    "dimension": 3,
                    "vectors": [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]],
                },
            )
        return httpx.Response(404)

    embedder = GatewayTextEmbedder(
        base_url="http://embedding-gateway.test",
        dims=0,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert requests == []
    assert embedder.dims == 0
    assert embedder.embed(["末班车", "旧仓库"]) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert embedder.dims == 3
    assert [request.url.path for request in requests] == ["/metadata", "/embed"]
    assert not requests[-1].headers.get("authorization")


@pytest.mark.parametrize(
    ("embedding_backend", "embedding_base_url", "embedding_model", "message"),
    [
        ("unknown", "", "", "Unsupported embedding backend"),
        ("gateway", "", "", "base URL"),
        ("remote", "", "model", "base URL"),
        ("remote", "http://embedding.test", "", "model"),
    ],
)
def test_create_memory_index_rejects_invalid_embedding_configuration(
    embedding_backend: str,
    embedding_base_url: str,
    embedding_model: str,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        create_memory_index(
            backend="qdrant",
            qdrant_url="http://qdrant.test:6333",
            qdrant_collection="chapter_memories_invalid_embedding",
            qdrant_client=FakeQdrantClient(),
            qdrant_models=FakeQdrantModels,
            embedding_backend=embedding_backend,
            embedding_base_url=embedding_base_url,
            embedding_model=embedding_model,
            embedding_required=True,
        )


def test_create_memory_index_supports_gateway_embedder_without_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metadata":
            return httpx.Response(200, json={"dimension": 3, "model": "test"})
        if request.url.path == "/embed":
            payload = json.loads(request.content.decode("utf-8"))
            vectors = [[1.0, 0.0, 0.0] for _ in payload["texts"]]
            return httpx.Response(200, json={"dimension": 3, "model": "test", "vectors": vectors})
        return httpx.Response(404)

    qdrant_client = FakeQdrantClient()
    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_gateway",
        qdrant_client=qdrant_client,
        qdrant_models=FakeQdrantModels,
        embedding_backend="gateway",
        embedding_base_url="http://embedding-gateway.test",
        embedding_dims=0,
        embedding_http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert isinstance(index.embedder, GatewayTextEmbedder)
    assert qdrant_client.collections == {}
    index.upsert_chapter(
        project_id="p1",
        chapter_number=1,
        title="title",
        summary="summary",
        body="body",
    )
    assert index.embedder.dims == 3
    assert (
        qdrant_client.collections[index.collection_name]["vectors_config"].size
        == 3
    )


def test_create_memory_index_accepts_collection_created_by_a_competing_role() -> None:
    class RacingQdrantClient(FakeQdrantClient):
        def create_collection(self, *, collection_name: str, vectors_config) -> None:
            super().create_collection(
                collection_name=collection_name,
                vectors_config=vectors_config,
            )
            raise RuntimeError("collection already exists")

    qdrant_client = RacingQdrantClient()

    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_race",
        qdrant_client=qdrant_client,
        qdrant_models=FakeQdrantModels,
        embedding_backend="hash",
        embedding_dims=64,
    )

    index.search(project_id="p1", query="query")
    assert index.collection_name.startswith("chapter_memories_race_")
    assert qdrant_client.collections[index.collection_name]["vectors_config"].size == 64


def test_create_memory_index_retries_transient_inspection_after_competing_role(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class EventuallyVisibleRacingClient(FakeQdrantClient):
        inspection_attempts = 0

        def create_collection(self, *, collection_name: str, vectors_config) -> None:
            super().create_collection(
                collection_name=collection_name,
                vectors_config=vectors_config,
            )
            raise RuntimeError("collection already exists")

        def get_collection(self, collection_name: str):
            self.inspection_attempts += 1
            if self.inspection_attempts == 1:
                raise RuntimeError("collection metadata is not visible yet")
            return super().get_collection(collection_name)

    monkeypatch.setattr(time, "sleep", lambda _seconds: None)
    qdrant_client = EventuallyVisibleRacingClient()

    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_eventual_race",
        qdrant_client=qdrant_client,
        qdrant_models=FakeQdrantModels,
        embedding_backend="hash",
        embedding_dims=64,
    )

    index.search(project_id="p1", query="query")
    assert index.collection_name.startswith("chapter_memories_eventual_race_")
    assert qdrant_client.inspection_attempts == 2


def test_existing_collection_inspection_failure_propagates_and_retries() -> None:
    class FlakyInspectionClient(FakeQdrantClient):
        inspection_attempts = 0

        def get_collection(self, collection_name: str):
            self.inspection_attempts += 1
            if self.inspection_attempts == 1:
                raise RuntimeError("collection inspection unavailable")
            return super().get_collection(collection_name)

    qdrant_client = FlakyInspectionClient()
    qdrant_client.create_collection(
        collection_name=f"chapter_memories_existing_{memory_embedding_identity(HashTextEmbedder(dims=64))}",
        vectors_config=FakeQdrantModels.VectorParams(
            size=64,
            distance=FakeQdrantModels.Distance.COSINE,
        ),
    )
    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_existing",
        qdrant_client=qdrant_client,
        qdrant_models=FakeQdrantModels,
        embedding_backend="hash",
        embedding_dims=64,
    )

    with pytest.raises(RuntimeError, match="inspection unavailable"):
        index.search(project_id="p1", query="first")
    assert index.search(project_id="p1", query="second") == []
    assert qdrant_client.inspection_attempts == 2


def test_existing_collection_with_unknown_vector_shape_fails_closed() -> None:
    qdrant_client = FakeQdrantClient()
    qdrant_client.collections[f"chapter_memories_named_{memory_embedding_identity(HashTextEmbedder(dims=64))}"] = {
        "vectors_config": {
            "title": FakeQdrantModels.VectorParams(
                size=64,
                distance=FakeQdrantModels.Distance.COSINE,
            ),
            "body": FakeQdrantModels.VectorParams(
                size=64,
                distance=FakeQdrantModels.Distance.COSINE,
            ),
        },
        "points": {},
    }
    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_named",
        qdrant_client=qdrant_client,
        qdrant_models=FakeQdrantModels,
        embedding_backend="hash",
        embedding_dims=64,
    )

    with pytest.raises(ValueError, match="determine vector size"):
        index.search(project_id="p1", query="query")


def test_create_memory_index_rejects_raced_collection_with_wrong_dimension() -> None:
    class WrongDimensionRacingClient(FakeQdrantClient):
        def create_collection(self, *, collection_name: str, vectors_config) -> None:
            super().create_collection(
                collection_name=collection_name,
                vectors_config=FakeQdrantModels.VectorParams(
                    size=vectors_config.size + 1,
                    distance=vectors_config.distance,
                ),
            )
            raise RuntimeError("collection already exists")

    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_wrong_race",
        qdrant_client=WrongDimensionRacingClient(),
        qdrant_models=FakeQdrantModels,
        embedding_backend="hash",
        embedding_dims=64,
    )

    with pytest.raises(ValueError, match="vector size 65, expected 64"):
        index.search(project_id="p1", query="query")


def test_create_memory_index_required_gateway_raises_when_unavailable() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "down"})

    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_gateway_required",
        qdrant_client=FakeQdrantClient(),
        qdrant_models=FakeQdrantModels,
        embedding_backend="gateway",
        embedding_base_url="http://embedding-gateway.test",
        embedding_dims=64,
        embedding_required=True,
        embedding_http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    with pytest.raises(RuntimeError, match="Embedding gateway required"):
        index.search(project_id="p1", query="query")


def test_create_memory_index_optional_gateway_reports_hash_degradation() -> None:
    def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(503, json={"status": "down"})

    requests = 0

    def counted_handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return handler(request)

    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_gateway_degraded",
        qdrant_client=FakeQdrantClient(),
        qdrant_models=FakeQdrantModels,
        embedding_backend="gateway",
        embedding_base_url="http://embedding-gateway.test",
        embedding_dims=64,
        embedding_required=False,
        embedding_http_client=httpx.Client(transport=httpx.MockTransport(counted_handler)),
    )

    assert isinstance(index.embedder, GatewayTextEmbedder)
    assert requests == 0
    assert index.embedding_status()["kind"] == "gateway"
    index.search(project_id="p1", query="query")
    assert index.embedding_status()["kind"] == "hash"
    assert index.embedding_status()["degraded"] is True
    assert index.embedding_status()["degraded_from"] == "gateway"


def test_existing_collection_dimension_mismatch_uses_side_by_side_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metadata":
            return httpx.Response(200, json={"dimension": 3, "model": "test"})
        if request.url.path == "/embed":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"dimension": 3, "model": "test", "vectors": [[1.0, 0.0, 0.0] for _ in payload["texts"]]},
            )
        return httpx.Response(404)

    qdrant_client = FakeQdrantClient()
    qdrant_client.create_collection(
        collection_name="chapter_memories",
        vectors_config=FakeQdrantModels.VectorParams(
            size=64,
            distance=FakeQdrantModels.Distance.COSINE,
        ),
    )

    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories",
        qdrant_client=qdrant_client,
        qdrant_models=FakeQdrantModels,
        embedding_backend="gateway",
        embedding_base_url="http://embedding-gateway.test",
        embedding_dims=0,
        embedding_http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )

    assert "chapter_memories_3d" not in qdrant_client.collections
    index.search(project_id="p1", query="query")
    assert index.collection_name.startswith("chapter_memories_")
    assert index.collection_name != "chapter_memories_3d"
    assert "chapter_memories" in qdrant_client.collections
    assert qdrant_client.collections["chapter_memories"]["vectors_config"].size == 64
    assert qdrant_client.collections[index.collection_name]["vectors_config"].size == 3


def test_gateway_memory_index_search_orders_by_semantic_vector_similarity() -> None:
    def vector_for(text: str) -> list[float]:
        return [0.0, 1.0] if "仓库" in text else [1.0, 0.0]

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metadata":
            return httpx.Response(200, json={"dimension": 2, "model": "test"})
        if request.url.path == "/embed":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={
                    "dimension": 2,
                    "model": "test",
                    "vectors": [vector_for(text) for text in payload["texts"]],
                },
            )
        return httpx.Response(404)

    index = create_memory_index(
        backend="qdrant",
        qdrant_url="http://qdrant.test:6333",
        qdrant_collection="chapter_memories_gateway_order",
        qdrant_client=FakeQdrantClient(),
        qdrant_models=FakeQdrantModels,
        embedding_backend="gateway",
        embedding_base_url="http://embedding-gateway.test",
        embedding_dims=0,
        embedding_http_client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    index.upsert_chapter(
        project_id="p1",
        chapter_number=1,
        title="幽灵列车",
        summary="失踪末班车回归",
        body="韩砚在雨夜看到那辆本不该出现的末班车。",
    )
    index.upsert_chapter(
        project_id="p1",
        chapter_number=2,
        title="废弃仓库",
        summary="旧仓库线索",
        body="他们在仓库里翻找旧档案。",
    )

    hits = index.search(project_id="p1", query="仓库线索", limit=2)

    assert [hit.chapter_number for hit in hits] == [2, 1]


@pytest.mark.parametrize("bad_model", ["other-model", ""])
def test_gateway_response_identity_must_match_prepared_model(bad_model):
    def handler(request):
        if request.url.path == "/metadata":
            return httpx.Response(200, json={"model": "prepared", "dimension": 8})
        return httpx.Response(200, json={"model": bad_model, "dimension": 8, "vectors": [[1.0] * 8]})
    embedder = GatewayTextEmbedder(base_url="http://gateway.test", dims=0, client=httpx.Client(transport=httpx.MockTransport(handler)))
    with pytest.raises(ValueError, match="identity"):
        embedder.embed(["text"])


def test_same_dimension_models_use_separate_memory_spaces():
    from forwin.retrieval.memory_index import HashTextEmbedder, QdrantChapterMemoryIndex
    client = FakeQdrantClient()
    first = HashTextEmbedder(dims=8)
    second = HashTextEmbedder(dims=8)
    second.model = "other-model"
    indexes = [QdrantChapterMemoryIndex(url=":memory:", collection_name="identity", embedder=e, client=client, qdrant_models=FakeQdrantModels) for e in [first, second]]
    indexes[0].upsert_chapter(project_id="p", chapter_number=1, title="one", summary="", body="body")
    assert indexes[1].search(project_id="p", query="body") == []
    assert indexes[0].collection_name != indexes[1].collection_name
    assert len(indexes[0].search(project_id="p", query="body")) == 1


def test_memory_cache_survives_failed_upsert_restart_and_suffix_identity():
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.retrieval.memory_index import HashTextEmbedder, QdrantChapterMemoryIndex
    from tests.postgres import postgres_test_url
    engine = get_engine(postgres_test_url("embedding-cache"))
    init_db(engine)
    sessions = get_session_factory(engine)
    calls = []
    class CountingEmbedder(HashTextEmbedder):
        def embed(self, texts):
            calls.extend(texts)
            return super().embed(texts)
    class FailingUpsert(FakeQdrantClient):
        def upsert(self, **kwargs):
            raise RuntimeError("interrupted before Qdrant write")
    try:
        def index(client, model="sha1-token-bigram-v1"):
            embedder = CountingEmbedder(dims=8)
            embedder.model = model
            result = QdrantChapterMemoryIndex(url=":memory:", collection_name="cache", embedder=embedder, client=client, qdrant_models=FakeQdrantModels)
            result.session_factory = sessions
            return result
        kw = dict(project_id="p", chapter_number=1, title="one", summary="summary", body="unchanged input", canon_commit_id="old")
        with pytest.raises(RuntimeError, match="interrupted"):
            index(FailingUpsert()).upsert_chapter(**kw)
        index(FakeQdrantClient()).upsert_chapter(**{**kw, "canon_commit_id": "suffix-new"})
        assert len(calls) == 1
        index(FakeQdrantClient(), "other-same-dimension").upsert_chapter(**kw)
        assert len(calls) == 2
    finally:
        engine.dispose()


@pytest.mark.parametrize("vector", [[1.0] * 7, [float("nan")] * 8, [float("inf")] * 8, [True] * 8])
def test_durable_cache_rejects_invalid_vector_before_writing(vector):
    from forwin.retrieval.embedding_cache import cached_embeddings
    class InvalidEmbedder(HashTextEmbedder):
        def embed(self, texts):
            return [vector for _ in texts]
    with pytest.raises(ValueError, match="dimension|finite"):
        cached_embeddings(InvalidEmbedder(dims=8), ["input"])


def test_unknown_embedding_identity_cannot_select_space_or_cache():
    from forwin.retrieval.memory_index import QdrantChapterMemoryIndex
    embedder = HashTextEmbedder(dims=8)
    embedder.model = ""
    client = FakeQdrantClient()
    index = QdrantChapterMemoryIndex(url=":memory:", collection_name="unknown", embedder=embedder, client=client, qdrant_models=FakeQdrantModels)
    with pytest.raises(ValueError, match="identity unavailable"):
        index.search(project_id="p", query="text")
    assert client.collections == {}


def test_cache_key_uses_actual_input_and_preprocessing_and_validates_stored_vectors():
    from sqlalchemy import select
    from forwin.models.embedding import EmbeddingCacheEntry
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.retrieval.embedding_cache import cached_embeddings, LLM_KB_PREPROCESSING
    from tests.postgres import postgres_test_url
    engine = get_engine(postgres_test_url("cache-validation"))
    init_db(engine)
    sessions = get_session_factory(engine)
    calls = []
    class CountingEmbedder(HashTextEmbedder):
        def embed(self, texts):
            calls.extend(texts)
            return super().embed(texts)
    embedder = CountingEmbedder(dims=8)
    try:
        cached_embeddings(embedder, ["input", "input"], session_factory=sessions)
        cached_embeddings(embedder, ["input"], session_factory=sessions)
        assert calls == ["input"]
        cached_embeddings(embedder, ["input"], preprocessing=LLM_KB_PREPROCESSING, session_factory=sessions)
        cached_embeddings(embedder, ["changed"], session_factory=sessions)
        assert calls == ["input", "input", "changed"]
        with sessions.begin() as session:
            row = session.scalars(select(EmbeddingCacheEntry).where(EmbeddingCacheEntry.embedding_identity == memory_embedding_identity(embedder))).first()
            row.vector_json = "[NaN,0,0,0,0,0,0,0]"
        with pytest.raises(ValueError, match="finite"):
            cached_embeddings(embedder, ["input", "changed"], session_factory=sessions)
        assert len(calls) == 3
    finally:
        engine.dispose()


@pytest.mark.parametrize("response_model", ["other-same-dimension", None, ""])
def test_remote_response_model_rejected_before_cache_or_point_write(response_model):
    from sqlalchemy import select
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.embedding import EmbeddingCacheEntry
    from forwin.retrieval.memory_index import RemoteTextEmbedder, QdrantChapterMemoryIndex
    from tests.postgres import postgres_test_url

    def handler(_request):
        payload = {"data": [{"embedding": [1.0] * 8}]}
        if response_model is not None:
            payload["model"] = response_model
        return httpx.Response(200, json=payload)

    engine = get_engine(postgres_test_url("remote-response-identity"))
    init_db(engine)
    sessions = get_session_factory(engine)
    client = FakeQdrantClient()
    embedder = RemoteTextEmbedder(
        base_url="http://remote.test", model="prepared-model", dims=8,
        client=httpx.Client(transport=httpx.MockTransport(handler)),
    )
    index = QdrantChapterMemoryIndex(
        url=":memory:", collection_name="remote-identity", embedder=embedder,
        client=client, qdrant_models=FakeQdrantModels, session_factory=sessions,
    )
    try:
        with pytest.raises(ValueError, match="response model identity"):
            index.upsert_chapter(project_id="p", chapter_number=1, title="Title", summary="Summary", body="Body")
        assert embedder.model == "prepared-model"
        with sessions() as session:
            assert session.scalars(select(EmbeddingCacheEntry)).all() == []
        assert client.upsert_calls == 0
        assert client.collections[index.collection_name]["points"] == {}
    finally:
        engine.dispose()


def test_remote_matching_response_model_persists_and_reuses_cached_vector():
    from sqlalchemy import select
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.embedding import EmbeddingCacheEntry
    from forwin.retrieval.memory_index import RemoteTextEmbedder, QdrantChapterMemoryIndex
    from tests.postgres import postgres_test_url

    requests = []
    vector = [0.0, 1.0] + [0.0] * 6

    def handler(request):
        requests.append(json.loads(request.content))
        return httpx.Response(200, json={"model": "prepared-model", "data": [{"embedding": vector}]})

    engine = get_engine(postgres_test_url("remote-matching-model"))
    init_db(engine)
    sessions = get_session_factory(engine)
    client = FakeQdrantClient()
    try:
        for commit in ("first", "new-source-same-input"):
            embedder = RemoteTextEmbedder(
                base_url="http://remote.test", model="prepared-model", dims=8,
                client=httpx.Client(transport=httpx.MockTransport(handler)),
            )
            index = QdrantChapterMemoryIndex(
                url=":memory:", collection_name="remote-matching", embedder=embedder,
                client=client, qdrant_models=FakeQdrantModels, session_factory=sessions,
            )
            index.upsert_chapter(project_id="p", chapter_number=1, title="Title", summary="Summary", body="Body", canon_commit_id=commit)
        assert requests == [{"model": "prepared-model", "input": ["Title\nSummary\nBody"], "dimensions": 8}]
        with sessions() as session:
            cached = session.scalars(select(EmbeddingCacheEntry)).one()
            assert json.loads(cached.vector_json) == vector
            assert cached.embedding_identity == memory_embedding_identity(embedder)
        assert len(client.collections[index.collection_name]["points"]) == 2
        assert all(point.vector == vector for point in client.collections[index.collection_name]["points"].values())
    finally:
        engine.dispose()


def test_postgres_simultaneous_cache_insert_conflict_returns_winning_vector():
    from concurrent.futures import ThreadPoolExecutor
    from threading import Barrier
    from sqlalchemy import event, select
    from forwin.models.base import get_engine, get_session_factory, init_db
    from forwin.models.embedding import EmbeddingCacheEntry
    from forwin.retrieval.embedding_cache import cached_embeddings
    from tests.postgres import postgres_test_url

    engine = get_engine(postgres_test_url("cache-insert-conflict"))
    init_db(engine)
    sessions = get_session_factory(engine)
    both_writers = Barrier(2, timeout=10)
    connections = []
    conflicts = []
    provider_vectors = []

    def synchronize_insert(connection, _cursor, statement, _parameters, _context, _many):
        if statement.startswith("INSERT INTO embedding_cache_entries"):
            connections.append(id(connection.connection.driver_connection))
            both_writers.wait()

    def record_conflict(context):
        if getattr(context.original_exception, "sqlstate", None) == "23505":
            conflicts.append(context.statement)

    class DistinctResponseEmbedder(HashTextEmbedder):
        def __init__(self, value):
            super().__init__(dims=8)
            self.vector = [value] + [0.0] * 7

        def embed(self, texts):
            provider_vectors.append(self.vector)
            return [self.vector for _ in texts]

    event.listen(engine, "before_cursor_execute", synchronize_insert)
    event.listen(engine, "handle_error", record_conflict)
    try:
        def consume(value):
            return cached_embeddings(DistinctResponseEmbedder(value), ["same input"], session_factory=sessions)[0]

        with ThreadPoolExecutor(max_workers=2) as executor:
            results = list(executor.map(consume, [1.0, 2.0]))
        with sessions() as session:
            row = session.scalars(select(EmbeddingCacheEntry)).one()
            stored = json.loads(row.vector_json)
        assert len(set(connections)) == 2
        assert len(conflicts) == 1
        assert "embedding_cache_entries" in conflicts[0]
        assert len(provider_vectors) == 2
        assert stored in provider_vectors
        assert results == [stored, stored]
    finally:
        event.remove(engine, "before_cursor_execute", synchronize_insert)
        event.remove(engine, "handle_error", record_conflict)
        engine.dispose()
