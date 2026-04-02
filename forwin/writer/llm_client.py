from __future__ import annotations

import logging
import time

import httpx

logger = logging.getLogger(__name__)


class LLMClient:
    """Synchronous wrapper for OpenAI-compatible LLM APIs."""

    def __init__(
        self,
        api_key: str,
        base_url: str = "https://api.minimaxi.com/v1",
        model: str = "MiniMax-M2.7",
        timeout_seconds: float = 90.0,
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        self.timeout_seconds = max(10.0, float(timeout_seconds))
        self.client = httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds))
        )

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.85,
        max_tokens: int = 16384,
        response_format: dict | None = None,
        timeout_seconds: float | None = None,
        retry_on_timeout: bool = True,
    ) -> str:
        """Send a chat completion request and return the content string.

        Retry policy:
        - 429 (rate limit): wait 5 s, retry once.
        - ReadTimeout / TimeoutException: wait 10 s, retry once.
        - Any other HTTP error: raise immediately.
        """
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }
        if response_format:
            payload["response_format"] = response_format
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/chat/completions"

        request_timeout = httpx.Timeout(
            max(5.0, float(timeout_seconds if timeout_seconds is not None else self.timeout_seconds)),
            connect=min(
                10.0,
                max(5.0, float(timeout_seconds if timeout_seconds is not None else self.timeout_seconds)),
            ),
        )

        for attempt in range(2):  # attempt 0 = first try, attempt 1 = single retry
            try:
                started_at = time.perf_counter()
                logger.debug(
                    "LLMClient.chat attempt=%d model=%s messages=%d max_tokens=%d",
                    attempt,
                    self.model,
                    len(messages),
                    max_tokens,
                )
                response = self.client.post(
                    url,
                    json=payload,
                    headers=headers,
                    timeout=request_timeout,
                )

                if response.status_code == 429:
                    if attempt == 0:
                        logger.warning(
                            "Rate limited (429). Waiting 5 s before retry."
                        )
                        time.sleep(5)
                        continue
                    else:
                        response.raise_for_status()

                response.raise_for_status()

                data = response.json()
                content: str = data["choices"][0]["message"]["content"]
                logger.debug(
                    "LLMClient.chat success: %d chars returned in %.2fs",
                    len(content),
                    time.perf_counter() - started_at,
                )
                return content

            except (httpx.ReadTimeout, httpx.TimeoutException) as exc:
                if retry_on_timeout and attempt == 0:
                    logger.warning(
                        "Request timed out (%s). Waiting 10 s before retry.", exc
                    )
                    time.sleep(10)
                    continue
                raise

        # Should never reach here, but make the type-checker happy.
        raise RuntimeError("LLMClient.chat: unexpected exit from retry loop")

    def embed(
        self,
        inputs: list[str],
        *,
        model: str,
        dimensions: int | None = None,
    ) -> list[list[float]]:
        payload: dict[str, object] = {
            "model": model,
            "input": inputs,
        }
        if dimensions is not None:
            payload["dimensions"] = dimensions
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        url = f"{self.base_url}/embeddings"
        response = self.client.post(url, json=payload, headers=headers)
        response.raise_for_status()
        data = response.json()
        rows = data.get("data") or []
        return [list(item.get("embedding") or []) for item in rows]

    def close(self) -> None:
        """Close the underlying httpx client."""
        self.client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
