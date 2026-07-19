from __future__ import annotations

from types import SimpleNamespace

import pytest
from pydantic import ValidationError

from forwin.checker.hard_floor import run_hard_floor
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.state_change import (
    DeliveredPayoffCandidate,
    EventCandidate,
    StateChangeCandidate,
)
from forwin.runtime.policy import RuntimePolicy
from forwin.writer.chapter_writer import ChapterWriter


def test_extraction_retry_uses_tail_window(monkeypatch) -> None:
    seen_bodies: list[str] = []
    writer = ChapterWriter(
        llm_client=SimpleNamespace(chat=lambda *args, **kwargs: "{}")
    )
    body = (
        "开头铺垫" * 500 + "中段推进" * 500 + "章末他当场获得三十万赔偿，敌人失去资格。"
    )

    def fake_chat_json(prompt: str, **kwargs):
        seen_bodies.append(prompt)
        if len(seen_bodies) == 1:
            raise RuntimeError("primary failed")
        if "三十万赔偿" in prompt:
            return {"new_events": [{"summary": "获得赔偿"}]}
        raise RuntimeError("missing tail facts")

    monkeypatch.setattr(writer, "_chat_json", fake_chat_json)
    result = writer._extract_structured_part(
        label="state_event_extraction",
        prompt_builder=lambda context, title, chapter_body: chapter_body,
        context=SimpleNamespace(),
        chapter_title="第一章",
        chapter_body=body,
        primary_temperature=0.25,
        primary_max_tokens=100,
        retry_temperature=0.2,
        retry_max_tokens=100,
    )

    assert result["new_events"][0]["summary"] == "获得赔偿"
    assert any("三十万赔偿" in item for item in seen_bodies[1:])


def test_state_event_retry_merges_valid_empty_head_with_tail_payoff(
    monkeypatch,
) -> None:
    seen_bodies: list[str] = []
    writer = ChapterWriter(
        llm_client=SimpleNamespace(chat=lambda *args, **kwargs: "{}")
    )
    body = "开头铺垫" * 500 + "中段推进" * 500 + "章末陈默获得追件视野。"

    def fake_chat_json(prompt: str, **kwargs):
        seen_bodies.append(prompt)
        if len(seen_bodies) == 1:
            raise RuntimeError("primary failed")
        if "获得追件视野" not in prompt:
            return {
                "state_changes": [],
                "new_events": [],
                "delivered_payoffs": [],
            }
        return {
            "state_changes": [],
            "new_events": [
                {
                    "summary": "陈默获得追件视野",
                    "significance": "major",
                    "involved_entity_names": ["陈默"],
                    "roles": ["protagonist"],
                }
            ],
            "delivered_payoffs": [
                {
                    "entity_name": "陈默",
                    "category": "power",
                    "direction": "gain",
                    "before_state": "无追件视野",
                    "after_state": "获得追件视野",
                    "evidence_quote": "陈默获得追件视野",
                }
            ],
        }

    monkeypatch.setattr(writer, "_chat_json", fake_chat_json)
    result = writer._extract_structured_part(
        label="state_event_extraction",
        prompt_builder=lambda context, title, chapter_body: chapter_body,
        context=SimpleNamespace(),
        chapter_title="第一章",
        chapter_body=body,
        primary_temperature=0.25,
        primary_max_tokens=100,
        retry_temperature=0.2,
        retry_max_tokens=100,
        required_list_models={
            "state_changes": StateChangeCandidate,
            "new_events": EventCandidate,
            "delivered_payoffs": DeliveredPayoffCandidate,
        },
    )

    assert len(seen_bodies) == 4
    assert result["new_events"][0]["summary"] == "陈默获得追件视野"
    assert result["delivered_payoffs"][0]["after_state"] == "获得追件视野"


