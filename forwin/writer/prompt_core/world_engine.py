"""Prompts for structured world-engine calls."""

from __future__ import annotations

import copy
import json
from collections.abc import Mapping, Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from forwin.book_state.knowledge import FACT_REF_PREFIX, KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES
from forwin.book_state.lexicon import BookLexicon, lexicon_to_payload
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import AGENDA_STATUSES, IMPACT_LEVELS, INTENT_ACTION_TYPES
from forwin.book_state.story_clock import StoryCalendar, calendar_to_payload
from forwin.protocol.book_state import WORLD_EDGE_TYPES, FactNode, MapNode, WorldEdge, WorldNode
from forwin.world_engine.constraints import ConstraintIssue
from forwin.world_engine.types import (
    AGENDA_PRIORITY_MAX, AGENDA_PRIORITY_MIN, NEW_AGENDA_REF, NEW_FACT_REF_PREFIX, ActorTurnBatch,
    AdjudicationResult, TickContext,
)

if TYPE_CHECKING:
    from forwin.world_engine.actor_turns import ActorView


MAX_ADJUDICATION_PROMPT_CHARS = 60000
MAX_ACTOR_TURN_PROMPT_CHARS = 60000
MAX_CONFLICT_REVIEW_PROMPT_CHARS = 60000
# What a chapter-plan goal keeps when the conflict review prompt is still over its guard.
MAX_CONFLICT_REVIEW_GOAL_CHARS = 200


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


ACTOR_TURN_INSTRUCTIONS = (
    "你为每个输入行动者各做一次回合决策，只依据该行动者自己的视图，返回 JSON 对象，不输出正文或 Markdown。",
    "视图只含该行动者知道或相信的内容：facts 按它持有的认知状态理解，public_facts 是它默认知道的公开事实，"
    "两者都可能与真相不符；不要假设视图之外的人物、地点或事实。",
    "turns 中每个输入 actor_id 恰好一项，含 0 到 max_new_intents_per_actor 个 new_intents、agenda_changes 与 "
    "next_turn_in；不得替其他行动者决策。",
    "引用只能使用该行动者视图中的 id：自身、agendas、pending_intents、holdings、entities、clocks、facts 与 "
    "public_facts 的 ref；location_id 只能是当前位置或 reachable_places 中的地点。不得触碰 boundary_refs 中的引用，"
    "也不要重复 vetoes 中被否决的行动。pending_intents 是它已有的计划，不要再次提出与之相同或重复的意图。",
    "agenda_ref 用自己的 agenda id，或留空；新建议程时 agenda_changes 中恰好一项的 agenda_ref 为允许值给出的新议程标记"
    "并附 goal，意图的 agenda_ref 写同一标记即指向它。priority 只能取允许值给出的范围。",
    "scheduled_in 与 next_turn_in 的 unit 只使用本书日历单位或 lexicon 别名；意图地点须在 scheduled_in 之内到达，"
    "reachable_places 的 minutes 是到达所需分钟数。expected_effects 只是预期变化的预览，不得包含 reader 的认知变化。"
    "约束反馈与重试反馈按 actor_id 修正对应结果；所有空列表返回 []。完整输出结构如下，额外的允许值列表补充 schema 中"
    "未列出的枚举。",
)


CONFLICT_REVIEW_INSTRUCTIONS = (
    "你替用户审一条世界与规划的冲突：世界推演想做的变化与当前规划相抵触。从 options 中选择一个处理选项，"
    "返回 JSON 对象，不输出正文或 Markdown。",
    "只依据输入：冲突记录、涉及引用在检测章节时的世界状态、当前卷规划与接下来的章节计划。"
    "不要假设输入之外的人物、地点或事实，不要编造补丁或新的计划。",
    "选项含义：veto_world 否决这次世界变化，行动者之后重新行动；plot_responds 接受变化，由下一个未写章节回应；"
    "accept_with_obligation 接受变化，并记下一条之后必须兑现的叙事义务；amend_plan 接受变化并修改规划，须由用户确认。",
    "优先保住当前卷的目标、赌注与回报方向，以及接下来章节计划仍然可写；规划不受损时尊重世界的因果。",
    "option 必须是 options 中的一项；rationale 用一两句话说明理由；evidence 只列输入中支持判断的引用 id 或字段名；"
    "risk_level 表示所选选项对规划的风险。完整输出结构如下。",
)

# The chapter-plan goal plot_responds adds to the next unwritten chapter; the conflict summary follows it.
PLOT_RESPONSE_GOAL_PREFIX = "回应已经发生的世界变化："


