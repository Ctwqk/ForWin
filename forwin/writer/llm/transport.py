from __future__ import annotations

import hashlib
import json
import logging
import queue
import threading
import time
import uuid
from email.utils import parsedate_to_datetime
from urllib.parse import urlparse

import httpx

from forwin.config import DEFAULT_MINIMAX_BASE_URL, DEFAULT_MINIMAX_MODEL
from forwin.model_adapter import ModelCapabilities

logger = logging.getLogger(__name__)
_RETRYABLE_HTTP_STATUS_CODES = {408, 409, 425, 429, 500, 502, 503, 504, 529}
_LLM_ROUTE_POLICY_VERSION = "v3.8-stage-aware-hard-replacement"
_ATTEMPT_RECORDED_ATTR = "_forwin_llm_attempt_recorded"


class TransportMixin:
    def _build_http_client(self) -> httpx.Client:
        return httpx.Client(
            timeout=httpx.Timeout(self.timeout_seconds, connect=min(10.0, self.timeout_seconds))
        )

    def _post_with_wall_timeout(
        self,
        url: str,
        *,
        json: dict[str, object],
        headers: dict[str, str],
        timeout: httpx.Timeout,
    ) -> httpx.Response:
        result_queue: queue.Queue[tuple[str, object]] = queue.Queue(maxsize=1)
        with self._client_lock:
            client = self.client

        def _worker() -> None:
            try:
                result_queue.put(
                    (
                        "response",
                        client.post(url, json=json, headers=headers, timeout=timeout),
                    )
                )
            except BaseException as exc:  # noqa: BLE001
                result_queue.put(("exception", exc))

        wall_timeout = max(0.1, self._timeout_seconds_value(timeout))
        thread = threading.Thread(
            target=_worker,
            name="forwin-llm-http-post",
            daemon=True,
        )
        thread.start()
        thread.join(wall_timeout)
        if thread.is_alive():
            self._replace_timed_out_client(client)
            raise httpx.ReadTimeout(
                f"LLM HTTP request exceeded wall timeout ({wall_timeout:.1f}s)"
            )
        kind, value = result_queue.get_nowait()
        if kind == "exception":
            raise value  # type: ignore[misc]
        return value  # type: ignore[return-value]

    def _replace_timed_out_client(self, timed_out_client: object) -> None:
        close = getattr(timed_out_client, "close", None)
        with self._client_lock:
            if self.client is timed_out_client:
                try:
                    if callable(close):
                        close()
                finally:
                    self.client = self._build_http_client()
                return
        if callable(close):
            close()

    def close(self) -> None:
        """Close the underlying httpx client."""
        with self._client_lock:
            self.client.close()

    def __enter__(self) -> "OpenAICompatibleAdapter":
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


__all__ = [
    'TransportMixin',
]
