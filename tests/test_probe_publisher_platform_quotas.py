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


def test_default_pages_include_fanqie_official_longform_rules() -> None:
    fanqie_pages = {item["page_key"]: item["url"] for item in quotas.DEFAULT_PAGES["fanqie"]}

    assert (
        fanqie_pages["official_longform_publish_rules"]
        == "https://fanqienovel.com/writer/zone/article/7639950766869839897"
    )


def test_default_pages_include_qidian_readonly_account_endpoints() -> None:
    qidian_pages = {item["page_key"]: item["url"] for item in quotas.DEFAULT_PAGES["qidian"]}

    assert (
        qidian_pages["account_can_create_work_endpoint"]
        == "https://write.qq.com/ccauthorweb/novel/iscancreatenovel"
    )
    assert (
        qidian_pages["day_words_calendar_endpoint"]
        == "https://write.qq.com/ccauthorweb/daywords/getMonthDayWords"
    )
    assert (
        qidian_pages["editor_frontend_static"]
        == "https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js"
    )
    assert (
        qidian_pages["editor_frontend_source_map"]
        == "https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js.map"
    )


def test_extract_limit_signals_adds_fanqie_longform_image_table_rules() -> None:
    signals = quotas.extract_limit_signals(
        platform="fanqie",
        page_key="official_longform_publish_rules",
        url="https://fanqienovel.com/writer/zone/article/7639950766869839897?secret=hidden",
        title="长篇网文发文规则（第二版）上线通知",
        text="""
        一、规则详情
        二、规则解读
        1. 可创建长篇作品数：可以新创建的长篇作品数量
        2. 可更新长篇作品数：可以发布新章节的长篇作品数量
        3. 可提交发布字数：可以新增提交发布（含修改）的字数额度
        """,
    )

    by_category = {item["category"]: item for item in signals}
    assert by_category["fanqie_longform_create_quota"]["limits"] == {
        "daily_create_longform_works": 1,
        "monthly_create_longform_works": 3,
    }
    assert by_category["fanqie_longform_update_work_quota"]["limits"] == {
        "lv0_lv1_daily_update_longform_works": 1,
        "lv2_lv3_daily_update_longform_works": 3,
        "lv4_plus_daily_update_longform_works": 5,
    }
    assert by_category["fanqie_longform_word_quota"]["limits"] == {
        "lv0_lv1_daily_submitted_words_lt": 10000,
        "lv2_lv3_daily_submitted_words_lt": 20000,
        "lv4_plus_daily_submitted_words_lt": 50000,
        "lv0_lv1_monthly_submitted_words_lt": 250000,
        "lv2_lv3_monthly_submitted_words_lt": 500000,
        "lv4_plus_monthly_submitted_words_lt": 1000000,
    }
    assert by_category["fanqie_longform_word_quota"]["quota_confirmed"] is True
    assert by_category["fanqie_longform_word_quota"]["source_evidence"] == "official_article_image_table"
    assert "secret=hidden" not in json.dumps(signals, ensure_ascii=False)


def test_extract_limit_signals_keeps_qidian_account_endpoint_as_current_state() -> None:
    signals = quotas.extract_limit_signals(
        platform="qidian",
        page_key="account_can_create_work_endpoint",
        url="https://write.qq.com/ccauthorweb/novel/iscancreatenovel",
        title="",
        text='{"returnCode":2000,"returnMsg":"成功","result":true,"info":"成功"}',
    )

    by_category = {item["category"]: item for item in signals}
    assert by_category["current_account_create_available"]["severity"] == "info"
    assert by_category["current_account_create_available"]["current_value"] is True
    assert by_category["current_account_create_available"]["quota_confirmed"] is False


def test_extract_limit_signals_does_not_treat_qidian_day_words_as_quota() -> None:
    signals = quotas.extract_limit_signals(
        platform="qidian",
        page_key="day_words_calendar_endpoint",
        url="https://write.qq.com/ccauthorweb/daywords/getMonthDayWords",
        title="",
        text="""
        {"returnCode":2000,"returnMsg":"月历","result":{"listMonthInfo":[
        {"dayWordsShowTxt":"当日发布 0 字","date":"2026-07-02","pubChapters":0}
        ]}}
        """,
    )

    by_category = {item["category"]: item for item in signals}
    assert "numeric_publish_frequency_quota" not in by_category
    assert by_category["current_publish_counter"]["severity"] == "info"
    assert by_category["current_publish_counter"]["quota_confirmed"] is False


