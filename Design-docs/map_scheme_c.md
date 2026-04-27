# ForWin 地图系统：方案 C Graph-based Weighted Map Generation

更新时间：2026-04-26

状态：Scheme C 后端主链已补齐到 V4.5.1：初始生成、arc expansion、显式 atlas edge、legacy region promotion、movement policy 和最小 map API 已落地。当前代码与设计差距统一入口见 [V4.5_markstone.md](/home/taiwei/.codex/worktrees/b77d/ForWin/Design-docs/V4.5_markstone.md)，V4.5.1 残余设计债见 [V4.5.1_markstone.md](/home/taiwei/.codex/worktrees/b77d/ForWin/Design-docs/V4.5.1_markstone.md)。完整可视化编辑器、复杂 route policy、交通工具体系和多策略 path planner 归入 V4.6+。

关键词：`方案 C：Graph-based Weighted Map Generation`、`SubWorld`、`RegionGraph`、`MapGraph`、`BookMapRuntime`、`CognitionOverlay`

## 1. 设计结论

ForWin 地图系统的主生成方案固定为：

```text
方案 C：Graph-based Weighted Map Generation
```

地图不是 tile map、Voronoi 图、BSP、WFC 或纯文本地点列表，而是分层、带权、有向、多重图：

```text
BookMap
  ├── SubWorldMetaGraph
  ├── SubWorldMap[]
  │     ├── RegionGraph
  │     ├── MapNode
  │     └── MapEdge
  └── MapPathIndex
```

核心语义：

- `SubWorld` 只表示大陆、星球、位面、异世界、星区等大尺度地图容器。
- `Region` 表示 subworld 内部的国家、宗门领地、山脉、城市圈、遗迹区等中尺度区域。
- `MapNode` 表示具体地点，例如城市、城门、客栈、遗迹入口、传送阵、密室。
- `MapEdge` 表示两个 `MapNode` 之间的有向带权路线。
- 全书地图由多个 subworld 子图和少量跨 subworld `MapEdge` 组成。

## 2. 语义迁移

旧 `SubWorld` 在 V2.9 系列中曾承担“剧情子世界 / 局部舞台 / 角色准入容器”的混合职责。方案 C 后的规则是：

- `SubWorld` 不再表示黑石城、青云宗外门、上古遗迹入口、客栈、炼丹塔等局部地点。
- 旧 `metadata_json.region_drafts` 和 `SubWorldPlanItem.region_seeds[]` 会提升为 `map_regions`。
- `SubWorldRosterItem` 保留，但语义收窄为大尺度地图容器内的角色准入 / 出场 roster。
- 角色、势力、物品、活动、事件的空间位置统一通过 `location_id` 引用 `MapNode`。

允许的 `SubWorld` 类型：

```text
continent
planet
plane
realm
star_sector
underground
divine_domain
demon_realm
virtual_world
```

允许的 `Region` 类型：

```text
kingdom
sect_domain
province
mountain
forest
wasteland
sea
city_cluster
borderland
ruin_zone
battlefront
resource_zone
forbidden_zone
```

当前 `MapNode` 使用最终 BookState DTO，支持常用地点类型：

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

## 3. 持久化模型

新增包：

```text
forwin/map/
```

模块职责：

- `models.py`：SQLAlchemy rows。
- `protocol.py`：Pydantic protocol、spec、runtime、result。
- `repository.py`：DTO/ORM 转换、map rows 持久化、runtime 组装。
- `generator.py`：方案 C graph-based 生成器。
- `pathfinding.py`：Dijkstra、metric、observer-aware path。
- `validator.py`：连通性、anchor、权重、region 覆盖校验。
- `service.py`：orchestrator / reviewer / writer 调用入口。

表结构：

- `sub_worlds` 保留并新增地图显式字段：
  - `subworld_type`
  - `scale_level`
  - `culture_profile_json`
  - `terrain_profile_json`
  - `danger_profile_json`
  - `generation_seed`
  - `map_status`
- `map_regions`
- `map_region_edges`
- `map_nodes`
- `map_edges`
- `map_generation_runs`

`map_edges` 是跨 subworld 连接的第一版承载表。跨 subworld route 的规则：

- `subworld_id = from_node.subworld_id`
- `metadata.target_subworld_id` 标记目标 subworld
- `metadata.inter_subworld_edge = true`
- from / to 节点必须是真实存在的 `MapNode`

迁移系统：

- 继续使用 `forwin/models/base.py` 的 lightweight migration。
- 当前地图 schema 版本为 `map_graph_schema_v1`。
- 不引入 Alembic。

## 4. 协议对象

核心输入：

