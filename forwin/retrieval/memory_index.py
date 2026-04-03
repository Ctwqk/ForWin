from __future__ import annotations

import json
import logging
import math
import re
import sqlite3
from contextlib import closing
from hashlib import sha1
from pathlib import Path
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


class LocalChapterMemoryIndex(ChapterMemoryIndex):
    def __init__(
        self,
        root_dir: str = "data/retrieval",
        *,
        embedder: TextEmbedder | None = None,
    ) -> None:
        self.root_dir = Path(root_dir)
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self.legacy_index_path = self.root_dir / "chapter_memories.json"
        self.index_dir = self.root_dir / "chapter_memories"
        self.index_dir.mkdir(parents=True, exist_ok=True)
        self.db_path = self.root_dir / "chapter_memories.sqlite3"
        self.embedder = embedder or HashTextEmbedder()
        self._fts_enabled = False
        self._bootstrap_db()

    def _bootstrap_db(self) -> None:
        with closing(self._connect()) as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS chapter_memories (
                    project_id TEXT NOT NULL,
                    chapter_number INTEGER NOT NULL,
                    title TEXT NOT NULL DEFAULT '',
                    summary TEXT NOT NULL DEFAULT '',
                    excerpt TEXT NOT NULL DEFAULT '',
                    embedding_json TEXT NOT NULL DEFAULT '[]',
                    PRIMARY KEY(project_id, chapter_number)
                )
                """
            )
            conn.execute(
                """
                CREATE INDEX IF NOT EXISTS ix_chapter_memories_project_chapter
                ON chapter_memories(project_id, chapter_number DESC)
                """
            )
            try:
                conn.execute(
                    """
                    CREATE VIRTUAL TABLE IF NOT EXISTS chapter_memories_fts
                    USING fts5(
                        project_id UNINDEXED,
                        chapter_number UNINDEXED,
                        title,
                        summary,
                        excerpt
                    )
                    """
                )
                self._fts_enabled = True
            except sqlite3.OperationalError:
                logger.warning(
                    "SQLite FTS5 unavailable for local memory index; falling back to SQL scan.",
                    exc_info=True,
                )
                self._fts_enabled = False

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path)
        conn.row_factory = sqlite3.Row
        return conn

    def _project_path(self, project_id: str) -> Path:
        safe_project_id = re.sub(r"[^A-Za-z0-9_.-]", "_", project_id or "default")
        return self.index_dir / f"{safe_project_id}.json"

    def _load_legacy_rows(self, project_id: str) -> list[dict]:
        rows: list[dict] = []
        project_path = self._project_path(project_id)
        if project_path.exists():
            try:
                return json.loads(project_path.read_text(encoding="utf-8")) or []
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to load project local chapter memory index.", exc_info=True)
        if self.legacy_index_path.exists():
            try:
                rows = json.loads(self.legacy_index_path.read_text(encoding="utf-8")) or []
            except (json.JSONDecodeError, OSError):
                logger.warning("Failed to load legacy local chapter memory index.", exc_info=True)
                return []
        return [row for row in rows if row.get("project_id") == project_id]

    def _ensure_project_imported(self, project_id: str) -> None:
        with closing(self._connect()) as conn:
            count = conn.execute(
                "SELECT COUNT(1) FROM chapter_memories WHERE project_id = ?",
                (project_id,),
            ).fetchone()[0]
            if count:
                return
        rows = self._load_legacy_rows(project_id)
        if not rows:
            return
        with closing(self._connect()) as conn:
            for row in rows:
                chapter_number = int(row.get("chapter_number") or 0)
                title = str(row.get("title") or "")
                summary = str(row.get("summary") or "")
                excerpt = str(row.get("excerpt") or "")
                embedding_json = json.dumps(row.get("embedding") or [], ensure_ascii=False)
                conn.execute(
                    """
                    INSERT OR REPLACE INTO chapter_memories(
                        project_id, chapter_number, title, summary, excerpt, embedding_json
                    ) VALUES (?, ?, ?, ?, ?, ?)
                    """,
                    (project_id, chapter_number, title, summary, excerpt, embedding_json),
                )
                if self._fts_enabled:
                    conn.execute(
                        """
                        DELETE FROM chapter_memories_fts
                        WHERE project_id = ? AND chapter_number = ?
                        """,
                        (project_id, chapter_number),
                    )
                    conn.execute(
                        """
                        INSERT INTO chapter_memories_fts(
                            project_id, chapter_number, title, summary, excerpt
                        ) VALUES (?, ?, ?, ?, ?)
                        """,
                        (project_id, chapter_number, title, summary, excerpt),
                    )
            conn.commit()

    def _fts_query(self, query: str) -> str:
        tokens = []
        for token in _tokenize(query):
            if len(token) == 1 and not ("\u4e00" <= token <= "\u9fff"):
                continue
            if token not in tokens:
                tokens.append(token)
            if len(tokens) >= 8:
                break
        if not tokens:
            return ""
        return " OR ".join(f'"{token}"' for token in tokens)

    @staticmethod
    def _decode_embedding(raw: str) -> list[float]:
        try:
            payload = json.loads(raw or "[]") or []
        except (json.JSONDecodeError, TypeError):
            return []
        return [float(item) for item in payload]

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
        embedding = self.embedder.embed([f"{title}\n{summary}\n{excerpt}"])[0]
        embedding_json = json.dumps(embedding, ensure_ascii=False)
        with closing(self._connect()) as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO chapter_memories(
                    project_id, chapter_number, title, summary, excerpt, embedding_json
                ) VALUES (?, ?, ?, ?, ?, ?)
                """,
                (project_id, chapter_number, title, summary, excerpt, embedding_json),
            )
            if self._fts_enabled:
                conn.execute(
                    """
                    DELETE FROM chapter_memories_fts
                    WHERE project_id = ? AND chapter_number = ?
                    """,
                    (project_id, chapter_number),
                )
                conn.execute(
                    """
                    INSERT INTO chapter_memories_fts(
                        project_id, chapter_number, title, summary, excerpt
                    ) VALUES (?, ?, ?, ?, ?)
                    """,
                    (project_id, chapter_number, title, summary, excerpt),
                )
            conn.commit()

    def search(
        self,
        *,
        project_id: str,
        query: str,
        limit: int = 3,
    ) -> list[MemorySnippet]:
        self._ensure_project_imported(project_id)
        query_embedding = self.embedder.embed([query])[0]
        candidate_limit = max(limit * 8, 12)
        rows: list[sqlite3.Row] = []
        with closing(self._connect()) as conn:
            fts_query = self._fts_query(query)
            if self._fts_enabled and fts_query:
                try:
                    rows = list(
                        conn.execute(
                            """
                            SELECT m.chapter_number, m.title, m.summary, m.excerpt, m.embedding_json
                            FROM chapter_memories_fts f
                            JOIN chapter_memories m
                              ON m.project_id = f.project_id
                             AND m.chapter_number = f.chapter_number
                            WHERE f.project_id = ?
                              AND chapter_memories_fts MATCH ?
                            ORDER BY bm25(chapter_memories_fts)
                            LIMIT ?
                            """,
                            (project_id, fts_query, candidate_limit),
                        ).fetchall()
                    )
                except sqlite3.OperationalError:
                    rows = []
            if not rows:
                rows = list(
                    conn.execute(
                        """
                        SELECT chapter_number, title, summary, excerpt, embedding_json
                        FROM chapter_memories
                        WHERE project_id = ?
                        ORDER BY chapter_number DESC
                        LIMIT ?
                        """,
                        (project_id, candidate_limit),
                    ).fetchall()
                )
        ranked: list[MemorySnippet] = []
        for row in rows:
            score = _cosine_similarity(
                query_embedding,
                self._decode_embedding(str(row["embedding_json"] or "[]")),
            )
            if score <= 0:
                continue
            ranked.append(
                MemorySnippet(
                    chapter_number=int(row["chapter_number"] or 0),
                    title=str(row["title"] or ""),
                    summary=str(row["summary"] or ""),
                    excerpt=str(row["excerpt"] or ""),
                    score=score,
                )
            )
        ranked.sort(key=lambda item: (-item.score, -item.chapter_number))
        return ranked[:limit]


