# ForWin Maintenance Log

本文件是项目总维护日志。只记录关键代码改动、行为变化、重要 bug、验证结果和部署状态；不记录日常过程流水账。

维护规则：

- 新条目追加到最上方。
- 每条保持短小，优先写“影响”和“验证”，不要复制完整实现细节。
- 如果改动尚未部署到 `8899`，必须明确写“未部署原因”和“切换条件”。
- 旧的专项日志可保留，但关键结论需要汇总到这里。

## 2026-04-26

### V4.5.1 残余设计文档收束

将 V4.5 后端验收完成后仍散落在各设计文档里的“未完成设计”重新分流：V4.5.x 仍需追踪的后端 contract、migration audit、trace payload、movement policy、Skill governance 进入 `V4.5.1_markstone.md`；World Studio、dashboard、脚本型 Skill、native GraphDelta extractor、完整 route/rule 产品化明确排除到 V4.6+。

关键变化：

- 新增 [V4.5.1_markstone.md](/home/taiwei/.codex/worktrees/b77d/ForWin/Design-docs/V4.5.1_markstone.md)，作为 V4.5.1 残余设计债入口。
- 更新 `V4.5_markstone.md`，把原有“差距”改为 V4.5.1 / V4.6+ 分类，避免把 4.6 产品化能力误当成 4.5.1 阻塞项。
- V4.5.1 明确只保留后端 contract / 文档 / 测试口径，不扩大到 UI、dashboard、脚本执行或 native extractor。
- 逐份审计 `V2_9_2.md`、`V2_9_3_skill_runtime.md`、`V3_8.md`、`V4_final_book_state_runtime.md`、`map_scheme_c.md`、`provisional_mechanism_check.md`、`review-design.rtf`、`review_fix_log_2026-04-15.md`；已覆盖或冲突的旧待办改为历史兼容/已覆盖，仍有效的后端残项并入 `V4.5.1_markstone.md`。

验证：

- `python3 -m pytest tests/test_v45_markstone_docs.py -q`
- 结果：`3 passed`

部署状态：未部署到 `8899`。原因：本轮只更新设计文档，不影响运行服务。切换条件：无。

### V4.5.1 Markstone 残余闭环

收束 V4.5 后端与文档口径：legacy region draft migration、SubWorldMetaGraph planner v0、movement reviewer policy v1 与文档旧状态 guard 已落地。

关键变化：

- `LegacyBookStateImporter` 会把 `SubWorld.metadata_json.region_drafts` 幂等提升为 `map_regions`，保留原草案，并在 migration report / subworld metadata 中写入 promotion report。
- Legacy import API 新增 `legacy_region_promotion_started / succeeded / failed` DecisionEvent，失败可通过 chapter/project ledger 关联定位。
- `ensure_book_map_from_genesis_atlas()` 会解析 Genesis `map_atlas.edges` 的跨 subworld edge，显式生成 `world_gate`，summary 记录 `interconnection_source=atlas_edges`；无可解析 edge 时回退 `default_chain`。
- Movement reviewer 新增 reviewer-only `map_context.movement_policy`：`allowed_access_rule_ids`、`travel_time_multiplier_by_edge_type`、`team_speed_multiplier`，并新增 deterministic issue `map_access_rule_unmet`。
- 新增文档 grep 测试，防止 BookState 追加 gate、无 map route、arc expansion 未接入等旧表述回流。

验证：

- `python3 -m pytest tests/test_book_state_legacy_import.py tests/test_map_world_integration.py -q`
- 结果：`17 passed`
- `python3 -m pytest tests/test_book_state_legacy_import.py tests/test_map_world_integration.py tests/test_v45_markstone_docs.py -q`
- 结果：`19 passed`
- `python3 -m pytest tests/test_book_state_legacy_import.py tests/test_map_world_integration.py tests/test_book_genesis_flow.py -q`
- 结果：`35 passed`
- `python3 -m pytest tests/test_world_v4_orchestrator_gate.py tests/test_api_split_modules.py -q`
- 结果：`7 passed`
- `python3 -m pytest -q --ignore=tests/browser --ignore=tests/test_mcp_server.py`
- 结果：`440 passed, 8 subtests passed`

部署状态：未部署到 `8899`。原因：本轮为代码与文档更新，当前会话未执行容器重建/重启。切换条件：确认无 active generation 后，重建并重启 `forwin` 服务。

### V4.5 Markstone 验收闭环补完

补齐 V4.5 验收闭环后端范围：BookState 成为 canon 优先路径，Scheme C 地图支持 arc expansion，reviewer 区分 objective path 与 observer-known path，并把 BookState / map / reviewer 的关键失败接入 `DecisionEvent`。

关键变化：

- `assemble_context()` 优先读取 BookState runtime overlay：narrative line、knowledge gap、角色地点和 `site_state <-> MapNode` binding。
- `WritingOrchestrator` 改为 BookState review/compile 先提交 canon；旧 `world_model_v4` 降级为 compatibility projection，失败记录 `legacy_projection_failed`，不回滚 BookState canon。
- 新增 `ensure_book_map_from_genesis_atlas()`，arc materialization 可从 Genesis atlas 增量生成缺失 subworld/region，并通过 `world_gate` 连通已有 BookMap。
- 新增最小 map API：map runtime、map path、ensure-from-genesis；BookState runtime/path/legacy import API 增加稳定 response model。
- Reviewer context 增加 reviewer-only objective graph 和 cognition overlay；movement reviewer 增加 hidden route、blocked route、false route、observer-known detour 的 deterministic issue code。
- Legacy import 返回 migration report，并为 legacy location/city/ruin 创建 `site_state <-> MapNode` binding，避免误建为 `SubWorld`。

验证：

- `python3 -m pytest tests/test_book_state_repository_projection_compiler.py::test_context_assembly_prefers_book_state_runtime_overlay tests/test_map_world_integration.py::test_arc_map_expansion_adds_missing_subworld_and_world_gate tests/test_map_world_integration.py::test_reviewer_flags_observer_known_hidden_route_detour -q`
- 结果：`3 passed`
- `python3 -m pytest tests/test_book_state_repository_projection_compiler.py tests/test_book_state_legacy_import.py tests/test_book_state_final.py tests/test_map_world_integration.py tests/test_map_genesis_adapter.py tests/test_map_generation.py -q`
- 结果：`27 passed`
- `python3 -m pytest tests/test_api_split_modules.py tests/test_world_v4_api.py tests/test_governance_decision_api.py -q`
- 结果：`10 passed, 3 subtests passed`
- `python3 -m pytest tests/test_book_genesis_flow.py tests/test_world_v4_orchestrator_gate.py -q`
- 结果：`22 passed`
- `python3 -m pytest -q --ignore=tests/browser --ignore=tests/test_mcp_server.py`
- 结果：`434 passed, 8 subtests passed`

部署状态：未部署到 `8899`。原因：本轮为代码与文档更新，当前会话未执行容器重建/重启。切换条件：确认无 active generation 后，重建并重启 `forwin` 服务。

### V4.5 M3：Genesis start-writing 自动生成 Scheme C BookMap

将 Scheme C 地图接入 Genesis 到写作的首段主链。新 Genesis 项目在 `start-writing` handoff 时会把 `world.map_atlas` 转换为 `SubWorldMapSpec`，生成初始 BookMap，并把可见地图摘要送入 writer context；已有 BookMap 的项目会跳过自动生成，避免覆盖手工地图。

关键变化：

- 新增 `forwin/map/genesis_adapter.py`，把 Genesis `submaps / regions / nodes / key_locations` 转为 Scheme C map spec，并保留 source ids 到 anchor metadata。
- `start_project_writing()` 在启动 generation task 前调用 BookMap 初始化；地图 validation 失败时阻断启动、回滚 arc/chapter 物化，并写入 `map_generation_failed` DecisionEvent。
- 新增 `map_generation_started / map_generation_succeeded / map_generation_failed` 审计事件类型。
- `assemble_context()` 会解析 Genesis core_cast 的 `current_base / home_location`，映射到生成后的 `MapNode`，并给 writer prompt 输出 `【地图运行时】` 可见位置和 nearby route 摘要。
- 更新 `V4.5_markstone.md`，将 M3 差距从 “start-writing 或 arc expansion 均未接入” 调整为 “start-writing 已接入”；后续 V4.5 验收闭环已补齐 arc expansion。

验证：

- `python3 -m pytest tests/test_map_genesis_adapter.py tests/test_map_generation.py tests/test_map_pathfinding.py tests/test_map_cognition_path.py tests/test_map_world_integration.py tests/test_book_genesis_flow.py tests/test_subworld_control.py -q`
- 结果：`46 passed`
- `python3 -m pytest tests/test_api_split_modules.py tests/test_api_pages_rendering.py -q`
- 结果：`6 passed`
- `python3 -m pytest -q --ignore=tests/browser --ignore=tests/test_mcp_server.py`
- 结果：`427 passed, 8 subtests passed`

部署状态：未部署到 `8899`。原因：本轮是主链代码与文档更新，当前会话未执行容器重建/重启。切换条件：确认无 active generation 后，重建并重启 `forwin` 服务。

### V4.5 Markstone 文档收束与旧设计清理

将旧 V2/V3 历史设计稿清理出 `Design-docs/`，新增 V4.5 当前差距与后续里程碑入口，避免新开发继续引用已被 BookState final runtime 和 Scheme C 地图覆盖的旧语义。

关键变化：

- 新增 [V4.5_markstone.md](/home/taiwei/.codex/worktrees/b77d/ForWin/Design-docs/V4.5_markstone.md)，汇总当前代码实况、保留文档、已删文档、文档-代码差距和 V4.5 后续里程碑。
- 删除旧设计稿：`V2_8.md`、`V2_8_1.md`、`V2_8_1_completion_status.md`、`V2_9.md`、`V2_9_1.md`、`V3_0.md`、`project_master_plan.md`、`third_version_v2_3_writer_human_error_isolation.md`、`v2_6_phased_rollout_plan.md`、`v2_7.md`。
- 更新 `V2_9_2.md`，明确其只保留 Genesis / Writer / Review / Governance 基线；其中旧 `SubWorld` 局部派生语义降级为历史兼容。
- 更新 `V4_final_book_state_runtime.md` 与 `map_scheme_c.md`，统一指向 V4.5 差距入口，并补充 BookState API/gate 与地图 service/API 边界。

