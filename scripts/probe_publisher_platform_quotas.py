#!/usr/bin/env python3
from __future__ import annotations

import argparse
from html import unescape
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit
from urllib.request import Request, urlopen

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.check_production_publisher_baseline import (  # noqa: E402
    publisher_browser_container_snapshot,
)
from scripts.monitor_forwin_runtime import redact_sensitive, utc_now  # noqa: E402


MAX_SNIPPET_CHARS = 96
DEFAULT_BROWSER_TEXT_LIMIT = 12000
BROWSER_TEXT_LIMIT_BY_PAGE_KEY = {
    "editor_frontend_static": 450000,
    "editor_frontend_source_map": 1_000_000,
}
FANQIE_LONGFORM_RULE_ARTICLE_URL = "https://fanqienovel.com/writer/zone/article/7639950766869839897"
PUBLISH_QUOTA_SIGNAL_CATEGORIES = {
    "numeric_publish_frequency_quota",
    "fanqie_longform_create_quota",
    "fanqie_longform_update_work_quota",
    "fanqie_longform_word_quota",
}
CONSERVATIVE_PUBLISH_CADENCE_SIGNAL_CATEGORIES = {
    "qidian_new_book_two_chapter_cadence",
    "qidian_new_book_two_update_reserve_cadence",
    "qidian_update_strategy_cadence",
    "qidian_stage_daily_word_cadence",
}
PUBLIC_STATIC_FALLBACK_PAGES = {
    ("qidian", "official_new_book_faq"),
    ("qidian", "official_chapter_word_faq"),
    ("qidian", "official_daily_update_faq"),
    ("qidian", "official_direct_publish_faq"),
    ("qidian", "official_full_attendance_faq"),
    ("qidian", "official_manuscript_reserve_article"),
    ("qidian", "official_submission_posture_article"),
    ("qidian", "official_update_strategy_article"),
    ("qidian", "official_version_notes"),
}


@dataclass(frozen=True)
class SignalRule:
    category: str
    severity: str
    keywords: tuple[str, ...]


SIGNAL_RULES: tuple[SignalRule, ...] = (
    SignalRule(
        category="create_book_rate_limit",
        severity="blocker",
        keywords=(
            "当日创建作品上限",
            "今日创建次数过多",
            "创建过于频繁",
            "达到创建上限",
            "创建作品上限",
        ),
    ),
    SignalRule(
        category="risk_control",
        severity="blocker",
        keywords=("风控", "账号异常", "验证码", "安全验证", "无发文权限", "暂无发文权限"),
    ),
    SignalRule(
        category="draft_limit",
        severity="rule",
        keywords=("草稿上限", "草稿数量", "草稿箱已满"),
    ),
    SignalRule(
        category="chapter_word_limit",
        severity="rule",
        keywords=("不得超过20000字", "不超过20000字", "20000字", "二万字", "章节不能为空"),
    ),
    SignalRule(
        category="chapter_word_recommendation",
        severity="rule",
        keywords=("2000-6000", "2000～6000", "2000至6000", "4000字", "6000字"),
    ),
    SignalRule(
        category="intro_requirement",
        severity="rule",
        keywords=("50-500", "5-500", "500字以内", "作品简介"),
    ),
    SignalRule(
        category="signing_threshold",
        severity="rule",
        keywords=("签约", "2万", "5万", "10万", "20万", "50000字", "100000字", "200000字"),
    ),
    SignalRule(
        category="publish_review",
        severity="rule",
        keywords=("审核", "发布章节", "章节发布", "定时发布", "半小时", "30分钟"),
    ),
)

NUMERIC_PUBLISH_FREQUENCY_PATTERN = re.compile(
    r"("
    r"(每日|每天|每小时|小时内|当日|今日|单日|单月)"
    r".{0,36}(上限|限|仅限|最多|不超过|不得超过|可提交|可创建|可更新|额度)"
    r".{0,36}(发布|发表|发文|更新|章节|作品|字数)"
    r".{0,36}([0-9０-９]+|一|二|两|三|四|五|六|七|八|九|十)"
    r"|"
    r"(每日|每天|每小时|小时内|当日|今日|单日|单月)"
    r".{0,36}(发布|发表|发文|更新|章节|作品|字数)"
    r".{0,36}(上限|限|仅限|最多|不超过|不得超过|额度)"
    r".{0,36}([0-9０-９]+|一|二|两|三|四|五|六|七|八|九|十)"
    r")",
)


