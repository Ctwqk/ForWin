from __future__ import annotations

import base64
import os
from collections.abc import Mapping


class BasicAuthConfigurationError(ValueError):
    pass


def basic_auth_credentials(
    source: Mapping[str, str] | None = None,
) -> tuple[str, str] | None:
    values = os.environ if source is None else source
    username = str(values.get("FORWIN_HTTP_BASIC_USER", "") or "").strip()
    password = str(values.get("FORWIN_HTTP_BASIC_PASSWORD", "") or "").strip()
    if bool(username) != bool(password):
        raise BasicAuthConfigurationError(
            "FORWIN_HTTP_BASIC_USER and FORWIN_HTTP_BASIC_PASSWORD "
            "must be set together"
        )
    if not username:
        return None
    return username, password


def basic_authorization_header(
    source: Mapping[str, str] | None = None,
) -> str | None:
    credentials = basic_auth_credentials(source)
    if credentials is None:
        return None
    username, password = credentials
    encoded = base64.b64encode(
        f"{username}:{password}".encode("utf-8")
    ).decode("ascii")
    return f"Basic {encoded}"