验证：

- `find Design-docs -maxdepth 1 -type f | sort`
- `grep -RIn "V4.5_markstone\\|方案 C：Graph-based Weighted Map Generation\\|BookState debug API" Design-docs`

部署状态：未部署到 `8899`。原因：本轮只更新文档和清理过期设计稿，不影响运行服务。切换条件：无。

### 地图系统方案 C：Graph-based Weighted Map Generation 首轮落地

将地图系统从旧 `SubWorld` 局部舞台语义升级为大尺度 `BookMap`：`SubWorld -> Region -> MapNode -> MapEdge`。本轮不新增 FastAPI 路由，只提供 service 层给 orchestrator、reviewer、writer 和测试调用。

关键变化：

- 新增设计/实现文档：[map_scheme_c.md](/home/taiwei/.codex/worktrees/b77d/ForWin/Design-docs/map_scheme_c.md)。
- 新增 `forwin/map/` 包，包含 `models / protocol / repository / generator / pathfinding / validator / service`。
- `SubWorld` 升级为大陆、星球、位面、异世界等大尺度地图容器，并新增 map metadata 显式字段。
- 新增 `map_regions / map_region_edges / map_generation_runs`，扩展 `map_nodes / map_edges`，并通过 `map_graph_schema_v1` 接入 lightweight migration。
- 实现 `方案 C：Graph-based Weighted Map Generation`：required regions、anchor nodes、RegionGraph MST、MapGraph MST、extra edges、权重计算、validation report 和 generation run 记录。
- 新增 `create_or_update_book_map()`，通过 `world_gate`、exit `MapNode` 和跨 subworld `MapEdge` 支持 BookMap 级连接。
- 路径计算支持 Dijkstra、metric 切换、有向边、多重边、bidirectional runtime reverse、blocked/hidden filter、observer cognition overlay、field overrides 和 false edges。
- `assemble_context()` 和 `ReviewContextPack` 接入 `map_context`；reviewer heuristic 可检查连续场景移动不可达或 travel time 超过章节时间推进。

验证：

- `python3 -m pytest tests/test_book_state_protocol.py tests/test_book_state_runtime.py tests/test_book_state_schema.py tests/test_map_models.py tests/test_map_generation.py tests/test_map_pathfinding.py tests/test_map_cognition_path.py tests/test_map_world_integration.py tests/test_subworld_control.py -q`
- 结果：`38 passed`
- `python3 -m py_compile forwin/map/protocol.py forwin/map/service.py forwin/map/generator.py forwin/map/__init__.py forwin/context/assembler.py forwin/protocol/context.py forwin/reviewer/context_builder.py forwin/reviewer/webnovel.py tests/test_map_generation.py tests/test_map_world_integration.py`

部署状态：未部署到 `8899`。原因：本轮是地图持久化、生成、路径和 reviewer/service 集成的代码与文档更新，未切换线上服务。切换条件：后续接入 orchestrator 主链路或线上 reviewer 前，先确认无 active generation，再重建并切换服务。

### BookState API / V4 gate 接入同步

同步 master worktree 中尚未提交的 BookState final runtime 接入：在保留当前地图升级与 BookState runtime 实现的基础上，补齐 API 调试入口和 V4 写作链路后的 BookState review/compile gate。

关键变化：

- 新增 `forwin/api_book_state_routes.py`，提供 runtime status、map path query 和 legacy import 三个 BookState 调试入口。
- `api_route_registry` 注册 `/api/projects/{project_id}/book-state/runtime`、`/book-state/map/path`、`/book-state/legacy-import`。
- `WritingOrchestrator` 在现有 V4 review/compiler commit 后，把 approved changes 转换为 `ApprovedGraphDeltaSet`，再经过 `BookStateReviewGate` 与 `BookStateCompiler`。
- `forwin.book_state.__init__` re-export `BookStateDeltaAdapter`、`BookStateReviewGate` 和 `NarrativeControlGraph`，避免调用方直接依赖内部模块。
- 新增 `tests/test_book_state_final.py` 作为 master worktree final runtime 集成测试。

验证：

- 待本轮 merge 后随地图/BookState 组合测试一起回归。

部署状态：未部署到 `8899`。原因：本轮只是把 master worktree 的未提交 BookState 接入同步到当前 worktree；线上切换仍需先确认无 active generation。

### BookState 持久化 / 回放 / 编译增量闭环

在首轮 BookState DTO、ORM、runtime 基座上，新增独立的持久化与 replay 垂直切片。旧 `world_model_v4` 仍是当前 orchestrator 主链路；本轮没有切换生产章节生成入口。

关键变化：

- 新增 `BookStateRepository`，支持最终 BookState 表的 DTO/ORM 双向转换、ledger 追加、snapshot 读写和运行时基础加载。
- 新增 `BookStateProjection`，支持按章节从 base rows、最近 snapshot 和 `GraphDelta` 回放重建 `BookStateRuntime`。
- 新增 `BookStateCompiler`，支持 `ApprovedGraphDeltaSet` append-only 写入、patch old_value 冲突保护、状态行追加和 snapshot 物化。
- 新增 `LegacyBookStateImporter`，把旧 `entities / entity_states / relation_edges` 与旧 V4 world lines / gaps / reveals 最小导入到最终 BookState 表。
- 新增 `BookStateCompileResult`，为后续替换 extractor/reviewer/orchestrator 提供明确编译结果接口。

验证：

- `.venv/bin/python -m pytest -q tests/test_book_state_protocol.py tests/test_book_state_schema.py tests/test_book_state_runtime.py tests/test_book_state_repository_projection_compiler.py tests/test_book_state_legacy_import.py`
- `.venv/bin/python -m pytest -q tests/test_world_v4_schema.py tests/test_world_v4_repository.py tests/test_world_v4_projection_materialization.py`

部署状态：未部署到 `8899`。原因：本轮只新增并行 BookState 闭环，不切换 API 或 orchestrator。切换条件：后续接入主链路前，需补 extractor/reviewer/retrieval API 适配并确认无 active generation。

### V4 Final BookState Runtime 设计落档与基础基座实现

新增最终版 BookState Runtime 设计文档，并实现首轮协议 / schema / runtime 基座。后续世界模型不再继续强化轻量 `world_model_v4`，而是按 `Typed Property Graph + MapGraph + Sparse Cognition Overlay + Append-only Delta Ledger + Materialized Snapshot` 破坏性重建。

关键变化：

- 新增设计文档：[V4_final_book_state_runtime.md](/home/taiwei/.codex/worktrees/b77d/ForWin/Design-docs/V4_final_book_state_runtime.md)。
- 明确 canon source 改为 `GraphDelta + patch rows`，旧 `entities / entity_states / relation_edges` 和轻量 v4 rows 降级为兼容投影、迁移来源和历史审计证据。
- 明确 `MapGraph` 独立于 ObjectiveWorldGraph，支持带权有向多重图、路径计算、hidden/blocked route、observer-aware known distance。
- 明确 `CognitionOverlay` 不复制客观世界图，只保存 mask、override、false additions。
- 给出 schema、runtime、pipeline、迁移、测试与验收标准。
- 新增 `forwin/protocol/book_state.py`、`forwin/models/book_state.py`、`forwin/book_state/`，实现最终 BookState DTO、ORM 表、MapGraph 路径计算、CognitionView 和 ObjectiveWorldGraph patch replay 基础。
- 首轮为避免旧 v4 `cognition_snapshots` 表冲突，最终版认知物化表先命名为 `book_cognition_snapshots`，后续迁移阶段再收口。

验证：

- `test -f Design-docs/V4_final_book_state_runtime.md`
- `grep -n "Typed Property Graph\\|MapGraph\\|CognitionOverlay\\|GraphDelta" Design-docs/V4_final_book_state_runtime.md`
- `PYTHONPATH=. pytest -q tests/test_book_state_protocol.py tests/test_book_state_schema.py tests/test_book_state_runtime.py`
- `PYTHONPATH=. pytest -q tests/test_world_v4_schema.py tests/test_world_v4_repository.py tests/test_world_v4_projection_materialization.py`

部署状态：未部署到 `8899`。原因：本轮新增基础代码但尚未切换 orchestrator / compiler 主路径，不需要服务切换。切换条件：后续实现进入服务切换前，必须先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`。

## 2026-04-24

### V3.0 WorldModel 设计文档与维护口径更新

新增 V3.0 统一设计文档，明确 WorldModel 是 canon 之后的确定性编译层，不替代 DB、Genesis、StateUpdater 或 DecisionEvent。

关键变化：

- 新增设计文档：`V3_0.md`。该历史文档已在 V4.5 文档清理中删除，语义由 `V4_final_book_state_runtime.md` 和 `V4.5_markstone.md` 覆盖。
- 文档把原 V3.1-V3.6 六轨路线压缩为四个交付阶段：`V3.1` WorldModel 核心基座与只读 Wiki、`V3.2` canon 后自动编译与上下文接入、`V3.3` World Studio 与 Obsidian proposal 闭环、`V3.4` 冲突治理/图谱/LLM Wiki 维护。
- 明确权威边界：DB / raw events / DecisionEvent 是源，`WorldModelSnapshot` 是可重建投影，Markdown / Obsidian 不是 canon。
- 记录当前实现边界：proposal review 已有状态流和重新编译框架，但完整 adapter 到 Genesis / EntityState / RelationEdge / Conflict resolution 仍是后续工作。

验证：

- `test -f Design-docs/V3_0.md`
- `grep -n "V3.0 WorldModel\\|四阶段路线图\\|V3.4" Design-docs/V3_0.md Design-docs/maintenance_log.md`

部署状态：未部署到 `8899`。原因：本轮只更新设计文档与维护日志，不需要重建服务。切换条件：后续若要让线上页面/API 使用 V3.0 代码，需要先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`，再执行容器重建与服务切换。

## 2026-04-22

### Genesis 世界观根模型统一（`BookGenesisPack.world`）

把 Genesis 的世界观权威结构从顶层散列字段收口为单一 `world` 根对象；后续 `world / map / story_engine` 三阶段都改为编辑 `pack.world` 下的不同切片。

关键变化：

