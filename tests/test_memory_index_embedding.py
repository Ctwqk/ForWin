from __future__ import annotations

import json

import httpx

from forwin.retrieval.memory_index import GatewayTextEmbedder, create_memory_index
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

    assert embedder.dims == 3
    assert embedder.embed(["末班车", "旧仓库"]) == [[1.0, 0.0, 0.0], [0.0, 1.0, 0.0]]
    assert [request.url.path for request in requests] == ["/metadata", "/embed"]
    assert not requests[-1].headers.get("authorization")


def test_create_memory_index_supports_gateway_embedder_without_api_key() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metadata":
            return httpx.Response(200, json={"dimension": 3, "model": "test"})
        if request.url.path == "/embed":
            payload = json.loads(request.content.decode("utf-8"))
            vectors = [[1.0, 0.0, 0.0] for _ in payload["texts"]]
            return httpx.Response(200, json={"dimension": 3, "vectors": vectors})
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
    assert index.embedder.dims == 3
    assert (
        qdrant_client.collections["chapter_memories_gateway"]["vectors_config"].size
        == 3
    )


def test_existing_collection_dimension_mismatch_uses_side_by_side_collection() -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path == "/metadata":
            return httpx.Response(200, json={"dimension": 3, "model": "test"})
        if request.url.path == "/embed":
            payload = json.loads(request.content.decode("utf-8"))
            return httpx.Response(
                200,
                json={"dimension": 3, "vectors": [[1.0, 0.0, 0.0] for _ in payload["texts"]]},
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

    assert index.collection_name == "chapter_memories_3d"
    assert "chapter_memories" in qdrant_client.collections
    assert qdrant_client.collections["chapter_memories"]["vectors_config"].size == 64
    assert qdrant_client.collections["chapter_memories_3d"]["vectors_config"].size == 3


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
