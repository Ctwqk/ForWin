from __future__ import annotations

import logging
import math
import re
from hashlib import sha1
from typing import Any
from uuid import UUID

import httpx

from forwin.protocol.context import MemorySnippet

logger = logging.getLogger(__name__)

_WORD_RE = re.compile(r"[A-Za-z0-9_]+|[\u4e00-\u9fff]")


class TextEmbedder:
    dims: int

    def embed(self, texts: list[str]) -> list[list[float]]:
        raise NotImplementedError


class HashTextEmbedder(TextEmbedder):
    def __init__(self, dims: int = 64) -> None:
        self.dims = max(8, int(dims))

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_embed_text(text, dims=self.dims) for text in texts]


class RemoteTextEmbedder(TextEmbedder):
    def __init__(
        self,
        *,
        api_key: str,
        base_url: str,
        model: str,
        dims: int,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dims = max(8, int(dims))
        self.client = httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.post(
            f"{self.base_url}/embeddings",
            json={
                "model": self.model,
                "input": texts,
                "dimensions": self.dims,
            },
            headers={
                "Authorization": f"Bearer {self.api_key}",
                "Content-Type": "application/json",
            },
        )
        response.raise_for_status()
        data = response.json()
        rows = data.get("data") or []
        embeddings = [list(item.get("embedding") or []) for item in rows]
        if len(embeddings) != len(texts):
            raise ValueError("embedding response size mismatch")
        return embeddings


def _tokenize(text: str) -> list[str]:
    base_tokens = _WORD_RE.findall(text or "")
    compact = "".join(ch for ch in text if "\u4e00" <= ch <= "\u9fff")
    bigrams = [compact[index:index + 2] for index in range(len(compact) - 1)]
    return [token.lower() for token in base_tokens + bigrams if token.strip()]


def _embed_text(text: str, dims: int = 64) -> list[float]:
    vector = [0.0] * dims
    for token in _tokenize(text):
        digest = sha1(token.encode("utf-8")).digest()
        slot = int.from_bytes(digest[:2], "big") % dims
        weight = 1.0 + (digest[2] / 255.0)
        vector[slot] += weight
    norm = math.sqrt(sum(value * value for value in vector))
    if norm == 0:
        return vector
    return [value / norm for value in vector]


def _cosine_similarity(left: list[float], right: list[float]) -> float:
    return sum(a * b for a, b in zip(left, right))


def _point_id(project_id: str, chapter_number: int) -> str:
    digest = sha1(f"{project_id}:{chapter_number}".encode("utf-8")).hexdigest()[:32]
    return str(UUID(digest))


def _create_qdrant_client(url: str) -> Any:
    try:
        from qdrant_client import QdrantClient
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("qdrant-client is not installed") from exc

    if url == ":memory:":
        return QdrantClient(location=":memory:")
    if "://" not in url:
        return QdrantClient(path=url)
    return QdrantClient(url=url)


def _qdrant_models() -> Any:
    try:
        from qdrant_client.http import models as rest
    except ImportError as exc:  # pragma: no cover
        raise RuntimeError("qdrant-client is not installed") from exc
    return rest


class ChapterMemoryIndex:
    def upsert_chapter(
        self,
        *,
        project_id: str,
        chapter_number: int,
        title: str,
        summary: str,
        body: str,
    ) -> None:
        raise NotImplementedError

    def search(
        self,
        *,
        project_id: str,
        query: str,
        limit: int = 3,
    ) -> list[MemorySnippet]:
        raise NotImplementedError


class QdrantChapterMemoryIndex(ChapterMemoryIndex):
    def __init__(
        self,
        *,
        url: str,
        collection_name: str,
        embedder: TextEmbedder | None = None,
        client: Any | None = None,
        qdrant_models: Any | None = None,
    ) -> None:
        self._rest = qdrant_models or _qdrant_models()
        self.client = client or _create_qdrant_client(url)
        self.collection_name = collection_name
        self.embedder = embedder or HashTextEmbedder()
        collections = {item.name for item in self.client.get_collections().collections}
        if collection_name not in collections:
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=self._rest.VectorParams(
                    size=self.embedder.dims,
                    distance=self._rest.Distance.COSINE,
                ),
            )

    def upsert_chapter(
        self,
        *,
        project_id: str,
        chapter_number: int,
        title: str,
        summary: str,
        body: str,
    ) -> None:
        excerpt = (body or "")[:500]
        vector = self.embedder.embed([f"{title}\n{summary}\n{excerpt}"])[0]
        self.client.upsert(
            collection_name=self.collection_name,
            points=[
                self._rest.PointStruct(
                    id=_point_id(project_id, chapter_number),
                    vector=vector,
                    payload={
                        "project_id": project_id,
                        "chapter_number": chapter_number,
                        "title": title,
                        "summary": summary,
                        "excerpt": excerpt,
                    },
                )
            ],
        )

    def search(
        self,
        *,
        project_id: str,
        query: str,
        limit: int = 3,
    ) -> list[MemorySnippet]:
        vector = self.embedder.embed([query])[0]
        response = self.client.query_points(
            collection_name=self.collection_name,
            query=vector,
            query_filter=self._rest.Filter(
                must=[
                    self._rest.FieldCondition(
                        key="project_id",
                        match=self._rest.MatchValue(value=project_id),
                    )
                ]
            ),
            limit=limit,
        )
        return [
            MemorySnippet(
                chapter_number=int(hit.payload.get("chapter_number") or 0),
                title=str(hit.payload.get("title") or ""),
                summary=str(hit.payload.get("summary") or ""),
                excerpt=str(hit.payload.get("excerpt") or ""),
                score=float(hit.score or 0.0),
            )
            for hit in response.points
        ]


def create_memory_index(
    *,
    backend: str = "qdrant",
    root_dir: str = "data/retrieval",  # noqa: ARG001 - kept for config compatibility.
    database_url: str | None = None,  # noqa: ARG001 - retained for legacy call compatibility.
    qdrant_url: str = "",
    qdrant_collection: str = "chapter_memories",
    embedding_backend: str = "hash",
    embedding_base_url: str = "",
    embedding_api_key: str = "",
    embedding_model: str = "",
    embedding_dims: int = 64,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> ChapterMemoryIndex:
    normalized = (backend or "qdrant").strip().lower()
    embedding_kind = (embedding_backend or "hash").strip().lower()
    if (
        embedding_kind in {"remote", "api", "openai"}
        and embedding_model
        and embedding_api_key
        and embedding_base_url
    ):
        try:
            embedder: TextEmbedder = RemoteTextEmbedder(
                api_key=embedding_api_key,
                base_url=embedding_base_url,
                model=embedding_model,
                dims=embedding_dims,
            )
        except Exception:
            logger.warning(
                "Remote embedder unavailable, falling back to hash embedder.",
                exc_info=True,
            )
            embedder = HashTextEmbedder(dims=embedding_dims)
    else:
        embedder = HashTextEmbedder(dims=embedding_dims)
    if normalized != "qdrant":
        raise ValueError(f"Unsupported retrieval backend: {backend}. Use qdrant.")
    if not qdrant_url:
        raise ValueError("FORWIN_QDRANT_URL is required when retrieval backend is qdrant.")
    return QdrantChapterMemoryIndex(
        url=qdrant_url,
        collection_name=qdrant_collection,
        embedder=embedder,
        client=qdrant_client,
        qdrant_models=qdrant_models,
    )
