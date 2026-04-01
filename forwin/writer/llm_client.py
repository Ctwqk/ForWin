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
    ) -> None:
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model = model
        # Long timeout for generation: connect=10s, read=120s
        self.client = httpx.Client(timeout=httpx.Timeout(120.0, connect=10.0))

    def chat(
        self,
        messages: list[dict],
        temperature: float = 0.85,
        max_tokens: int = 16384,
        response_format: dict | None = None,
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
                response = self.client.post(url, json=payload, headers=headers)

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
                if attempt == 0:
                    logger.warning(
                        "Request timed out (%s). Waiting 10 s before retry.", exc
                    )
                    time.sleep(10)
                    continue
                raise

        # Should never reach here, but make the type-checker happy.
        raise RuntimeError("LLMClient.chat: unexpected exit from retry loop")

    def close(self) -> None:
        """Close the underlying httpx client."""
        self.client.close()

    def __enter__(self) -> "LLMClient":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()