class QdrantChapterMemoryIndex(ChapterMemoryIndex):
    def __init__(
        self,
        *,
        url: str,
        collection_name: str,
        embedder: TextEmbedder | None = None,
    ) -> None:
        try:
            from qdrant_client import QdrantClient
            from qdrant_client.http import models as rest
        except ImportError as exc:  # pragma: no cover
            raise RuntimeError("qdrant-client is not installed") from exc

        self._rest = rest
        self.client = QdrantClient(url=url)
        self.collection_name = collection_name
        self.embedder = embedder or HashTextEmbedder()
        collections = {item.name for item in self.client.get_collections().collections}
        if collection_name not in collections:
            self.client.create_collection(
                collection_name=collection_name,
                vectors_config=rest.VectorParams(
                    size=self.embedder.dims,
                    distance=rest.Distance.COSINE,
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
        hits = response.points
        return [
            MemorySnippet(
                chapter_number=int(hit.payload.get("chapter_number") or 0),
                title=str(hit.payload.get("title") or ""),
                summary=str(hit.payload.get("summary") or ""),
                excerpt=str(hit.payload.get("excerpt") or ""),
                score=float(hit.score or 0.0),
            )
            for hit in hits
        ]


def create_memory_index(
    *,
    backend: str = "local",
    root_dir: str = "data/retrieval",
    qdrant_url: str = "",
    qdrant_collection: str = "chapter_memories",
    embedding_backend: str = "hash",
    embedding_base_url: str = "",
    embedding_api_key: str = "",
    embedding_model: str = "",
    embedding_dims: int = 64,
) -> ChapterMemoryIndex:
    normalized = (backend or "local").strip().lower()
    embedder: TextEmbedder
    embedding_kind = (embedding_backend or "hash").strip().lower()
    if (
        embedding_kind in {"remote", "api", "openai"}
        and embedding_model
        and embedding_api_key
        and embedding_base_url
    ):
        try:
            embedder = RemoteTextEmbedder(
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
    if normalized == "qdrant" and qdrant_url:
        try:
            return QdrantChapterMemoryIndex(
                url=qdrant_url,
                collection_name=qdrant_collection,
                embedder=embedder,
            )
        except Exception:
            logger.warning(
                "Qdrant memory index unavailable, falling back to local index.",
                exc_info=True,
            )
    return LocalChapterMemoryIndex(root_dir=root_dir, embedder=embedder)
