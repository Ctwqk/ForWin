from __future__ import annotations

import base64
import binascii
import ipaddress
import re
import secrets
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

from fastapi import HTTPException, Request, Response
from fastapi.responses import PlainTextResponse


_EXTENSION_KEY_AUTH_ROUTES = (
    ("POST", re.compile(r"^/api/publishers/comment-sync-jobs/[^/]+/result$")),
)
_OPERATOR_PRINCIPAL_STATE_KEY = "forwin_operator_principal"


@dataclass(frozen=True)
class OperatorPrincipal:
    actor_id: str
    auth_method: str


def trusted_operator_proxy_enabled(config) -> bool:
    header = str(getattr(config, "http_trusted_operator_header", "") or "").strip()
    proxies = tuple(
        str(value or "").strip()
        for value in getattr(config, "http_trusted_operator_proxies", ()) or ()
        if str(value or "").strip()
    )
    return bool(header and proxies)


def operator_auth_configured(config) -> bool:
    return basic_auth_enabled(config) or trusted_operator_proxy_enabled(config)


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
    extension_namespace = path.startswith("/api/publishers/extension/")
    if not extension_namespace and not any(
        route_method == method and pattern.fullmatch(path)
        for route_method, pattern in _EXTENSION_KEY_AUTH_ROUTES
    ):
        return False

    expected = str(getattr(config, "publisher_extension_api_key", "") or "")
    provided = str(request.headers.get("x-forwin-extension-key", "") or "")
    return bool(expected and provided and secrets.compare_digest(provided, expected))


def make_basic_auth_middleware(config):
    user = str(getattr(config, "http_basic_user", "") or "")
    password = str(getattr(config, "http_basic_password", "") or "")
    exempt_prefixes = tuple(
        str(item) for item in getattr(config, "http_basic_exempt_paths", ()) or ()
    )

    async def middleware(
        request: Request,
        call_next: Callable[[Request], Awaitable[Response]],
    ) -> Response:
        path = request.url.path
        if _path_is_exempt(path, exempt_prefixes) or _valid_extension_key(
            config, request
        ):
            return await call_next(request)

        proxy_principal = _trusted_proxy_principal(request, config)
        if proxy_principal is not None:
            setattr(request.state, _OPERATOR_PRINCIPAL_STATE_KEY, proxy_principal)
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

        setattr(
            request.state,
            _OPERATOR_PRINCIPAL_STATE_KEY,
            OperatorPrincipal(
                actor_id=f"basic:{candidate_user}",
                auth_method="basic",
            ),
        )

        return await call_next(request)

    return middleware


def _trusted_proxy_principal(request: Request, config) -> OperatorPrincipal | None:
    if not trusted_operator_proxy_enabled(config):
        return None
    client_host = str(getattr(request.client, "host", "") or "").strip()
    try:
        client_ip = ipaddress.ip_address(client_host)
    except ValueError:
        return None
    trusted_networks = (
        ipaddress.ip_network(str(value).strip(), strict=False)
        for value in getattr(config, "http_trusted_operator_proxies", ()) or ()
    )
    if not any(client_ip in network for network in trusted_networks):
        return None
    header = str(getattr(config, "http_trusted_operator_header", "") or "").strip()
    subject = str(request.headers.get(header, "") or "").strip()
    if (
        not subject
        or len(subject) > 200
        or any(ord(character) < 32 or ord(character) == 127 for character in subject)
    ):
        return None
    return OperatorPrincipal(
        actor_id=f"proxy:{subject}",
        auth_method="trusted_proxy",
    )


def require_operator_principal(request: Request, config) -> OperatorPrincipal:
    principal = getattr(request.state, _OPERATOR_PRINCIPAL_STATE_KEY, None)
    if isinstance(principal, OperatorPrincipal):
        return principal
    proxy_principal = _trusted_proxy_principal(request, config)
    if proxy_principal is not None:
        return proxy_principal
    if not operator_auth_configured(config):
        raise HTTPException(
            status_code=503,
            detail={
                "code": "operator_auth_not_configured",
                "message": "publisher operator recovery requires configured authentication",
            },
        )
    raise HTTPException(
        status_code=401,
        detail={
            "code": "operator_auth_required",
            "message": "publisher operator recovery requires an authenticated principal",
        },
        headers={"WWW-Authenticate": 'Basic realm="ForWin"'},
    )


__all__ = [
    "OperatorPrincipal",
    "basic_auth_enabled",
    "make_basic_auth_middleware",
    "operator_auth_configured",
    "require_operator_principal",
    "trusted_operator_proxy_enabled",
]
