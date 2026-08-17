from __future__ import annotations

import forwin.review.llm_webnovel as llm_webnovel
from forwin.protocol.context import ReviewContextPack
from forwin.protocol.state_change import StateChangeCandidate
from forwin.protocol.writer import LoreCandidate, WriterOutput
from forwin.review.draft_service import DraftReviewService
from forwin.review.llm_webnovel import LLMWebNovelReviewer


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


def test_llm_reviewer_compares_rule_claims_with_exact_canon_invariant() -> None:
    invariant = {
        "invariant_key": "book_state_rule:rule-transit-protocol",
        "kind": "active_rule",
        "label": "通行协议",
        "current_value": {"public_version": "三印同亮，门右移一格。"},
        "constraints": {"immutable_definition": True},
    }
    context = ReviewContextPack(
        project_id="p1",
        project_title="规则评审",
        chapter_number=6,
        chapter_plan_title="第六章",
        chapter_plan_one_line="再次验证通行协议",
        canon_invariants=[invariant],
    )
    writer_output = WriterOutput(
        project_id="p1",
        chapter_number=6,
        title="第六章",
        body="众人宣称三印同亮后门会右移两格。",
        end_of_chapter_summary="通行协议再次触发。",
        state_changes=[
            StateChangeCandidate(
                entity_name="通行协议",
                entity_kind="rule",
                field="public_version",
                old_value="三印同亮，门右移一格。",
                new_value="三印同亮，门右移两格。",
                reason="正文宣称规则发生变化",
            )
        ],
        lore_candidates=[
            LoreCandidate(
                subject_name="通行协议",
                subject_type="rule",
                description="三印同亮，门右移两格。",
                evidence_refs=["body:门会右移两格"],
            )
        ],
    )
    reviewer = LLMWebNovelReviewer(enabled=False)

    payload = reviewer._llm_payload(context, writer_output)
    messages = reviewer._llm_review_messages(
        payload=payload,
        evidence_ids=[item["evidence_id"] for item in payload["evidence_index"]],
    )

    assert payload["world"]["canon_invariants"] == [invariant]
    invariant_evidence = next(
        item
        for item in payload["evidence_index"]
        if item["evidence_id"]
        == "canon_invariant:book_state_rule:rule-transit-protocol"
    )
    assert "三印同亮，门右移一格。" in invariant_evidence["summary"]
    assert {
        "source": "state_change",
        "subject_name": "通行协议",
        "field": "public_version",
        "claim": "三印同亮，门右移两格。",
    } in payload["draft"]["rule_claims"]
    assert {
        "source": "lore_candidate",
        "subject_name": "通行协议",
        "field": "public_version",
        "claim": "三印同亮，门右移两格。",
    } in payload["draft"]["rule_claims"]
    prompt = "\n".join(item["content"] for item in messages)
    assert "canon invariant 优先于章节计划、摘要和本章声称" in prompt
    assert "静默改写规则定义必须 fail" in prompt


def test_llm_reviewer_does_not_prune_immutable_canon_invariants() -> None:
    invariants = [
        {
            "invariant_key": f"book_state_rule:rule-{index}",
            "kind": "active_rule",
            "label": f"规则{index}",
            "current_value": {"public_version": f"精确定义{index}"},
            "constraints": {"immutable_definition": True},
        }
        for index in range(1, 14)
    ]
    context = ReviewContextPack(
        project_id="p1",
        project_title="规则容量",
        chapter_number=20,
        chapter_plan_title="容量检查",
        chapter_plan_one_line="逐项遵守既有规则。",
        canon_invariants=invariants,
    )
    writer_output = WriterOutput(
        project_id="p1",
        chapter_number=20,
        title="容量检查",
        body="正文没有改写任何规则。",
        end_of_chapter_summary="规则保持稳定。",
    )

    payload = LLMWebNovelReviewer(enabled=False)._llm_payload(
        context,
        writer_output,
    )

    assert payload["world"]["canon_invariants"] == invariants
    assert any(
        item["evidence_id"] == "canon_invariant:book_state_rule:rule-13"
        and "精确定义13" in item["summary"]
        for item in payload["evidence_index"]
    )


def test_draft_review_promotes_blocking_canon_quality_signal_to_repair_issue() -> None:
    issues = DraftReviewService._canon_quality_issues(
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


def test_llm_reviewer_timeout_can_be_overridden_by_env(monkeypatch) -> None:
    timeouts: list[float] = []

    def fake_call_chat_compat(*args, **kwargs):  # noqa: ANN002, ANN003, ANN202
        timeouts.append(kwargs["timeout_seconds"])
        return '{"verdict":"pass","issues":[],"review_summary":"ok"}'

    monkeypatch.setenv("FORWIN_WEBNOVEL_REVIEW_TIMEOUT_SECONDS", "77")
    monkeypatch.setattr(llm_webnovel, "call_chat_compat", fake_call_chat_compat)

    context = ReviewContextPack(
        project_id="p1",
        project_title="质量门禁",
        chapter_number=1,
        chapter_plan_title="第一章",
        chapter_plan_one_line="测试",
    )
    writer_output = WriterOutput(
        project_id="p1",
        chapter_number=1,
        title="第一章",
        body="林陈找到备份光盘。",
        end_of_chapter_summary="测试",
    )

    verdict = LLMWebNovelReviewer(llm_client=object()).review(context, writer_output)

    assert verdict is not None
    assert timeouts == [77.0]
