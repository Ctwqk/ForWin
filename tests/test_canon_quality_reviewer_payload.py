from __future__ import annotations

from forwin.protocol.context import ReviewContextPack
from forwin.protocol.writer import WriterOutput
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
