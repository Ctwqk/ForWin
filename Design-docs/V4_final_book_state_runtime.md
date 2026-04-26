# ForWin V4 Final BookState Runtime 设计规格

更新时间：2026-04-26

状态：设计落档；BookState 基座与地图系统方案 C 首轮实现已落地，尚未切换线上主链路。

来源：现有 `world_model_v4` 代码盘点、V4/V4.1 历史实现计划，以及最终版世界状态图 / 地图图 / 认知图数据结构设计。

## 1. 设计结论

ForWin 最终世界模型采用：

```text
Typed Property Graph
+ Append-only Delta Ledger
+ Sparse Cognition Overlay
+ Materialized Snapshot
```

也就是：

```text
BookState
  ├── MapGraph
  ├── ObjectiveWorldGraph
  ├── CognitionOverlays
  ├── NarrativeControlGraph
  └── DeltaLedger
```

这次升级是破坏性的。现有轻量 `world_model_v4` 的 `WorldLine / WorldDelta / Belief / KnowledgeGap / RevealEvent / ReaderExperienceDelta` 已经把“幕后世界线 + 信息差 + reveal gate”跑通，但它不是最终世界状态图。最终版以 typed graph 和 patch ledger 为 canon source，旧 v4 rows 只作为迁移来源、兼容投影和历史审计证据。

## 2. 当前代码基线

当前代码中存在三层相关实现：

- 旧 `world_model`：从 Genesis、EntityState、RelationEdge、CanonEvent 编译只读 Wiki / World Studio 页面。
- 轻量 `world_model_v4`：以 world lines、deltas、beliefs、knowledge gaps、reveal events、reader experience 为主的 append-only ledger。
- 旧状态表：`entities / entity_states / relation_edges` 仍是很多上下文、writer、reviewer 的兼容输入。

当前缺口：

- 没有最终版 `world_nodes / world_edges / fact_nodes` typed property graph。
- 没有独立 `MapGraph`，地图仍主要来自 Genesis `map_atlas` 或文本摘要。
- 没有 `MapEdge` 路径计算、blocked/hidden route、observer-aware known distance。
- 认知层仍以 beliefs/gaps/snapshots 为主，不是 sparse overlay。
- `WorldDelta` 仍是摘要型事件，不是可 replay 的 node/edge/fact/map/cognition patch。
- projection 会派生 `EntityState`，但没有真正的 snapshot + delta replay runtime。

因此后续实现不应继续硬化现有轻量 v4 语义，而应把它迁移到最终 BookState 结构。

## 3. Canon 边界

最终权威边界如下：

- `ObjectiveWorldGraph` 只存客观真相。
- `MapGraph` 单独维护空间层级、路线、距离、时间、风险、通行成本。
- 世界节点当前位置是 state 字段中的 `location_id`，引用 `MapNode`。
- 特殊地点使用 `MapNode + site_state` 双结构：MapNode 管“在哪里”，site_state 管“当前发生什么”。
- 认知不复制世界图，只存 mask、override、false additions。
- LLM 和 writer 不直接输出完整图，只输出 patch candidates。
- canon 只由 compiler 在 review gate 通过后写入 `GraphDelta` 和 patch rows。
- `entities / entity_states / relation_edges` 变为兼容投影，不再是最终 canon source。

## 4. 目标与非目标

目标：

- 建立最终 BookState runtime，支持 as-of chapter 查询、snapshot replay、路径计算、认知视角查询。
- 将世界状态、地图状态、认知状态、叙事控制状态全部纳入 append-only delta ledger。
- 支持 reviewer 检查角色移动、信息越权、揭示公平性、地图封锁与隐藏路线。
- 保留现有 writing pipeline，但把 canon commit 路径切到 `extract -> review gate -> compiler -> projection`。
- 继续支持 SQLite 开发环境，同时让 repository/API 设计可迁移到 PostgreSQL JSONB。

非目标：

- 本轮设计不要求立即切换 PostgreSQL。
- 不承诺旧项目无损迁移。
- 不把 Neo4j 作为主存储；可后续作为可视化/分析投影。
- 不让 World Studio 或 Obsidian 成为 canon writer。
- 不把地图路线塞入普通 `world_edges`。

## 5. ObjectiveWorldGraph

