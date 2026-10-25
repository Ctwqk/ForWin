"""Writer-facing repair instructions for world consistency errors found at draft review."""

from __future__ import annotations

from collections.abc import Mapping

from forwin.writer.prompt_core.world_view import UNNAMED_ACTOR_LABEL, UNNAMED_FACT_TOPIC

DEFAULT_WORLD_REPAIR_FIX = "改写相关情节，使人物行动、移动与时间符合本书世界状态。"

# World gate v6 error code -> what the writer changes in the prose.
WORLD_REPAIR_FIXES: dict[str, str] = {
    "world_variable_level_out_of_range": "将相关世界状况改写到既定档位范围内，不要写出超出范围的状态。",
    "invalid_field_value": "改写相关状态，使其符合既定世界设定；涉及世界变量时沿用有效的档位与范围。",
    "rule_violated": "调整相关行动、关系或状态变化，使其符合既定规则；不要补写未获准揭示的规则内容或秘密真相。",
    "movement_rule_blocked": "改走规则允许的路线，或先取得所需出行方式，再安排移动。",
    "movement_too_fast": "让这段行程花更多时间（拉长本章时间推进或写出途中耗时），或改为更近的目的地、更快的已知路线。",
    "movement_total_too_fast": "同一角色本章几段行程的总耗时超出本章时间跨度：拉长时间推进，删减或合并行程，或改为更近的地点。",
    "movement_overlap": "同一角色的行程在时间上重叠：先写完上一段行程再出发，或调整出发时间。",
    "movement_unreachable": "改为地图上能走通的路线或地点，或先写出打通道路的过程。",
    "movement_unknown_map_node": "改为地图上已有的地点。",
    "movement_route_unknown_to_mover": "先写出角色发现或得知这条路线的过程，或改走角色已知的路线。",
    "time_offset_out_of_range": "调整出发与抵达的时间，使其落在本章时间范围内，且先出发后抵达。",
    "restrained_entity_moved": "先写出角色被释放或脱困，再让其移动；否则让其留在原地。",
    "dead_entity_acts": "删去已死亡角色的行动、对话与移动；需要其出场时改为回忆、遗物或他人转述。",
    "item_holder_mismatch": "让物品由当前持有者交出，或先写出物品如何转到交出者手中。",
    "told_by_unaware_source": "改由已知情的角色告知，或先写出告知者如何得知此事。",
    "contradicts_hidden_fact": "不要把与隐藏真相矛盾的说法写成事实，改为传闻、误判或谎言。",
    "premature_reveal": "本章不要向读者揭示这一秘密，保留悬念，只留暗示。",
    "immutable_rule_definition_conflict": "不要改写既定世界规则的定义，按现有规则写作。",
    "chapter_delta_missing": "重写正文后会重新抽取章节 delta。",
    "chapter_delta_unverified": "重写正文后会重新抽取章节 delta。",
}


# A broken protection's kind (book_state.narrative.PROTECTION_KINDS), or "milestone" for a ref a later milestone
# needs -> what the writer keeps. {name} is the protected entity, {observer} who may not learn a fact and {topic}
# the fact's topic; a fact's proposition never fills them.
PROTECTION_REPAIR_FIXES: dict[str, str] = {
    "must_exist": "本章不得让{name}覆灭、解散或消失；保留其存在。",
    "must_be_alive": "本章不得让{name}死亡；保留其存活。",
    "must_hold_state": "本章不得改变{name}的这一状态；保持其原状。",
    "must_not_know": "本章不要让{observer}得知或确信「{topic}」；至多留下怀疑或传闻。",
    "milestone": "本章不得让{name}死亡、覆灭或消失；保留其存在。",
}
# The {observer} of a protection that keeps a fact from the reader.
READER_OBSERVER_LABEL = "读者"


PLAN_BINDING_REPAIR_TEXT = {
    "missing": "本章计划的第 {n} 项世界绑定（{kind}）没有在正文中落实；按本章目标补写相应情节。",
    "partial": "本章计划的第 {n} 项世界绑定（{kind}）仅部分落实；按本章目标补足相应情节。",
    "fix": "按本章目标落实第 {n} 项世界绑定（{kind}），让相应目标在正文中完整交付。",
}


def world_repair_fix(code: str) -> str:
    return WORLD_REPAIR_FIXES.get(code, DEFAULT_WORLD_REPAIR_FIX)


def protection_repair_fix(kind: str, names: Mapping[str, str]) -> str:
    """The fix for a broken protection or milestone ref; ``names`` holds ``name``, ``observer`` and ``topic``."""
    template = PROTECTION_REPAIR_FIXES.get(kind)
    if template is None:
        return DEFAULT_WORLD_REPAIR_FIX
    return template.format(
        name=names.get("name") or UNNAMED_ACTOR_LABEL,
        observer=names.get("observer") or UNNAMED_ACTOR_LABEL,
        topic=names.get("topic") or UNNAMED_FACT_TOPIC,
    )


__all__ = [
    "DEFAULT_WORLD_REPAIR_FIX",
    "PLAN_BINDING_REPAIR_TEXT",
    "PROTECTION_REPAIR_FIXES",
    "READER_OBSERVER_LABEL",
    "WORLD_REPAIR_FIXES",
    "protection_repair_fix",
    "world_repair_fix",
]