def test_extract_limit_signals_adds_qidian_batch_import_quota_from_editor_static() -> None:
    signals = quotas.extract_limit_signals(
        platform="qidian",
        page_key="editor_frontend_static",
        url="https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js?v=secret",
        title="",
        text="""
        A.get("/ccauthorweb/bookchapterimport/getuploadnumoftheday?CBID=".concat(t))
          .then(function(e){e<50?a.props.onClick():window.$.lightTip.error("今日上传已达到50个文件上限，请明日再批量上传")});
        t>10&&(t=10,window.$.lightTip.error("单次最多上传10个文件，可分批再上传剩余文件"));
        """,
    )

    by_category = {item["category"]: item for item in signals}
    assert by_category["qidian_batch_import_file_quota"]["severity"] == "rule"
    assert by_category["qidian_batch_import_file_quota"]["quota_confirmed"] is True
    assert by_category["qidian_batch_import_file_quota"]["limits"] == {
        "daily_batch_import_files": 50,
        "single_batch_import_files": 10,
    }
    assert by_category["qidian_batch_import_file_quota"]["source_evidence"] == "official_editor_frontend_static"
    assert "v=secret" not in json.dumps(signals, ensure_ascii=False)


def test_extract_limit_signals_adds_qidian_publish_path_source_map_evidence() -> None:
    source_map = {
        "sources": [
            "components/publishDialog/index.js",
            "api/publishChapter.js",
            "api/getLastFourPublishTime.js",
        ],
        "sourcesContent": [
            """
            getLastFourPublishTime(window._CBID).then(res => {
              this.setState({ recentPublishTimes: res })
            })
            <label className='psl-title pst-title'>常设时间</label>
            if (targetChapterType === 1 && inViewChapter.actualwords < 1000) {
              $.lightTip.error('无法发布，VIP章节字数必须大于等于1000字。')
            }
            """,
            """
            return fetch.post("/Chapter/publishChapter", qs.stringify({
              status: isSchedule ? 5 : 2,
              chapterword: words,
            }))
            """,
            """
            export default function getDraftChapters(CBID) {
              return fetch.get(`/Chapter/getLastFourChapterPublishTime?CBID=${CBID}`)
            }
            """,
        ],
    }

    signals = quotas.extract_limit_signals(
        platform="qidian",
        page_key="editor_frontend_source_map",
        url="https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js.map?secret=hidden",
        title="",
        text=json.dumps(source_map, ensure_ascii=False),
    )

    by_category = {item["category"]: item for item in signals}
    assert by_category["qidian_publish_frontend_path_observed"]["severity"] == "info"
    assert by_category["qidian_publish_frontend_path_observed"]["quota_confirmed"] is False
    assert (
        by_category["qidian_publish_frontend_path_observed"]["source_evidence"]
        == "official_editor_frontend_source_map"
    )
    assert "secret=hidden" not in json.dumps(signals, ensure_ascii=False)


def test_summarize_probe_does_not_treat_qidian_source_map_evidence_as_publish_quota() -> None:
    source_map_signals = quotas.extract_limit_signals(
        platform="qidian",
        page_key="editor_frontend_source_map",
        url="https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js.map",
        title="",
        text=json.dumps(
            {
                "sources": [
                    "components/publishDialog/index.js",
                    "api/publishChapter.js",
                    "api/getLastFourPublishTime.js",
                ],
                "sourcesContent": [
                    "getLastFourPublishTime(window._CBID) recentPublishTimes 常设时间",
                    'fetch.post("/Chapter/publishChapter", qs.stringify({status: isSchedule ? 5 : 2, chapterword: words}))',
                    "getLastFourChapterPublishTime",
                ],
            },
            ensure_ascii=False,
        ),
    )

    report = quotas.summarize_probe(
        checked_at="2026-07-02T06:30:00Z",
        pages=[
            {
                "platform": "qidian",
                "page_key": "editor_frontend_source_map",
                "ok": True,
                "signals": source_map_signals,
            }
        ],
        expected_platforms=["qidian"],
    )

    assert report["platforms"]["qidian"]["publish_quota_confirmed"] is False
    assert report["publish_true_gate"]["allowed"] is False
    assert report["status"] == "quota_incomplete"