- `BookGenesisPack` 顶层现在固定为 `book_brief + world + book_arc_blueprint + subworld_policy + execution_bootstrap + stage_states`，不再在顶层持久化 `world_bible / map_atlas / story_engine`。
- `world` 根对象新增并统一收纳 `minimum_world_system / minimum_extension_pack / world_bible / map_atlas / story_engine / institution_profiles / resource_economy_profiles / world_extensions / template_libraries`。
- Genesis 服务端新增 world-root normalize / upgrade 路径；旧 revision 读取时会把顶层旧键自动升级投影到 `world`，但再次保存时只写回新结构。
- `map_atlas.submaps[] / nodes[]` 与 `story_engine.factions[]` 现在会自动补稳定 `id`；跨制度与资源模板引用统一走 `scope_ref = { type, id }`。
- `start-writing`、project detail、context assembler、Genesis 前端工作台与阶段保存逻辑已全部改为从 `pack.world.*` 读取或写回。

验证：

- `python3 -m py_compile forwin/book_genesis.py forwin/api_schemas.py forwin/api.py forwin/api_project_payloads.py forwin/api_project_ops.py forwin/context/assembler.py forwin/world_templates.py`
- `PYTHONPATH=. pytest -q tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py tests/test_subworld_control.py`
- 全量回归：`PYTHONPATH=. pytest -q`

结果：

- Genesis 新项目默认带完整 `world` 根骨架
- 旧 revision 自动升级与新结构写回已覆盖回归
- 全量测试通过：`249 passed, 8 subtests passed`

部署状态：未单独部署到 `8899`。原因：本轮主要是根模型重构、前端路径切换与本地全量回归；未在当前节点单独执行部署切换。

### 首页 favicon 404 修复与页面实测回归

在真实浏览器走 Genesis 页面链路时，发现首页首次加载会请求 `/favicon.ico` 并产生 404 console error；已通过页面内联 favicon 修复。

关键变化：

- `render_page_document()` 现在统一注入内联 favicon，避免浏览器额外请求缺失资源。
- 页面渲染测试补了 `rel=\"icon\"` 与 `data:image/svg+xml` 断言，防止后续回归。
- 修复后重新构建容器，并用 `master` worktree 的 `.env` 做页面实测。

验证：

- `PYTHONPATH=. pytest -q tests/test_api_pages_rendering.py`
- Playwright live 流程：
  - 首页打开
  - 新建书本并进入 Genesis 工作台
  - `world` 阶段编辑保存
  - `map` 阶段新增 `submap`
  - `story_engine` 阶段新增 `faction` 并绑定 `base_subworld`
- 容器使用 `/home/taiwei/ForWin/.env` 启动后，`GET /health` 返回 `{\"status\":\"ok\"}`

结果：

- 缺失 favicon 的 console error 已消失
- Genesis 关键页面链路实测可走通

部署状态：未单独记录为 `8899` 新部署；该修复已并入 `master` 文档口径。

## 2026-04-21

### V2.9.3 Skill Runtime 接入（Genesis / Writer / Reviewer / PromptTrace / ModelAdapter）

把 `SKILL.md` 类工作流正式吸收到 ForWin 运行时里，作为内部 workflow layer，而不是外部插件。首批覆盖 `Genesis + Writer + Reviewer`，并同步引入 `ModelAdapter` 抽象。

关键变化：

- 新增 `forwin/skills/`：支持加载 `forwin_skills/**/SKILL.md`、按 `scope + stage_key + task_family` 路由、把技能编译成 prompt layers。
- 新增首批内置 skills：Genesis 六阶段、writer `chapter-outline / scene-drafting / style-control`、reviewer `chapter-continuity / repair-plan`。
- `BookGenesisService` 现在会在阶段生成 / refine / launch arc 规划前选择 skill layers，并把 `selected_skills / skill_summary / kind=skill` 写入 `PromptTrace`。
- `ChapterWriter` 与 orchestrator 现在支持 writer skill layers；章节初稿、重写链都会落单独 writer trace，并通过 `parent_trace_id` 串起 review / rewrite 链路。
- `HistoricalReviewHub` 支持 reviewer skill rubric，但技能只增强 review notes 和 repair guidance，不改写最终 `verdict`。
- 新增 `ModelAdapter / ModelCapabilities`；现有 OpenAI-compatible LLM client 继续沿用原行为，但升级为 adapter 语义。
- 新增全局 runtime/config 字段：`skill_runtime_enabled / skill_registry_path / skill_strictness / enabled_skill_groups / disabled_skill_ids`。
- 新增设计文档：[V2_9_3_skill_runtime.md](/home/taiwei/.codex/worktrees/461a/ForWin/Design-docs/V2_9_3_skill_runtime.md)。

验证：

- `python3 -m py_compile forwin/model_adapter.py forwin/skills/__init__.py forwin/skills/models.py forwin/skills/policy.py forwin/skills/loader.py forwin/skills/registry.py forwin/skills/router.py forwin/skills/prompt_layer.py forwin/book_genesis.py forwin/writer/llm_client.py forwin/writer/chapter_writer.py forwin/writer/prompts.py forwin/reviewer/hub.py forwin/orchestrator/loop.py forwin/runtime_settings.py forwin/config.py tests/test_skill_runtime.py tests/test_book_genesis_flow.py tests/test_phase05_regressions.py`
- `PYTHONPATH=. pytest -q tests/test_skill_runtime.py tests/test_book_genesis_flow.py tests/test_phase05_regressions.py`

部署状态：未部署到 `8899`。原因：本轮先完成运行时接入、trace 持久化和本地回归，还没有执行容器重建。切换条件：先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`，再执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

## 2026-04-22

### V2.9.2B review/repair、task-center 与 API wiring 收口

把这轮主干修补集中收口到三块：review/repair 语义、task-center 统计与轮询语义、API/task-center wiring。

关键变化：

- `blackbox` 仍保留“三次 review fail 后允许直接放行”的自动化语义，但只对**第 3 次 rewrite 后仍然 review fail 的真实正文**生效；writer 异常、writer 返回空正文、repair 没有产出可 review draft 时，不再 force accept。
- repair 链改成固定三次：第 1 次 `scene`，第 2 次 `band`，第 3 次由 reviewer escalation 决定走 `band` 还是 `arc`；普通 review 不再自动把 repair scope 往 `arc` 推。
- `RepairInstruction` 新增 `scope_reason`；review detail API 现在会暴露 `latest_repair_scope_reason`，便于前端和治理面板解释为什么第 3 次升到 `band` 或 `arc`。
- `GenerationControlInfo.accepted_chapters` 修正为 accepted-only；新增 `drafted_chapters`、`generated_chapters`，task-center 与驾驶舱 UI 改成“已生成 / 已接受 / 待 Review”三套统计口径。
- task-center 后端把 provisional history 回填从 per-task 查询改成 bulk 匹配；前端首页把平台轮询改成视图感知，books/task-center 列表签名改成轻量 fingerprint，不再对整份 payload 做递归排序 + `JSON.stringify`。
- `api.py` 的任务中心能力抽到 `TaskCenterService`，路由注册改成 `ApiRouteDeps` / `TaskRouteDeps` 依赖注入；`forwin/publishers/deprecated/` 整个旧发布实现目录已删除，仅保留 `server_uploader.py` 墓碑 stub。

验证：

- `python3 -m pytest -q tests/test_generation_control_payload.py`
- `python3 -m pytest -q tests/test_api_split_modules.py`
- `python3 -m pytest -q tests/test_governance_decision_api.py`
- `python3 -m pytest -q tests/test_phase05_regressions.py -k "rewrite or force_accept or repair or task_center or generation_control"`
- `python3 -m pytest -q tests/test_generation_task_persistence.py -k provisional_preview_history_is_backfilled_from_execution`
- `python3 -m pytest -q`

结果：

- targeted regressions 全部通过
- 全量 Python 回归：`235 passed, 8 subtests passed`

部署状态：未部署到 `8899`。原因：本轮只完成代码和本地全量回归，还没有重建 `forwin` 容器并切到线上端口。切换条件：先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`，再执行 `docker compose build forwin` 与 `docker compose up -d forwin`，随后 smoke `GET /health`、`GET /api/task-center/items?limit=20`、`GET /api/projects?limit=5`。

## 2026-04-20

### V2.9.2A 文化命名生成器接入（culture lexicon / Genesis 自动命名 / 前端一键生成）

把文明词库命名生成器正式接进 Genesis：文化背景可以声明命名文明与叠加文明，系统生成 map/story 时会带命名辅助；前端也能在需要名字的字段上一键随机生成。

关键变化：

- 新增 `forwin/naming/culture_name_generator.py`，内置 `中华 / 维京 / 罗马 / 西欧/英国 / 南美/拉丁 / 基督教 / 穆斯林` 词库与混合文明命名逻辑。
- `world_bible.culture_profiles[]` 新增 `generator_civilization / generator_overlays`；normalize 会自动补人名、地区名、地点名样例。
- 新增 Genesis 名称生成接口：`POST /api/projects/{project_id}/genesis/generate-name`，可基于当前文化背景为字段返回建议值，不直接覆写 revision。
- Genesis 前端在 `character_name_examples / region_name_examples / location_name_examples`、角色名、势力名、对手名、小世界名、地区名、地点名旁新增 `自动生成` 按钮。
- system stage generation 现在会把文化命名辅助注入 `map / story_engine` prompt；fallback map/story 也会优先尝试走文化命名生成器，而不是只用硬编码占位名。

验证：

- `python3 -m py_compile forwin/naming/__init__.py forwin/naming/culture_name_generator.py forwin/book_genesis.py forwin/api_schemas.py forwin/api_project_ops.py forwin/api_project_routes.py forwin/api_route_registry.py tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`

结果：

- `12 passed`

部署状态：未部署到 `8899`。原因：本轮完成代码、规格和本地回归，但还没有重建 `forwin` 容器。切换条件：先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`，再执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

### V2.9.2A 文化背景与命名体系骨架（culture profiles / culture refs）

给 Genesis 根层补上“文化背景 -> 人名/地区名/地点名”命名体系骨架，并把地图、角色、势力对象接到同一套文化引用上，方便后续补真实文化母本与命名词库。

关键变化：

- `world_bible` 新增 `culture_profiles[]`，每项包含 `id / name / summary / inspiration / social_markers / aesthetic_keywords / character_name_style / region_name_style / location_name_style / *_examples / usage_notes`。
- `世界观与背景` 阶段新增 `文化背景` 子项工作台；可以逐条编辑文化背景，而不是只靠一个 `naming_style` 字符串。
- `map_atlas.submaps[] / regions[] / nodes[]` 新增可选 `culture_profile_id`；后续可把小世界、地区、地点挂到对应文化背景。
- `story_engine.core_cast[] / factions[] / opposition[]` 新增可选 `culture_profile_id`，为角色名、势力名与地域命名风格预留绑定点。
- Genesis world/map/story 生成 prompt 与 fallback 已同步扩到文化背景和文化引用；world/map/story normalize 路径会收口这些字段，避免结构漂移。

验证：

- `python3 -m py_compile forwin/book_genesis.py forwin/api_pages.py tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`

结果：

- `11 passed`

部署状态：未部署到 `8899`。原因：本轮只完成本地结构搭建与回归，还没有重建 `forwin` 容器。切换条件：先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`，再执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