### 5.1 世界节点

最终世界节点类型固定为 12 种：

```text
character
faction
group
item
resource
ability
rule
activity
site_state
event
fact
objective
```

所有世界节点共享 10 个基础字段：

```text
id
project_id
node_type
name
aliases
description
importance
created_at_chapter
retired_at_chapter
is_active
```

节点字段分为：

- `profile`：长期属性，不频繁变化。
- `state`：章节/故事时间变化，必须进入 delta ledger。
- `metadata`：扩展字段，不计入核心字段数。

节点字段数量按最终设计固定：

```text
character: 42
faction: 38
group: 31
item: 40
resource: 31
ability: 34
rule: 31
activity: 42
site_state: 39
event: 34
fact: 32
objective: 34
```

实现要求：

- Pydantic 层应提供统一 `WorldNode`，并用 `node_type` 控制 profile/state schema。
- ORM 层使用基础列 + `profile_json` + state ledger，不为每种节点建一张表。
- `fact` 可在 runtime 中作为 `FactNode` 读写，但持久层保留独立 `fact_nodes` 表以便检索与认知系统高频查询。

### 5.2 世界关系

世界关系使用 `WorldEdge`，统一字段 15 个：

```text
id
project_id
source_id
target_id
edge_type
edge_family
directionality
weight
confidence
established_at_chapter
ended_at_chapter
is_active
visibility_default
state
evidence_refs
```

世界关系类型共 50 种，分 8 类：

```text
organization: member_of, leader_of, subordinate_to, branch_of, part_of
possession: owns, possesses, equipped_with, bound_to, created_by, grants_ability
control_conflict: controls, governs, claims, occupies, protects, besieges, blockades
social: family_of, ally_of, enemy_of, mentor_of, romantic_with, trusts, distrusts, owes_debt_to
capability_rule: has_ability, uses, requires, counters, weak_against, forbidden_by
event_causal: participates_in, witnesses, causes, prevents, enables, results_in, damages
fact_evidence: supports, contradicts, evidence_for, proves, disproves
activity_objective: organizes, hosts, competes_in, rewards, targets, advances_objective
```

实现要求：

- 禁止用 `located_in` 表示当前位置；统一使用世界节点 state 中的 `location_id`。
- `betrayal` 不作为长期 edge type，通常记录为 event/fact，并通过 patch 改变 trust/distrust 边。
- 所有 edge patch 必须有 `edge_family`，reviewer 可据此做规则检查。

## 6. MapGraph

地图图独立于 ObjectiveWorldGraph。

```text
BookMap = Hierarchical Weighted Directed Multigraph

SubWorld -> Region -> MapNode
MapEdge = weighted directed route between MapNodes
```

运行时结构：

```python
class BookMapRuntime:
    project_id: str

    subworlds_by_id: dict[str, SubWorldNode]
    inter_subworld_edges_by_id: dict[str, InterSubWorldEdge]

    regions_by_id: dict[str, RegionNode]
    region_edges_by_id: dict[str, RegionEdge]

    map_nodes_by_id: dict[str, MapNode]
    map_edges_by_id: dict[str, MapEdge]

    outgoing_edges: dict[str, list[str]]
    incoming_edges: dict[str, list[str]]

    regions_by_subworld: dict[str, list[str]]
    nodes_by_region: dict[str, list[str]]

    path_cache: dict[tuple[str, str, str, str], PathResult]
```

底层路径计算仍使用 `MapGraph`，但持久化与生成层已经升级为 `BookMapRuntime`：

- `SubWorld` 是大陆、星球、位面、异世界、星区等大尺度容器。
- `Region` 是 subworld 内部的国家、宗门领地、山脉、城市圈、遗迹区等中尺度分区。
- `MapNode` 是具体地点。
- `MapEdge` 是地点之间的带权有向路线。
- 跨 subworld route 第一版复用 `map_edges`，并通过 `metadata.target_subworld_id` 与 `metadata.inter_subworld_edge` 标记。

### 6.1 地图生成方案

地图生成主方案固定为：

```text
方案 C：Graph-based Weighted Map Generation
```

不使用 Noise / Voronoi / BSP / WFC 作为主生成算法。当前实现位于 `forwin/map/generator.py`，算法名 `anchor_graph_v1`，流程是：

