"""World-seed descriptions of the structural rule vocabulary."""

RULE_PREDICATE_DESCRIPTIONS = {
    "actor_cannot_enter": "location_ref: 地图节点 id；include_children: 布尔值，默认 true；except_refs: 获准行动者 id 列表，默认 []；except_member_of: 获准成员所属组织 id 列表，默认 []。禁止其他行动者进入该地点及指定范围内的子地点。",
    "requires_travel_mode": "edge_types: 地图边类型列表；mode_ids: lexicon 出行方式 id 列表。没有其中任一出行方式的行动者不能经过这些类型的边。",
    "exclusive_relation": "edge_type: 世界关系类型；max_per_source: 至少为 1 的整数。同一来源同时有效的该类关系不得超过此数量。",
    "irreversible_state": "node_types: 节点类型列表；field: alive_status 或 status；value: alive_status 只能为 dead，status 只能为 destroyed、dissolved、disbanded 或 inactive。进入此状态后不可改变该字段。",
    "blocked_channel": "fact_ref: 事实引用；source_refs: 行动者 id 列表；channels: witnessed、told、rumor、document、inferred、seeded 中的渠道列表。所列来源不得通过这些渠道传播该事实。",
}
