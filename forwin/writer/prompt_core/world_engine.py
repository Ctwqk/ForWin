"""Prompts for structured world-engine calls."""

from __future__ import annotations

import copy
import json
from collections.abc import Collection, Mapping, Sequence
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from forwin.book_state.knowledge import FACT_REF_PREFIX, KNOWLEDGE_CHANNELS, KNOWLEDGE_STATES
from forwin.book_state.lexicon import BookLexicon, lexicon_to_payload
from forwin.book_state.runtime import BookStateRuntime, resolve_world_node_location_id
from forwin.book_state.schema import ACTOR_NODE_TYPES, AGENDA_STATUSES, IMPACT_LEVELS, INTENT_ACTION_TYPES
from forwin.book_state.story_clock import StoryCalendar, calendar_to_payload
from forwin.protocol.book_state import WORLD_EDGE_TYPES, FactNode, MapNode, WorldEdge, WorldNode
from forwin.world_engine.constraints import ENDED_STATUSES, ConstraintIssue
from forwin.world_engine.types import (
    AGENDA_PRIORITY_MAX, AGENDA_PRIORITY_MIN, NEW_AGENDA_REF, NEW_FACT_REF_PREFIX, ActorTurnBatch,
    AdjudicationResult, DriftResult, TickContext,
)

if TYPE_CHECKING:
    from forwin.world_engine.actor_turns import ActorView


MAX_ADJUDICATION_PROMPT_CHARS = 60000
# The event facts an adjudication shows, up to this many in all: each due intent's newest ones first, up to
# its guaranteed share; older events stay in canon but out of the prompt.
MAX_ADJUDICATION_EVENT_FACTS = 20
MAX_ADJUDICATION_EVENT_FACTS_PER_INTENT = 5
MAX_ACTOR_TURN_PROMPT_CHARS = 60000
MAX_DRIFT_PROMPT_CHARS = 60000
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
    "同一 effects 内的新事实可通过给出的新事实引用前缀加 key 引用；只有当意图的结算确实需要时，"
    "可在全部 outcomes 中新建至多 max_new_actors 个次要行动者（new_actors，仅 character 或 group，"
    "以 actor:new:<key> 引用）。名字须符合本书 lexicon 的称谓风格且不得与已有名字或别名重复；"
    "location_id 必须是地图节点，affiliation_refs 只可引用已有 group、faction 或 organization。",
    "所有 at、depart_at、arrive_at 都是自纪元起的整数分钟，须在 window_start 到 now 之间；"
    "地图 travel_time 的单位是小时。时长描述只使用本书日历单位或 lexicon 别名。",
    "使用 seed 使同一输入的裁决稳定。约束反馈按 intent_id 修正对应结果；所有空列表返回 []。"
    "完整输出结构如下，额外的允许值列表补充 schema 中未列出的枚举。",
)


