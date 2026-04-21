from __future__ import annotations

import json

from forwin.api_pages_shared import (
    LLM_PROVIDER_PRESETS,
    PAGE_DOM_HELPERS_JS,
    render_page_document,
)


_HOME_CSS_PATHS = ("home/page.css",)
_HOME_BODY_PATHS = ("home/body.html",)
_HOME_SCRIPT_PATHS = (
    "home/app_state.js",
    "home/app_genesis.js",
    "home/app_library.js",
    "home/app_task_progress.js",
    "home/app_task_governance.js",
    "home/app_task_drawer.js",
    "home/app_bootstrap.js",
)


def render_home_page(
    *,
    has_api_key: bool,
    base_url: str,
    model: str,
    operation_mode: str,
    freeze_failed_candidates: bool,
    min_chapter_chars: int = 2500,
    review_interval_chapters: int = 0,
    default_genre: str = "玄幻",
    default_chapters: int = 3,
    extension_api_key_configured: bool = False,
    extension_install_path: str = "browser_extension/forwin-publisher",
) -> str:
    normalized_min_chars = max(500, int(min_chapter_chars))
    normalized_review_interval = max(0, int(review_interval_chapters))
    normalized_default_chapters = int(default_chapters)
    return render_page_document(
        title="ForWin 创作台",
        css_paths=_HOME_CSS_PATHS,
        body_paths=_HOME_BODY_PATHS,
        script_paths=_HOME_SCRIPT_PATHS,
        replacements={
            "@@HAS_API_KEY_TEXT@@": "已保存" if has_api_key else "未保存",
            "@@EXTENSION_BADGE_CLASS@@": "ok" if extension_api_key_configured else "warn",
            "@@EXTENSION_READY_TEXT@@": "已配置" if extension_api_key_configured else "未配置",
            "@@BASE_URL_JSON@@": json.dumps(base_url, ensure_ascii=False),
            "@@MODEL_JSON@@": json.dumps(model, ensure_ascii=False),
            "@@MODEL_PROVIDER_PRESETS_JSON@@": json.dumps(LLM_PROVIDER_PRESETS, ensure_ascii=False),
            "@@OPERATION_MODE_JSON@@": json.dumps(operation_mode, ensure_ascii=False),
            "@@FREEZE_FAILED_JSON@@": json.dumps(bool(freeze_failed_candidates)),
            "@@MIN_CHAPTER_CHARS@@": str(normalized_min_chars),
            "@@MIN_CHAPTER_CHARS_JSON@@": json.dumps(normalized_min_chars),
            "@@REVIEW_INTERVAL_CHAPTERS@@": str(normalized_review_interval),
            "@@REVIEW_INTERVAL_CHAPTERS_JSON@@": json.dumps(normalized_review_interval),
            "@@DEFAULT_GENRE@@": default_genre,
            "@@DEFAULT_GENRE_JSON@@": json.dumps(default_genre, ensure_ascii=False),
            "@@DEFAULT_CHAPTERS@@": str(normalized_default_chapters),
            "@@DEFAULT_CHAPTERS_JSON@@": json.dumps(normalized_default_chapters),
            "@@EXTENSION_READY@@": json.dumps(bool(extension_api_key_configured)),
            "@@EXTENSION_INSTALL_PATH@@": json.dumps(extension_install_path, ensure_ascii=False),
            "@@PAGE_DOM_HELPERS_JS@@": PAGE_DOM_HELPERS_JS,
        },
    )
