#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

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
            "page_key": "official_version_notes",
            "url": "https://write.qq.com/portal/version",
        },
    ],
}


def sanitize_url(url: Any) -> str:
    parsed = urlsplit(str(url or ""))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, "", ""))


def normalize_space(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


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

    if all_blockers:
        status = "blocked"
        publish_true_gate = {
            "allowed": False,
            "reason": "visible_account_blocker",
            "blocker_count": len(all_blockers),
            "confirmed_platforms": confirmed_platforms,
            "unconfirmed_platforms": unconfirmed_platforms,
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
    else:
        status = "quota_incomplete"
        publish_true_gate = {
            "allowed": False,
            "reason": "numeric_publish_frequency_quota_unconfirmed",
            "blocker_count": 0,
            "confirmed_platforms": confirmed_platforms,
            "unconfirmed_platforms": unconfirmed_platforms,
        }

    return redact_sensitive(
        {
            "status": status,
            "checked_at": checked_at,
            "platforms": platforms,
            "publish_true_gate": publish_true_gate,
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
        signals = extract_limit_signals(
            platform=str(page.get("platform") or ""),
            page_key=str(page.get("page_key") or ""),
            url=str(page.get("url") or page.get("requested_url") or ""),
            title=str(page.get("title") or ""),
            text=str(page.get("text") or ""),
        )
        pages.append(
            {
                "platform": page.get("platform") or "",
                "page_key": page.get("page_key") or "",
                "ok": bool(page.get("ok")),
                "url": sanitize_url(page.get("url") or page.get("requested_url") or ""),
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