ACTOR_TURN_INSTRUCTIONS = (
    "你为每个输入行动者各做一次回合决策，只依据该行动者自己的视图，返回 JSON 对象，不输出正文或 Markdown。",
    "视图只含该行动者知道或相信的内容：facts 按它持有的认知状态理解，public_facts 是它默认知道的公开事实，"
    "两者都可能与真相不符；不要假设视图之外的人物、地点或事实。",
    "turns 中每个输入 actor_id 恰好一项，含 0 到 max_new_intents_per_actor 个 new_intents、至多 "
    "max_agenda_changes_per_actor 个 agenda_changes 与 next_turn_in；不得替其他行动者决策。",
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

# The review of a chapter conflict: repaired chapter text still breaks a protection or a milestone of the plan.
CHAPTER_CONFLICT_REVIEW_INSTRUCTIONS = (
    "你替用户审一条正文与规划的冲突：本章正文经过修复，仍然违反规划设下的叙事保护或里程碑。从 options 中选择一个处理选项，"
    "返回 JSON 对象，不输出正文或 Markdown。",
    "只依据输入：冲突记录（proposed_change.boundary 是被违反的约束与正文证据）、涉及引用在检测章节时的世界状态、"
    "当前卷规划与接下来的章节计划。不要假设输入之外的人物、地点或事实，不要编造正文、补丁或新的计划。",
    "选项含义：rewrite_chapter 把正文退回修复，改写到遵守这条约束；amend_plan 接受正文并解除这条约束，须由用户确认。",
    "优先保住当前卷的目标、赌注与回报方向，以及接下来章节计划仍然可写；正文改写后仍能完成本章计划时选择 rewrite_chapter。",
    "option 必须是 options 中的一项；rationale 用一两句话说明理由；evidence 只列输入中支持判断的引用 id 或字段名；"
    "risk_level 表示所选选项对规划的风险。完整输出结构如下。",
)

# The chapter-plan goal plot_responds adds to the next unwritten chapter; the change's public events follow it.
PLOT_RESPONSE_GOAL_PREFIX = "回应已经发生的世界变化："
# The response to an accepted change with no public event: it names nothing the reader may not know yet.
PLOT_RESPONSE_GENERIC_GOAL = (
    "回应幕后已经发生的世界变化：只写人物能察觉到的后果，不点明尚未公开的人物、事件或真相。"
)
# The response to an accepted onstage slip: nothing has happened yet; the next chapter stages the due intent.
ONSTAGE_SLIP_RESPONSE_GOAL = "让已经到期、必须在台前发生的人物行动在本章登场，不要再次推迟。"


def plot_response_goal(summary: str) -> str:
    """The goal that has the next unwritten chapter respond to an accepted world change's public events."""
    return f"{PLOT_RESPONSE_GOAL_PREFIX}{summary.strip()}"


def world_change_response(public_event_summaries: Sequence[str], *, onstage_slip: bool) -> str:
    """The writer-visible text of an accepted world change: a plan goal, an obligation summary or payoff test.

    Only the change's public event summaries reach the writer (Ruling P32); without one it gets a template.
    """
    if onstage_slip:
        return ONSTAGE_SLIP_RESPONSE_GOAL
    summaries = [text for summary in public_event_summaries if (text := summary.strip())]
    return plot_response_goal(" ".join(summaries)) if summaries else PLOT_RESPONSE_GENERIC_GOAL


# What an actor learns when the tick sends its turn back or vetoes its plan: the broken rule's code,
# never the reason behind it or anything the actor does not know.
ACTOR_CONSTRAINT_FEEDBACK = "The last proposal broke the world rule {code}; propose a different action."
ACTOR_VETO_SUMMARY = "The world did not let this plan go ahead."


# The BookState model each conflict-snapshot slice kind dumps, for dropping its default-valued fields.
_SLICE_MODELS = {"world_node": WorldNode, "world_edge": WorldEdge, "map_node": MapNode, "fact": FactNode}
# Live entities an adjudication shows in full when they stand where a due intent's owner or targets are.
_COLOCATED_TYPES = frozenset({*ACTOR_NODE_TYPES, "item", "resource", "site_state"})
# Records of what happened or was planned: an adjudication shows them only as a due intent.
_RECORD_TYPES = frozenset({"event", "intent"})
# The intents still to happen; the compact index lists these, not settled ones.
_PENDING_INTENT_STATUSES = frozenset({"planned", "due", "held"})
EVENT_FACT_TYPE = "event"


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


def _round_robin(
    queues: Sequence[Sequence[str]], *, rounds: int, total: int, taken: Collection[str] = (),
) -> list[str]:
    """Round by round, each queue's first entry not in ``taken`` or picked yet: at most ``rounds`` from each and
    ``total`` in all. Stops early once every queue is spent."""
    picked: list[str] = []
    remaining = [iter(queue) for queue in queues]
    for _round in range(rounds):
        before = len(picked)
        for queue in remaining:
            if len(picked) >= total:
                return picked
            ref = next((ref for ref in queue if ref not in taken and ref not in picked), None)
            if ref is not None:
                picked.append(ref)
        if len(picked) == before:
            break
    return picked


def build_adjudication_messages(
    *, context: TickContext, intents: Sequence[WorldNode], runtime: BookStateRuntime,
    lexicon: BookLexicon, calendar: StoryCalendar | None,
    feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    retry_feedback: Sequence[str] = (),
    max_new_actors: int = 1,
) -> list[dict]:
    node_locations: dict[str, str] = {}

    def location_id(node: WorldNode) -> str:
        if node.id not in node_locations:
            node_locations[node.id] = (resolve_world_node_location_id(runtime.world, node.id)
                                       or node.profile.get("location_id", ""))
        return node_locations[node.id]

    def node_payload(node: WorldNode) -> dict:
        return _without_empty({
            **node.model_dump(mode="json", exclude_defaults=True, exclude_none=True, exclude={"state"}),
            "state": runtime.world.get_state(node.id) if runtime.world.get_node(node.id) else node.state,
            "location_id": location_id(node),
        })

    def record(node: WorldNode) -> bool:
        """An event, or an intent other than the due ones: what happened or was planned, not who is there."""
        return str(node.node_type) in _RECORD_TYPES and node.id not in intent_ids

    def scope(group: Sequence[WorldNode]) -> tuple[set[str], set[str]]:
        """The full bodies and the locations a call about ``group`` shows.

        Full bodies: the intents, their owners and targets, what those hold and the live entities beside
        them. Past events and other intents grow with the book, so they never get a full body (Ruling P28).
        """
        actor_refs = {ref for intent in group
                      for ref in [intent.profile.get("owner_ref"), *intent.profile.get("target_refs", [])] if ref}
        locations = {location_id(node) for node in [*group, *[node for node in nodes if node.id in actor_refs]]}
        locations.update(actor_refs & runtime.map.nodes_by_id.keys())
        locations.discard("")
        bodies = {node.id for node in group} | {node.id for node in nodes if not record(node) and (
            node.id in actor_refs
            or (node.is_active and str(node.node_type) in _COLOCATED_TYPES and location_id(node) in locations)
            or (node.node_type in {"item", "resource"}
                and runtime.world.get_state(node.id).get("holder_id") in actor_refs)
        )}
        return bodies, locations

    nodes = sorted(runtime.world.nodes_by_id.values(), key=lambda node: node.id)
    intent_ids = {node.id for node in intents}
    full_ids, locations = scope(intents)

    def indexed(node: WorldNode) -> bool:
        """Every other live node, and the intents still to happen; events and settled intents stay out."""
        if node.id in full_ids or node.node_type == "rule":
            return False
        if record(node):
            return str(node.node_type) == "intent" and (
                runtime.world.get_state(node.id).get("status") in _PENDING_INTENT_STATUSES
            )
        return True

    edges = [edge for edge in sorted(runtime.world.edges_by_id.values(), key=lambda edge: edge.id)
             if edge.source_id in full_ids or edge.target_id in full_ids]
    map_edges = [edge for edge in sorted(runtime.map.edges_by_id.values(), key=lambda edge: edge.id)
                 if edge.from_node_id in locations or edge.to_node_id in locations]
    map_ids = locations | {ref for edge in map_edges for ref in (edge.from_node_id, edge.to_node_id)}
    payload = {
        "context": context.model_dump(mode="json"),
        "max_new_actors": max(0, max_new_actors),
        "intents": [node_payload(node) for node in intents],
        "world_nodes": [node_payload(node) for node in nodes
                        if node.id in full_ids - intent_ids and node.node_type != "rule"],
        "world_node_index": [{"id": node.id, "node_type": node.node_type, "name": node.name}
                             for node in nodes if indexed(node)],
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
    # Every scoped standing fact, and only the newest scoped event facts: the book's events keep growing.
    # Each due intent shows its own newest ones, so one busy intent's history never crowds out another's.
    events = sorted((ref for ref in fact_payloads if facts[ref].fact_type == EVENT_FACT_TYPE),
                    key=lambda ref: (-facts[ref].created_at_chapter, ref))
    # A lone intent's full bodies are the call's own; with several, each intent's are worked out only when
    # there are events to share out.
    bodies_by_intent = ([full_ids] if len(intents) == 1 or not events
                        else [scope([intent])[0] for intent in intents])
    by_intent = [[ref for ref in events if bodies.intersection(facts[ref].related_node_refs)]
                 for bodies in bodies_by_intent]
    # Each intent's guaranteed share comes first; then the newest scoped events no intent's own full bodies
    # take part in, such as the ones a shown node names; spare room then goes round by round to the intents
    # that still have events.
    picked = _round_robin(by_intent, rounds=MAX_ADJUDICATION_EVENT_FACTS_PER_INTENT,
                          total=MAX_ADJUDICATION_EVENT_FACTS)
    attributed = {ref for queue in by_intent for ref in queue}
    picked.extend([ref for ref in events if ref not in attributed][:MAX_ADJUDICATION_EVENT_FACTS - len(picked)])
    picked.extend(_round_robin(by_intent, rounds=MAX_ADJUDICATION_EVENT_FACTS,
                               total=MAX_ADJUDICATION_EVENT_FACTS - len(picked), taken=picked))
    shown = {ref for ref in fact_payloads if facts[ref].fact_type != EVENT_FACT_TYPE}
    shown.update(picked)
    payload["facts"] = [fact_payloads[ref] for ref in sorted(shown)]
    # Objective truth is sufficient for adjudication; no cognition overlay is serialized.
    allowed = {
        "knowledge_state": KNOWLEDGE_STATES, "knowledge_channel": KNOWLEDGE_CHANNELS,
        "edge_type": sorted(WORLD_EDGE_TYPES), "new_fact_ref_prefix": NEW_FACT_REF_PREFIX,
        # The state.status values that end a faction, organization, group or site.
        "ended_status": sorted(ENDED_STATUSES),
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


DRIFT_INSTRUCTIONS = (
    "你是世界状态的结算者。依据输入变量、范围内的公开事件、持有者与时钟，返回 JSON，不输出正文或 Markdown。",
    "每个输入 variable_id 恰好返回一个 change。new_level 是 scale 的下标，每次最多升降一档，必须在范围内；"
    "没有充分原因就保持原档。trend 只能是 rising、falling 或 stable；summary 简述原因。",
    "只产生 events，不产生意图。事件参与者必须属于变量的 holder_refs、scope_refs 或位于范围内的行动者；"
    "location_id 必须来自输入地图，at 是 window_start 到 now 之间的整数分钟。不得编造引用。",
)


def build_drift_messages(
    *, context: TickContext, variables: Sequence[WorldNode], runtime: BookStateRuntime,
    lexicon: BookLexicon, calendar: StoryCalendar | None,
    feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None,
    retry_feedback: Sequence[str] = (),
) -> list[dict]:
    """Author-side variable drift, scoped actors and recent public events; no cognition overlays."""
    scope = {ref for variable in variables for ref in variable.profile.get("scope_refs", [])}
    holders = {ref for variable in variables for ref in variable.profile.get("holder_refs", [])}
    nodes = sorted(runtime.world.nodes_by_id.values(), key=lambda node: node.id)

    def body(node: WorldNode) -> dict:
        return {**node.model_dump(mode="json", exclude={"state"}, exclude_defaults=True),
                "state": runtime.world.get_state(node.id)}

    events = [node for node in nodes if node.node_type == "event" and node.is_active
              and runtime.world.get_state(node.id).get("visibility_level") == "public"
              and (node.profile.get("location_id") in scope
                   or holders.intersection(runtime.world.get_state(node.id).get("participant_ids", [])))]
    events.sort(key=lambda node: (-node.created_at_chapter, node.id))
    payload = {
        "context": context.model_dump(mode="json"),
        "variables": [body(node) for node in variables],
        "world_nodes": [body(node) for node in nodes if node.id in scope | holders or (
            node.node_type in ACTOR_NODE_TYPES and resolve_world_node_location_id(runtime.world, node.id) in scope)],
        "map_nodes": [node.model_dump(mode="json", exclude_defaults=True)
                      for node in sorted(runtime.map.nodes_by_id.values(), key=lambda node: node.id)],
        "recent_public_events": [body(node) for node in events[:20]],
        "clocks": [body(node) for node in nodes if node.node_type == "clock" and node.is_active
                   and runtime.world.get_state(node.id).get("status") == "running"],
        "calendar": calendar_to_payload(calendar) if calendar is not None else None,
        "lexicon": lexicon_to_payload(lexicon),
    }
    ids = {node.id for node in variables}
    feedback_payload = {ref: [asdict(issue) for issue in issues] for ref, issues in (feedback or {}).items()
                        if ref in ids and issues}
    user = json.dumps(payload, ensure_ascii=False)
    if feedback_payload:
        user += "\n\nConstraint feedback by variable:\n" + json.dumps(feedback_payload, ensure_ascii=False)
    if retry_feedback:
        user += "\n\nRetry validation feedback:\n" + "\n".join(retry_feedback)
    system = "\n".join(DRIFT_INSTRUCTIONS) + "\n" + json.dumps(DriftResult.model_json_schema(), ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]


def build_actor_turn_messages(
    *, context: TickContext, views: Sequence[ActorView], calendar: StoryCalendar | None,
    lexicon: BookLexicon, max_new_intents: int, max_agenda_changes: int,
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
        "max_agenda_changes_per_actor": max_agenda_changes,
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
    instructions: Sequence[str] = CONFLICT_REVIEW_INSTRUCTIONS,
) -> list[dict]:
    """One conflict for the reviewer: its record, the involved refs' state, the volume plan and the next chapters.

    ``instructions`` are the ones for the conflict's source. Over the guard, the BookState slices drop their
    default-valued fields first, then each chapter-plan goal is cut to MAX_CONFLICT_REVIEW_GOAL_CHARS; the
    caller rejects a prompt that is still over.
    """
    payload = {**copy.deepcopy(dict(context_snapshot)), "options": list(options)}
    system = "\n".join(instructions) + "\n" + json.dumps(output_schema, ensure_ascii=False)
    user = json.dumps(payload, ensure_ascii=False)
    if len(user) > MAX_CONFLICT_REVIEW_PROMPT_CHARS and "book_state" in payload:
        payload["book_state"] = [_compact_slice(item) for item in payload["book_state"]]
        user = json.dumps(payload, ensure_ascii=False)
    if len(user) > MAX_CONFLICT_REVIEW_PROMPT_CHARS and "upcoming_chapters" in payload:
        for chapter in payload["upcoming_chapters"]:
            chapter["goals"] = [goal[:MAX_CONFLICT_REVIEW_GOAL_CHARS] for goal in chapter.get("goals", [])]
        user = json.dumps(payload, ensure_ascii=False)
    return [{"role": "system", "content": system}, {"role": "user", "content": user}]