### V2.9.2A 地区层与运行时地区草案（Genesis regions / 多势力归属 / runtime region drafts）

把 Genesis 地图从 `submaps + nodes` 升级为 `submaps + regions + nodes` 三层结构，并让运行时新 subworld 能携带地区草案；同时补齐角色多势力归属与势力跨地区分布。

关键变化：

- `BookGenesisPack.map_atlas` 新增 `regions[]`，固定支持 `subworld_name / parent_region_id / level / kind / culture_traits / climate / terrain / controller_factions / resource_themes`；地区层级限制为最多两级。
- Genesis 地图工作台改为 `小世界 / 地区 / 地点` 三类子项，`地点` 可挂地区；结构化表单继续与最终 JSON 并存，子项级 AI 改写新增支持 `regions[n]`。
- `story_engine.core_cast[]` 新增 `home_region / current_region / faction_memberships[]`；`faction_memberships` 允许多势力归属，但只允许唯一 primary。
- `story_engine.factions[]` 新增 `headquarters_region / footprint[]`；`story_engine.opposition[]` 新增 `base_region / backing_factions[]`，同时保留旧字段兼容投影。
- Genesis map fallback / generate prompt 统一扩到 `regions[]`；story engine fallback / generate prompt 同步消费地区层，而不是只靠 subworld/location。
- `SubWorldPlanItem` 新增 `region_seeds[]`；runtime 新 subworld 生成时现在会顺手给出最小地区脚手架，`SubWorldManager.apply_arc_delta()` 会把它们写入 `SubWorld.metadata_json.region_drafts`，并标记 `region_source=runtime_generated`、`region_promotion_state=draft`。
- `assemble_context()` 现在会同时汇总 Genesis 正式地区和当前 active subworld 的 runtime region drafts，作为 `genesis_map_overview` 的一部分喂给 writer / reviewer。

验证：

- `python3 -m py_compile forwin/protocol/subworld.py forwin/director/arc_director.py forwin/subworld_manager.py forwin/book_genesis.py forwin/api_pages.py forwin/state/repo.py forwin/context/assembler.py tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py tests/test_subworld_control.py`
- `PYTHONPATH=. pytest -q tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py tests/test_subworld_control.py`
- `PYTHONPATH=. pytest -q tests/test_arc_execution_scoping.py tests/test_continue_project_orphan_review.py tests/test_phase05_regressions.py -k "create_project or continue_project or subworld"`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/api/tasks/active-generation-check`
- `docker compose build forwin`
- `docker compose up -d forwin`
- Playwright headless live check on `http://127.0.0.1:8899/`
  - 打开 Genesis 工作台，确认 `地图与空间拓扑` 阶段可切换到 `地区` 子项类型
  - 确认 `地区` 子项工作台显示 `subworld_name / parent_region_id / level / kind / culture_traits / climate / terrain / controller_factions / resource_themes`
  - 确认 `角色势力与叙事引擎` 阶段显示 `home_region / current_region / faction_memberships`

结果：

- `tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py tests/test_subworld_control.py`: `20 passed`
- `tests/test_arc_execution_scoping.py tests/test_continue_project_orphan_review.py tests/test_phase05_regressions.py -k "create_project or continue_project or subworld"`: `9 passed, 121 deselected`
- `GET /health`: `{"status":"ok"}`
- live Genesis 页面已确认地区层与角色地区/多势力字段可见并可进入工作台

部署状态：已部署到 `8899`。部署前确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`；部署后 `GET /health` 正常，Playwright live 检查确认 Genesis 地图工作台已经暴露 `地区` 子项类型，角色工作台已经暴露 `home_region / current_region / faction_memberships`。

### V2.9.2 Genesis 结构化编辑补齐（属性表单 / 子世界属性 / 角色-势力-子世界关联）

把 Genesis 工作台从“主要改 JSON”推进到“结构化属性编辑 + 最终 JSON 并存”，并补齐 `subworld` 的文化/气候/地形维度，以及角色/势力与子世界的显式关联字段。

关键变化：

- Genesis 每个阶段现在都先显示结构化字段表单，再显示“最终 JSON / 高级编辑”；结构化字段改动会同步回 JSON，用户不再只能直接改原始 JSON。
- `世界观与背景` 阶段保留 `规则 / 历史 / 命名 / 禁区` 子项工作台，可按字段定向对话改写，不要求整段重生。
- `地图与空间拓扑` 的 `submaps` 扩展 `culture_traits`、`climate`、`terrain`、`governing_power`、`resident_factions`、`resource_themes`；`nodes` 也新增 `parent_subworld` 与气候/地形/文化备注。
- `角色势力与叙事引擎` 的角色、势力、对手盘补齐跨对象关联：角色支持 `home_subworld / home_location / current_base / affiliated_faction / affiliated_family`，势力支持 `base_subworld / base_location / territory_scope / culture_keywords`，对手盘支持 `base_subworld / base_location / backing_faction`。
- `整本书多 Arc 路线图` 继续保留自动生成优先，同时开放 Arc 级结构化子项编辑；用户可选中单个 Arc 做局部改写，但不强制手工逐 Arc 编排。
- Genesis 前端新增本地 draft 同步和阶段/子项双层结构化表单渲染；原始 JSON 仍然保留为最终真值视图与高级编辑入口。

验证：

- `python3 -m py_compile forwin/book_genesis.py forwin/api_pages.py tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`
- `node --check /tmp/forwin_home_script_debug.js`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/api/tasks/active-generation-check`
- `docker compose build forwin`
- `docker compose up -d forwin`
- Playwright headless live check on `http://127.0.0.1:8899/`
  - 确认 Genesis 阶段属性表单可见
  - 确认 `地图与空间拓扑` 可新增 `小地图 / 子世界` 并显示文化/气候/地形等字段
  - 确认 `角色势力与叙事引擎` 显示角色与势力的子世界/归属关联字段
  - 确认 `整本书多 Arc 路线图` 显示 Arc 级子项工作台

结果：

- `tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`: `11 passed`
- live Genesis 页面已确认结构化编辑可用；当时剩余 console error 为 `favicon.ico 404`，未影响 Genesis 流程。该问题已于 `2026-04-22` 修复。

部署状态：已部署到 `8899`。部署前确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`；部署后 `GET /health` 正常，Playwright live 流程确认 Genesis 阶段属性表单、子项工作台与最终 JSON 同时可用。

### V2.9.2 Genesis 模型选择补齐（每个 AI 入口可选 profile）

Genesis 工作台里的 AI 入口补齐模型选择，避免阶段生成/重生/对话改写继续偷用默认模型。

关键变化：

- `生成`、`重生`、`改写当前阶段`、`改写选中子项` 统一支持 `model_profile_id`，Genesis 工作台新增共享 `Model Profile` 下拉。
- Genesis API 新增阶段运行请求体；后端会按所选 profile 构建临时 runtime config，而不是继续隐式吃默认模型。
- `PromptTrace.model_profile` 现在额外记录 `profile_id` / `profile_name`，Trace 面板可直接看到这次用了哪条模型配置。
- 回归新增：验证 Genesis 阶段生成会吃到选中的 profile，并把 profile 信息持久化到 trace；页面渲染也校验 Genesis 模型选择控件存在。

验证：

- `python3 -m py_compile forwin/api.py forwin/api_schemas.py forwin/book_genesis.py forwin/api_pages.py tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`
- `curl -fsS http://127.0.0.1:8899/api/tasks/active-generation-check`
- `docker compose build forwin`
- `docker compose up -d forwin`
- `curl -fsS http://127.0.0.1:8899/health`
- Playwright live 检查 `/?workspace=genesis&project_id=...`，确认 Genesis 工作台已显示 `Model Profile` 下拉

部署状态：已部署到 `8899`。部署前确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`；本轮本地回归 `10 passed`，live Genesis 页面已确认模型选择控件存在。

### V2.9.2 Genesis 交互补强（世界观子项工作台 / Arc 蓝图自动生成口径）

把 Genesis 工作台从“地图/角色支持子项对话改写”继续扩到 `WorldBible`，同时明确 `BookArcBlueprint` 仍然沿用自动生成优先、手动编辑可选的产品边界。

关键变化：

- `世界观与背景` 阶段新增 4 个可选子目标：`规则`、`历史`、`命名`、`禁区`；可选中单个目标并通过 AI 对话只改该字段。
- Genesis 子项工作台从“只支持数组对象”扩成“同时支持单字段目标”；地图/角色/势力原有子项工作流保持不变。
- `BookGenesisService.refine_stage()` 对 `target_path` 不再只接受 dict 子对象；现在支持标量与数组字段，采用 `{ "value": ... }` 包装协议定向改写。
- 前端在空的 `world_bible` 上直接点“改写选中子项”时，会先把默认模板同步进当前阶段 JSON，再发起 AI refine，避免 `target_path` 因字段不存在而失败。
- `整本书多 Arc 路线图` 阶段补了明确文案：默认保留自动生成逻辑，用户可选手改 JSON 或对整阶段发指令调整，不强制拆成手工逐 Arc 编辑。

验证：

- `python3 -m py_compile forwin/api_pages.py forwin/book_genesis.py tests/test_api_pages_rendering.py tests/test_book_genesis_flow.py`
- `PYTHONPATH=. pytest -q tests/test_book_genesis_flow.py tests/test_api_pages_rendering.py`
- Playwright headless live check on `http://127.0.0.1:8899/`
  - 确认 `世界观与背景` 显示 `规则/历史/命名/禁区`
  - 确认 `整本书多 Arc 路线图` 显示“Arc 蓝图默认沿用自动生成逻辑”

