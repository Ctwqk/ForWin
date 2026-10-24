"""Writer-facing repair instructions for world consistency errors found at draft review."""

from __future__ import annotations

DEFAULT_WORLD_REPAIR_FIX = "改写相关情节，使人物行动、移动与时间符合本书世界状态。"

# World gate v6 error code -> what the writer changes in the prose.
WORLD_REPAIR_FIXES: dict[str, str] = {
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


PLAN_BINDING_REPAIR_TEXT = {
    "missing": "本章计划的第 {n} 项世界绑定（{kind}）没有在正文中落实；按本章目标补写相应情节。",
    "partial": "本章计划的第 {n} 项世界绑定（{kind}）仅部分落实；按本章目标补足相应情节。",
    "fix": "按本章目标落实第 {n} 项世界绑定（{kind}），让相应目标在正文中完整交付。",
}


def world_repair_fix(code: str) -> str:
    return WORLD_REPAIR_FIXES.get(code, DEFAULT_WORLD_REPAIR_FIX)


__all__ = ["DEFAULT_WORLD_REPAIR_FIX", "WORLD_REPAIR_FIXES", "PLAN_BINDING_REPAIR_TEXT", "world_repair_fix"]
