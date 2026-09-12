"""One identity and durable cache for prepared embedding inputs."""
from __future__ import annotations

import json
import math
from hashlib import sha256

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from forwin.models.embedding import EmbeddingCacheEntry

MEMORY_PREPROCESSING = "title-summary-body500-v1"
LLM_KB_PREPROCESSING = "llm-kb-section-text-v1"


def memory_embedding_identity(embedder, *, preprocessing=MEMORY_PREPROCESSING) -> str:
    # Fallback identity describes the actual implementation, not the failed gateway.
    actual = getattr(embedder, "_fallback", None) or embedder
    model = str(getattr(actual, "model", "") or "").strip()
    backend = str(getattr(actual, "kind", "") or "").strip()
    if not model or not backend or int(actual.dims) <= 0:
        raise ValueError("embedding model/backend identity unavailable")
    return sha256(json.dumps({
        "backend": backend,
        "endpoint": str(getattr(actual, "base_url", "")),
        "model": model, "dimensions": actual.dims,
        "preprocessing": preprocessing,
    }, sort_keys=True).encode()).hexdigest()


def validate_vector(vector, dimensions):
    if not isinstance(vector, list) or len(vector) != dimensions:
        raise ValueError("embedding vector dimension mismatch")
    if any(isinstance(value, bool) or not isinstance(value, (float, int)) or not math.isfinite(value) for value in vector):
        raise ValueError("embedding vector must contain finite numbers")
    return vector


def cached_embeddings(embedder, texts, *, preprocessing=MEMORY_PREPROCESSING, session_factory=None):
    embedder.prepare()
    identity = memory_embedding_identity(embedder, preprocessing=preprocessing)
    inputs = {sha256(text.encode()).hexdigest(): text for text in texts}
    vectors = {}
    if session_factory is not None and inputs:
        with session_factory() as session:
            rows = session.scalars(select(EmbeddingCacheEntry).where(
                EmbeddingCacheEntry.embedding_identity == identity,
                EmbeddingCacheEntry.input_hash.in_(inputs),
            ))
            for row in rows:
                if row.dimensions != embedder.dims:
                    raise ValueError("cached embedding dimension mismatch")
                vectors[row.input_hash] = validate_vector(json.loads(row.vector_json), embedder.dims)
    missing = [key for key in inputs if key not in vectors]
    if missing:
        generated = embedder.embed([inputs[key] for key in missing])
        if memory_embedding_identity(embedder, preprocessing=preprocessing) != identity:
            raise ValueError("embedding identity changed during request")
        if len(generated) != len(missing):
            raise ValueError("embedding response size mismatch")
        for key, vector in zip(missing, generated):
            vectors[key] = validate_vector(vector, embedder.dims)
        if session_factory is not None:
            # Commit before external upsert: failed point writes can restart without
            # embedding again. Concurrent cache writers share immutable keys.
            with session_factory() as session, session.begin():
                for key in missing:
                    try:
                        with session.begin_nested():
                            session.add(EmbeddingCacheEntry(input_hash=key, embedding_identity=identity,
                                dimensions=embedder.dims, vector_json=json.dumps(vectors[key], allow_nan=False)))
                            session.flush()
                    except IntegrityError:
                        existing = session.get(EmbeddingCacheEntry, (key, identity))
                        if existing is None:
                            raise
                        if existing.dimensions != embedder.dims:
                            raise ValueError("cached embedding dimension mismatch")
                        vectors[key] = validate_vector(json.loads(existing.vector_json), embedder.dims)
    return [vectors[sha256(text.encode()).hexdigest()] for text in texts]
