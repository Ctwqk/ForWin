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
    r"(每日|每天|每小时|小时内|当日|今日).{0,24}(发布|发表|发文|更新|章节).{0,24}"
    r"([0-9０-９]+|一|二|两|三|四|五|六|七|八|九|十)",
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
    return signals


def _visible_account_blockers(signals: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        item
        for item in signals
        if item.get("severity") == "blocker"
        and str(item.get("page_key") or "") in {"dashboard", "create_work"}
    ]


def summarize_probe(
    *,
    checked_at: str,
    pages: list[dict[str, Any]],
    expected_platforms: list[str],
) -> dict[str, Any]:
    platforms: dict[str, dict[str, Any]] = {}
    all_blockers: list[dict[str, Any]] = []
    numeric_publish_quota_confirmed = False

    for platform in expected_platforms:
        platforms[platform] = {
            "page_count": 0,
            "ok_page_count": 0,
            "signal_count": 0,
            "categories": [],
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
            if category == "numeric_publish_frequency_quota":
                numeric_publish_quota_confirmed = True
        entry["categories"] = sorted(categories)
        blockers = _visible_account_blockers(signals)
        entry["visible_account_blockers"].extend(blockers)
        all_blockers.extend(blockers)

    if all_blockers:
        status = "blocked"
        publish_true_gate = {
            "allowed": False,
            "reason": "visible_account_blocker",
            "blocker_count": len(all_blockers),
        }
    elif numeric_publish_quota_confirmed:
        status = "quota_confirmed"
        publish_true_gate = {
            "allowed": True,
            "reason": "numeric_publish_frequency_quota_confirmed",
            "blocker_count": 0,
        }
    else:
        status = "quota_incomplete"
        publish_true_gate = {
            "allowed": False,
            "reason": "numeric_publish_frequency_quota_unconfirmed",
            "blocker_count": 0,
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
                result["text"] = page.locator("body").inner_text(timeout=6000)[:12000]
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
        {"platform": platform, **page}
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
