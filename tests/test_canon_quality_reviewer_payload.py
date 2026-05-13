from __future__ import annotations

from forwin.protocol.context import ReviewContextPack
from forwin.protocol.writer import WriterOutput
from forwin.reviewer.hub import HistoricalReviewHub
from forwin.reviewer.llm_webnovel import LLMWebNovelReviewer


def test_llm_reviewer_payload_includes_deterministic_quality_report() -> None:
    context = ReviewContextPack(
        project_id="p1",
        project_title="质量门禁",
        chapter_number=1,
        chapter_plan_title="第一章",
        chapter_plan_one_line="测试",
        deterministic_quality_report={
            "blocking_signals": [
                {
                    "signal_id": "sig-1",
                    "signal_type": "placeholder_leakage",
                    "description": "正文包含相关人员。",
                    "evidence_refs": ["body:1-5"],
                }
            ],
            "warning_signals": [],
        },
    )
    writer_output = WriterOutput(
        project_id="p1",
        chapter_number=1,
        title="第一章",
        body="签名人：相关人员。",
        end_of_chapter_summary="测试",
    )

    payload = LLMWebNovelReviewer(enabled=False)._llm_payload(context, writer_output)

    assert payload["deterministic_quality_report"]["blocking_signals"][0]["signal_id"] == "sig-1"
    assert any(item["evidence_id"] == "canon_quality:sig-1" for item in payload["evidence_index"])


def test_review_hub_promotes_blocking_canon_quality_signal_to_repair_issue() -> None:
    issues = HistoricalReviewHub._canon_quality_issues(
        {
            "blocking_signals": [
                {
                    "signal_id": "sig-countdown",
                    "signal_type": "countdown_non_monotonic",
                    "severity": "error",
                    "target_scope": "ledger",
                    "description": "倒计时回升。",
                    "payload": {"repair_hint": "不要把同一个终端审计窗口延长。"},
                }
            ],
            "warning_signals": [],
        }
    )

    assert len(issues) == 1
    assert issues[0].reviewer == "canon_quality"
    assert issues[0].severity == "error"
    assert issues[0].evidence_refs == ["canon_quality:sig-countdown"]
    assert issues[0].suggested_fix == "不要把同一个终端审计窗口延长。"