DEFAULT_PAGES: dict[str, list[dict[str, str]]] = {
    "fanqie": [
        {
            "page_key": "dashboard",
            "url": "https://fanqienovel.com/main/writer/",
        },
        {
            "page_key": "create_work",
            "url": "https://fanqienovel.com/main/writer/create",
        },
        {
            "page_key": "official_work_guide",
            "url": "https://fanqienovel.com/docs/8231/90699",
        },
        {
            "page_key": "official_longform_publish_rules",
            "url": FANQIE_LONGFORM_RULE_ARTICLE_URL,
        },
        {
            "page_key": "official_changelog",
            "url": "https://fanqienovel.com/writer/zone/change-log",
        },
        {
            "page_key": "official_backend_notice",
            "url": "https://notice.fanqienovel.com/docs/9476/zuojiahoutai",
        },
    ],
    "qidian": [
        {
            "page_key": "dashboard",
            "url": "https://write.qq.com/portal/dashboard",
        },
        {
            "page_key": "create_work",
            "url": "https://write.qq.com/portal/dashboard/create-novel?from=S5",
        },
        {
            "page_key": "account_can_create_work_endpoint",
            "url": "https://write.qq.com/ccauthorweb/novel/iscancreatenovel",
        },
        {
            "page_key": "day_words_calendar_endpoint",
            "url": "https://write.qq.com/ccauthorweb/daywords/getMonthDayWords",
        },
        {
            "page_key": "editor_frontend_static",
            "url": "https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js",
        },
        {
            "page_key": "editor_frontend_source_map",
            "url": "https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js.map",
        },
        {
            "page_key": "official_new_book_faq",
            "url": "https://write.qq.com/ask/qfokgyc",
        },
        {
            "page_key": "official_chapter_word_faq",
            "url": "https://write.qq.com/ask/qfoycqb",
        },
        {
            "page_key": "official_daily_update_faq",
            "url": "https://write.qq.com/ask/qjdwzhv",
        },
        {
            "page_key": "official_direct_publish_faq",
            "url": "https://write.qq.com/ask/qqbosdy",
        },
        {
            "page_key": "official_full_attendance_faq",
            "url": "https://write.qq.com/ask/qjdbpvx",
        },
        {
            "page_key": "official_update_strategy_article",
            "url": "https://write.qq.com/portal/content/20483235608067701?feedType=1&lcid=",
        },
        {
            "page_key": "official_manuscript_reserve_article",
            "url": "https://write.qq.com/portal/content/20731268901906701?feedType=1&lcid=",
        },
        {
            "page_key": "official_submission_posture_article",
            "url": "https://write.qq.com/portal/content/20368305408917301?feedType=1&lcid=",
        },
        {
            "page_key": "official_version_notes",
            "url": "https://write.qq.com/portal/version",
        },
    ],
}


def sanitize_url(url: Any) -> str:
    parsed = urlsplit(str(url or ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def public_static_fetch_url(url: Any) -> str:
    parsed = urlsplit(str(url or ""))
    if parsed.netloc == "write.qq.com" and parsed.path.startswith("/portal/content/"):
        query_items = [
            (key, value)
            for key, value in parse_qsl(parsed.query, keep_blank_values=True)
            if key in {"feedType", "lcid"}
        ]
        return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query_items), ""))
    return sanitize_url(url)


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def html_to_text(value: str) -> str:
    without_scripts = re.sub(r"<(script|style)\b[^>]*>.*?</\1>", " ", value, flags=re.IGNORECASE | re.DOTALL)
    return normalize_space(unescape(re.sub(r"<[^>]+>", " ", without_scripts)))


def fetch_public_static_text(url: str) -> str:
    if not url:
        return ""
    request = Request(
        public_static_fetch_url(url),
        headers={"User-Agent": "Mozilla/5.0 ForWinQuotaProbe/1.0"},
    )
    try:
        with urlopen(request, timeout=20) as response:  # noqa: S310 - fixed official/public source URLs only.
            charset = response.headers.get_content_charset() or "utf-8"
            return html_to_text(response.read().decode(charset, errors="replace"))
    except Exception:  # noqa: BLE001
        return ""


def page_text_for_signal_extraction(
    *,
    platform: str,
    page_key: str,
    url: str,
    browser_text: str,
    public_text_fetcher=fetch_public_static_text,
) -> str:
    text = str(browser_text or "")
    if (platform, page_key) not in PUBLIC_STATIC_FALLBACK_PAGES:
        return text
    fallback_text = html_to_text(public_text_fetcher(public_static_fetch_url(url)))
    if not fallback_text:
        return text
    return normalize_space(f"{text}\n{fallback_text}")