部署状态：已部署到 `8899`。部署前确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`；部署后 `curl -fsS http://127.0.0.1:8899/health` 正常，`docker compose ps forwin` 为 `healthy`。

## 2026-04-19

### Linux 发布浏览器恢复在线（Chromium 切换 / 脏 profile 自愈 / preferred client 心跳恢复）

修复后端 Linux 发布浏览器长期不 heartbeat 的根因，并把恢复逻辑做成启动期自愈。

关键变化：

- 根因定位为两段叠加：
  - 旧的 Linux profile 曾在不支持命令行开发扩展的浏览器态下把 `ForWin Publisher Bridge` 标成 `DISABLED + unsupportedDeveloperExtension`
  - 后续即使启动链、API key、backend URL 正确，也会持续复用这份坏 profile，导致扩展 worker 永远不启动、preferred client 不 heartbeat
- `publisher-browser` 镜像改装系统 `chromium`，启动脚本优先使用 `/usr/bin/chromium`，不再依赖 Playwright 自带浏览器作为 Linux 发布浏览器主执行体。
- `launch_linux_extension_browser.sh` 新增 profile 自愈：首次 heartbeat 失败时，自动清空脏 profile、重新 qualify、再启动一次；从而把历史残留的坏 profile 自动修复掉。
- 命令行扩展加载统一补 `--enable-unsafe-extension-debugging`，并同步到 profile qualifier / probe 脚本，避免调试链与主链参数再分叉。

验证：

- `bash -n scripts/launch_linux_extension_browser.sh`
- `python3 -m py_compile scripts/qualify_linux_extension_profile.py scripts/plugin_publish_probe.py scripts/fanqie_editor_probe.py scripts/fanqie_card_probe.py`
- `docker compose build publisher-browser`
- 调试验证：
  - 用旧 profile 启动时，`chrome://extensions` 显示扩展 `state=DISABLED`、`unsupportedDeveloperExtension=true`
  - 用 fresh profile + system `chromium` 启动时，preferred client `b361cd2a-fce3-4733-8636-de64d82db066` 成功恢复 recent heartbeat
- 部署验证：
  - `docker compose up -d publisher-browser`
  - `docker compose logs --tail=120 publisher-browser`
  - `docker compose ps forwin publisher-browser`
  - 容器内查询 `/app/data/novel.db`，确认 `b361...` 的 `extension_version=0.1.17`、`backend_base_url=http://forwin:8899`、`last_heartbeat_at` 为最新，且 `fanqie/qidian` 两个平台 `connected=1`

部署状态：已部署到 `8899`。当前 `forwin` 与 `publisher-browser` 都是 `healthy`；Linux 侧 preferred client `b361cd2a-fce3-4733-8636-de64d82db066` 已恢复在线并接管双平台心跳。

### V2.9.2 Book Genesis 根层前置化（设计与代码前推，未部署）

把“创建书本”从空壳项目创建，前推为 Genesis 根层工作流；当前主干统一规格已从 `V2.9.1` 前推到 `V2.9.2`。

关键变化：

- `Project` 新增 `creation_status` 与 `active_genesis_revision_id`；新增 `BookGenesisRevision` 与 `PromptTrace` 真值表，Genesis 根层以 `BookGenesisPack` 版本化保存。
- `POST /api/projects` 改为新建即进入 `creating`，直接创建 Genesis revision；新增 Genesis API：读取、编辑、阶段生成、阶段锁定、阶段重生。
- 新增 `POST /api/projects/{id}/start-writing`；Genesis 完成后默认停在 `genesis_ready`，显式启动写作时才 materialize 全书 arc 骨架，并且只为当前 active arc 生成 `ChapterPlan`。
- `ArcPlanVersion` 新增 `arc_number/chapter_start/chapter_end/planned_target_size/planned_soft_min/planned_soft_max`；`phase24` 对 Genesis-backed 项目优先读取 arc 自己持久化的 sizing，不再从全书总章数反推。
- writer / reviewer context 新增 Genesis 继承字段：`genesis_context_refs`、world/map/story engine 摘要；writer prompt 已开始显式引用根层信息。
- 首页书本入口改成 Genesis 语义：新建后自动进入 Genesis 工作台；书本卡片按 `creating -> 继续创世`、`genesis_ready -> 启动写作`、`writing -> 继续沿用现有写作入口` 切换主按钮。
- 新增/更新设计文档：新增 `V2_9_2.md` 作为当前统一规格；`V2_9_1.md` 退为历史统一基线；`V2_8_1_completion_status.md` 与历史文档顶部说明同步前推到 `V2.9.2`。

验证：

- `python3 -m py_compile forwin/book_genesis.py forwin/api.py forwin/api_project_payloads.py forwin/api_schemas.py forwin/models/project.py forwin/models/genesis.py forwin/models/base.py forwin/orchestrator/loop.py forwin/orchestrator/phase24.py forwin/context/assembler.py forwin/protocol/context.py forwin/writer/prompts.py forwin/reviewer/context_builder.py forwin/reviewer/webnovel.py forwin/state/updater.py forwin/state/repo.py forwin/governance.py`
- `python3 -m py_compile forwin/api_pages.py tests/test_book_genesis_flow.py`
- `PYTHONPATH=. pytest -q tests/test_book_genesis_flow.py`
- `PYTHONPATH=. pytest -q tests/test_arc_execution_scoping.py tests/test_continue_project_orphan_review.py tests/test_project_publish_bindings.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py -k "create_project or continue_project"`

结果：

- `tests/test_book_genesis_flow.py`: `4 passed`
- `tests/test_arc_execution_scoping.py tests/test_continue_project_orphan_review.py tests/test_project_publish_bindings.py`: `11 passed`
- `tests/test_phase05_regressions.py -k "create_project or continue_project"`: `4 passed, 119 deselected`

部署状态：未部署到 `8899`。原因：本轮只完成代码、前端入口与设计文档前推，尚未执行容器重建与重启；当前也未做 `8899` smoke。切换条件：先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`，再执行 `docker compose build forwin` 与 `docker compose up -d forwin`，随后 smoke `GET /`、`GET /api/projects?limit=5`、`GET /api/projects/{project_id}/genesis`、`POST /api/projects/{project_id}/start-writing`。

## 2026-04-19

### V2.9.2 Linux 发布浏览器心跳闭环（preferred client 健康检查 / 扩展错误持久化 / 启动等待）

把 `publisher-browser` 从“Chrome 能启动就算活着”改成“preferred Linux client 真正 heartbeat 成功才算在线”，收紧后端浏览器在线判定。

关键变化：

- 新增 `forwin.publishers.healthcheck` 与 `scripts/check_publisher_browser_heartbeat.py`：直接读取 `publisher_extension_clients` / `publisher_extension_platform_states`，检查 `FORWIN_PUBLISHER_PREFERRED_CLIENT_ID`（或 profile marker 里的 client id）是否在新鲜心跳窗口内。
- `publisher-browser` 启动脚本新增 heartbeat 等待：Chrome 拉起并恢复 session 后，会等待 preferred client 在后端数据库里形成 recent heartbeat；超时则杀掉 Chrome 并退出，让容器重启而不是假装 healthy。
- `docker-compose.yml` 里的 `publisher-browser` healthcheck 改为“双重校验”：既检查 profile qualified，也检查 preferred client heartbeat。
- 扩展 `background.js` 新增持久化运行态：
  - `forwinPublisherBackgroundStatus`
  - `forwinPublisherBackgroundErrors`
  用于记录 worker 加载、bootstrap 尝试/成功/失败、heartbeat 尝试/成功/失败，不再只靠 `console.warn`。

验证：

- `python3 -m py_compile forwin/publishers/healthcheck.py scripts/check_publisher_browser_heartbeat.py`
- `PYTHONPATH=. pytest -q tests/test_publisher_browser_healthcheck.py tests/test_project_publish_bindings.py`
- `cd browser_extension/forwin-publisher && npm test`
- `docker compose build forwin publisher-browser`
- `docker compose up -d forwin publisher-browser`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/api/publishers/platforms`
- `docker compose logs --tail=120 publisher-browser`

部署状态：已部署到 `8899`。`forwin` 容器 healthy；`publisher-browser` 也已切到新逻辑，并按预期在 preferred Linux client `b361cd2a-fce3-4733-8636-de64d82db066` 没有 recent heartbeat 时输出明确 JSON 诊断并重启，而不是继续伪装成健康在线。当前最新诊断显示：preferred client 仍 stale，最近在线的仍是 macOS 侧 client `5ade19c9-a5b4-45c2-aea6-5f4f63af1b3e`。

### V2.8.1 正式收口（版本对齐 / subworld 兼容 / 发布验收）

在保留当前主干 `V2.9` subworld 基线能力的前提下，完成 `V2.8.1` 的正式收口，对齐代码、回归、版本文档和部署口径。

关键变化：

- subworld 对旧 arc 规划输出改为保守 fallback：当 `ArcDirector.plan_arc()` 没有返回 `subworld_delta` 时，只保留 bootstrap，不再凭空补造 canonical 新角色或新 slot，避免污染既有 2.8.1 主链语义。
- `assemble_context()` 对旧 repo / test double 保持兼容；缺失 `get_allowed_entity_snapshots()` 时回退旧接口，relations 过滤也兼容老签名。
- 修复 `sub_worlds` / `sub_world_roster_items` 的迁移插入口径：bootstrap backfill 显式写入 `created_at` / `updated_at`，模型同步补 `server_default`，避免 fresh DB 在 API lifespan 中触发 `NOT NULL` 失败。
- publisher 平台概览里的 `extension_online` 改为以 extension client heartbeat 为准，不再让 platform state 伪装扩展仍在线。
- 重写 `V2_8_1_completion_status.md`，明确当前可以正式宣告 `V2.8.1` 完成；`V2_9.md` 增加版本边界说明，明确 subworld 属于 2.9 规格但已前置落地。

验证：