def plot_response_goal(summary: str) -> str:
    """The goal that has the next unwritten chapter respond to an accepted world change."""
    return f"{PLOT_RESPONSE_GOAL_PREFIX}{summary.strip()}"


# The BookState model each conflict-snapshot slice kind dumps, for dropping its default-valued fields.
_SLICE_MODELS = {"world_node": WorldNode, "world_edge": WorldEdge, "map_node": MapNode, "fact": FactNode}


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


def build_actor_turn_messages(
    *, context: TickContext, views: Sequence[ActorView], calendar: StoryCalendar | None,
    lexicon: BookLexicon, max_new_intents: int,
    feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    retry_feedback: Sequence[str] = (),
) -> list[dict]:
    """One batch of actor turns: the shared calendar and lexicon, and each actor's own view only.

    Actors batched together share one prompt, so between them isolation rests on the instructions;
    a batch size of 1 keeps every call to a single actor's knowledge.
    """
    actors = [_without_empty(view.model_dump(mode="json")) for view in views]
    payload = {
        "context": context.model_dump(mode="json"),
        "max_new_intents_per_actor": max_new_intents,
        "calendar": calendar_to_payload(calendar) if calendar is not None else None,
        "lexicon": lexicon_to_payload(lexicon),
        "actors": actors,
    }
    allowed = {
        "action_type": INTENT_ACTION_TYPES, "impact_level": IMPACT_LEVELS, "agenda_status": AGENDA_STATUSES,
        "agenda_priority": [AGENDA_PRIORITY_MIN, AGENDA_PRIORITY_MAX],
        "knowledge_state": KNOWLEDGE_STATES, "knowledge_channel": KNOWLEDGE_CHANNELS,
        "edge_type": sorted(WORLD_EDGE_TYPES), "new_agenda_ref": NEW_AGENDA_REF,
        "new_fact_ref_prefix": NEW_FACT_REF_PREFIX,
    }
    system = ("\n".join(ACTOR_TURN_INSTRUCTIONS)
              + "\n" + json.dumps(ActorTurnBatch.model_json_schema(), ensure_ascii=False)
              + "\nAllowed values:\n" + json.dumps(allowed, ensure_ascii=False))
    actor_ids = {view.actor_id for view in views}
    feedback_payload = {ref: [asdict(issue) for issue in issues] for ref, issues in (feedback or {}).items()
                        if ref in actor_ids and issues}
    suffix = ""
    if feedback_payload:
        suffix += "\n\nConstraint feedback by actor:\n" + json.dumps(feedback_payload, ensure_ascii=False)
    if retry_feedback:
        suffix += "\n\nRetry validation feedback:\n" + "\n".join(retry_feedback)
    user = json.dumps(payload, ensure_ascii=False) + suffix
    if len(user) > MAX_ACTOR_TURN_PROMPT_CHARS:
        # Public facts an actor holds no record of go first; what it learned stays.
        for actor in actors:
            actor.pop("public_facts", None)
        user = json.dumps(payload, ensure_ascii=False) + suffix
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def _compact_slice(item: dict) -> dict:
    model = _SLICE_MODELS.get(item.get("kind"))
    if model is None:
        return item
    return {**item, "value": model.model_validate(item["value"]).model_dump(mode="json", exclude_defaults=True)}


def build_conflict_review_messages(
    *, context_snapshot: Mapping[str, Any], options: Sequence[str], output_schema: Mapping[str, Any],
) -> list[dict]:
    """One conflict for the reviewer: its record, the involved refs' state, the volume plan and the next chapters.

    Over the guard, the BookState slices drop their default-valued fields first, then each chapter-plan
    goal is cut to MAX_CONFLICT_REVIEW_GOAL_CHARS; the caller rejects a prompt that is still over.
    """
    payload = {**copy.deepcopy(dict(context_snapshot)), "options": list(options)}
    system = "\n".join(CONFLICT_REVIEW_INSTRUCTIONS) + "\n" + json.dumps(output_schema, ensure_ascii=False)
    user = json.dumps(payload, ensure_ascii=False)
    if len(user) > MAX_CONFLICT_REVIEW_PROMPT_CHARS and "book_state" in payload:
        payload["book_state"] = [_compact_slice(item) for item in payload["book_state"]]
        user = json.dumps(payload, ensure_ascii=False)
    if len(user) > MAX_CONFLICT_REVIEW_PROMPT_CHARS and "upcoming_chapters" in payload:
        for chapter in payload["upcoming_chapters"]:
            chapter["goals"] = [goal[:MAX_CONFLICT_REVIEW_GOAL_CHARS] for goal in chapter.get("goals", [])]
        user = json.dumps(payload, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
