from __future__ import annotations

from types import SimpleNamespace

from forwin.protocol.state_change import (
    DeliveredPayoffCandidate,
    EventCandidate,
    StateChangeCandidate,
)
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
