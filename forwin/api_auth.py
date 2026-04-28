from __future__ import annotations

import base64
import binascii
import re
import secrets
from collections.abc import Awaitable, Callable

from fastapi import Request, Response
from fastapi.responses import PlainTextResponse


_EXTENSION_KEY_AUTH_ROUTES = (
    ("GET", re.compile(r"^/api/publishers/upload-jobs/[^/]+$")),
    ("POST", re.compile(r"^/api/publishers/upload-jobs/[^/]+/result$")),
    ("POST", re.compile(r"^/api/publishers/comment-sync-jobs/[^/]+/result$")),
)


def basic_auth_enabled(config) -> bool:
    user = str(getattr(config, "http_basic_user", "") or "")
    password = str(getattr(config, "http_basic_password", "") or "")
    if bool(user) != bool(password):
        raise RuntimeError(
            "FORWIN_HTTP_BASIC_USER and FORWIN_HTTP_BASIC_PASSWORD must be set together"
        )
    return bool(user and password)


def _unauthorized() -> Response:
    return PlainTextResponse(
        "Authentication required",
        status_code=401,
        headers={"WWW-Authenticate": 'Basic realm="ForWin"'},
    )


def _path_is_exempt(path: str, exempt_prefixes: tuple[str, ...]) -> bool:
    for raw_prefix in exempt_prefixes:
        prefix = str(raw_prefix or "").strip()
        if not prefix:
            continue
        if path == prefix or (prefix.endswith("/") and path.startswith(prefix)):
            return True
    return False


def _valid_extension_key(config, request: Request) -> bool:
    method = request.method.upper()
    path = request.url.path
    if not any(
        route_method == method and pattern.fullmatch(path)
        for route_method, pattern in _EXTENSION_KEY_AUTH_ROUTES
    ):
        return False

    expected = str(getattr(config, "publisher_extension_api_key", "") or "")
    provided = str(request.headers.get("x-forwin-extension-key", "") or "")
    return bool(
        expected
        and provided
        and secrets.compare_digest(provided, expected)
    )


def make_basic_auth_middleware(config):
    user = str(getattr(config, "http_basic_user", "") or "")
    password = str(getattr(config, "http_basic_password", "") or "")
    exempt_prefixes = tuple(
        str(item)
        for item in getattr(config, "http_basic_exempt_paths", ()) or ()
    )

    async def middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if _path_is_exempt(path, exempt_prefixes) or _valid_extension_key(config, request):
            return await call_next(request)

        header = request.headers.get("authorization", "")
        if not header.lower().startswith("basic "):
            return _unauthorized()
        try:
            decoded = base64.b64decode(
                header.split(" ", 1)[1],
                validate=True,
            ).decode("utf-8")
            candidate_user, candidate_password = decoded.split(":", 1)
        except (binascii.Error, UnicodeDecodeError, ValueError):
            return _unauthorized()

        if not (
            secrets.compare_digest(candidate_user, user)
            and secrets.compare_digest(candidate_password, password)
        ):
            return _unauthorized()

        return await call_next(request)

    return middleware
