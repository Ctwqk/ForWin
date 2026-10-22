"""Prompts for structured world-engine calls."""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import Any

from forwin.book_state.knowledge import FACT_REF_PREFIX, KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES
from forwin.book_state.lexicon import BookLexicon, lexicon_to_payload
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.story_clock import StoryCalendar, calendar_to_payload
from forwin.protocol.book_state import WORLD_EDGE_TYPES, WorldNode
from forwin.world_engine.constraints import ConstraintIssue
from forwin.world_engine.types import AdjudicationResult, NEW_FACT_REF_PREFIX, TickContext


MAX_ADJUDICATION_PROMPT_CHARS = 60000


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


def _without_empty(value: Any) -> Any:
    if isinstance(value, dict):
        cleaned = {key: _without_empty(item) for key, item in value.items()}
        return {key: item for key, item in cleaned.items() if item not in (None, "", [], {})}
    if isinstance(value, list):
        return [_without_empty(item) for item in value]
    return value


def _fact_references(value: Any, *, is_ref: bool = False) -> set[str]:
    if isinstance(value, str):
        if value.startswith(FACT_REF_PREFIX):
            return {value.removeprefix(FACT_REF_PREFIX)}
        return {value} if is_ref else set()
    if isinstance(value, dict):
        return {ref for key, item in value.items()
                for ref in _fact_references(item, is_ref=key.endswith(("_ref", "_refs")))}
    if isinstance(value, list):
        return {ref for item in value for ref in _fact_references(item, is_ref=is_ref)}
    return set()


def build_adjudication_messages(
    *, context: TickContext, intents: Sequence[WorldNode], runtime: BookStateRuntime,
    lexicon: BookLexicon, calendar: StoryCalendar | None,
    feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    retry_feedback: Sequence[str] = (),
) -> list[dict]:
    def location_id(node: WorldNode) -> str:
        return resolve_world_node_location_id(runtime.world, node.id) or node.profile.get("location_id", "")

    def node_payload(node: WorldNode) -> dict:
        return _without_empty({
            **node.model_dump(mode="json", exclude_defaults=True, exclude_none=True, exclude={"state"}),
            "state": runtime.world.get_state(node.id) if runtime.world.get_node(node.id) else node.state,
            "location_id": location_id(node),
        })

    nodes = sorted(runtime.world.nodes_by_id.values(), key=lambda node: node.id)
    intent_ids = {node.id for node in intents}
    actor_refs = {ref for intent in intents
                  for ref in [intent.profile.get("owner_ref"), *intent.profile.get("target_refs", [])] if ref}
    locations = {location_id(node) for node in [*intents, *[node for node in nodes if node.id in actor_refs]]}
    locations.update(actor_refs & runtime.map.nodes_by_id.keys())
    locations.discard("")
    full_ids = intent_ids | actor_refs | locations
    full_ids.update(node.id for node in nodes if (
        location_id(node) in locations
        or (node.node_type in {"item", "resource"} and runtime.world.get_state(node.id).get("holder_id") in actor_refs)
        or node.node_type == "rule"
    ))
    edges = [edge for edge in sorted(runtime.world.edges_by_id.values(), key=lambda edge: edge.id)
             if edge.source_id in full_ids or edge.target_id in full_ids]
    map_edges = [edge for edge in sorted(runtime.map.edges_by_id.values(), key=lambda edge: edge.id)
                 if edge.from_node_id in locations or edge.to_node_id in locations]
    map_ids = locations | {ref for edge in map_edges for ref in (edge.from_node_id, edge.to_node_id)}
    payload = {
        "context": context.model_dump(mode="json"),
        "intents": [node_payload(node) for node in intents],
        "world_nodes": [node_payload(node) for node in nodes
                        if node.id in full_ids - intent_ids and node.node_type != "rule"],
        "world_node_index": [{"id": node.id, "node_type": node.node_type, "name": node.name}
                             for node in nodes if node.id not in full_ids],
        "world_edges": [edge.model_dump(mode="json", exclude_defaults=True) for edge in edges],
        "rules": [{"id": node.id, "hardness": node.profile.get("hardness"),
                   "statement": node.profile.get("statement")} for node in nodes if node.node_type == "rule"],
        "map_nodes": [node.model_dump(mode="json", exclude_defaults=True, exclude_none=True) for node in sorted(
            runtime.map.nodes_by_id.values(), key=lambda node: node.id,
        ) if node.id in map_ids],
        "map_node_index": [{"id": node.id, "node_type": node.node_type, "name": node.name} for node in sorted(
            runtime.map.nodes_by_id.values(), key=lambda node: node.id,
        ) if node.id not in map_ids],
        "map_edges": [edge.model_dump(mode="json", exclude_defaults=True) for edge in map_edges],
        "calendar": calendar_to_payload(calendar) if calendar is not None else None,
        "lexicon": lexicon_to_payload(lexicon),
    }
    related_refs = full_ids | map_ids | {edge.id for edge in [*edges, *map_edges]}
    facts = runtime.world.facts_by_id
    pending = _fact_references({key: value for key, value in payload.items() if not key.endswith("_index")})
    pending.update(fact.id for fact in facts.values()
                   if related_refs.intersection(fact.related_node_refs + fact.related_edge_refs))
    fact_payloads: dict[str, dict] = {}
    while pending:
        ref = pending.pop()
        if ref not in facts or ref in fact_payloads:
            continue
        fact_payloads[ref] = {"ref": f"{FACT_REF_PREFIX}{ref}", **facts[ref].model_dump(mode="json", exclude_defaults=True)}
        pending.update(_fact_references(fact_payloads[ref]) - fact_payloads.keys())
    payload["facts"] = [fact_payloads[ref] for ref in sorted(fact_payloads)]
    # Objective truth is sufficient for adjudication; no cognition overlay is serialized.
    allowed = {
        "knowledge_state": KNOWLEDGE_STATES, "knowledge_channel": KNOWLEDGE_CHANNELS,
        "edge_type": sorted(WORLD_EDGE_TYPES), "new_fact_ref_prefix": NEW_FACT_REF_PREFIX,
    }
    system = ("\n".join(ADJUDICATION_INSTRUCTIONS)
              + "\n" + json.dumps(AdjudicationResult.model_json_schema(), ensure_ascii=False)
              + "\nAllowed values:\n" + json.dumps(allowed, ensure_ascii=False))
    feedback_payload = {ref: [asdict(issue) for issue in issues] for ref, issues in (feedback or {}).items()
                        if ref in intent_ids and issues}
    suffix = ""
    if feedback_payload:
        suffix += "\n\nConstraint feedback by intent:\n" + json.dumps(feedback_payload, ensure_ascii=False)
    if retry_feedback:
        suffix += "\n\nRetry validation feedback:\n" + "\n".join(retry_feedback)
    user = json.dumps(payload, ensure_ascii=False) + suffix
    if len(user) > MAX_ADJUDICATION_PROMPT_CHARS:
        payload.pop("world_node_index")
        payload.pop("map_node_index")
        user = json.dumps(payload, ensure_ascii=False) + suffix
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
