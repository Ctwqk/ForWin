from __future__ import annotations

import logging



logger = logging.getLogger(__name__)


class EmbeddingsMixin:
    def embed(
        self,
        inputs: list[str],
        *,
        model: str = "",
        dimensions: int | None = None,
    ) -> list[list[float]]:
        # Retrieval settings win; otherwise use the first configured registry entry,
        # independently of the chat pin and route order.
        profile = next((p for p in self.profiles.values() if p.api_key and p.base_url), None)
        base_url = self.embedding_base_url or (profile.base_url if profile else "")
        api_key = self.embedding_api_key if self.embedding_base_url else (profile.api_key if profile else "")
        embedding_model = model or self.embedding_model or (profile.model if profile else "")
        if not base_url or not embedding_model:
            raise RuntimeError("no configured embedding endpoint or model")
        payload: dict[str, object] = {
            "model": embedding_model,
            "input": inputs,
        }
        if dimensions is not None:
            payload["dimensions"] = dimensions
        headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        }
        url = f"{base_url.rstrip('/')}/embeddings"
        response = self.client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        rows = data.get("data") or []
        return [list(item.get("embedding") or []) for item in rows]


__all__ = [
    'EmbeddingsMixin',
]
