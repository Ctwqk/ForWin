from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx


class HubRequestError(Exception):
    """publish-hub refused the request (4xx): `code` is its error code, `status` the HTTP status.

    Retrying the same request will not help. A 409 can still be an answer to act on, for example
    publication_already_published or idempotency_conflict.
    """

    def __init__(self, status: int, code: str, message: str = "") -> None:
        self.status = int(status)
        self.code = str(code)
        self.message = str(message)
        super().__init__(
            f"publish-hub answered {self.status} {self.code}"
            + (f": {self.message}" if self.message else "")
        )


class HubUnavailable(Exception):
    """publish-hub could not be asked (network error, timeout, 5xx) or its answer was unusable.

    The request may or may not have been applied, so the caller keeps what it was sending and
    tries again later; an idempotency key makes the repeat safe.
    """


# 408 and 429 are 4xx, but a proxy in front of the hub sends them to mean "try again later".
_RETRYABLE_CLIENT_ERRORS = frozenset({408, 429})
_MESSAGE_LIMIT = 300


class PublishHubClient:
    """Producer-side client of publish-hub (spec section 7.1).

    `submit` sends exactly the dict it is given. The bytes depend only on the dict's contents,
    never on its key order, so a body stored as jsonb and sent again goes out identically.
    """

    def __init__(
        self,
        url: str,
        key: str,
        timeout: float,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        base_url = str(url or "").strip().rstrip("/")
        api_key = str(key or "").strip()
        if not base_url:
            raise ValueError("publish-hub URL is required")
        if not api_key:
            raise ValueError("publish-hub API key is required")
        if float(timeout) <= 0:
            raise ValueError("publish-hub timeout must be positive")
        self._base_url = base_url
        self._http = httpx.Client(
            base_url=base_url,
            headers={"Authorization": f"Bearer {api_key}"},
            timeout=float(timeout),
            transport=transport,
            follow_redirects=False,
            # The hub is an internal address (a tunnel on magi3); the key must not go to a proxy.
            trust_env=False,
        )

    def __repr__(self) -> str:
        return f"PublishHubClient(url={self._base_url!r})"

    def close(self) -> None:
        self._http.close()

    def __enter__(self) -> PublishHubClient:
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def submit(self, body: Mapping[str, Any]) -> tuple[dict[str, Any], int]:
        """POST /v1/jobs. Returns the job and 201 (new) or 200 (same key and same body)."""
        content = json.dumps(
            body, ensure_ascii=False, separators=(",", ":"), sort_keys=True, allow_nan=False
        ).encode("utf-8")
        response = self._request(
            "POST",
            "/v1/jobs",
            content=content,
            headers={"Content-Type": "application/json"},
        )
        if response.status_code not in (200, 201):
            raise HubUnavailable(
                f"publish-hub answered an unexpected {response.status_code} to a job request"
            )
        return self._object(response), response.status_code

    def get_job(self, job_id: str) -> dict[str, Any]:
        response = self._request("GET", f"/v1/jobs/{quote(str(job_id), safe='')}")
        return self._object(response)

    def find_by_key(self, key: str) -> dict[str, Any] | None:
        """The producer's job for an idempotency key, or None when the hub has none."""
        response = self._request("GET", "/v1/jobs", params={"idempotency_key": key})
        items = self._object(response).get("items")
        if not isinstance(items, list):
            raise HubUnavailable("publish-hub answered a key lookup without an item list")
        return items[0] if items else None

    def changes(self, after: int, limit: int = 200) -> tuple[list[dict[str, Any]], int]:
        """GET /v1/changes: the items after the cursor and the cursor to continue from."""
        response = self._request(
            "GET", "/v1/changes", params={"after": int(after), "limit": int(limit)}
        )
        data = self._object(response)
        items, next_after = data.get("items"), data.get("next_after")
        if not isinstance(items, list) or not isinstance(next_after, int):
            raise HubUnavailable("publish-hub answered /v1/changes in an unexpected shape")
        return items, next_after

    def destinations(self) -> list[dict[str, Any]]:
        response = self._request("GET", "/v1/destinations")
        data = self._json(response)
        if not isinstance(data, list):
            raise HubUnavailable("publish-hub answered /v1/destinations in an unexpected shape")
        return data

    def _request(self, method: str, path: str, **kwargs: Any) -> httpx.Response:
        try:
            response = self._http.request(method, path, **kwargs)
        except httpx.HTTPError as exc:
            raise HubUnavailable(
                f"publish-hub request failed: {type(exc).__name__}: {exc}"
            ) from exc
        status = response.status_code
        if 200 <= status < 300:
            return response
        if 400 <= status < 500 and status not in _RETRYABLE_CLIENT_ERRORS:
            code, message = _error_of(response)
            raise HubRequestError(status, code, message)
        raise HubUnavailable(f"publish-hub answered {status}")

    @staticmethod
    def _json(response: httpx.Response) -> Any:
        try:
            return response.json()
        except ValueError as exc:
            raise HubUnavailable("publish-hub answered with a body that is not JSON") from exc

    def _object(self, response: httpx.Response) -> dict[str, Any]:
        data = self._json(response)
        if not isinstance(data, dict):
            raise HubUnavailable("publish-hub answered with a body that is not a JSON object")
        return data


def _error_of(response: httpx.Response) -> tuple[str, str]:
    """The (code, message) of publish-hub's {"error": {"code", "message"}} envelope."""
    try:
        error = response.json()["error"]
        code, message = str(error["code"]), str(error.get("message") or "")
    except (ValueError, KeyError, TypeError, AttributeError):
        code = f"http_{response.status_code}"
        message = response.text
    return code, message[:_MESSAGE_LIMIT]


__all__ = ["HubRequestError", "HubUnavailable", "PublishHubClient"]