- `python3 -m py_compile forwin/subworld_manager.py forwin/context/assembler.py forwin/publishers/manager.py forwin/models/subworld.py forwin/models/base.py tests/test_phase05_regressions.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py -k 'test_orchestrator_records_phase4_outputs or test_publisher_manager_tracks_extension_heartbeat_and_stale_state or test_publishers_page_and_extension_api_routes or test_publishers_page_uses_extension_bridge_flow or test_retrieval_broker_applies_budget or test_tasks_list_endpoint_returns_recent_items'`
- `PYTHONPATH=. pytest -q`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/api/tasks/active-generation-check`
- `docker compose build forwin`
- `docker compose up -d forwin`
- `curl -fsS http://127.0.0.1:8899/`
- `curl -fsS 'http://127.0.0.1:8899/api/projects?limit=5'`
- `curl -fsS 'http://127.0.0.1:8899/api/task-center/items?limit=20'`
- `curl -fsS 'http://127.0.0.1:8899/api/projects/00b889c433944991871d3edc6e2281a9/causal-replay?scope=arc'`
- `curl -fsS 'http://127.0.0.1:8899/api/projects/00b889c433944991871d3edc6e2281a9/governance-insights'`

部署状态：已部署到 `8899`。部署前检查 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`；部署后 smoke 通过，当前本地全量回归为 `208 passed, 8 subtests passed`。

## 2026-04-18

### V2.8.1 全量补齐增量（scene-era contract / WNER 证据 / feedback 校准 / trope 导入 / 稳定性）

按 `V2_8_1.md` 与完成情况文档补齐剩余设计口径，同时保留旧数据兼容。

关键变化：

- `WriterOutput` 扩展 `scene_continuation`、`lore_candidates`、`timeline_hints`、`writer_notes`；scene prompt 增加 continuation 标签，structured extraction 增加第三段续航信息抽取。
- WNER 将 `confirmed_signals` 提升为一等 evidence，保存 `reviewer_mode` 与 `confirmed_signal_refs`；LLM hard fail 不能只依赖 audience signal。
- reader estimate 改为平台指标优先，缺失时回退 `comment_proxy`；aggregate/trend 暴露 `estimation_method` 与 `scale_confidence`。
- 新增 feedback action effectiveness 视图，并接入 governance insights。
- Trope registry 保持 seed 真值，新增完整 JSON 清单校验、summary、过滤和 validate API；未提供 188 清单时继续标记为 starter。
- band checkpoint 增加基于 WNER review meta 的 director imbalance warn 规则，并避免对无体验证据的旧式极简输出误报。
- task-center/API 暴露重启中断状态与恢复建议，新增 active generation task 只读检查接口。

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/protocol/scene.py forwin/protocol/writer.py forwin/protocol/review.py forwin/writer/prompts.py forwin/writer/chapter_writer.py forwin/reviewer/webnovel.py forwin/reviewer/hub.py forwin/orchestrator/feedback_aggregator.py forwin/audience_metrics.py forwin/protocol/trope_library.py forwin/governance_checks.py forwin/orchestrator/loop.py forwin/api_schemas.py forwin/api.py forwin/models/base.py forwin/models/publisher.py tests/test_writer_split_pipeline.py tests/test_audience_feedback_alignment.py tests/test_phase05_regressions.py tests/test_governance_decision_api.py tests/test_governance_review_and_checkpoint.py tests/test_generation_task_persistence.py`
- `PYTHONPATH=. pytest -q tests/test_writer_split_pipeline.py tests/test_audience_feedback_alignment.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_generation_task_persistence.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py -k "wener or trope_templates_and_band_experience_override_api or chapter_review_api_exposes_v27_fields"`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_api_pages_rendering.py tests/test_project_operation_guards.py tests/test_continue_project_orphan_review.py tests/test_api_runtime_progress.py tests/test_bulk_delete_api.py`
- `PYTHONPATH=. pytest -q`

部署状态：未部署到 `8899`。原因：本轮完成代码与本地回归，尚未重建/重启容器。切换条件：先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`，再执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

## 2026-04-17

### P1/P2B 治理层补完（future constraint 开关 / arc replay / constraint 生命周期）

把原始治理层设计剩余缺口收完，重点是让已有治理对象形成稳定导演闭环，不新增新表。

关键变化：

- `future_constraints_enabled` 成为真实判定开关：关闭时 constraint 仍可保存和展示，但不进入 chapter review、context pack、band checkpoint、`future_constraint_block`、next-band compatibility 或 future preservation 判定。
- band checkpoint 补齐 `intra_band_consistency`：未处理 review、latest review fail、failed provisional gate、同 band 未决 checkpoint 都会形成明确 issue。
- `next_band_compatibility` 扩展为读取 next band task contract、active constraints 与当前 band 末状态；hard constraint 才能 fail，next-band task/soft 冲突只 warn。
- checkpoint evaluator 异常会落 `BandCheckpoint(status="error")`，写 `checkpoint_evaluator_error` 与 `band_checkpoint_hit`，并暂停运行。
- review/checkpoint issue 新增 `issue_group`，区分 `fact_conflict`、`director_imbalance`、`runtime_observation`、`governance_action`；UI 同步显示 issue group 与 future preservation category。
- narrative constraint 新增编辑/停用 API，所有 lifecycle 修改必须带 reason，并写 `constraint_updated` / `constraint_archived`。
- `causal-replay` 支持 `scope=arc` 与可选 `arc_id`；项目 payload 暴露 `active_arc_id`，drawer 默认可看 arc 因果回放。
- `governance-insights` 增加 `issue_group_distribution`，推荐项开始按事实冲突/导演失衡聚合。
- 书本 drawer 补齐 constraint 编辑/停用、future constraints 是否参与判定、arc replay、issue group/category 展示。

影响文件：

- `forwin/governance.py`
- `forwin/governance_checks.py`
- `forwin/protocol/review.py`
- `forwin/context/assembler.py`
- `forwin/state/repo.py`
- `forwin/state/updater.py`
- `forwin/orchestrator/loop.py`
- `forwin/api_schemas.py`
- `forwin/api_project_payloads.py`
- `forwin/api.py`
- `forwin/api_pages.py`
- `tests/test_governance_decision_api.py`
- `tests/test_governance_review_and_checkpoint.py`
- `tests/test_api_pages_rendering.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/governance.py forwin/protocol/review.py forwin/governance_checks.py forwin/api_schemas.py forwin/api_project_payloads.py forwin/state/repo.py forwin/context/assembler.py forwin/state/updater.py forwin/orchestrator/loop.py forwin/api.py forwin/api_pages.py tests/test_governance_decision_api.py tests/test_governance_review_and_checkpoint.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_api_pages_rendering.py tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py`
- `PYTHONPATH=. pytest -q`
- `node --check /tmp/forwin_home_script.js`
- `docker compose build forwin`
- `docker compose up -d forwin`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/`
- `curl -fsS http://127.0.0.1:8899/api/task-center/items?limit=20`
- `curl -fsS http://127.0.0.1:8899/api/projects?limit=5`
- `GET /api/projects/{project_id}/causal-replay?scope=arc`
- `GET /api/projects/{project_id}/governance-insights`

部署状态：已部署到 `8899`。部署前确认无 `starting/running/terminating` generation task；当前容器状态 `healthy`，本地全量 Python 回归 `178 passed, 5 subtests passed`，`phase05` 回归 `113 passed`。

### P2A 闭环增量（reason contract / replay&insights UI / taxonomy / 风险分类）

围绕现有治理层做一次收口式增量，不新增治理对象，只补齐口径、可见性和统计闭环。

关键变化：

- `DecisionEvent.event_type` 收成中心化 taxonomy，并在 API / orchestrator 落事件时统一校验，不再继续散落自由字符串。
- 治理动作开始强制 `reason`：项目治理修改、manual checkpoint、checkpoint pass/override、review approve 缺 `reason` 时统一返回 `400`。
- `future_constraint_block` 真正贯穿到 `blocking_reason`；chapter review 因 hard future constraint 失败时，continue / approve+continue 会稳定暴露阻断原因和 `decision_event_id`。
- `governance-insights` 补齐 `recommended_adjustments` 与 `recent_examples`，继续只基于 `DecisionEvent` / checkpoint verdict 反推，不新增第二套统计真值。
- runtime observation 补齐 stage / duration / fallback 摘要事件；writer fallback 开始直接消费 `generation_meta["model_fallbacks"]`，落成 `fallback_profile_switched`。
- `future_resource_preservation` 从单一粗粒度 warn 升级为 5 类风险分类：`character_locked_out`、`thread_closed_too_early`、`relationship_closed_too_early`、`secret_over_explained`、`growth_arc_completed_too_early`。
- 书本 drawer 新增“因果回放”“治理洞察”卡片；治理动作改走轻量 reason modal；决策事件深链修正为字符串 ID，不再错误地按数字处理。

影响文件：

- `forwin/governance.py`
- `forwin/api_schemas.py`
- `forwin/api_project_payloads.py`
- `forwin/governance_checks.py`
- `forwin/orchestrator/loop.py`
- `forwin/writer/llm_client.py`
- `forwin/api.py`
- `forwin/api_pages.py`
- `tests/test_generation_control_payload.py`
- `tests/test_governance_review_and_checkpoint.py`
- `tests/test_governance_decision_api.py`
- `tests/test_api_pages_rendering.py`
- `tests/test_project_operation_guards.py`
- `tests/test_phase05_regressions.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/governance.py forwin/api_schemas.py forwin/api_project_payloads.py forwin/governance_checks.py forwin/orchestrator/loop.py forwin/writer/llm_client.py forwin/api.py forwin/api_pages.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py tests/test_phase05_regressions.py tests/test_generation_control_payload.py tests/test_governance_decision_api.py tests/test_governance_review_and_checkpoint.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q`
- `docker compose build forwin`
- `docker compose up -d forwin`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/`
- `curl -fsS http://127.0.0.1:8899/api/task-center/items?limit=20`
- `curl -fsS http://127.0.0.1:8899/api/projects?limit=5`
- `docker compose ps forwin`
- `docker compose logs --tail=120 forwin`

部署状态：已部署到 `8899`。部署前已确认 `task-center` 无 `starting/running/terminating` 生成任务；当前容器状态 `healthy`，线上 `health` / 首页 / `task-center` / `projects` smoke 正常，本地全量 Python 回归 `176 passed, 5 subtests passed`。

### P1 收尾、全量回归与 `8899` 部署

把治理层 P1 最后一段闭环收完，并完成 `8899` 切换与线上 smoke。

关键变化：