```python
SubWorldMapSpec
MapAnchorNodeSpec
InterSubWorldConnectionSpec
```

核心输出：

```python
MapGenerationResult
BookMapGenerationResult
MapValidationReport
BookMapRuntime
PathResult
```

公共 metric：

```text
distance
travel_time
travel_cost
risk
narrative_cost
composite
```

兼容 alias：

```text
risk_cost
composite_cost
```

`target_node_count` 的硬约束：

```text
target_node_count >= len(required_anchor_nodes) + target_region_count * 3
```

不满足时直接 `ValueError`，不静默降级。

## 5. 生成算法

生成器位于 `forwin/map/generator.py`，算法名：

```text
anchor_graph_v1
```

固定流程：

1. 读取 `SubWorldMapSpec`。
2. 根据 `required_region_roles` 创建必需 regions。
3. 补足普通 regions 到 `target_region_count`。
4. 使用 deterministic `random.Random(generation_seed)` 分配 terrain、culture、danger。
5. 在 region 层生成候选边。
6. 用 MST 保证 `RegionGraph` 连通。
7. 添加 extra region edges 形成绕路和战略通道。
8. 先放置 required anchor `MapNode`。
9. 每个 region 至少生成 `entry_node`、`hub`、`boundary_node`。
10. 补足普通 `MapNode` 到 `target_node_count`。
11. 生成同 region、跨 region、anchor-driven 和 hidden route 候选边。
12. 在无向候选图上执行 MST，保证 subworld 内主图连通。
13. 添加 extra `MapEdge` 到 `round(node_count * target_edge_density)`。
14. 计算 `distance / travel_time / travel_cost / risk_level / narrative_cost`。
15. 运行 validation。
16. validation 通过后落库，并记录 `map_generation_runs`。

ID 稳定性：

- 同一 `spec + generation_seed` 使用 stable hash / UUID5 生成稳定 ID。
- 同一 seed 生成结果稳定。
- 不同 seed 会改变普通 region/node/edge 拓扑和权重分布。

## 6. BookMap 与跨 SubWorld

单 subworld 生成入口：

```python
create_or_update_subworld_map(session, spec, commit=False)
```

BookMap 生成入口：

```python
create_or_update_book_map(
    session,
    specs,
    interconnections=None,
    commit=False,
)
```

当 `interconnections` 为空时，BookMap 生成器会为相邻 specs 创建默认 `world_gate`：

- 自动创建 deterministic exit `MapNode`
- 自动创建 exit connector，把 exit node 接入本地 region hub
- 创建高成本跨 subworld `MapEdge`
- 在 `BookMapRuntime.inter_subworld_edges_by_id` 中投影为 `InterSubWorldEdge`

跨 subworld 默认边权较高：

```text
distance = 1000
travel_time = 120
travel_cost = 500
risk_level = 5
narrative_cost = 10
```

这些值可通过 `InterSubWorldConnectionSpec` 覆写或使用 `cost_multiplier` 调整。

## 7. 路径计算

路径计算位于 `forwin/map/pathfinding.py`，service 入口：

```python
compute_distance(...)
compute_known_distance(...)
resolve_world_node_location_id(...)
```

运行时规则：

- 使用 Dijkstra。
- 权重必须非负；Pydantic 和 repository 都拒绝负数。
- 有向边不自动反向。
- `bidirectional=True` 在 runtime 中展开 reverse edge。
- 多重边允许共存，Dijkstra 按 metric 选择最优 route。
- `status=blocked` 默认跳过，除非 `allow_blocked=True`。
- hidden route 默认在客观图可用；observer 视角按 cognition overlay 过滤。

世界节点间距离不直接在 `WorldNode` 上计算：

```text
world node -> state.location_id -> MapNode -> MapGraph.shortest_path()
```

特殊地点使用双结构：

```text
MapNode: 负责在哪里、怎么去、属于哪个 region
site_state WorldNode: 负责是否开启、谁控制、探索进度、宝物/陷阱状态
```

## 8. 认知视角

认知视角复用最终 BookState 的 `CognitionOverlay`：

- `hidden_refs` 可隐藏 observer 不知道的 `map_edge:<edge_id>`。
- `field_overrides` 可让 observer 认为路线被 blocked、权重变化或状态不同。
- `false_edges` 可加入错误认知路线，用于误判和错误计划模拟。
- `compute_known_distance()` 会加载 observer 最新 overlay，并构造 cognition-aware `MapGraph`。

典型差异：

```text
objective path: 黑石城 -> 密道 -> 遗迹内殿
known path:     黑石城 -> 官道绕行 -> 山路 -> 遗迹外门 -> 遗迹内殿
```

