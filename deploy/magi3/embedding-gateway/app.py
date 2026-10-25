"""Embedding gateway backed by sentence-transformers (copied from Constructure for magi3, CPU by default)."""

from __future__ import annotations

import asyncio
from collections import OrderedDict
import logging
import os
from typing import Optional

from fastapi import FastAPI
from pydantic import BaseModel, Field
from sentence_transformers import SentenceTransformer

try:
    import torch
except Exception:  # pragma: no cover - torch import is environment-specific
    torch = None

logging.basicConfig(
    level=getattr(logging, os.getenv("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s %(levelname)s %(message)s",
)
log = logging.getLogger("embedding-gateway")

MODEL_NAME = os.getenv("EMBED_MODEL", "all-MiniLM-L6-v2")
REQUESTED_DEVICE = os.getenv("EMBED_DEVICE", "cpu")
CACHE_SIZE = int(os.getenv("EMBED_CACHE_SIZE", "16384"))


def _select_device(requested: str) -> str:
    req = (requested or "auto").strip().lower()
    cuda_available = bool(torch and torch.cuda.is_available())
    if req == "cuda":
        if cuda_available:
            return "cuda"
        log.warning("EMBED_DEVICE=cuda requested but CUDA unavailable; falling back to CPU")
        return "cpu"
    if req == "cpu":
        return "cpu"
    return "cuda" if cuda_available else "cpu"


class EmbedRequest(BaseModel):
    texts: list[str] = Field(default_factory=list)


class EmbedResponse(BaseModel):
    model: str
    dimension: int
    vectors: list[list[float]]


class _GatewayModel:
    def __init__(self, model_name: str, requested_device: str, cache_size: int) -> None:
        self._model_name = model_name
        self._device = _select_device(requested_device)
        self._cache_size = max(0, cache_size)
        self._cache: OrderedDict[str, list[float]] = OrderedDict()
        log.info("Loading embedding model %s on %s", self._model_name, self._device)
        self._model = SentenceTransformer(self._model_name, device=self._device)
        self._dimension = len(self.embed_one("dimension probe"))
        log.info("Embedding gateway ready (model=%s dim=%d device=%s)", self._model_name, self._dimension, self._device)

    @property
    def model_name(self) -> str:
        return self._model_name

    @property
    def dimension(self) -> int:
        return self._dimension

    @property
    def device(self) -> str:
        return self._device

    def _cache_get(self, text: str) -> Optional[list[float]]:
        if self._cache_size <= 0:
            return None
        vector = self._cache.get(text)
        if vector is None:
            return None
        self._cache.move_to_end(text)
        return vector

    def _cache_set(self, text: str, vector: list[float]) -> None:
        if self._cache_size <= 0:
            return
        self._cache[text] = vector
        self._cache.move_to_end(text)
        while len(self._cache) > self._cache_size:
            self._cache.popitem(last=False)

    def embed(self, texts: list[str]) -> list[list[float]]:
        if not texts:
            return []

        vectors: list[Optional[list[float]]] = [None] * len(texts)
        missing_texts: list[str] = []
        missing_indices: dict[str, list[int]] = {}

        for idx, text in enumerate(texts):
            cached = self._cache_get(text)
            if cached is not None:
                vectors[idx] = cached
                continue
            if text not in missing_indices:
                missing_indices[text] = []
                missing_texts.append(text)
            missing_indices[text].append(idx)

        if missing_texts:
            encoded = self._model.encode(
                missing_texts,
                convert_to_numpy=True,
                normalize_embeddings=True,
            )
            encoded_list = encoded.tolist()
            for text, vector in zip(missing_texts, encoded_list):
                self._cache_set(text, vector)
                for idx in missing_indices[text]:
                    vectors[idx] = vector

        out: list[list[float]] = []
        for vector in vectors:
            if vector is None:
                raise RuntimeError("embedding pipeline produced an empty vector slot")
            out.append(vector)
        return out

    def embed_one(self, text: str) -> list[float]:
        return self.embed([text])[0]


gateway = _GatewayModel(MODEL_NAME, REQUESTED_DEVICE, CACHE_SIZE)
app = FastAPI(title="Embedding Gateway")


@app.get("/health")
async def health() -> dict:
    return {
        "status": "ok",
        "model": gateway.model_name,
        "dimension": gateway.dimension,
        "device": gateway.device,
    }


@app.get("/metadata")
async def metadata() -> dict:
    return {
        "model": gateway.model_name,
        "dimension": gateway.dimension,
        "device": gateway.device,
    }


@app.post("/embed", response_model=EmbedResponse)
async def embed(req: EmbedRequest) -> EmbedResponse:
    vectors = await asyncio.to_thread(gateway.embed, req.texts)
    return EmbedResponse(model=gateway.model_name, dimension=gateway.dimension, vectors=vectors)
