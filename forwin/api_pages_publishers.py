from __future__ import annotations

import json
from urllib.parse import urlsplit

from forwin.api_pages_shared import PAGE_DOM_HELPERS_JS, render_page_document


_PUBLISHERS_CSS_PATHS = ("publishers/page.css", "shared/i18n.css", "shared/topbar.css")
_PUBLISHERS_BODY_PATHS = ("publishers/body.html",)
_PUBLISHERS_SCRIPT_PATHS = (
    "shared/i18n.js",
    "shared/forwin-topbar.js",
    "publishers/app_bridge.js",
    "publishers/app_uploads.js",
    "publishers/app_extension_events.js",
    "publishers/app_bootstrap.js",
)


def _publish_hub_ops_url(value: object) -> str:
    """The configured /ops address if it is an absolute http(s) URL, else "" (no link)."""
    text = str(value or "").strip()
    try:
        parts = urlsplit(text)
    except ValueError:
        return ""
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return ""
    return text


def _js_string(value: str) -> str:
    """A JSON string literal that is also safe inside an inline <script> element."""
    return (
        json.dumps(value)
        .replace("<", "\\u003c")
        .replace(">", "\\u003e")
        .replace("&", "\\u0026")
    )


def render_publishers_page(
    *,
    backend_ready: dict[str, object],
    extension_install_path: str,
    publish_hub_ops_url: str = "",
) -> str:
    return render_page_document(
        title="ForWin 发布",
        css_paths=_PUBLISHERS_CSS_PATHS,
        body_paths=_PUBLISHERS_BODY_PATHS,
        script_paths=_PUBLISHERS_SCRIPT_PATHS,
        replacements={
            "@@BACKEND_EXTENSION_KEY_READY@@": json.dumps(
                bool(backend_ready.get("extension_api_key_configured"))
            ),
            "@@EXTENSION_INSTALL_PATH_JSON@@": json.dumps(
                extension_install_path,
                ensure_ascii=False,
            ),
            "@@EXTENSION_INSTALL_PATH@@": extension_install_path,
            "{PAGE_DOM_HELPERS_JS}": PAGE_DOM_HELPERS_JS,
            # Last: the substitution is a value from the config, and no later replacement
            # may read a placeholder out of it.
            "@@PUBLISH_HUB_OPS_URL_JSON@@": _js_string(_publish_hub_ops_url(publish_hub_ops_url)),
        },
    )