## 9. Writer / Reviewer 集成

Writer context：

- `assemble_context()` 会注入 `map_context`。
- `map_context.active_locations` 记录活跃实体当前位置。
- `map_context.nearby_nodes` 记录当前地点附近节点。
- `map_context.reachable_nodes` 记录从当前地点按 `travel_time` 可达的候选地点。
- `map_context.map_nodes` / `map_context.map_edges` 为 reviewer movement check 提供紧凑图。

Reviewer：

- `ReviewContextPack` 已新增 `map_context`。
- heuristic reviewer 会读取 scene location，按 `travel_time` 计算连续场景移动成本。
- 如果章节时间推进不足以覆盖地图 travel time，会报：
  - `map_travel_time_exceeds_chapter_time`
- 如果两个 scene location 在地图上不可达，会报：
  - `map_path_unreachable`

当前 reviewer 检查是 deterministic heuristic，不替代后续完整 compiler gate。

## 10. 验证标准

地图生成必须满足：

- required anchors 全部存在。
- required anchors 都有 `subworld_id` 和 `region_id`。
- 每个 region 至少有 entry/hub/boundary 基础节点。
- subworld 内主图连通。
- hidden route 不能成为唯一主路径。
- blocked edge 不能切断主线可达性。
- `distance / travel_time / travel_cost / risk_level / narrative_cost` 非负。
- 图密度不能过低或过高。
- 跨 subworld edge 的 from/to `MapNode` 必须存在。
- 每次生成都写 `map_generation_runs`，用于复现和 debug。

当前验证命令：

```bash
python3 -m pytest tests/test_book_state_protocol.py tests/test_book_state_runtime.py tests/test_book_state_schema.py tests/test_map_models.py tests/test_map_generation.py tests/test_map_pathfinding.py tests/test_map_cognition_path.py tests/test_map_world_integration.py tests/test_subworld_control.py -q
```

当前结果：

```text
38 passed
```

编译检查：

```bash
python3 -m py_compile forwin/map/protocol.py forwin/map/service.py forwin/map/generator.py forwin/map/__init__.py forwin/context/assembler.py forwin/protocol/context.py forwin/reviewer/context_builder.py forwin/reviewer/webnovel.py tests/test_map_generation.py tests/test_map_world_integration.py
```

## 11. 当前实现边界

已完成：

- `forwin/map/` 包落地。
- map rows、protocol、repository、service 落地。
- `SubWorld` 大尺度地图字段和 lightweight migration 落地。
- deterministic Scheme C 生成器落地。
- `MapGenerationRunRow` 记录生成输入、摘要、validation report。
- Dijkstra、metric、bidirectional runtime reverse、多重边、hidden/blocked 路径落地。
- `CognitionOverlay` hidden refs、field overrides、false edges 参与认知路径。
- `resolve_world_node_location_id()` 和 world-node distance helper 落地。
- BookMap 跨 subworld `world_gate` 支持落地。
- writer/reviewer `map_context` 接入。
- reviewer 移动时间检查落地。
- 测试中明确包含 `方案 C：Graph-based Weighted Map Generation`。

V4.5.1 已补齐：

- 最小地图 FastAPI route：map runtime、map path、ensure-from-genesis；仍不声明为外部公共 API。
- SubWorldMetaGraph planner v0 已能解析 Genesis `map_atlas.edges` 的跨 subworld edge，显式生成 `world_gate`，并在无可解析 edge 时回退默认相邻链。
- reviewer 当前已覆盖 objective / observer-known path、hidden / blocked / false route、detour、access rule 与 movement speed policy。

仍作为 V4.5.1 后端残项追踪：

- Movement policy 字段语义、issue code 和 trace payload 需要保持稳定。
- Genesis map_atlas 到 BookMap 的 source id、冲突报告和重复 ensure 行为需要继续以后端 contract 固化。

排除到后续产品化版本：

- 线上 `8899` 切换不属于设计完成口径。
- 真实地理板块、tile renderer、Neo4j 主存储。
- Noise / Voronoi / BSP / WFC 作为主生成算法。
- 完整 map/cognition rule pack、复杂 route policy、交通工具体系和可视化编辑器。

## 12. 后续注意事项

- 不再把城市、宗门外门、客栈、遗迹入口建成 `SubWorld`。
- 不把地图路线塞进普通 `WorldEdge`。
- 不让 writer context 泄露 observer 不知道的 hidden objective route。
- 不把 `CognitionOverlay` snapshot 当 canon source。
- 生成失败时不写 map rows；validation 失败只返回 result。
- 更新旧 Genesis / runtime region draft 时，优先提升为 `map_regions`，并保留 legacy source metadata。