1. 根据 `SubWorldMapSpec.required_region_roles` 生成必需 regions。
2. 补足普通 regions。
3. 构建 region candidate edges。
4. 用 MST 保证 `RegionGraph` 连通。
5. 放置 required anchor `MapNode`。
6. 每个 region 生成 entry / hub / boundary 基础节点。
7. 生成候选 `MapEdge`。
8. 用 MST 保证 subworld 内 MapNode 主图连通。
9. 添加 extra edges 到 `round(node_count * target_edge_density)`。
10. 计算 `distance / travel_time / travel_cost / risk_level / narrative_cost`。
11. validation 通过后写入 map rows 和 `map_generation_runs`。

### 6.2 地图节点

`MapNode` 代表具体地点，常用类型：

```text
settlement
site
building
room
zone
waypoint
landmark
camp
dungeon_room
```

当前 DTO 仍复用最终 BookState `MapNode`，并新增地图生成所需字段：

```text
subworld_id
region_id
description
```

`subworld_id / region_id` 是生成、路径索引和 reviewer movement check 的硬要求。`parent_id / hierarchy_path` 只表达更细粒度空间包含关系，不替代 region 归属。

### 6.3 地图边

`MapEdge` 代表两个 `MapNode` 之间的有向带权路线。常用类型：

```text
road
path
river
sea_route
flight_route
tunnel
portal
rail
border_crossing
hidden_route
mountain_pass
space_route
world_gate
```

`MapEdge` 至少保存：

```text
id
project_id
subworld_id
from_node_id
to_node_id
edge_type
bidirectional
distance
travel_time
travel_cost
risk_level
narrative_cost
access_rule_id
status
discovered_by_default
visibility_default
metadata
```

实现要求：

- 持久化层可允许 `bidirectional=true`。
- runtime 必须展开为有向边，方便表达上山/下山、入城/出城、顺流/逆流的不同权重。
- 权重字段保存可计算数值，中文显示文本放入 `metadata`。
- 所有权重必须非负；Pydantic 和 repository 都拒绝负数。
- 路径算法默认 Dijkstra；有 coordinates 时可选 A*，但真实可达距离仍沿 MapEdge 计算。

### 6.4 PathResult

路径计算返回：

```text
reachable
from_node_id
to_node_id
metric
total_distance
total_travel_time
total_travel_cost
total_risk
total_narrative_cost
path_node_ids
path_edge_ids
blocked_reason
explanation
```

核心接口：

```python
def shortest_path(
    self,
    from_node_id: str,
    to_node_id: str,
    *,
    metric: str = "travel_time",
    observer: tuple[str, str] | None = None,
    allow_hidden: bool = False,
    allow_blocked: bool = False,
) -> PathResult:
    ...
```

世界节点距离查询流程：

```text
world node -> state.location_id -> MapGraph.shortest_path()
```

location fallback 顺序：

```text
state.location_id
current_activity_id -> activity.state.current_location_id
faction -> state.headquarters_location_id
site_state -> profile.map_node_id
missing -> unreachable
```

## 7. CognitionOverlay

认知层不复制 ObjectiveWorldGraph，只记录差异：

```text
CognitionOverlay = mask + override + false additions
```

观察者类型：

```text
reader
character
faction
group
system
```

统一 Ref 格式：

```text
node:<node_id>
field:<node_id>:<field_path>
edge:<edge_id>
fact:<fact_id>
map_node:<map_node_id>
map_edge:<map_edge_id>
world_line:<world_line_id>
promise:<promise_id>
knowledge_gap:<gap_id>
```

`CognitionOverlay` 核心字段：

```text
id
project_id
observer_type
observer_id
as_of_chapter
as_of_story_time
visible_refs
hidden_refs
suspected_refs
confirmed_refs
field_overrides
false_nodes
false_edges
false_facts
evidence_by_ref
```

认知状态枚举：

```text
hidden
unknown
hinted
suspected
partially_known
known
confirmed
misled
false
stale
```

实现要求：

- `CognitionView.can_see(ref)` 是 writer-safe 和 reviewer 检查的基础。
- `field_overrides` 可影响地图边状态、角色位置、事实真值等 observer belief。
- `false_edges` 必须能加入 MapGraph 的 observer 路径模拟，用于错误计划和误判路线。
- reader、主角、反派、势力的认知应能并存并按 chapter 查询。

