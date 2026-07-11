from __future__ import annotations

import html
import json

from forwin.api_pages_shared import (
    PAGE_DOM_HELPERS_JS,
    render_page_document,
)


_HOME_CSS_PATHS = ("home/page.css", "shared/i18n.css", "shared/topbar.css")
_HOME_BODY_PATHS = ("home/body.html",)
_HOME_SCRIPT_PATHS = (
    "shared/i18n.js",
    "shared/forwin-topbar.js",
    "home/app_state.js",
    "home/app_genesis.js",
    "home/app_library.js",
    "home/app_task_progress.js",
    "home/app_task_control.js",
    "home/app_task_drawer.js",
    "home/app_bootstrap.js",
)


def render_home_page(
    *,
    default_genre: str = "玄幻",
    default_chapters: int = 3,
    extension_api_key_configured: bool = False,
    extension_install_path: str = "browser_extension/forwin-publisher",
    rule_decision_breakdown: list[dict[str, object]] | None = None,
) -> str:
    normalized_default_chapters = int(default_chapters)
    return render_page_document(
        title="ForWin 工作台",
        css_paths=_HOME_CSS_PATHS,
        body_paths=_HOME_BODY_PATHS,
        script_paths=_HOME_SCRIPT_PATHS,
        replacements={
            "@@EXTENSION_BADGE_CLASS@@": "ok"
            if extension_api_key_configured
            else "warn",
            "@@EXTENSION_READY_TEXT@@": "已配置"
            if extension_api_key_configured
            else "未配置",
            "@@DEFAULT_GENRE@@": default_genre,
            "@@DEFAULT_GENRE_JSON@@": json.dumps(default_genre, ensure_ascii=False),
            "@@DEFAULT_CHAPTERS@@": str(normalized_default_chapters),
            "@@DEFAULT_CHAPTERS_JSON@@": json.dumps(normalized_default_chapters),
            "@@EXTENSION_READY@@": json.dumps(bool(extension_api_key_configured)),
            "@@EXTENSION_INSTALL_PATH@@": json.dumps(
                extension_install_path, ensure_ascii=False
            ),
            "@@EXTENSION_INSTALL_PATH_TEXT@@": html.escape(extension_install_path),
            "@@REVIEW_ENGINE_BREAKDOWN_HTML@@": _render_rule_decision_breakdown(
                rule_decision_breakdown or []
            ),
            "@@PAGE_DOM_HELPERS_JS@@": PAGE_DOM_HELPERS_JS,
        },
    )


def _render_rule_decision_breakdown(items: list[dict[str, object]]) -> str:
    if not items:
        return ""
    rows: list[str] = []
    for item in items[:12]:
        outcome = html.escape(str(item.get("outcome") or "unknown"))
        rule_id = html.escape(str(item.get("rule_id") or "unknown_rule"))
        reason = html.escape(str(item.get("reason") or ""))
        count = html.escape(str(item.get("count") or 0))
        status_chip = html.escape(str(item.get("status_chip") or "需要人工判断"))
        rows.append(
            "\n".join(
                [
                    '<div class="chapter-row">',
                    f"<strong>{rule_id}</strong>",
                    f'<span class="status-chip" data-chip="{status_chip}">{status_chip}</span>',
                    f'<div class="meta-line">outcome={outcome} | count={count}</div>',
                    f'<div class="meta-line">{reason}</div>' if reason else "",
                    "</div>",
                ]
            )
        )
    return "\n".join(
        [
            '<section class="card review-engine-breakdown">',
            '<div class="section-head"><h2>Review Engine Decisions</h2></div>',
            *rows,
            "</section>",
        ]
    )
