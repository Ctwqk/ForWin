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
    kind = "hash"

    def __init__(
        self,
        dims: int = 64,
        *,
        degraded_from: str = "",
        degradation_reason: str = "",
    ) -> None:
        self.dims = max(8, int(dims))
        self.degraded_from = str(degraded_from or "")
        self.degradation_reason = str(degradation_reason or "")

    def embed(self, texts: list[str]) -> list[list[float]]:
        return [_embed_text(text, dims=self.dims) for text in texts]


class RemoteTextEmbedder(TextEmbedder):
    kind = "remote"

    def __init__(
        self,
        *,
        api_key: str = "",
        base_url: str,
        model: str,
        dims: int,
        client: httpx.Client | None = None,
    ) -> None:
        self.api_key = str(api_key or "")
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.dims = max(8, int(dims))
        self.client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))

    def embed(self, texts: list[str]) -> list[list[float]]:
        headers = {"Content-Type": "application/json"}
        if self.api_key:
            headers["Authorization"] = f"Bearer {self.api_key}"
        response = self.client.post(
            f"{self.base_url}/embeddings",
            json={
                "model": self.model,
                "input": texts,
                "dimensions": self.dims,
            },
            headers=headers,
        )
        response.raise_for_status()
        data = response.json()
        rows = data.get("data") or []
        embeddings = [list(item.get("embedding") or []) for item in rows]
        if len(embeddings) != len(texts):
            raise ValueError("embedding response size mismatch")
        return embeddings