def test_state_event_window_merge_uses_domain_keys_and_global_limits() -> None:
    models = {
        "state_changes": StateChangeCandidate,
        "new_events": EventCandidate,
        "delivered_payoffs": DeliveredPayoffCandidate,
    }

    def event(target: str, summary: str) -> dict[str, object]:
        return {
            "summary": summary,
            "significance": "major",
            "involved_entity_names": ["陈默", target],
            "roles": ["protagonist", "mentioned"],
        }

    def payoff(label: str) -> dict[str, str]:
        return {
            "entity_name": "陈默",
            "category": "power",
            "direction": "gain",
            "before_state": f"尚未获得{label}",
            "after_state": f"获得{label}",
            "evidence_quote": f"陈默获得{label}",
        }

    merged = ChapterWriter._merge_required_model_lists(
        [
            {
                "state_changes": [
                    {
                        "entity_name": "陈默",
                        "entity_kind": "character",
                        "field": "location",
                        "old_value": "门岗",
                        "new_value": "走廊",
                        "reason": "进入追查",
                    }
                ],
                "new_events": [event("零号柜", "陈默发现零号柜。")],
                "delivered_payoffs": [payoff("能力A")],
            },
            {
                "state_changes": [
                    {
                        "entity_name": "陈默",
                        "entity_kind": "character",
                        "field": "location",
                        "old_value": "走廊",
                        "new_value": "零号柜",
                        "reason": "抵达目标",
                    }
                ],
                "new_events": [
                    event("零号柜", "陈默发现零号柜"),
                    event("目标B", "陈默确认目标B"),
                    event("目标C", "陈默确认目标C"),
                    event("目标D", "陈默确认目标D"),
                    event("目标E", "陈默确认目标E"),
                ],
                "delivered_payoffs": [
                    payoff("能力A"),
                    payoff("能力B"),
                    payoff("能力C"),
                    payoff("能力D"),
                    payoff("能力E"),
                ],
            },
        ],
        models,
    )

    assert len(merged["state_changes"]) == 1
    assert merged["state_changes"][0]["new_value"] == "零号柜"
    assert len(merged["new_events"]) == 4
    assert merged["new_events"][0]["summary"] == "陈默发现零号柜"
    assert len(merged["delivered_payoffs"]) == 4


def test_event_window_identity_uses_proposition_not_participant_set() -> None:
    healed = EventCandidate(
        summary="陈默的伤口已经完全愈合。",
        significance="major",
        involved_entity_names=["陈默"],
        roles=["protagonist"],
    )
    unlocked = EventCandidate(
        summary="陈默解锁临时追件视野。",
        significance="major",
        involved_entity_names=["陈默"],
        roles=["protagonist"],
    )
    healed_without_roles = EventCandidate(
        summary="陈默的伤口已经完全愈合",
        significance="major",
        involved_entity_names=["陈默", "苏晴"],
        roles=[],
    )

    healed_key = ChapterWriter._structured_list_identity("new_events", healed)
    unlocked_key = ChapterWriter._structured_list_identity("new_events", unlocked)
    witnessed_key = ChapterWriter._structured_list_identity(
        "new_events",
        healed_without_roles,
    )

    assert healed_key != unlocked_key
    assert healed_key == witnessed_key

    merged = ChapterWriter._merge_required_model_lists(
        [
            {
                "state_changes": [],
                "new_events": [healed.model_dump(mode="json")],
                "delivered_payoffs": [],
            },
            {
                "state_changes": [],
                "new_events": [healed_without_roles.model_dump(mode="json")],
                "delivered_payoffs": [],
            },
        ],
        {
            "state_changes": StateChangeCandidate,
            "new_events": EventCandidate,
            "delivered_payoffs": DeliveredPayoffCandidate,
        },
    )
    merged_event = merged["new_events"][0]
    protagonist_index = merged_event["involved_entity_names"].index("陈默")
    assert merged_event["roles"][protagonist_index] == "protagonist"


def test_writer_output_rebases_generic_numeric_title_to_context_chapter() -> None:
    writer = ChapterWriter(
        llm_client=SimpleNamespace(chat=lambda *args, **kwargs: "{}")
    )

    output = writer._writer_output_from_dict(
        SimpleNamespace(project_id="project-1", chapter_number=29),
        {
            "title": "第11章",
            "body": "林夜继续追踪潮门。",
            "end_of_chapter_summary": "林夜抵达潮门。",
        },
    )

    assert output.title == "第29章"


