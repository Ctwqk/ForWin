"""Prompts for structured world-engine calls."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict

from forwin.book_state.knowledge import FACT_REF_PREFIX, KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES
from forwin.book_state.lexicon import BookLexicon, lexicon_to_payload
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import AGENDA_STATUSES, IMPACT_LEVELS, INTENT_ACTION_TYPES, INTENT_STATUSES
from forwin.book_state.story_clock import StoryCalendar, calendar_to_payload
from forwin.protocol.book_state import WORLD_EDGE_TYPES, WorldNode
from forwin.world_engine.constraints import ConstraintIssue
from forwin.world_engine.types import AdjudicationResult, NEW_FACT_REF_PREFIX, TickContext


ADJUDICATION_INSTRUCTIONS = (
    "你是世界规则的结算者。一次结算输入的全部到点幕后意图，返回 JSON 对象，不输出正文或 Markdown。",
    "每个输入 intent_id 必须恰好有一个 outcome，result 只能是 resolved 或 disrupted，附 summary 和 effects。"
    "失败或受阻也必须返回 disrupted，不得省略意图，不得替其他意图结算。",
    "依据客观状态、资源持有、位置与规则的 hardness/statement 决定结果。隐藏事实也是世界事实，"
    "可用于结算；不要为了读者体验改变事实。不得生成 reader 的认知变化或将 reader 放进 known_by。",
    "effects 只描述本次实际发生的变化，不照抄 expected_effects。引用只能来自输入的世界节点、地图节点或事实。"
    "同一 effects 内的新事实可通过给出的新事实引用前缀加 key 引用；不得创造新行动者。",
    "所有 at、depart_at、arrive_at 都是自纪元起的整数分钟，须在 window_start 到 now 之间；"
    "地图 travel_time 的单位是小时。时长描述只使用本书日历单位或 lexicon 别名。",
    "使用 seed 使同一输入的裁决稳定。约束反馈按 intent_id 修正对应结果；所有空列表返回 []。"
    "完整输出结构如下，额外的允许值列表补充 schema 中未列出的枚举。",
)


def build_adjudication_messages(
    *, context: TickContext, intents: Sequence[WorldNode], runtime: BookStateRuntime,
    lexicon: BookLexicon, calendar: StoryCalendar | None,
    feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    retry_feedback: Sequence[str] = (),
) -> list[dict]:
    def node_payload(node: WorldNode) -> dict:
        return {
            **node.model_dump(mode="json"),
            "state": runtime.world.get_state(node.id) if runtime.world.get_node(node.id) else node.state,
            "location_id": resolve_world_node_location_id(runtime.world, node.id),
        }

    nodes = sorted(runtime.world.nodes_by_id.values(), key=lambda node: node.id)
    payload = {
        "context": context.model_dump(mode="json"),
        "intents": [node_payload(node) for node in intents],
        "world_nodes": [node_payload(node) for node in nodes],
        "world_edges": [edge.model_dump(mode="json") for edge in sorted(
            runtime.world.edges_by_id.values(), key=lambda edge: edge.id,
        )],
        "rules": [{"id": node.id, "hardness": node.profile.get("hardness"),
                   "statement": node.profile.get("statement")} for node in nodes if node.node_type == "rule"],
        "facts": [{"ref": f"{FACT_REF_PREFIX}{fact.id}", **fact.model_dump(mode="json")}
                  for fact in sorted(runtime.world.facts_by_id.values(), key=lambda fact: fact.id)],
        "map_nodes": [node.model_dump(mode="json") for node in sorted(
            runtime.map.nodes_by_id.values(), key=lambda node: node.id,
        )],
        "map_edges": [edge.model_dump(mode="json") for edge in sorted(
            runtime.map.edges_by_id.values(), key=lambda edge: edge.id,
        )],
        "calendar": calendar_to_payload(calendar) if calendar is not None else None,
        "lexicon": lexicon_to_payload(lexicon),
    }
    # Objective truth is sufficient for adjudication; no cognition overlay is serialized.
    allowed = {
        "action_type": INTENT_ACTION_TYPES, "intent_status": INTENT_STATUSES,
        "agenda_status": AGENDA_STATUSES, "impact_level": IMPACT_LEVELS,
        "knowledge_state": KNOWLEDGE_STATES, "knowledge_channel": KNOWLEDGE_CHANNELS,
        "edge_type": sorted(WORLD_EDGE_TYPES), "new_fact_ref_prefix": NEW_FACT_REF_PREFIX,
    }
    system = ("\n".join(ADJUDICATION_INSTRUCTIONS)
              + "\n" + json.dumps(AdjudicationResult.model_json_schema(), ensure_ascii=False)
              + "\nAllowed values:\n" + json.dumps(allowed, ensure_ascii=False))
    user = json.dumps(payload, ensure_ascii=False)
    if feedback:
        user += "\n\nConstraint feedback by intent:\n" + json.dumps({
            ref: [asdict(issue) for issue in issues] for ref, issues in feedback.items()
        }, ensure_ascii=False)
    if retry_feedback:
        user += "\n\nRetry validation feedback:\n" + "\n".join(retry_feedback)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