def snippet_around(text: str, keyword: str) -> str:
    normalized = normalize_space(text)
    index = normalized.find(keyword)
    if index < 0:
        return normalized[:MAX_SNIPPET_CHARS]
    radius = max(12, (MAX_SNIPPET_CHARS - len(keyword)) // 2)
    start = max(0, index - radius)
    end = min(len(normalized), index + len(keyword) + radius)
    snippet = normalized[start:end].strip()
    if start > 0:
        snippet = "..." + snippet
    if end < len(normalized):
        snippet = snippet + "..."
    return snippet[:MAX_SNIPPET_CHARS]


def _contains_keyword(text: str, keyword: str) -> bool:
    return keyword in text


def _fanqie_longform_static_rule_signals(
    *,
    platform: str,
    page_key: str,
    url: str,
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    if platform != "fanqie" or page_key != "official_longform_publish_rules":
        return []
    normalized = normalize_space(f"{title} {text}")
    if "长篇网文发文规则" not in normalized and "可提交发布字数" not in normalized:
        return []
    source_url = sanitize_url(url or FANQIE_LONGFORM_RULE_ARTICLE_URL)
    base = {
        "platform": platform,
        "page_key": page_key,
        "source_url": source_url,
        "title": normalize_space(title)[:120],
        "severity": "rule",
        "source_evidence": "official_article_image_table",
        "quota_confirmed": True,
    }
    return [
        {
            **base,
            "category": "fanqie_longform_create_quota",
            "matched_keyword": "长篇网文发文规则: 可创建长篇作品数",
            "snippet": "单账号单日可创建长篇作品数仅限1本，单月上限3本。",
            "limits": {
                "daily_create_longform_works": 1,
                "monthly_create_longform_works": 3,
            },
        },
        {
            **base,
            "category": "fanqie_longform_update_work_quota",
            "matched_keyword": "长篇网文发文规则: 可更新长篇作品数",
            "snippet": "单日可更新长篇作品数按作者等级分层: Lv.0/Lv.1限1本，Lv.2/Lv.3上限3本，Lv.4及以上上限5本。",
            "limits": {
                "lv0_lv1_daily_update_longform_works": 1,
                "lv2_lv3_daily_update_longform_works": 3,
                "lv4_plus_daily_update_longform_works": 5,
            },
        },
        {
            **base,
            "category": "fanqie_longform_word_quota",
            "matched_keyword": "长篇网文发文规则: 可提交发布字数",
            "snippet": "可提交发布字数按作者等级分层: 单日<1万/<2万/<5万，单月<25万/<50万/<100万。",
            "limits": {
                "lv0_lv1_daily_submitted_words_lt": 10000,
                "lv2_lv3_daily_submitted_words_lt": 20000,
                "lv4_plus_daily_submitted_words_lt": 50000,
                "lv0_lv1_monthly_submitted_words_lt": 250000,
                "lv2_lv3_monthly_submitted_words_lt": 500000,
                "lv4_plus_monthly_submitted_words_lt": 1000000,
            },
        },
    ]


def _qidian_endpoint_state_signals(
    *,
    platform: str,
    page_key: str,
    url: str,
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    if platform != "qidian":
        return []
    source_url = sanitize_url(url)
    base = {
        "platform": platform,
        "page_key": page_key,
        "source_url": source_url,
        "title": normalize_space(title)[:120],
        "severity": "info",
        "quota_confirmed": False,
    }
    payload: Any = None
    stripped = str(text or "").strip()
    if stripped:
        try:
            payload = json.loads(stripped)
        except json.JSONDecodeError:
            payload = None

    if page_key == "account_can_create_work_endpoint":
        current_value = None
        if isinstance(payload, dict) and isinstance(payload.get("result"), bool):
            current_value = bool(payload["result"])
        elif re.search(r'"result"\s*:\s*true', stripped, re.I):
            current_value = True
        elif re.search(r'"result"\s*:\s*false', stripped, re.I):
            current_value = False
        if current_value is None:
            return []
        return [
            {
                **base,
                "category": "current_account_create_available",
                "severity": "info" if current_value else "blocker",
                "matched_keyword": f"iscancreatenovel={str(current_value).lower()}",
                "snippet": "Qidian current account create-work endpoint returned available=true."
                if current_value
                else "Qidian current account create-work endpoint returned available=false.",
                "current_value": current_value,
            }
        ]

    if page_key == "day_words_calendar_endpoint" and (
        "dayWordsShowTxt" in stripped or "当日发布" in stripped or "pubChapters" in stripped
    ):
        return [
            {
                **base,
                "category": "current_publish_counter",
                "matched_keyword": "dayWordsShowTxt",
                "snippet": snippet_around(stripped, "当日发布" if "当日发布" in stripped else "dayWordsShowTxt"),
            }
        ]
    return []


def _contains_literal_or_js_escape(text: str, phrase: str) -> bool:
    return phrase in text or phrase.encode("unicode_escape").decode("ascii") in text


def _qidian_editor_frontend_static_signals(
    *,
    platform: str,
    page_key: str,
    url: str,
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    if platform != "qidian" or page_key != "editor_frontend_static":
        return []
    normalized = normalize_space(text)
    has_daily_batch_limit = "getuploadnumoftheday" in normalized and (
        "e<50" in normalized
        or "<50" in normalized
        or _contains_literal_or_js_escape(normalized, "今日上传已达到50个文件上限")
    )
    has_single_batch_limit = (
        "t>10" in normalized
        or ">10" in normalized
        or _contains_literal_or_js_escape(normalized, "单次最多上传10个文件")
    )
    if not has_daily_batch_limit and not has_single_batch_limit:
        return []

    limits: dict[str, int] = {}
    snippets: list[str] = []
    matched_parts: list[str] = []
    if has_daily_batch_limit:
        limits["daily_batch_import_files"] = 50
        matched_parts.append("bookchapterimport/getuploadnumoftheday<50")
        snippets.append("批量导入入口按账号/作品读取今日上传文件数；达到50个文件时提示次日再批量上传。")
    if has_single_batch_limit:
        limits["single_batch_import_files"] = 10
        matched_parts.append("single_batch_import_files<=10")
        snippets.append("批量导入单次最多处理10个文件，剩余文件需分批上传。")

    return [
        {
            "platform": platform,
            "page_key": page_key,
            "source_url": sanitize_url(url),
            "title": normalize_space(title)[:120],
            "category": "qidian_batch_import_file_quota",
            "severity": "rule",
            "matched_keyword": "; ".join(matched_parts),
            "snippet": " ".join(snippets)[:MAX_SNIPPET_CHARS],
            "limits": limits,
            "source_evidence": "official_editor_frontend_static",
            "quota_confirmed": True,
        }
    ]


def _qidian_editor_frontend_source_map_signals(
    *,
    platform: str,
    page_key: str,
    url: str,
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    if platform != "qidian" or page_key != "editor_frontend_source_map":
        return []
    try:
        payload = json.loads(str(text or ""))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    sources = payload.get("sources")
    sources_content = payload.get("sourcesContent")
    if not isinstance(sources, list) or not isinstance(sources_content, list):
        return []

    contents_by_source: dict[str, str] = {}
    for index, source in enumerate(sources):
        if not isinstance(source, str) or index >= len(sources_content):
            continue
        content = sources_content[index]
        if isinstance(content, str):
            contents_by_source[source] = content

    publish_dialog = contents_by_source.get("components/publishDialog/index.js", "")
    publish_api = contents_by_source.get("api/publishChapter.js", "")
    last_four_api = contents_by_source.get("api/getLastFourPublishTime.js", "")
    publish_path_text = normalize_space(" ".join([publish_dialog, publish_api, last_four_api]))
    if not publish_path_text:
        return []

    has_publish_endpoint = "/Chapter/publishChapter" in publish_api
    has_publish_status = "status: isSchedule ? 5 : 2" in publish_api
    has_last_four_schedule_shortcut = (
        "getLastFourPublishTime(window._CBID)" in publish_dialog
        and "recentPublishTimes" in publish_dialog
        and "常设时间" in publish_dialog
        and "getLastFourChapterPublishTime" in last_four_api
    )
    if not (has_publish_endpoint and has_publish_status and has_last_four_schedule_shortcut):
        return []

    if NUMERIC_PUBLISH_FREQUENCY_PATTERN.search(publish_path_text):
        return []

    return [
        {
            "platform": platform,
            "page_key": page_key,
            "source_url": sanitize_url(url),
            "title": normalize_space(title)[:120],
            "category": "qidian_publish_frontend_path_observed",
            "severity": "info",
            "matched_keyword": "publishChapter + getLastFourChapterPublishTime",
            "snippet": (
                "官方source map显示publishChapter提交发布/定时发布请求；"
                "getLastFourChapterPublishTime用于定时发布常设时间，未暴露数值发布频率额度。"
            )[:MAX_SNIPPET_CHARS],
            "source_evidence": "official_editor_frontend_source_map",
            "quota_confirmed": False,
            "inspected_sources": [
                "components/publishDialog/index.js",
                "api/publishChapter.js",
                "api/getLastFourPublishTime.js",
            ],
        }
    ]


def _qidian_source_map_review_warning_signals(
    *,
    platform: str,
    page_key: str,
    url: str,
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    if platform != "qidian" or page_key != "editor_frontend_source_map":
        return []
    try:
        payload = json.loads(str(text or ""))
    except json.JSONDecodeError:
        return []
    if not isinstance(payload, dict):
        return []
    sources = payload.get("sources")
    sources_content = payload.get("sourcesContent")
    if not isinstance(sources, list) or not isinstance(sources_content, list):
        return []

    for index, source in enumerate(sources):
        if source != "components/sideTask/task3.js" or index >= len(sources_content):
            continue
        content = sources_content[index]
        if not isinstance(content, str):
            continue
        normalized = normalize_space(content)
        if "新书审核期注意避免频繁发布、修改章节" not in normalized:
            continue
        return [
            {
                "platform": platform,
                "page_key": page_key,
                "source_url": sanitize_url(url),
                "title": normalize_space(title)[:120],
                "category": "qidian_new_book_review_frequency_warning",
                "severity": "rule",
                "matched_keyword": "新书审核期注意避免频繁发布、修改章节",
                "snippet": "官方source map提示新书审核期避免频繁发布或修改章节；这是审核风险提示，不是数值发布额度。",
                "source_evidence": "official_editor_frontend_source_map",
                "quota_confirmed": False,
            }
        ]
    return []


def _qidian_update_cadence_guidance_signals(
    *,
    platform: str,
    page_key: str,
    url: str,
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    if platform != "qidian":
        return []

    normalized = normalize_space(f"{title} {text}")
    base = {
        "platform": platform,
        "page_key": page_key,
        "source_url": sanitize_url(url),
        "title": normalize_space(title)[:120],
        "quota_confirmed": False,
    }

    if page_key == "official_daily_update_faq" and (
        "并不是所有起点作家都必须每天更新" in normalized
        or ("更新频率" in normalized and "自己可以根据实际情况来决定" in normalized)
    ):
        return [
            {
                **base,
                "category": "qidian_daily_update_guidance",
                "severity": "info",
                "matched_keyword": "并不是所有起点作家都必须每天更新",
                "snippet": "官方问答说明并非所有起点作家都必须每天更新；更新频率可按创作进度安排。",
                "source_evidence": "official_ask_daily_update_faq",
            }
        ]

    if page_key == "official_direct_publish_faq" and (
        "起点可以直接发书" in normalized
        and ("每天更新2章" in normalized or "每天更新两章" in normalized)
        and "3万字" in normalized
    ):
        return [
            {
                **base,
                "category": "qidian_new_book_two_chapter_cadence",
                "severity": "rule",
                "matched_keyword": "起点可以直接发书，每天更新2章，直至3万字左右",
                "snippet": "官方问答给出起点新书直接发书后的保守节奏: 每天更新2章，直至约3万字观察站短。",
                "source_evidence": "official_ask_direct_publish_faq",
                "limits": {
                    "recommended_new_book_daily_chapters": 2,
                    "recommended_until_words_approx": 30000,
                },
            }
        ]

    if page_key == "official_update_strategy_article" and (
        "每天更新的章节数" in normalized
        and "三到四章" in normalized
        and "至少保持两更" in normalized
    ):
        return [
            {
                **base,
                "category": "qidian_update_strategy_cadence",
                "severity": "info",
                "matched_keyword": "每天更新的章节数，以三到四章为宜；至少保持两更",
                "snippet": "官方创作学堂文章建议日更章节数以3到4章为宜，做不到则至少保持两更，并均匀间隔更新时间。",
                "source_evidence": "official_update_strategy_article",
                "limits": {
                    "recommended_daily_chapters_min": 2,
                    "recommended_daily_chapters_max": 4,
                },
            }
        ]

    if page_key == "official_manuscript_reserve_article" and (
        "新书期的一天两更" in normalized or ("新书" in normalized and "每天两更" in normalized)
    ):
        return [
            {
                **base,
                "category": "qidian_new_book_two_update_reserve_cadence",
                "severity": "info",
                "matched_keyword": "保证新书期的一天两更",
                "snippet": "官方创作学堂文章建议保留存稿以保证新书期一天两更，直到上架后一段时间再逐步减少。",
                "source_evidence": "official_manuscript_reserve_article",
                "limits": {
                    "recommended_new_book_daily_updates": 2,
                },
            }
        ]

    if page_key == "official_submission_posture_article" and (
        "未签约之前" in normalized
        and "建议日更不少于一千" in normalized
        and "签约之后" in normalized
        and "建议日更不少于两千" in normalized
        and "日更不少于四千" in normalized
    ):
        return [
            {
                **base,
                "category": "qidian_stage_daily_word_cadence",
                "severity": "info",
                "matched_keyword": "未签约日更不少于一千，签约后不少于两千，上架后不少于四千",
                "snippet": "官方投稿指导按作品阶段给出日更字数建议: 未签约不少于1000，签约后不少于2000，上架后不少于4000。",
                "source_evidence": "official_submission_posture_article",
                "limits": {
                    "recommended_unsigned_daily_words_min": 1000,
                    "recommended_signed_daily_words_min": 2000,
                    "recommended_vip_daily_words_min": 4000,
                },
            }
        ]

    if page_key == "official_full_attendance_faq" and (
        "全勤奖" in normalized
        and ("VIP章节日更4000字" in normalized or "每天更新不低于四千字" in normalized)
    ):
        return [
            {
                **base,
                "category": "qidian_full_attendance_update_incentive",
                "severity": "rule",
                "matched_keyword": "全勤奖 + VIP章节日更4000字",
                "snippet": "官方问答把VIP章节日更4000字列为全勤奖获取条件；这是福利/激励门槛，不是公开发布频率额度。",
                "source_evidence": "official_ask_full_attendance_faq",
                "limits": {
                    "vip_daily_update_words_for_full_attendance": 4000,
                },
            }
        ]

    return []


def _qidian_version_note_interval_signals(
    *,
    platform: str,
    page_key: str,
    url: str,
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    if platform != "qidian" or page_key != "official_version_notes":
        return []
    normalized = normalize_space(f"{title} {text}")
    if "章节申请解禁间隔时间调整至2小时" not in normalized:
        return []
    return [
        {
            "platform": platform,
            "page_key": page_key,
            "source_url": sanitize_url(url),
            "title": normalize_space(title)[:120],
            "category": "qidian_chapter_unblock_request_interval",
            "severity": "rule",
            "matched_keyword": "章节申请解禁间隔时间调整至2小时",
            "snippet": "官方版本说明确认章节申请解禁间隔时间为2小时；这是解禁申请间隔，不是发布频率额度。",
            "source_evidence": "official_version_notes",
            "quota_confirmed": True,
            "limits": {
                "chapter_unblock_request_interval_hours": 2,
            },
        }
    ]


def extract_limit_signals(
    *,
    platform: str,
    page_key: str,
    url: str,
    title: str,
    text: str,
) -> list[dict[str, Any]]:
    normalized = normalize_space(text)
    signals: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for rule in SIGNAL_RULES:
        for keyword in rule.keywords:
            if not _contains_keyword(normalized, keyword):
                continue
            identity = (rule.category, keyword)
            if identity in seen:
                continue
            seen.add(identity)
            signals.append(
                {
                    "platform": platform,
                    "page_key": page_key,
                    "source_url": sanitize_url(url),
                    "title": normalize_space(title)[:120],
                    "category": rule.category,
                    "severity": rule.severity,
                    "matched_keyword": keyword,
                    "snippet": snippet_around(normalized, keyword),
                }
            )
            break

    match = NUMERIC_PUBLISH_FREQUENCY_PATTERN.search(normalized)
    if match:
        signals.append(
            {
                "platform": platform,
                "page_key": page_key,
                "source_url": sanitize_url(url),
                "title": normalize_space(title)[:120],
                "category": "numeric_publish_frequency_quota",
                "severity": "rule",
                "matched_keyword": match.group(0)[:40],
                "snippet": snippet_around(normalized, match.group(0)),
            }
        )
    signals.extend(
        _fanqie_longform_static_rule_signals(
            platform=platform,
            page_key=page_key,
            url=url,
            title=title,
            text=text,
        )
    )
    signals.extend(
        _qidian_endpoint_state_signals(
            platform=platform,
            page_key=page_key,
            url=url,
            title=title,
            text=text,
        )
    )
    signals.extend(
        _qidian_editor_frontend_static_signals(
            platform=platform,
            page_key=page_key,
            url=url,
            title=title,
            text=text,
        )
    )
    signals.extend(
        _qidian_editor_frontend_source_map_signals(
            platform=platform,
            page_key=page_key,
            url=url,
            title=title,
            text=text,
        )
    )
    signals.extend(
        _qidian_source_map_review_warning_signals(
            platform=platform,
            page_key=page_key,
            url=url,
            title=title,
            text=text,
        )
    )
    signals.extend(
        _qidian_update_cadence_guidance_signals(
            platform=platform,
            page_key=page_key,
            url=url,
            title=title,
            text=text,
        )
    )
    signals.extend(
        _qidian_version_note_interval_signals(
            platform=platform,
            page_key=page_key,
            url=url,
            title=title,
            text=text,
        )
    )
    return signals


def _visible_account_blockers(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in signals
        if item.get("severity") == "blocker"
        and str(item.get("page_key") or "")
        in {"dashboard", "create_work", "account_can_create_work_endpoint"}
    ]


def summarize_probe(
    *,
    checked_at: str,
    pages: list[dict[str, Any]],
    expected_platforms: list[str],
) -> dict[str, Any]:
    platforms: dict[str, dict[str, Any]] = {}
    all_blockers: list[dict[str, Any]] = []

    for platform in expected_platforms:
        platforms[platform] = {
            "page_count": 0,
            "ok_page_count": 0,
            "signal_count": 0,
            "categories": [],
            "publish_quota_confirmed": False,
            "conservative_publish_cadence_confirmed": False,
            "visible_account_blockers": [],
        }

    for page in pages:
        platform = str(page.get("platform") or "")
        if platform not in platforms:
            platforms[platform] = {
                "page_count": 0,
                "ok_page_count": 0,
                "signal_count": 0,
                "categories": [],
                "publish_quota_confirmed": False,
                "conservative_publish_cadence_confirmed": False,
                "visible_account_blockers": [],
            }
        entry = platforms[platform]
        entry["page_count"] += 1
        if page.get("ok"):
            entry["ok_page_count"] += 1
        signals = page.get("signals") if isinstance(page.get("signals"), list) else []
        entry["signal_count"] += len(signals)
        categories = set(entry["categories"])
        for signal in signals:
            category = str(signal.get("category") or "")
            if category:
                categories.add(category)
            if category in PUBLISH_QUOTA_SIGNAL_CATEGORIES:
                entry["publish_quota_confirmed"] = True
            if category in CONSERVATIVE_PUBLISH_CADENCE_SIGNAL_CATEGORIES:
                entry["conservative_publish_cadence_confirmed"] = True
        entry["categories"] = sorted(categories)
        blockers = _visible_account_blockers(signals)
        entry["visible_account_blockers"].extend(blockers)
        all_blockers.extend(blockers)

    confirmed_platforms = [
        platform for platform in expected_platforms if platforms.get(platform, {}).get("publish_quota_confirmed")
    ]
    unconfirmed_platforms = [
        platform for platform in expected_platforms if not platforms.get(platform, {}).get("publish_quota_confirmed")
    ]
    conservative_platforms = [
        platform
        for platform in expected_platforms
        if not platforms.get(platform, {}).get("publish_quota_confirmed")
        and platforms.get(platform, {}).get("conservative_publish_cadence_confirmed")
    ]
    single_chapter_unconfirmed_platforms = [
        platform
        for platform in expected_platforms
        if not platforms.get(platform, {}).get("publish_quota_confirmed")
        and not platforms.get(platform, {}).get("conservative_publish_cadence_confirmed")
    ]
    single_chapter_allowed = not all_blockers and not single_chapter_unconfirmed_platforms

    if all_blockers:
        status = "blocked"
        publish_true_gate = {
            "allowed": False,
            "reason": "visible_account_blocker",
            "blocker_count": len(all_blockers),
            "confirmed_platforms": confirmed_platforms,
            "unconfirmed_platforms": unconfirmed_platforms,
        }
        single_chapter_publish_true_gate = {
            "allowed": False,
            "reason": "visible_account_blocker",
            "blocker_count": len(all_blockers),
            "hard_quota_platforms": confirmed_platforms,
            "conservative_platforms": conservative_platforms,
            "unconfirmed_platforms": single_chapter_unconfirmed_platforms,
            "max_chapters_per_platform": 1,
        }
    elif not unconfirmed_platforms:
        status = "quota_confirmed"
        publish_true_gate = {
            "allowed": True,
            "reason": "numeric_publish_frequency_quota_confirmed",
            "blocker_count": 0,
            "confirmed_platforms": confirmed_platforms,
            "unconfirmed_platforms": [],
        }
        single_chapter_publish_true_gate = {
            "allowed": True,
            "reason": "hard_quota_or_conservative_cadence_confirmed",
            "blocker_count": 0,
            "hard_quota_platforms": confirmed_platforms,
            "conservative_platforms": [],
            "unconfirmed_platforms": [],
            "max_chapters_per_platform": 1,
        }
    else:
        status = "quota_incomplete"
        publish_true_gate = {
            "allowed": False,
            "reason": "numeric_publish_frequency_quota_unconfirmed",
            "blocker_count": 0,
            "confirmed_platforms": confirmed_platforms,
            "unconfirmed_platforms": unconfirmed_platforms,
        }
        single_chapter_publish_true_gate = {
            "allowed": single_chapter_allowed,
            "reason": (
                "hard_quota_or_conservative_cadence_confirmed"
                if single_chapter_allowed
                else "numeric_publish_frequency_quota_unconfirmed"
            ),
            "blocker_count": 0,
            "hard_quota_platforms": confirmed_platforms,
            "conservative_platforms": conservative_platforms,
            "unconfirmed_platforms": single_chapter_unconfirmed_platforms,
            "max_chapters_per_platform": 1,
        }

    return redact_sensitive(
        {
            "status": status,
            "checked_at": checked_at,
            "platforms": platforms,
            "publish_true_gate": publish_true_gate,
            "single_chapter_publish_true_gate": single_chapter_publish_true_gate,
            "blocked_items": [
                {
                    "kind": "publisher_quota_or_risk_signal",
                    "platform": item.get("platform"),
                    "page_key": item.get("page_key"),
                    "category": item.get("category"),
                    "matched_keyword": item.get("matched_keyword"),
                    "source_url": item.get("source_url"),
                    "snippet": item.get("snippet"),
                }
                for item in all_blockers
            ],
        }
    )


def _browser_probe_script(pages: list[dict[str, str]]) -> str:
    return (
        r'''
from playwright.sync_api import sync_playwright
import json
import sys

pages = json.loads(sys.stdin.read())
results = []

with sync_playwright() as p:
    browser = p.chromium.connect_over_cdp("http://127.0.0.1:9222")
    try:
        if not browser.contexts:
            raise RuntimeError("production browser has no CDP contexts")
        ctx = browser.contexts[0]
        for item in pages:
            page = ctx.new_page()
            result = {
                "platform": item.get("platform", ""),
                "page_key": item.get("page_key", ""),
                "requested_url": item.get("url", ""),
                "ok": False,
                "url": "",
                "title": "",
                "text": "",
            }
            try:
                try:
                    page.goto(item.get("url", ""), wait_until="domcontentloaded", timeout=35000)
                    page.wait_for_timeout(3500)
                except Exception as exc:  # noqa: BLE001
                    result["navigation_error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
                    page.wait_for_timeout(1500)
                result["url"] = page.url
                result["title"] = page.title()
                text_limit = int(item.get("text_limit") or 12000)
                result["text"] = page.locator("body").inner_text(timeout=6000)[:text_limit]
                result["ok"] = bool(result["text"] or result["title"] or result["url"])
            except Exception as exc:  # noqa: BLE001
                result["error"] = f"{type(exc).__name__}: {str(exc)[:240]}"
            finally:
                results.append(result)
                page.close()
    finally:
        browser.close()

print(json.dumps({"ok": True, "pages": results}, ensure_ascii=False))
'''
    )


def run_command_with_input(
    args: list[str],
    *,
    timeout: float = 30.0,
    input_text: str = "",
) -> dict[str, Any]:
    try:
        proc = subprocess.run(
            args,
            cwd=str(REPO_ROOT),
            input=input_text,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            check=False,
        )
    except FileNotFoundError as exc:
        return {"ok": False, "error": str(exc), "stdout": "", "stderr": ""}
    except subprocess.TimeoutExpired as exc:
        return {
            "ok": False,
            "error": f"timeout after {timeout}s",
            "stdout": exc.stdout or "",
            "stderr": exc.stderr or "",
        }
    return {
        "ok": proc.returncode == 0,
        "stdout": proc.stdout,
        "stderr": proc.stderr,
        "returncode": proc.returncode,
    }


def browser_quota_pages_snapshot(args: argparse.Namespace) -> dict[str, Any]:
    if bool(getattr(args, "skip_browser", False)):
        return {"ok": True, "skipped": True, "pages": []}
    container = str(getattr(args, "publisher_browser_container", "") or "")
    if not container:
        return {"ok": False, "error": "publisher browser container id is not configured", "pages": []}
    platforms = list(getattr(args, "expected_platform", []) or ["fanqie", "qidian"])
    page_specs = [
        {
            "platform": platform,
            **page,
            "text_limit": BROWSER_TEXT_LIMIT_BY_PAGE_KEY.get(
                page.get("page_key", ""),
                DEFAULT_BROWSER_TEXT_LIMIT,
            ),
        }
        for platform in platforms
        for page in DEFAULT_PAGES.get(platform, [])
    ]
    proc = run_command_with_input(
        [
            "colima",
            "ssh",
            "-p",
            str(getattr(args, "colima_profile", "swarmbridged")),
            "--",
            "docker",
            "exec",
            "-i",
            container,
            "python",
            "-c",
            _browser_probe_script(page_specs),
        ],
        timeout=180,
        input_text=json.dumps(page_specs),
    )
    if not proc.get("ok"):
        return {
            "ok": False,
            "error": str(proc.get("stderr") or proc.get("error") or proc.get("stdout") or "")[:500],
            "pages": [],
        }
    try:
        payload = json.loads(str(proc.get("stdout") or "{}"))
    except json.JSONDecodeError as exc:
        return {"ok": False, "error": f"invalid browser JSON: {exc}", "pages": []}
    return payload if isinstance(payload, dict) else {"ok": False, "error": "non-object browser JSON", "pages": []}


def build_report(args: argparse.Namespace) -> dict[str, Any]:
    expected = list(getattr(args, "expected_platform", []) or ["fanqie", "qidian"])
    container = publisher_browser_container_snapshot(args)
    if container.get("ok"):
        setattr(args, "publisher_browser_container", container.get("container_id"))
    browser = (
        browser_quota_pages_snapshot(args)
        if container.get("ok")
        else {"ok": False, "error": container.get("error"), "pages": []}
    )
    pages: list[dict[str, Any]] = []
    for page in browser.get("pages", []) if isinstance(browser.get("pages"), list) else []:
        if not isinstance(page, dict):
            continue
        platform = str(page.get("platform") or "")
        page_key = str(page.get("page_key") or "")
        url = str(page.get("url") or page.get("requested_url") or "")
        text = page_text_for_signal_extraction(
            platform=platform,
            page_key=page_key,
            url=url,
            browser_text=str(page.get("text") or ""),
        )
        signals = extract_limit_signals(
            platform=platform,
            page_key=page_key,
            url=url,
            title=str(page.get("title") or ""),
            text=text,
        )
        pages.append(
            {
                "platform": platform,
                "page_key": page_key,
                "ok": bool(page.get("ok")),
                "url": sanitize_url(url),
                "title": normalize_space(page.get("title"))[:120],
                "navigation_error": page.get("navigation_error") or "",
                "error": page.get("error") or "",
                "signals": signals,
            }
        )
    summary = summarize_probe(checked_at=utc_now(), pages=pages, expected_platforms=expected)
    summary.update(
        {
            "publisher_browser": {
                "container": container,
                "pages_ok": bool(browser.get("ok")),
                "error": browser.get("error", ""),
            },
            "pages": pages,
            "actions_taken": [{"kind": "probed_publisher_platform_quotas"}],
        }
    )
    return redact_sensitive(summary)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Read-only probe for Fanqie/Qidian visible quota, rate-limit, and publish-gate signals."
    )
    parser.add_argument("--colima-profile", default="swarmbridged")
    parser.add_argument("--skip-browser", action="store_true")
    parser.add_argument(
        "--expected-platform",
        action="append",
        default=["fanqie", "qidian"],
        choices=sorted(DEFAULT_PAGES),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    report = build_report(parse_args(argv))
    print(json.dumps(report, ensure_ascii=False, sort_keys=True))
    return 0 if report.get("status") in {"quota_confirmed", "quota_incomplete"} else 1


if __name__ == "__main__":
    raise SystemExit(main())
