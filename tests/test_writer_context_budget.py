"""Retrieval retention follows actual Writer input, not stored view size."""

from copy import deepcopy

import pytest

from forwin.protocol.context import ChapterContextPack, MemorySnippet, RepairContract
from forwin.protocol.scene import ScenePlan
from forwin.retrieval.broker_core.broker import RetrievalBroker
from forwin.writer.prompt_core.builders import (
    build_preview_chapter_prompt,
    build_scene_breakdown_prompt,
    build_scene_generation_prompt,
    build_scene_stitch_prompt,
    build_single_chapter_draft_prompt,
)


def context():
    return ChapterContextPack(
        project_title="归航",
        premise="核实交接记录",
        genre="冒险",
        setting_summary="港口",
        chapter_number=3,
        chapter_plan_title="复核",
        chapter_plan_one_line="追查保管权",
        chapter_goals=["查找凭据"],
        previous_chapter_summaries=["钥匙交给船长。", "布袋已封口，开封必须登记。"],
        active_entities=[
            {
                "entity_id": "captain",
                "kind": "character",
                "name": "船长",
                "description": "保管人",
                "current_state": {},
            }
        ],
        active_relations=[
            {
                "source_name": "船长",
                "target_name": "船员",
                "relation_type": "同事",
                "description": "共同值班",
            }
        ],
        retrieved_memories=[
            MemorySnippet(
                chapter_number=1, title="交接", summary="守卫签收。", excerpt="原文"
            )
        ],
        map_context={
            "map_node_count": 1,
            "review_graph": {
                "map_nodes": [{"id": "port", "name": "港口", "metadata": {}}]
            },
        },
        arc_payoff_map={},
        band_delight_schedule={
            "band_id": "band1",
            "chapter_start": 1,
            "chapter_end": 5,
        },
        canon_quality_context={"is_final_chapter": True},
    )


def prompts(pack):
    # Prompt telemetry intentionally mutates its quality view. Comparisons
    # must not accidentally perform that mutation on the estimator's input.
    return [
        build_single_chapter_draft_prompt(pack.model_copy(deep=True)),
        build_preview_chapter_prompt(pack.model_copy(deep=True)),
        build_scene_breakdown_prompt(pack.model_copy(deep=True)),
        build_scene_generation_prompt(
            pack.model_copy(deep=True), ScenePlan(scene_no=1, objective="查证")
        ),
        build_scene_stitch_prompt(pack.model_copy(deep=True), []),
    ]


def visible_contexts(pack):
    """Extract real context blocks, excluding each call's task/output wrapper."""
    single, preview, breakdown, scene, stitch = [
        messages[1]["content"] for messages in prompts(pack)
    ]
    return [
        single.split("\n\n【输出要求】", 1)[0],
        preview.split("\n\n【输出要求】", 1)[0],
        breakdown.split("\n\n", 1)[1].split("\n\n请将本章拆成", 1)[0],
        scene.split("\n\n【Scene 任务】", 1)[0],
        stitch.split("\n\n请把以下 scenes", 1)[0],
    ]


@pytest.mark.parametrize(
    "field",
    [
        "map_book_state",
        "map_metadata",
        "entity_state",
        "relation",
        "memory_excerpt",
        "arc_payoff",
        "band_template",
        "future_audit",
        "quality_detail",
    ],
)
def test_unrendered_fields_do_not_evict_history(field):
    plain = context()
    inflated = plain.model_copy(deep=True)
    extra = "未显示元数据" * 2000
    if field == "map_book_state":
        inflated.map_context["book_state"] = {"source": extra}
    elif field == "map_metadata":
        inflated.map_context["review_graph"]["map_nodes"][0]["metadata"]["source"] = (
            extra
        )
    elif field == "entity_state":
        inflated.active_entities[0].current_state["source"] = extra
    elif field == "relation":
        inflated.active_relations[0].description = extra
    elif field == "memory_excerpt":
        inflated.retrieved_memories[0].excerpt = extra
    elif field == "arc_payoff":
        inflated.arc_payoff_map.awe_kit = [extra]
    elif field == "band_template":
        inflated.band_delight_schedule.band_contract_template["source"] = extra
    elif field == "future_audit":
        inflated.future_plan_audit_summary["source"] = extra
    else:
        inflated.canon_quality_context["unrendered_audit"] = extra
    before = inflated.model_dump(mode="json")
    assert prompts(plain) == prompts(inflated)
    assert RetrievalBroker._estimate_chars(plain) == RetrievalBroker._estimate_chars(
        inflated
    )
    broker = RetrievalBroker(
        context_budget_chars=max(map(len, visible_contexts(plain)))
    )
    trimmed = broker._trim_pack(inflated)
    assert trimmed.previous_chapter_summaries == plain.previous_chapter_summaries
    assert trimmed.retrieved_memories == inflated.retrieved_memories
    assert prompts(trimmed) == prompts(plain)
    assert inflated.model_dump(mode="json") == before


@pytest.mark.parametrize("final_chapter", [False, True])
def test_estimate_matches_largest_actual_context_and_does_not_write_telemetry(final_chapter):
    pack = context()
    pack.canon_quality_context = {"is_final_chapter": final_chapter}
    before = deepcopy(pack.model_dump(mode="json"))
    assert RetrievalBroker._estimate_chars(pack) == max(
        map(len, visible_contexts(pack))
    )
    assert pack.model_dump(mode="json") == before
    assert "form_prompt_constraints_remaining" not in pack.canon_quality_context


@pytest.mark.parametrize("field", ["repair", "summary", "map_route"])
def test_rendered_evidence_still_consumes_budget(field):
    plain = context()
    extra = plain.model_copy(deep=True)
    if field == "repair":
        extra.repair_contract = RepairContract(must_preserve=["布袋封口不能静默消失。"])
    elif field == "summary":
        extra.previous_chapter_summaries[-1] += "两名见证人已经签署登记表。"
    else:
        extra.map_context["review_graph"]["map_edges"] = [
            {
                "from_node_id": "port",
                "to_node_id": "store",
                "travel_time": 0.5,
                "visibility": "public",
                "metadata": {"source_control": "需要登记"},
            }
        ]
    assert max(map(len, visible_contexts(extra))) > max(
        map(len, visible_contexts(plain))
    )
    assert RetrievalBroker._estimate_chars(extra) > RetrievalBroker._estimate_chars(
        plain
    )


def test_pruning_recomputes_visible_cost_until_budget_or_retention_floor():
    pack = context()
    pack.retrieved_memories = [
        MemorySnippet(
            chapter_number=i,
            title=f"记录{i}",
            summary="已登记。",
            excerpt="未显示" * 2000,
        )
        for i in (1, 2)
    ]
    mandatory = pack.model_copy(update={"retrieved_memories": []})
    budget = max(map(len, visible_contexts(mandatory)))
    result = RetrievalBroker(context_budget_chars=budget)._trim_pack(pack)
    assert not result.retrieved_memories
    assert result.previous_chapter_summaries == pack.previous_chapter_summaries
    assert max(map(len, visible_contexts(result))) <= budget


def test_soft_budget_preserves_mandatory_contract_and_last_summary():
    pack = context()
    pack.repair_contract = RepairContract(
        must_preserve=["必须保留" * 2000], must_not_reveal=["禁揭示"]
    )
    result = RetrievalBroker(context_budget_chars=10)._trim_pack(pack)
    assert result.repair_contract == pack.repair_contract
    assert result.previous_chapter_summaries == pack.previous_chapter_summaries[-1:]
    assert "禁揭示" in visible_contexts(result)[0]
    assert RetrievalBroker._estimate_chars(result) > 10