## 8. NarrativeControlGraph

叙事控制不属于客观世界图。

节点类型：

```text
world_line
plot_thread
promise
knowledge_gap
reveal_plan
review_constraint
```

边类型：

```text
opens
advances
delays
blocks
resolves
foreshadows
pays_off
creates_gap
closes_gap
reveals
hides
escalates
protects_until
contradicts_plan
```

迁移规则：

- 旧 `world_lines` 迁移为 `narrative_nodes(node_type=world_line)`。
- 旧 `knowledge_gaps` 迁移为 `narrative_nodes(node_type=knowledge_gap)`，并通过 fact refs 绑定客观事实。
- 旧 `reveal_events` 迁移为 reveal plan / reveal ledger。
- 旧 `reader_experience_deltas` 迁移为 promise/payoff 证据。

## 9. DeltaLedger

最终 ledger 以 `GraphDelta` 为主：

```text
id
project_id
chapter_number
story_time
delta_type
source_type
source_id
world_line_id
summary
node_patches
edge_patches
fact_patches
evidence_refs
```

`delta_type`：

```text
world_state
map_state
cognition
narrative_control
repair
retcon_block
```

patch 类型：

```text
NodePatch: node_id, node_type, op, field_path, old_value, new_value, reason, visibility_default
EdgePatch: edge_id, op, source_id, target_id, edge_type, edge_family, field_path, old_value, new_value, reason
FactPatch: fact_id, op, proposition, truth_value, related_refs, old_value, new_value, reason, sensitivity_level
MapPatch: target_type, target_id, op, field_path, old_value, new_value, reason, discovered_by_default, access_change, affected_path_cache_keys, visibility_default
```

patch op：

```text
create
set
merge
append
remove
replace
deactivate
```

实现要求：

- LLM 输出 patch candidates；compiler 写入 approved patch rows。
- `old_value` 校验失败时默认 block，除非是明确 repair/retcon path。
- 地图 patch 影响路径时，按 `project_id + map_version` 失效缓存，或按 `affected_path_cache_keys` 精确失效。
- GraphDelta append-only，禁止覆盖式更新。

## 10. Snapshot 策略

WorldSnapshot 字段：

```text
id
project_id
as_of_chapter
as_of_story_time
base_snapshot_id
world_node_state_index
active_edge_ids
active_fact_ids
active_world_line_ids
open_gap_ids
source_delta_ids
built_at
```

MapSnapshot 字段：

```text
id
project_id
as_of_chapter
map_node_index
map_edge_index
blocked_edge_ids
hidden_edge_ids
built_at
```

CognitionSnapshot 字段：

```text
id
project_id
observer_type
observer_id
as_of_chapter
overlay_id
visible_refs
suspected_refs
confirmed_refs
built_at
```

物化频率：

- 每个 band 结束物化一次。
- 每 20 章强制物化一次。
- manual checkpoint 物化一次。
- 重要 world_line 阶段变化后物化一次。

查询第 N 章状态：

```text
1. 找到 <= N 的最近 snapshot
2. replay 后续 graph_deltas
3. 得到 as_of_chapter=N 的 BookStateRuntime
```

## 11. 持久化表

最终新增或重建表：

```text
world_nodes
world_node_states
world_edges
fact_nodes

map_nodes
map_edges

graph_deltas
graph_delta_patches

cognition_overlays
cognition_overlay_patches

world_snapshots
map_snapshots
cognition_snapshots

narrative_nodes
narrative_edges
```

保留但降级为兼容/迁移来源：

```text
entities
entity_states
relation_edges
world_lines
world_deltas
beliefs
knowledge_gaps
reveal_events
reader_experience_deltas
world_model_snapshots_v4
```

SQLite 实现说明：

- JSONB 字段先用 TEXT JSON。
- 索引按 project_id、chapter、node_type、edge_type、source/target、observer 建立。
- repository 方法不得假设 SQLite-only 行为。

## 12. Runtime 模块

建议文件边界：