def test_writer_output_restores_flattened_payoff_quote_to_verbatim_body() -> None:
    writer = ChapterWriter(
        llm_client=SimpleNamespace(chat=lambda *args, **kwargs: "{}")
    )
    context = ChapterContextPack(
        project_id="project-1",
        project_title="测试项目",
        premise="测试前提",
        genre="都市玄幻",
        setting_summary="测试设定",
        chapter_number=1,
        chapter_plan_title="第一章",
        chapter_plan_one_line="周行签收续命丹。",
        chapter_goals=["完成签收"],
        must_not_reveal=[],
    )
    payoff_paragraph = (
        "周行走到二楼拐角的时候，手机忽然自动播报了一条消息，声音不大，"
        "但在安静的楼道里格外清晰：\n\n"
        "“续命丹已签收。见习路权临时开启。”"
    )
    body = f"{payoff_paragraph}\n\n周行抬头看向新出现的通道。"

    output = writer._writer_output_from_dict(
        context,
        {
            "title": "第一章",
            "body": body,
            "end_of_chapter_summary": "周行签收续命丹并开启见习路权。",
            "new_events": [
                {
                    "summary": "周行开启见习路权。",
                    "significance": "major",
                    "involved_entity_names": ["周行"],
                    "roles": ["protagonist"],
                }
            ],
            "delivered_payoffs": [
                {
                    "entity_name": "周行",
                    "category": "power",
                    "direction": "gain",
                    "before_state": "没有见习路权",
                    "after_state": "见习路权临时开启",
                    "evidence_quote": (
                        "周行走到二楼拐角的时候，手机忽然自动播报了一条消息，"
                        "声音不大，但在安静的楼道里格外清晰："
                        "“续命丹已签收。见习路权临时开启。”"
                    ),
                }
            ],
        },
    )

    assert output.delivered_payoffs[0].evidence_quote == payoff_paragraph
    assert output.delivered_payoffs[0].evidence_quote in body

    result = run_hard_floor(
        writer_output=output,
        context_pack=context,
        repo=None,
        project_id=context.project_id,
        chapter_number=context.chapter_number,
        policy=RuntimePolicy.for_profile("pulp"),
    )

    assert result.metadata["pulp_beat"]["visible_payoff_present"] is True


def test_payoff_quote_restoration_requires_one_exact_non_whitespace_match() -> None:
    paragraph = "周行收到提示：\n\n“见习路权临时开启。”"
    flattened = "周行收到提示：“见习路权临时开启。”"

    assert (
        ChapterWriter._unique_whitespace_equivalent_body_slice(
            f"{paragraph}\n{paragraph}",
            flattened,
        )
        == flattened
    )
    assert (
        ChapterWriter._unique_whitespace_equivalent_body_slice(
            paragraph,
            "周行收到提示：“见习路权永久开启。”",
        )
        == "周行收到提示：“见习路权永久开启。”"
    )


def test_payoff_quote_length_ignores_restored_body_whitespace() -> None:
    writer = ChapterWriter(
        llm_client=SimpleNamespace(chat=lambda *args, **kwargs: "{}")
    )
    flattened = "周行" + ("甲" * 220) + "见习路权临时开启"
    formatted = "\n".join(
        flattened[offset : offset + 10] for offset in range(0, len(flattened), 10)
    )
    assert len(flattened) <= 240 < len(formatted)

    output = writer._writer_output_from_dict(
        SimpleNamespace(project_id="project-1", chapter_number=1),
        {
            "title": "第一章",
            "body": formatted,
            "delivered_payoffs": [
                {
                    "entity_name": "周行",
                    "category": "power",
                    "direction": "gain",
                    "before_state": "见习路权未开启",
                    "after_state": "见习路权临时开启",
                    "evidence_quote": flattened,
                }
            ],
        },
    )

    assert output.delivered_payoffs[0].evidence_quote == formatted


def test_payoff_quote_still_rejects_more_than_240_content_characters() -> None:
    payload = {
        "entity_name": "周行",
        "category": "power",
        "direction": "gain",
        "before_state": "见习路权未开启",
        "after_state": "见习路权临时开启",
        "evidence_quote": "周行" + ("甲" * 239),
    }

    with pytest.raises(ValidationError, match="at most 240 non-whitespace"):
        DeliveredPayoffCandidate(**payload)

    quote_schema = DeliveredPayoffCandidate.model_json_schema()["properties"][
        "evidence_quote"
    ]
    assert quote_schema["minLength"] == 4
    assert quote_schema["maxLength"] == 240


def test_writer_output_rejects_non_string_body() -> None:
    writer = ChapterWriter(
        llm_client=SimpleNamespace(chat=lambda *args, **kwargs: "{}")
    )

    with pytest.raises(ValidationError):
        writer._writer_output_from_dict(
            SimpleNamespace(project_id="project-1", chapter_number=1),
            {"title": "第一章", "body": {"text": "正文"}},
        )
