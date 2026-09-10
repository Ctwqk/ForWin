"""Frozen human-labelled body/prefix cases; tests form evidence, not live-model accuracy."""

import json

import pytest

from forwin.canon_quality.chapter_review_form.historical import (
    review_historical_chapter_with_form,
)
from forwin.protocol.writer import WriterOutput

DIMENSIONS = ("possession", "knowledge", "life_state", "time", "place", "obligations")
SCENARIOS = (
    (
        "possession",
        [{"subject": "林青", "item": "铜钥匙", "state": "lost", "regained": False}],
        "林青从腰间取出那把已经丢失且未曾找回的铜钥匙，打开了锁着的北门。",
        "丢失且未找回的钥匙不能在后章被直接取出使用。",
    ),
    (
        "knowledge",
        [{"subject": "林青", "unknown_fact": "暗号是霜月", "received_evidence": []}],
        "林青未经任何告知便准确报出了秘密暗号霜月。叙述明确说他没有猜测。",
        "前缀没有获得秘密的信息来源，后章却确认准确知道。",
    ),
    (
        "life_state",
        [{"subject": "老周", "state": "dead", "resurrection_allowed": False}],
        "同一条现实时间线里，早已死去的老周活着亲自推开门，绝非回忆或幻象。",
        "死亡且不允许复活的角色在现实后章主动行动。",
    ),
    (
        "time",
        {"day": 3, "clock": "18:00", "time_travel_allowed": False},
        "这是紧接上一刻的现在，时间却明确回到了第三天上午八点，没有倒叙或时间旅行。",
        "连续场景从第三天18点退到同日8点，且没有允许的时间机制。",
    ),
    (
        "place",
        {"subject": "林青", "location": "北城", "instant_travel_allowed": False},
        "一息之后林青已经站在千里之外的南港；没有离城、旅途、传送或场景省略。",
        "前缀在北城且不允许瞬移，后章瞬间抵达千里外。",
    ),
    (
        "obligations",
        [
            {
                "id": "debt",
                "deadline_chapter": 2,
                "summary": "本章结束前打开北门",
                "status": "active",
            }
        ],
        "第二章到此结束。北门依然锁着，开门的承诺没有兑现，也没有明确延期。",
        "本章到期的开门承诺明确未兑现或延期。",
    ),
)


@pytest.mark.parametrize(
    "dimension,prior,body,annotation", SCENARIOS, ids=[s[0] for s in SCENARIOS]
)
def test_annotated_dependency_conflict_has_exact_frozen_body_and_prefix_evidence(
    dimension, prior, body, annotation
):
    prefix = {
        "project_id": "book",
        "through_chapter": 1,
        "complete": True,
        "facts": {key: [] for key in DIMENSIONS},
        "character_rows": [],
        "countdown_rows": [],
        "open_signal_rows": [],
        "obligations": [],
    }
    prefix["facts"][dimension] = prior
    if dimension == "obligations":
        prefix["obligations"] = prior

    class LabelledModel:
        def complete_json(self, *, messages, **kwargs):
            payload = json.loads(messages[-1]["content"])
            # An independent labelled fixture binds the exact input pair; a
            # generic fail response cannot accidentally pass this regression.
            assert payload["chapter_body"] == body
            assert payload["prefix_context"]["facts"][dimension] == prior
            form = payload["form"]
            answers = {
                "project_id": "book",
                "chapter_number": 2,
                "form_schema_version": form["form_schema_version"],
                "chapter_summary": annotation,
                "characters": [],
                "countdowns": [],
                "open_signals": [],
                "obligations": [
                    {
                        "id": "debt",
                        "addressed": {
                            "value": "unaddressed",
                            "confidence": 0.99,
                            "evidence_quote": body,
                            "subject_of_quote": "debt",
                            "explanation": annotation,
                        },
                    }
                ]
                if dimension == "obligations"
                else [],
            }
            return {
                "answers": answers,
                "body_sha256": payload["body_sha256"],
                "prefix_sha256": payload["prefix_sha256"],
                "coverage_complete": True,
                "coverage": [
                    {
                        "dimension": key,
                        "status": "fail" if key == dimension else "pass",
                        "explanation": annotation
                        if key == dimension
                        else "全文没有这个维度的其他变化或冲突。",
                        "body_evidence": [
                            {"start": 0, "end": len(body), "quote": body}
                        ],
                        "prefix_refs": [f"/facts/{key}"],
                    }
                    for key in DIMENSIONS
                ],
            }

    result = review_historical_chapter_with_form(
        session=None,
        project_id="book",
        chapter_number=2,
        writer_output=WriterOutput(
            project_id="book",
            chapter_number=2,
            title="后续章节",
            body=body,
            end_of_chapter_summary="",
        ),
        llm_client=LabelledModel(),
        prefix_context=prefix,
        draft_id="draft",
    )
    check = next(item for item in result.checks if item.dimension == dimension)
    assert check.status == "fail", result.model_dump()
    assert check.explanation == annotation
    assert any(
        ref.startswith("body:") and ref.endswith(f"#0:{len(body)}")
        for ref in check.evidence_refs
    )