```text
forwin/protocol/book_state.py
forwin/models/book_state.py
forwin/book_state/repository.py
forwin/book_state/runtime.py
forwin/book_state/map_graph.py
forwin/book_state/cognition.py
forwin/book_state/projection.py
forwin/book_state/compiler.py
forwin/book_state/legacy_import.py
```

核心类：

```python
class ObjectiveWorldGraph:
    nodes_by_id: dict[str, WorldNode]
    states_by_node_id: dict[str, dict]
    edges_by_id: dict[str, WorldEdge]
    outgoing_edges: dict[str, list[str]]
    incoming_edges: dict[str, list[str]]
    facts_by_id: dict[str, FactNode]
    facts_by_related_ref: dict[str, list[str]]

class CognitionView:
    observer_type: str
    observer_id: str
    as_of_chapter: int
    visible_refs: set[str]
    hidden_refs: set[str]
    suspected_refs: set[str]
    confirmed_refs: set[str]
    field_overrides: dict[str, Any]
    false_nodes: dict[str, WorldNode]
    false_edges: dict[str, WorldEdge]
    false_facts: dict[str, FactNode]
    evidence_by_ref: dict[str, list[str]]

class BookStateRuntime:
    project_id: str
    as_of_chapter: int
    world: ObjectiveWorldGraph
    map: MapGraph
    cognition_by_observer: dict[tuple[str, str], CognitionView]
    narrative: NarrativeControlGraph
```

## 13. Pipeline 改造

最终 canon commit 流程：

```text
writer output
  -> patch extractor
  -> review gate
  -> approved GraphDelta
  -> BookStateCompiler
  -> append ledger
  -> rebuild snapshots/projections
  -> retrieval packs/debug/export
```

WriterOutput 调整：

- 保留现有正文、scene、state/event/thread candidates。
- 新增或替换为 `node_patches / edge_patches / fact_patches / map_patches / cognition_patches / narrative_patches`。
- writer 自报永远是候选，不直接入 canon。

Reviewer 新增检查：

- patch schema 合法性。
- `old_value` 与 as-of state 是否一致。
- 角色是否知道不该知道的 fact。
- 角色移动是否有客观或认知可达路径。
- hidden route 是否被错误使用。
- blocked route 是否被绕过。
- reveal 是否有 reader hinted/suspected/evidence 铺垫。
- promise debt 是否持续累积无 payoff plan。

Context/Retrieval 调整：

- writing pack 使用 observer-safe `CognitionView`。
- review/compiler pack 可访问 ObjectiveWorldGraph 和 hidden truth。
- planning pack 可看 narrative control graph 与 future reveal plan。
- map context 提供当前位置、可达路径、重要封锁、认知未知路线，而不是整张地图 dump。

## 14. 迁移策略

迁移是 best-effort，不承诺无损。

旧结构映射：

```text
entities -> world_nodes
entity_states -> world_node_states
relation_edges -> world_edges
world_lines -> narrative_nodes(world_line)
world_deltas -> graph_deltas(summary-only legacy deltas)
beliefs -> cognition_overlays / false_facts / suspected_refs
knowledge_gaps -> narrative_nodes(knowledge_gap) + fact refs
reveal_events -> narrative_nodes(reveal_plan) / reveal ledger
reader_experience_deltas -> promise/payoff evidence
Genesis map_atlas -> map_nodes / map_edges seed
```

迁移默认：

- entity kind 无法映射到 12 种节点时，放入 `metadata.legacy_kind`，并按最近语义映射。
- 旧 `location` entity 优先转为 `MapNode`，如带剧情状态则同时生成 `site_state`。
- 旧 `WorldDelta.summary` 没有 patch 级字段时，生成 `GraphDelta(delta_type=repair or world_state, metadata.legacy_summary_only=true)`。
- 旧 beliefs 中的 proposition 若无法绑定 fact，生成 `false_facts` 或 `metadata.unresolved_belief=true`。

## 15. 实施路线

### Phase A: Final schema and protocol

- 新增 `book_state` 协议模型与 ORM。
- 增加 forward-only SQLite migrations。
- 注册新模型到 `forwin/models/__init__.py`。
- 建 schema tests，覆盖表、索引、enum、DTO validation。

### Phase B: Runtime graph and pathing

