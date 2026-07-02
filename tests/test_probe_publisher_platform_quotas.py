from __future__ import annotations

import json
from types import SimpleNamespace

import pytest

import scripts.probe_publisher_platform_quotas as quotas


def test_extract_limit_signals_classifies_current_account_create_limit() -> None:
    signals = quotas.extract_limit_signals(
        platform="fanqie",
        page_key="create_work",
        url="https://fanqienovel.com/main/writer/create?ticket=secret",
        title="创建作品",
        text="""
        创建作品
        已到达当日创建作品上限，请明日再试。
        作品简介请输入50-500字以内的作品简介。
        """,
    )

    create_limit = [item for item in signals if item["category"] == "create_book_rate_limit"]
    assert create_limit
    assert create_limit[0]["severity"] == "blocker"
    assert create_limit[0]["matched_keyword"] == "当日创建作品上限"
    assert create_limit[0]["source_url"] == "https://fanqienovel.com/main/writer/create"
    assert "ticket=secret" not in json.dumps(signals, ensure_ascii=False)


def test_extract_limit_signals_keeps_platform_rule_thresholds_as_rules() -> None:
    signals = quotas.extract_limit_signals(
        platform="qidian",
        page_key="official_faq",
        url="https://write.qq.com/ask/qfoycqb",
        title="章节字数说明",
        text="章节不能为空，单章不得超过20000字，建议2000-6000字。满100000字可申请签约。",
    )

    by_category = {item["category"]: item for item in signals}
    assert by_category["chapter_word_limit"]["severity"] == "rule"
    assert by_category["chapter_word_recommendation"]["severity"] == "rule"
    assert by_category["signing_threshold"]["severity"] == "rule"
    assert all(len(item["snippet"]) <= quotas.MAX_SNIPPET_CHARS for item in signals)


def test_summarize_probe_marks_publish_true_unverified_without_numeric_frequency_quota() -> None:
    report = quotas.summarize_probe(
        checked_at="2026-07-02T05:30:00Z",
        pages=[
            {
                "platform": "qidian",
                "page_key": "dashboard",
                "ok": True,
                "signals": quotas.extract_limit_signals(
                    platform="qidian",
                    page_key="dashboard",
                    url="https://write.qq.com/portal/dashboard",
                    title="工作台",
                    text="工作台 作品管理 章节不能为空，单章不得超过20000字。",
                ),
            }
        ],
        expected_platforms=["qidian"],
    )

    assert report["status"] == "quota_incomplete"
    assert report["publish_true_gate"]["allowed"] is False
    assert report["publish_true_gate"]["reason"] == "numeric_publish_frequency_quota_unconfirmed"
    assert report["platforms"]["qidian"]["visible_account_blockers"] == []


def test_build_report_redacts_browser_page_text(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(quotas, "utc_now", lambda: "2026-07-02T05:30:00Z")
    monkeypatch.setattr(
        quotas,
        "publisher_browser_container_snapshot",
        lambda args: {"ok": True, "container_id": "container-1"},
    )
    monkeypatch.setattr(
        quotas,
        "browser_quota_pages_snapshot",
        lambda args: {
            "ok": True,
            "pages": [
                {
                    "platform": "fanqie",
                    "page_key": "create_work",
                    "ok": True,
                    "url": "https://fanqienovel.com/main/writer/create?session=secret",
                    "title": "创建作品",
                    "text": "已到达当日创建作品上限 raw page text should not be copied wholesale",
                }
            ],
        },
    )

    report = quotas.build_report(
        SimpleNamespace(
            colima_profile="swarmbridged",
            expected_platform=["fanqie"],
            skip_browser=False,
        )
    )

    serialized = json.dumps(report, ensure_ascii=False)
    assert "raw page text should not be copied wholesale" not in serialized
    assert "session=secret" not in serialized
    assert report["status"] == "blocked"
    assert report["platforms"]["fanqie"]["visible_account_blockers"][0]["category"] == "create_book_rate_limit"


def test_browser_quota_pages_snapshot_sends_page_specs_to_container(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_command_with_input(args, *, timeout: float, input_text: str):
        captured["args"] = args
        captured["timeout"] = timeout
        captured["input_text"] = input_text
        return {
            "ok": True,
            "stdout": json.dumps(
                {
                    "ok": True,
                    "pages": [
                        {
                            "platform": "qidian",
                            "page_key": "dashboard",
                            "ok": True,
                            "url": "https://write.qq.com/portal/dashboard",
                            "title": "工作台",
                            "text": "工作台",
                        }
                    ],
                },
                ensure_ascii=False,
            ),
        }

    monkeypatch.setattr(quotas, "run_command_with_input", fake_run_command_with_input)

    result = quotas.browser_quota_pages_snapshot(
        SimpleNamespace(
            skip_browser=False,
            publisher_browser_container="container-1",
            colima_profile="swarmbridged",
            expected_platform=["qidian"],
        )
    )

    assert result["ok"] is True
    assert result["pages"][0]["platform"] == "qidian"
    page_specs = json.loads(str(captured["input_text"]))
    assert page_specs[0]["platform"] == "qidian"
    assert page_specs[0]["url"] == "https://write.qq.com/portal/dashboard"
    assert "container-1" in captured["args"]