class GatewayTextEmbedder(TextEmbedder):
    kind = "gateway"

    def __init__(
        self,
        *,
        base_url: str,
        dims: int = 384,
        client: httpx.Client | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.client = client or httpx.Client(timeout=httpx.Timeout(30.0, connect=10.0))
        detected_dims = self._detect_dims()
        requested_dims = int(dims or 0)
        if detected_dims <= 0:
            raise ValueError("embedding gateway metadata unavailable")
        self.dims = requested_dims if requested_dims > 0 else detected_dims
        if self.dims <= 0:
            raise ValueError("embedding gateway dimension could not be detected")
        if detected_dims > 0 and detected_dims != self.dims:
            logger.warning(
                "Embedding gateway dimension %s differs from configured dimension %s.",
                detected_dims,
                self.dims,
            )

    def _detect_dims(self) -> int:
        try:
            response = self.client.get(f"{self.base_url}/metadata")
            response.raise_for_status()
            payload = response.json()
            return int(payload.get("dimension") or 0)
        except Exception:
            logger.warning("Embedding gateway metadata unavailable.", exc_info=True)
            return 0

    def embed(self, texts: list[str]) -> list[list[float]]:
        response = self.client.post(
            f"{self.base_url}/embed",
            json={"texts": texts},
            headers={"Content-Type": "application/json"},
        )
        response.raise_for_status()
        payload = response.json()
        vectors = [list(vector or []) for vector in payload.get("vectors") or []]
        if len(vectors) != len(texts):
            raise ValueError("embedding gateway response size mismatch")
        mismatched = [len(vector) for vector in vectors if len(vector) != self.dims]
        if mismatched:
            raise ValueError(
                f"embedding gateway dimension mismatch: expected {self.dims}, got {mismatched[0]}"
            )
        return vectors


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


def _vector_size_from_config(vectors_config: Any) -> int | None:
    if hasattr(vectors_config, "size"):
        try:
            return int(vectors_config.size)
        except (TypeError, ValueError):
            return None
    if isinstance(vectors_config, dict) and len(vectors_config) == 1:
        return _vector_size_from_config(next(iter(vectors_config.values())))
    return None


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
        self.embedder = embedder or HashTextEmbedder()
        self.collection_name = self._resolve_collection_name(collection_name)
        self._ensure_collection()

    def _ensure_collection(self) -> None:
        collections = {item.name for item in self.client.get_collections().collections}
        if self.collection_name in collections:
            return
        try:
            self.client.create_collection(
                collection_name=self.collection_name,
                vectors_config=self._rest.VectorParams(
                    size=self.embedder.dims,
                    distance=self._rest.Distance.COSINE,
                ),
            )
        except Exception as exc:
            collections = {item.name for item in self.client.get_collections().collections}
            if self.collection_name not in collections:
                raise
            existing_size = self._collection_vector_size(self.collection_name)
            if existing_size != self.embedder.dims:
                raise ValueError(
                    f"Qdrant collection {self.collection_name!r} has vector size "
                    f"{existing_size}, expected {self.embedder.dims}."
                ) from exc
            logger.info(
                "Qdrant collection %s was created by another process.",
                self.collection_name,
            )

    def _resolve_collection_name(self, collection_name: str) -> str:
        collections = {item.name for item in self.client.get_collections().collections}
        if collection_name not in collections:
            return collection_name
        existing_size = self._collection_vector_size(collection_name)
        if existing_size in {None, self.embedder.dims}:
            return collection_name
        candidate = f"{collection_name}_{self.embedder.dims}d"
        logger.warning(
            "Qdrant collection %s has vector size %s, expected %s; using %s instead.",
            collection_name,
            existing_size,
            self.embedder.dims,
            candidate,
        )
        if candidate not in collections:
            return candidate
        candidate_size = self._collection_vector_size(candidate)
        if candidate_size in {None, self.embedder.dims}:
            return candidate
        raise ValueError(
            f"Qdrant collection {candidate!r} has vector size {candidate_size}, "
            f"expected {self.embedder.dims}."
        )

    def _collection_vector_size(self, collection_name: str) -> int | None:
        try:
            collection = self.client.get_collection(collection_name)
        except Exception:
            logger.warning(
                "Could not inspect Qdrant collection %s vector size.",
                collection_name,
                exc_info=True,
            )
            return None
        vectors_config = getattr(
            getattr(getattr(collection, "config", None), "params", None),
            "vectors",
            None,
        )
        return _vector_size_from_config(vectors_config)

    def collection_vector_size(self) -> int | None:
        return self._collection_vector_size(self.collection_name)

    def embedding_status(self) -> dict[str, object]:
        return embedding_status(self.embedder)

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
    qdrant_url: str = "",
    qdrant_collection: str = "chapter_memories",
    embedding_backend: str = "hash",
    embedding_base_url: str = "",
    embedding_api_key: str = "",
    embedding_model: str = "",
    embedding_dims: int = 64,
    embedding_required: bool = False,
    embedding_http_client: httpx.Client | None = None,
    qdrant_client: Any | None = None,
    qdrant_models: Any | None = None,
) -> ChapterMemoryIndex:
    normalized = (backend or "qdrant").strip().lower()
    embedding_kind = (embedding_backend or "hash").strip().lower()
    if embedding_kind in {"gateway", "embedding_gateway", "local_gateway"} and embedding_base_url:
        try:
            embedder: TextEmbedder = GatewayTextEmbedder(
                base_url=embedding_base_url,
                dims=embedding_dims,
                client=embedding_http_client,
            )
        except Exception as exc:
            logger.error(
                "Embedding gateway unavailable.",
                exc_info=True,
            )
            if embedding_required:
                raise RuntimeError("Embedding gateway required but unavailable") from exc
            embedder = HashTextEmbedder(
                dims=embedding_dims,
                degraded_from="gateway",
                degradation_reason=str(exc),
            )
    elif (
        embedding_kind in {"remote", "api", "openai"}
        and embedding_model
        and embedding_base_url
    ):
        try:
            embedder = RemoteTextEmbedder(
                api_key=embedding_api_key,
                base_url=embedding_base_url,
                model=embedding_model,
                dims=embedding_dims,
                client=embedding_http_client,
            )
        except Exception as exc:
            logger.error(
                "Remote embedder unavailable.",
                exc_info=True,
            )
            if embedding_required:
                raise RuntimeError("Remote embedder required but unavailable") from exc
            embedder = HashTextEmbedder(
                dims=embedding_dims,
                degraded_from="remote",
                degradation_reason=str(exc),
            )
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


def embedding_status(embedder: TextEmbedder) -> dict[str, object]:
    kind = str(getattr(embedder, "kind", "") or embedder.__class__.__name__).lower()
    degraded_from = str(getattr(embedder, "degraded_from", "") or "")
    return {
        "kind": kind,
        "dims": int(getattr(embedder, "dims", 0) or 0),
        "degraded": bool(degraded_from),
        "degraded_from": degraded_from,
        "degradation_reason": str(getattr(embedder, "degradation_reason", "") or ""),
    }
