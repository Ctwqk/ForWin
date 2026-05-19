from __future__ import annotations

import json

from forwin.api_pages_shared import PAGE_DOM_HELPERS_JS, render_page_document


_PUBLISHERS_CSS_PATHS = ("publishers/page.css", "shared/i18n.css")
_PUBLISHERS_BODY_PATHS = ("publishers/body.html",)
_PUBLISHERS_SCRIPT_PATHS = (
    "shared/i18n.js",
    "shared/forwin-topbar.js",
    "publishers/app_bridge.js",
    "publishers/app_uploads.js",
    "publishers/app_extension_events.js",
    "publishers/app_bootstrap.js",
)


def render_publishers_page(
    *,
    backend_ready: dict[str, object],
    extension_install_path: str,
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
        },
    )