- 书本 drawer / continue modal 补齐治理深链：支持从 `blocking_reason`、chapter review、band checkpoint 直接跳到对应决策链，决策时间线支持范围筛选。
- 修复治理上下文装配回归：`context/assembler` 改为按关键字参数读取 active constraints，避免 chapter review / governance pack 组装时报错。
- 为 Python 3.13 下失效的 Starlette `TestClient` 增加测试侧兼容调用层，绕开 AnyIO threadpool 死锁；`phase05` 回归恢复可跑。
- 对齐三条旧回归用例到当前治理语义：chapter task contract 会参与 review，因此相关 fixture 改为显式满足任务合同，而不是继续假设旧版宽松 verdict。
- `forwin` 容器已重建并切换到 `8899`；线上首页、health 与 task-center smoke 正常。

影响文件：

- `forwin/api_pages.py`
- `forwin/context/assembler.py`
- `tests/test_api_pages_rendering.py`
- `tests/test_phase05_regressions.py`
- `Design-docs/maintenance_log.md`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/api_pages.py forwin/context/assembler.py tests/test_api_pages_rendering.py tests/test_phase05_regressions.py`
- `PYTHONPATH=. pytest -q tests/test_api_pages_rendering.py tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py`
- `PYTHONPATH=. pytest -q`
- `docker compose build forwin`
- `docker compose up -d forwin`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/`
- `curl -fsS http://127.0.0.1:8899/api/task-center/items?limit=20`
- `docker compose ps forwin`
- `docker compose logs --tail=120 forwin`

部署状态：已部署到 `8899`。当前容器状态 `healthy`，本地全量 Python 回归 `173 passed`，`phase05` 回归 `113 passed`。

### 治理因果链 / replay / insights 收口

把 P1 的 `DecisionEvent` 从“事件列表”收成可追溯因果链，并启动 P2 的 replay / insight 接口。

关键变化：

- `DecisionEvent` 新增 `parent_event_id` / `causal_root_id`，形成稳定的 run / repair / approve / override 因果链。
- `pause requested`、`terminate requested`、`continue requested`、`pause reached`、`terminate reached`、`repair started/failed/succeeded`、`forced_accept_applied`、`band_checkpoint_hit` 等关键路径开始稳定落事件。
- `ChapterReviewDetail.decision_refs`、`BandCheckpointDetail.decision_refs` 开始真实填充；`blocking_reason` 会尽量回链到对应决策事件。
- 新增 `GET /api/projects/{project_id}/causal-replay` 与 `GET /api/projects/{project_id}/governance-insights`，支持按 root 链回放和基于现有事件/ checkpoint 的 override / blocking 聚合。
- runtime observation 继续细化：补入 `provisional_gate_evaluated`、`llm_request_started/succeeded/failed`、`retry_attempt`、`memory_index_upsert_*` 摘要事件。

影响文件：

- `forwin/models/governance.py`
- `forwin/models/base.py`
- `forwin/governance.py`
- `forwin/config.py`
- `forwin/state/repo.py`
- `forwin/state/updater.py`
- `forwin/api_schemas.py`
- `forwin/api_project_payloads.py`
- `forwin/api.py`
- `forwin/orchestrator/loop.py`
- `tests/test_generation_control_payload.py`
- `tests/test_governance_decision_api.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/models/governance.py forwin/governance.py forwin/config.py forwin/models/base.py forwin/state/repo.py forwin/state/updater.py forwin/api_schemas.py forwin/api_project_payloads.py forwin/api.py forwin/orchestrator/loop.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py`
- `PYTHONPATH=. pytest -q tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py tests/test_api_pages_rendering.py`

部署状态：未部署到 `8899`。原因：本轮仍是本地代码与回归验证，尚未重建/重启容器；切换前仍需确认没有运行中的 generation task。切换条件：确认 `8899` 无活跃生成任务后，执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

### 治理层前端入口补齐

把此前只在后端生效的治理能力补进现有 drawer / modal，不新建独立页面。

关键变化：

- 书本详情 drawer 新增治理设置卡片，展示 `governance`、`blocking_reason`、`latest_band_checkpoint`、约束摘要。
- drawer 新增项目级治理修改入口，可直接调整默认运行模式、推进策略、review 间隔、auto band checkpoint、manual checkpoint、future constraints。
- drawer 新增轻量治理操作：插入 manual checkpoint、创建 narrative constraint、对 band checkpoint 执行 override。
- drawer 新增决策时间线展示，直接读取 `decision_timeline`，让阻断点和治理动作可解释。
- 继续生成不再直接裸调 API，而是复用现有生成 modal，允许在 continue 时做运行级治理覆盖。
- 新建生成任务 modal 也新增治理覆盖项：`progression_mode`、`auto_band_checkpoint`、`manual_checkpoints_enabled`、`future_constraints_enabled`。

影响文件：

- `forwin/api_pages.py`
- `tests/test_api_pages_rendering.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/api_pages.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_project_operation_guards.py tests/test_governance_review_and_checkpoint.py`
- `node --check /tmp/forwin_home_script.js`

部署状态：未部署到 `8899`。原因：本轮只完成页面层接线与本地回归，尚未重建并切换容器；切换仍需避开运行中的 generation task。切换条件：确认 `8899` 无 `starting/running/terminating` 生成任务后，执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

### 治理层增量实现（strict gate / band checkpoint / decision log）

围绕现有 `WritingOrchestrator -> chapter review -> canon -> phase3/phase4` 主链，新增治理层骨架并接入运行时。

关键变化：

- 新增项目治理设置 `projects.governance_json`，新项目默认进入严格治理：`serial_canon_band_guard + auto_band_checkpoint + blackbox`。
- 新增独立治理域：`BandCheckpoint`、`NarrativeConstraint`、`DecisionEvent`，不再把治理真值混在 chapter review 或 `stage_history` 里。
- `generate` / `continue-generation` / `approve review + continue` 开始套用项目治理和运行级覆盖；项目/任务 payload 暴露 `governance`、`latest_band_checkpoint`、`blocking_reason`、`next_gate`。
- 主循环新增 strict progression gate：前序章未 `accepted` 时阻断后章；跨 band 时要求上一 band checkpoint 已 `pass/overridden`。
- 新增 manual checkpoint / governance / constraints / decision-events API；manual checkpoint v1 限制在章边界与 band 边界。
- 自动 band checkpoint 已落地为一等对象，`warn/fail` 会触发暂停；chapter 级与 band 级的关键决策开始写入 `DecisionEvent`。

影响文件：

- `forwin/governance.py`
- `forwin/governance_checks.py`
- `forwin/models/governance.py`
- `forwin/models/base.py`
- `forwin/models/project.py`
- `forwin/models/phase.py`
- `forwin/state/repo.py`
- `forwin/state/updater.py`
- `forwin/context/assembler.py`
- `forwin/reviewer/hub.py`
- `forwin/orchestrator/loop.py`
- `forwin/api.py`
- `forwin/api_project_payloads.py`
- `forwin/api_runtime.py`
- `forwin/api_schemas.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/governance.py forwin/models/governance.py forwin/context/assembler.py forwin/state/repo.py forwin/state/updater.py forwin/api_schemas.py forwin/api_project_payloads.py forwin/api_runtime.py forwin/config.py forwin/api.py forwin/orchestrator/loop.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_project_operation_guards.py tests/test_continue_project_orphan_review.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py -k "chapter_review_api_exposes_v27_fields or trope_templates_and_band_experience_override_api"`

部署状态：未部署到 `8899`。原因：本轮只完成代码与本地回归，尚未重建/重启容器；同时切换前仍需确认没有运行中的 generation task。切换条件：补完剩余治理 UI/API 回归后，执行 `docker compose build forwin`，确认任务空闲，再 `docker compose up -d forwin`。

### Chapter review / band checkpoint 的 P1 治理检查

继续补上此前未完成的两项：

- `HistoricalReviewHub` 在 chapter review 中新增“规划任务履约”和“hard future constraint”检查，约束冲突可直接给出 `fail`。
- auto band checkpoint 从纯 accepted 判定扩展为 band task completion、future constraint / next-band compatibility、future resource preservation 风险检查。
- 新增治理专项测试，覆盖 chapter review 的 task/constraint 检查和 auto band checkpoint 的 warn 行为。

影响文件：

- `forwin/reviewer/hub.py`
- `forwin/governance_checks.py`
- `forwin/orchestrator/loop.py`
- `tests/test_governance_review_and_checkpoint.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/reviewer/hub.py forwin/governance_checks.py forwin/orchestrator/loop.py tests/test_governance_review_and_checkpoint.py`
- `PYTHONPATH=. pytest -q tests/test_governance_review_and_checkpoint.py`

部署状态：同上，未部署到 `8899`。

### 总规划与当前完成度文档

新增 `Design-docs/project_master_plan.md`，把 `v2.3 Writer 主链`、`v2.6 评论反馈层`、`v2.7 体验层` 合并成统一规划，并按当前代码实现给出完成度判断。

关键结论：

- 项目当前最准确的定义是“长篇连载自动化写作系统”，而不是单一 Writer 原型。
- `v2.3` 主链已经可运行，但 scene-era contract 还没补齐，尤其缺 `scene continuation`、`lore_candidates`、`timeline_hints`、`writer_notes`。
- `v2.6` 已不只是 phased rollout 早期阶段，Phase A/B/C 主骨架基本都已接入。
- `v2.7` 已进入可运行阶段，但 reviewer 闭环和体验校准闭环仍需加强。
- 总规划文档同时收束了项目功能、特性、当前真实状态机，以及建议的后续优先级。

影响文件：

- `Design-docs/project_master_plan.md`
- `Design-docs/maintenance_log.md`

## 2026-04-16

### 书本详情任务管理前端改进

书本详情抽屉新增“状态机驾驶舱”，把任务管理从零散指标改为按写作状态机展示。

关键变化：

- 详情第一屏显示当前状态、下一步、安全操作和运行风险。
- `needs_review`、`paused`、`failed/partial_failed`、`running`、`completed` 分别给出不同的人类可读说明和主操作。
- `needs_review` 会展示阻塞队列，可直接查看 Review、接受、接受并继续。
- `failed` 会展示失败章节队列，可查看失败原因；可继续时提供“重试剩余章节”。
- 运行中任务明确提示安全暂停只在 checkpoint 停住，强制终止作为次级高风险操作。
- 书本列表主按钮按状态切换为“处理 Review / 继续生成剩余章节 / 查看进度 / 写作完成 / 生成首批章节”，避免出现不可解释的禁用按钮。
- 章节流水线在缺少当前任务 stage history 时，会根据章节状态推断 accepted/drafted 的已完成步骤，避免书本入口把已完成章节显示成“未到达”。

