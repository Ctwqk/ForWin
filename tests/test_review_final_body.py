from __future__ import annotations

import json

from forwin.protocol.context import ReviewContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.scene import SceneOutput
from forwin.protocol.state_change import TimeAdvance
from forwin.protocol.writer import WriterOutput
from forwin.review.llm_webnovel import LLMWebNovelReviewer


def _stitched_chapter() -> tuple[ReviewContextPack, WriterOutput]:
    context = ReviewContextPack(
        project_id="final-body",
        project_title="封档",
        chapter_number=10,
        chapter_plan_title="第10章",
        chapter_plan_one_line="摘报必须在封档收讫前送达。",
        canon_invariants=[
            {
                "invariant_key": "book_state_rule:receipt",
                "current_value": "收讫后不得补录。",
                "constraints": {"immutable_definition": True},
            }
        ],
    )
    # The stitch fixes the scene's chronology in the middle of the final body.
    output = WriterOutput(
        project_id=context.project_id,
        chapter_number=10,
        title="第10章",
        body=(
            "案房等候封档。" * 100
            + "抄录完成：辰正二刻。离津：辰正三刻。送达：巳初一刻。"
            + "收讫：巳初二刻。"
            + "账册随即封存。" * 100
        ),
        end_of_chapter_summary="摘报在收讫前送达，账册封存。",
        scene_outputs=[
            SceneOutput(
                scene_no=2,
                scene_objective="及时送达摘报",
                text="抄录完成：巳初二刻。离津：巳初三刻。",
                micro_summary="巳初三刻离津。",
            )
        ],
        time_advance=TimeAdvance(
            new_time_label="巳初二刻",
            duration_description="辰正二刻抄录，辰正三刻离津，巳初一刻送达后收讫。",
        ),
    )
    return context, output


def test_review_prompt_reads_final_body_instead_of_pre_stitch_scenes() -> None:
    context, output = _stitched_chapter()
    reviewer = LLMWebNovelReviewer(enabled=False)

    payload = reviewer._llm_payload(context, output)
    messages = reviewer._llm_review_messages(
        payload=payload,
        evidence_ids=[item["evidence_id"] for item in payload["evidence_index"]],
    )
    prompt = "\n".join(item["content"] for item in messages)

    assert output.body in prompt
    assert "离津：巳初三刻" not in prompt
    assert "巳初三刻离津" not in prompt
    assert "scene:2" not in {item["evidence_id"] for item in payload["evidence_index"]}
    assert payload["world"]["canon_invariants"] == context.canon_invariants
    assert payload["draft"]["time_advance"] == output.time_advance.model_dump(mode="json")
    assert output.scene_outputs[0].text == "抄录完成：巳初二刻。离津：巳初三刻。"


def test_repair_escalation_reads_the_same_final_body() -> None:
    context, output = _stitched_chapter()

    payload = LLMWebNovelReviewer._repair_escalation_payload(
        context=context,
        writer_output=output,
        review=ReviewVerdict(verdict="pass", issues=[]),
        repair_attempts=[],
    )
    prompt = json.dumps(payload, ensure_ascii=False)

    assert output.body in prompt
    assert "离津：巳初三刻" not in prompt
    assert "巳初三刻离津" not in prompt


def test_final_body_chronology_error_still_produces_blocking_review() -> None:
    context, output = _stitched_chapter()
    output = output.model_copy(
        update={"body": output.body.replace("离津：辰正三刻", "离津：巳初三刻")}
    )
    reviewer = LLMWebNovelReviewer(enabled=False)
    payload = reviewer._llm_payload(context, output)
    evidence_ids = {item["evidence_id"] for item in payload["evidence_index"]}

    verdict = reviewer._verdict_from_payload(
        payload={
            "verdict": "fail",
            "issues": [
                {
                    "rule_name": "chronology",
                    "severity": "error",
                    "description": "巳初三刻离津，不可能于巳初一刻送达。",
                    "issue_type": "continuity",
                    "target_scope": "chapter",
                    "evidence_refs": ["draft:body"],
                }
            ],
        },
        context=context,
        writer_output=output,
        fallback_on_invalid=False,
        allowed_evidence_ids=evidence_ids,
    )

    assert verdict.verdict == "fail"
    assert verdict.recommended_action == "rewrite"
    assert verdict.issues[0].severity == "error"
    assert verdict.issues[0].evidence_refs == ["draft:body"]