- 实现 `ObjectiveWorldGraph`。
- 实现 `MapGraph`、Dijkstra、bidirectional 展开、path cache。
- 实现 `CognitionView` 和 observer path filtering。
- 实现 `distance_between_world_nodes`。

### Phase C: Delta replay and snapshots

- 实现 patch apply。
- 实现 snapshot build/load/replay。
- 实现 map snapshot 和 cognition snapshot。
- 保证 as-of chapter 查询稳定。

### Phase D: Compiler and pipeline switch

- 重写 compiler 接收 `ApprovedGraphDeltaSet`。
- extractor 输出 patch candidates。
- review gate 改查 final graph。
- orchestrator canon commit 切到 BookStateCompiler。
- 旧 `StateUpdater` 只用于兼容投影。

### Phase E: API, UI, export

- 更新 `/api/projects/{project_id}/world-model/v4/*` 兼容端点返回新结构摘要。
- 新增 graph/map/cognition/delta debug endpoints。
- World Studio 增加 Graph / Map / Cognition / Deltas 视图。
- Exporter 输出 Objective Timeline、Map Topology、Cognition Diff、Narrative Control 页面。

### Phase F: Legacy import and cleanup

- 提供 `legacy_import` 工具。
- 为旧项目生成一次 final BookState bootstrap snapshot。
- 标记旧 v4 tables 为 legacy read-only。
- 后续单独计划清理轻量 v4 DTO 命名。

## 16. 测试计划

协议与 schema：

- 12 种 world node 可 validate。
- 50 种 world edge 可 validate。
- 8 种 map node、10 种 map edge 可 validate。
- overlay refs、patch op、snapshot DTO 可 validate。
- SQLite migrations 可在旧库上补表不丢数据。

Runtime：

- GraphDelta replay 得到正确 as-of state。
- NodePatch/EdgePatch/FactPatch/MapPatch apply 正确。
- 最近 snapshot + replay 与从初始状态全量 replay 结果一致。
- path cache 在 MapPatch 后正确失效。

Map：

- 有向边不自动反向。
- `bidirectional=true` 在 runtime 展开。
- 多路线按 metric 选择最短路径。
- hidden route 对客观路径可见，对未知 observer 不可见。
- false edge 能让 observer 得到错误路径。

Cognition：

- reader hidden fact 不进入 writing pack。
- protagonist suspected fact 可用于怀疑型行动，不能用于 confirmed 行动。
- field override 能改变 observer 看到的位置/路线状态。
- false fact 与 objective fact 的 diff 可被 reviewer 读取。

Pipeline：

- writer output patch candidates 不直接入 canon。
- review fail 时 compiler 不写 ledger。
- review pass 时 compiler append graph delta 并重建 snapshot。
- accepted chapter 后旧 `EntityState` 兼容投影仍可供老上下文读取。

E2E：

- 构造“黑石城 -> 密道 -> 遗迹内殿”地图。
- 客观最短路径走密道。
- 主角不知道密道时绕路。
- 主角发现密道后 MapPatch + CognitionPatch 让 known distance 变短。
- reviewer 能发现角色在未知道密道前使用密道的越权移动。

## 17. 验收标准

本次最终版实现完成的最低标准：

- 新项目可从 Genesis seed 出 `BookStateRuntime`。
- 接受章节后只通过 `GraphDelta` 更新 canon。
- 能查询任意章节的 world/map/cognition snapshot。
- 能计算客观地图距离与 observer known distance。
- writer pack 不泄露 hidden objective truth。
- review/compiler pack 能看到客观真相并阻止信息越权。
- World Studio 能展示至少 graph nodes、map path、cognition refs、delta ledger 四类调试信息。
- 旧 v4 测试要么迁移到 final BookState 语义，要么明确标记 legacy compatibility。

## 18. 当前实现进度

2026-04-26 BookState 首轮实现范围：