def test_summarize_probe_does_not_treat_qidian_batch_import_quota_as_publish_quota() -> None:
    qidian_signals = quotas.extract_limit_signals(
        platform="qidian",
        page_key="editor_frontend_static",
        url="https://write.qq.com/portal/public/editor/static/js/main.49f0b475.chunk.js",
        title="",
        text='getuploadnumoftheday e<50 "今日上传已达到50个文件上限" "单次最多上传10个文件"',
    )

    report = quotas.summarize_probe(
        checked_at="2026-07-02T06:10:00Z",
        pages=[
            {
                "platform": "qidian",
                "page_key": "editor_frontend_static",
                "ok": True,
                "signals": qidian_signals,
            }
        ],
        expected_platforms=["qidian"],
    )

    assert report["platforms"]["qidian"]["publish_quota_confirmed"] is False
    assert report["status"] == "quota_incomplete"
    assert report["publish_true_gate"]["allowed"] is False


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


def test_summarize_probe_requires_quota_confirmation_for_each_expected_platform() -> None:
    fanqie_signals = quotas.extract_limit_signals(
        platform="fanqie",
        page_key="official_longform_publish_rules",
        url="https://fanqienovel.com/writer/zone/article/7639950766869839897",
        title="长篇网文发文规则（第二版）上线通知",
        text="可创建长篇作品数 可更新长篇作品数 可提交发布字数",
    )
    qidian_signals = quotas.extract_limit_signals(
        platform="qidian",
        page_key="official_chapter_word_faq",
        url="https://write.qq.com/ask/qfoycqb",
        title="章节字数说明",
        text="章节内容不能为空，单章章节字数不超过20000，但建议单章章节字数控制在2000-6000字内。",
    )

    report = quotas.summarize_probe(
        checked_at="2026-07-02T05:30:00Z",
        pages=[
            {
                "platform": "fanqie",
                "page_key": "official_longform_publish_rules",
                "ok": True,
                "signals": fanqie_signals,
            },
            {
                "platform": "qidian",
                "page_key": "official_chapter_word_faq",
                "ok": True,
                "signals": qidian_signals,
            },
        ],
        expected_platforms=["fanqie", "qidian"],
    )

    assert report["platforms"]["fanqie"]["publish_quota_confirmed"] is True
    assert report["platforms"]["qidian"]["publish_quota_confirmed"] is False
    assert report["publish_true_gate"]["allowed"] is False
    assert report["publish_true_gate"]["unconfirmed_platforms"] == ["qidian"]
    assert report["status"] == "quota_incomplete"


def test_summarize_probe_allows_single_platform_when_its_quota_is_confirmed() -> None:
    fanqie_signals = quotas.extract_limit_signals(
        platform="fanqie",
        page_key="official_longform_publish_rules",
        url="https://fanqienovel.com/writer/zone/article/7639950766869839897",
        title="长篇网文发文规则（第二版）上线通知",
        text="可创建长篇作品数 可更新长篇作品数 可提交发布字数",
    )

    report = quotas.summarize_probe(
        checked_at="2026-07-02T05:30:00Z",
        pages=[
            {
                "platform": "fanqie",
                "page_key": "official_longform_publish_rules",
                "ok": True,
                "signals": fanqie_signals,
            },
        ],
        expected_platforms=["fanqie"],
    )

    assert report["status"] == "quota_confirmed"
    assert report["publish_true_gate"]["allowed"] is True
    assert report["publish_true_gate"]["confirmed_platforms"] == ["fanqie"]


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


def test_browser_quota_pages_snapshot_uses_larger_text_limit_for_qidian_static(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    captured: dict[str, object] = {}

    def fake_run_command_with_input(args, *, timeout: float, input_text: str):
        captured["input_text"] = input_text
        return {"ok": True, "stdout": json.dumps({"ok": True, "pages": []})}

    monkeypatch.setattr(quotas, "run_command_with_input", fake_run_command_with_input)

    quotas.browser_quota_pages_snapshot(
        SimpleNamespace(
            skip_browser=False,
            publisher_browser_container="container-1",
            colima_profile="swarmbridged",
            expected_platform=["qidian"],
        )
    )

    page_specs = json.loads(str(captured["input_text"]))
    by_key = {item["page_key"]: item for item in page_specs}
    assert by_key["dashboard"]["text_limit"] == quotas.DEFAULT_BROWSER_TEXT_LIMIT
    assert by_key["editor_frontend_static"]["text_limit"] > 300_000
    assert by_key["editor_frontend_source_map"]["text_limit"] > 800_000