影响文件：`forwin/api_pages.py`。

验证：

- `.venv/bin/python -m py_compile forwin/api_pages.py`
- `.venv/bin/python -m unittest tests.test_api_pages_rendering`
- `node --check /tmp/forwin_home_script.js`

部署状态：新镜像已通过 `docker compose build forwin` 构建，尚未重启 `8899`。原因是 `8899` 仍有运行中的生成任务 `78b13db89f74`；部署前仍需确认没有运行中的生成任务，或先安全暂停。

### 书本详情抽屉自动重开 bug

修复了书本详情打开后，用户关闭抽屉又被轮询自动打开的问题。

根因：`openTaskDrawer()` / `refreshCurrentDrawerIfChanged()` 的异步请求在用户关闭抽屉后仍可能返回，旧请求继续调用 `renderDrawerSnapshot()`，导致 overlay 被重新打开。

修复：为 drawer 增加 `drawerRequestToken`。每次打开刷新携带 token，关闭抽屉时递增 token 并清空当前任务；旧请求返回后发现 token 失效会直接丢弃。

影响文件：`forwin/api_pages.py`。

验证：

- `.venv/bin/python -m py_compile forwin/api_pages.py`
- `.venv/bin/python -m unittest tests.test_api_pages_rendering`
- `node --check /tmp/forwin_home_script.js`

部署状态：新镜像已通过 `docker compose build forwin` 构建，但未重启 `8899`。原因是 `8899` 当时存在运行中的生成任务 `78b13db89f74`，第 2 章处于 `writing_chapter`；直接重启会中断写作任务。切换条件：任务结束，或先安全暂停后再 `docker compose up -d forwin`。

### 失败原因可点开查看

书本详情的章节时间线中，失败、暂停、`needs_review` 等异常步骤变为可点击节点。

点击后展示：

- 当前步骤、章节、章节状态、任务状态和到达时间。
- `task.message`、`task.error`、失败章节、暂停章节、冻结产物。
- 如果章节有 review，额外拉取 review 详情，展示 verdict、issues、recommended_action、summary。

影响文件：`forwin/api_pages.py`。

维护备注：这是 UI 层的诊断入口，不改变任务状态机。

### 写作流程状态机文档

新增/更新 `Design-docs/writing_flow_state_machine.md`，记录当前真实写作流程状态机。

覆盖范围：

- API 任务状态：`starting/running/paused/needs_review/cancelled/failed/partial_failed/completed`。
- Orchestrator 主流程：规划、project 创建、arc envelope、provisional gate、chapter loop。
- 单章流水线：context、writer、review、canon、post-acceptance。
- Pause/continue 语义。
- LLM 单模型 retry 与跨 profile fallback。

同时记录了 2026-04-16 的测试失败根因：测试 fake chapter body 短于默认 `min_chapter_chars=2500`，触发 `char_count_low` warning，导致期望状态从 `partial_failed/pass` 偏移到 `needs_review/warn`。修复方式是扩展测试正文，保持测试意图不变。

验证：`timeout 180s .venv/bin/python -m unittest tests.test_phase05_regressions`，结果 113 tests passed。

### 安全暂停 / 继续生成

实现生成任务的安全暂停与继续能力。

行为变化：

- 新增 `pause_requested` / `paused`，保留 `terminating/cancelled` 作为强制终止语义。
- 暂停不打断正在进行的 LLM HTTP request，只在安全 checkpoint 停住。
- 继续生成只处理 `planned` / `failed` 章节，不重写 `accepted` 章节。
- 存在真实 `needs_review` 章节时，continue 拒绝执行，要求先处理 review。
- 任务/书本详情返回 `generation_control`，用于展示计划状态、写作状态、review 状态、当前章、下一章、已完成/失败/待 review 章节、可暂停/可继续等。

维护风险：重启容器仍会中断当前进程里的运行中任务；安全暂停是应用级协议，不等于 Docker 级热切换。

### LLM retry 后自动换模型

实现两层 LLM 请求策略。

行为变化：

- 内层保留当前 `LLMClient.chat()` 的单 profile retry。
- 当前 profile retry 用尽后，如果错误是 transient 类型，切换到下一个已保存且有 API Key 的 profile，重发同一个 request。
- transient 类型包括 `429`、`5xx`、`529`、timeout、network disconnect、connection reset。
- `400`、鉴权失败、prompt/schema/JSON 解析失败、内容质量失败、review fail 不触发跨模型 fallback。
- fallback 顺序固定为本次请求指定 profile 或默认 profile 优先，其后按保存配置顺序尝试。
- 每个任务启动时冻结 fallback profile 列表，任务运行中改模型配置不影响已启动任务。

维护备注：fallback 是单个 LLM request 粒度，不是整章失败后再换模型。

### 模型配置与中文站预设

配置页模型设置改为尽量下拉选择，减少手输。

新增/调整：

- MiniMax 中文站预设，base URL 默认 `https://api.minimaxi.com/v1`。
- Kimi / Moonshot.cn 中文站预设，支持保存 API Key 和模型 profile。
- 模型字段保留自定义输入，同时提供推荐模型下拉。
- 新建生成任务的模型选择使用已保存 profile。

维护备注：用户明确 MiniMax 和 Kimi 都买的是中文站，默认配置必须优先中文站，不应切到国际站域名。

### 生成设置

新增生成设置：

- `min_chapter_chars`，默认 `2500`。
- `review_interval_chapters`，`0` 表示不启用周期性人工检查，`N>0` 表示每 N 章进入人工 review checkpoint。

影响：最低字数会影响 ContinuityChecker verdict；测试夹具和短正文模拟必须显式满足或覆盖该设置，否则容易误触发 `char_count_low`。

### Provisional 机制核对

根据 `Design-docs/provisional_mechanism_check.md` 核对当前 provisional 预演设计，并补充状态机文档。

关键结论：

- `provisional` 是正式 canonical 提交前的预写/预审层，不是整本书预演，也不是每章双写。
- 运行位置应在 Writer/Review Hub 之后，canonical State Updater 之前。
- 失败的 provisional execution 会阻断后续推进并标记相关章节失败。
- Promote 到 canonical 仍只能通过 canon/state updater 路径完成。

维护风险：如果未来继续强化 band 级 provisional，需要避免把 provisional 候选直接写入 canonical 历史。

### 任务中心 / 书本页 UI

首页从任务中心单视角扩展为书本、任务、配置三个视角。

关键变化：

- 书本页展示每本书的章节规划、已生成章节、上传入口和自动化配置。
- 任务中心保留生成与上传任务混排。
- 详情统一通过右侧抽屉展示。
- 新增批量删除入口，但 active generation/upload 仍应被 API guard 拒绝删除。

维护备注：书本详情抽屉会被轮询刷新，因此新增 UI 功能时要避免刷新破坏用户已展开正文、滚动位置或关闭状态。

### 8899 部署注意事项

当前 `docker-compose.yml` 中 `forwin` 服务没有挂载源码，`uvicorn` 也不是 reload 模式。修改 `forwin/api_pages.py` 这类服务端渲染 HTML/JS 后，必须重建并重启容器才会在 `8899` 生效。

注意：

- `docker compose build forwin` 不会中断运行任务。
- `docker compose up -d forwin` 会重建/重启 `forwin` 容器，会中断进程内运行任务。
- 部署前必须查 `/api/task-center/items`，确认无 `starting/running/terminating` 任务，或先让任务安全暂停。

### 容器内 curl 缺失

`forwin` 容器内没有安装 `curl`，因此不能用 `docker exec forwin curl ...` 调试外部 LLM API。

替代方案：

- 在宿主机 `.venv` 或 shell 中发请求。
- 用容器内 Python 标准库/httpx 脚本发请求。
- 如确实需要容器内 curl，需要修改 Dockerfile 安装，但这会增大镜像并要求重建。

## 2026-04-24

### Codex Pro Runtime 接入

ForWin 新增 Host Codex Bridge 方案：宿主机 bridge 复用本机 `codex exec` / Pro 登录，Docker 内 ForWin 通过 `host.docker.internal:8897` 调用，不把 Pro 订阅伪装成 API key profile。

关键约束：

- `chapter_plan_materialization` 固定走普通 LLM，不走 Codex。
- Genesis、writer、reviewer、repair、Phase4、WorldModel 相关调用可按路由优先走 Codex，失败回退普通 LLM。
- Codex 后台写能力只能进入 governed action/proposal/review/conflict 管理层，不能直接写 canon、不能直接调用 `StateUpdater.apply_*`。
- PromptTrace 增加 `backend`、`codex_job_id`、`permission_profile`、`fallback_used`，用于后续排查 Codex/fallback 行为。

运维备注：

- 宿主机启动 bridge：`FORWIN_CODEX_BRIDGE_TOKEN=... forwin-codex-bridge`。
- 容器侧启用：`FORWIN_CODEX_ENABLED=true`，并配置同一个 `FORWIN_CODEX_BRIDGE_TOKEN`。
- 首页配置页新增 Codex Bridge health，只检查 bridge/CLI 可用性，不检查 OpenAI API key。

## 2026-04-15

### Review / API guard 修复汇总

从 `Design-docs/review_fix_log_2026-04-15.md` 汇总关键结论。

修复：

- `/api/generate` 省略 `base_url` 或 `model` 时，runtime model overrides 正确使用已保存 runtime/profile 值。
- 已有项目存在 active generation 时，禁止再次启动 generation/continue。
- 项目删除和批量删除会拒绝 active generation/upload 项目，避免删除运行中任务的数据。
- Project automation updates 支持 `publish_bindings`，双平台绑定可在项目创建后继续编辑。
- FastAPI lifespan 在测试中复用预配置 runtime objects，避免 `TestClient` 回归测试重新初始化注入状态。

影响文件：

- `forwin/api.py`
- `forwin/api_schemas.py`
- `tests/test_phase05_regressions.py`
- `tests/test_project_publish_bindings.py`
- `tests/test_project_operation_guards.py`

验证：

- `python3 -m pytest -q tests/test_project_operation_guards.py`
- `python3 -m pytest -q tests/test_project_publish_bindings.py`
- `python3 -m pytest -q tests/test_phase05_regressions.py`
- `python3 -m pytest -q`
- `npm test` in `browser_extension/forwin-publisher`

结果：

- `tests/test_phase05_regressions.py`: 110 passed
- Python test suite: 144 passed
- Browser extension tests: 26 passed