- 新增 `forwin/protocol/book_state.py`，提供最终版 WorldNode、WorldEdge、FactNode、MapNode、MapEdge、GraphDelta、patch、CognitionOverlay、Snapshot 等 DTO。
- 新增 `forwin/models/book_state.py`，提供最终版 BookState 表模型与 `book_state_schema_v1` forward-only migration。
- 新增 `forwin/book_state/` runtime 基座，包含 `ObjectiveWorldGraph`、`MapGraph`、`CognitionView`、`BookStateRuntime` 和 `distance_between_world_nodes`。
- `MapGraph` 已支持带权有向多重图、`bidirectional` runtime 展开、Dijkstra 最短路径、hidden route 过滤、observer-aware known path。
- `ObjectiveWorldGraph` 已支持基础 node/edge/fact patch apply 与 node state replay。
- 新增测试：`tests/test_book_state_protocol.py`、`tests/test_book_state_schema.py`、`tests/test_book_state_runtime.py`。

2026-04-26 地图系统方案 C 首轮实现范围：

- 新增设计文档：[map_scheme_c.md](/home/taiwei/.codex/worktrees/2a32/ForWin/Design-docs/map_scheme_c.md)。
- 新增 `forwin/map/`，实现 `models / protocol / repository / generator / pathfinding / validator / service`。
- `SubWorld` 显式升级为大尺度地图容器，新增 `subworld_type / scale_level / culture_profile_json / terrain_profile_json / danger_profile_json / generation_seed / map_status`。
- 新增 `map_regions / map_region_edges / map_generation_runs`，并扩展 `map_nodes` 的 `subworld_id / region_id / description` 与 `map_edges` 的 `subworld_id` 等字段。
- `generator.py` 实现 `方案 C：Graph-based Weighted Map Generation`：required regions、anchor nodes、RegionGraph MST、MapGraph MST、extra edges、权重计算和 validation。
- `service.py` 提供 `create_or_update_subworld_map()`、`create_or_update_book_map()`、`get_book_map_runtime()`、`compute_distance()`、`compute_known_distance()`、`resolve_world_node_location_id()`。
- 跨 subworld 第一版复用 `map_edges`，通过 `world_gate`、exit `MapNode`、exit connector 和 `metadata.target_subworld_id` 表达。
- writer context 新增 `map_context`，reviewer context 同步携带 map graph 紧凑数据。
- reviewer heuristic 已能检查连续场景地图移动是否不可达，或 `travel_time` 是否超过章节时间推进。
- 新增测试：`tests/test_map_models.py`、`tests/test_map_generation.py`、`tests/test_map_generation_scheme_c.py`、`tests/test_map_pathfinding.py`、`tests/test_map_cognition_path.py`、`tests/test_map_world_integration.py`。

实现备注：

- 由于旧轻量 v4 已占用 `cognition_snapshots` 表名，首轮最终版物化认知快照使用 `book_cognition_snapshots`。后续迁移阶段再决定是否破坏性收口为最终表名。
- 现阶段尚未切换 orchestrator canon commit；旧 v4 compiler 仍在运行时路径中。
- 现阶段没有实现 repository/compiler/API/UI/pipeline 的最终切换。
- 地图系统本轮不新增 FastAPI 路由；只提供 service 层给 orchestrator、reviewer、writer 和测试调用。
- 本轮没有使用外部开源地图生成代码，生成器是 ForWin 内部 deterministic graph generator。

验证：

- `PYTHONPATH=. pytest -q tests/test_book_state_protocol.py tests/test_book_state_schema.py tests/test_book_state_runtime.py`
- `PYTHONPATH=. pytest -q tests/test_world_v4_schema.py tests/test_world_v4_repository.py tests/test_world_v4_projection_materialization.py`
- `python3 -m pytest tests/test_book_state_protocol.py tests/test_book_state_runtime.py tests/test_book_state_schema.py tests/test_map_models.py tests/test_map_generation_scheme_c.py tests/test_map_generation.py tests/test_map_pathfinding.py tests/test_map_cognition_path.py tests/test_map_world_integration.py tests/test_subworld_control.py -q`
- 当前地图相关验证结果：`37 passed`

## 19. 后续实现注意事项

- 不要在旧 `world_model_v4` 上继续堆最终结构字段；新结构应独立在 `book_state` 模块中落地，再逐步替换调用方。
- 不要把地图节点放进普通 `world_nodes`，特殊地点必须是 `MapNode + site_state`。
- 不要让 cognition snapshot 成为 canon source；snapshot 只是 overlay 的物化视图。
- 不要让 Writer 看到 review/compiler-only hidden truth。
- 不要为了兼容旧表牺牲 patch replay 的可验证性。
