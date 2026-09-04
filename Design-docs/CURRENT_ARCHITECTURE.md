# ForWin Current Architecture

更新时间：2026-09-04

完整的当前设计、运行链路与剩余耦合见 [CURRENT_DESIGN.md](CURRENT_DESIGN.md)。本文保留精简的代码边界。

状态：active-current。本文档是当前架构入口；旧 V2/V3/V4 side-by-side 计划只作为历史背景或兼容说明读取。

## 当前权威口径

ForWin 当前工程基线是：

```text
Genesis / Writer / Review 主链
+ Skill Runtime
+ Observability
+ Final BookState Runtime
+ Scheme C BookMap
= 当前工程基线
```

唯一 canon source 是 `BookState DB Canon`。章节生产仍由 `Arc -> Band -> Chapter -> Scene` 驱动；入 canon 的最终路径是 `BookStateGraphDeltaExtractor -> BookStateReviewGate -> BookStateCompiler -> GraphDelta ledger -> Snapshot`。

## Runtime 边界

- 书本根真值：`Genesis`。
- Genesis 只在写前可变；`start-writing` 成功后 active revision 标记为 `locked`，workspace mutation 全部拒绝，运行时计划只认物化后的 `ArcPlanVersion` / `ChapterPlan`。
- 世界状态 canon：`BookState + GraphDelta + Snapshot`。
- 世界状态读侧：`BookStateQuery` 是实体、关系、事件、剧情线和故事时间的唯一 accepted-state 查询入口；`ReviewQuery` 只读取已接受章节摘要和已接受 review notes。
- 地图 canon：`BookMap / Scheme C`，语义为 `SubWorld -> Region -> MapNode -> MapEdge`。
- 上下文来源：`BookState + BookMap + Genesis + approved projections`。
- 运行策略：项目只有一份带版本号的 `RuntimePolicy`，durable generation task 保存不可变 policy snapshot；`InfrastructureConfig` 只负责环境凭据、端点、worker/存储和只读模型目录。
- 当前策略与生成审计契约：`RuntimePolicy.schema_version=2`；Generation Audit 固定为 `report-only`，按数据库中 `ChapterPlan.status="accepted"` row 计数，对 `all profiles` 使用 `cadence=6`，即每六个 accepted DB chapter 记录一次 `generation_audit_checkpoint_reached`，其 `event_family="runtime_observation"`，并且固定为 `no pause/delegation/block`，不改变 generation `RunResult`。
- 任务入口：Genesis handoff、continue、auto-continue、scheduler 与 durable worker 统一经过 `GenerationApplicationService`；worker 只执行持久化任务的 `execute_claimed`。CLI、MCP 与网页都调用 HTTP 用例，不构造或运行 `ChapterPipeline`；`RuntimeContainer` 是唯一 pipeline 构造点。
- 运行时计划：`forwin.planning.PlanningService` 是主要门面，`PlanningQuery` 读取 active arc/chapter/band 计划；Genesis 物化、repair patch 和 post-Canon replan 仍有写路径。`PlanHealthService` 将 future audit 投影为 typed health；patch validation 由计划修订流程负责。
- 实体准入：`EntityRegistrar` 只构建并验证候选稿上的 `EntityAdmissionPlan`，不会写 `Entity` / `EntityAlias`；分类器异常、遗漏、别名歧义和唯一性冲突均 fail-closed。只有 `CanonAdmissionService` 通过 `EntityAdmissionCommitter` 在 Canon 事务中落实无冲突计划。
- review 主链：`review.DraftReviewService` 聚合章节文本、体验、计划契约、地图、人格和 lint；draft review 与 canon gate 通过 `QualityAnalysisRunRow` 共享 primary quality 分析；`review.repair.RepairService` 是 draft/canon repair 的两个显式入口；`review.decision.FinalResidualPolicy` 只评估 repair 耗尽后的残留，不决定 canon。`CanonPreparationService` 在事务外完成资格、quality、实体计划与 BookState review，`CanonAdmissionService.commit_plan` 是唯一 accepted-chapter 原子写入口；`BookStateReviewGate` 是 GraphDelta 入 canon 前的 deterministic guardrail。
- skill runtime：仅作为 prompt / workflow instruction layer，参与 PromptTrace，不写 canon，不绕过 DecisionEvent 或 BookState gate。

## 模块与入口边界

- 章节生产入口是 `forwin.generation.pipeline.ChapterPipeline`。它静态组合 run control、audit control、review、repair planning、chapter execution、writer、finalization 等 stage owner，构造器只接收具体类型协作者；类体不再做跨模块函数赋值。旧 `WritingOrchestrator`、模块回注、伪造 `__module__` 和完整 pipeline 反向注入均已删除。
- `forwin.generation.pipeline_core` 以 stage owner class 保存 pipeline 行为，以模块私有函数保存纯计算；不通过 `common.py` 转发外域类型。`RepairExecution` 与 `CanonPreparationContext` 是冻结的窄能力集，review/canon 域不依赖 `ChapterPipeline`。
- Genesis 只有 `forwin.genesis` 一个包，workspace 与 handoff 是其子域；`book_genesis.py`、`book_genesis_core`、`genesis_workspace`、`genesis_handoff` 旧入口均已删除。
- `forwin.http.create_app()` 是唯一 FastAPI 组装入口。每个 App 持有独立 `HttpRuntime`，其 config、session factory、pipeline、task cache/lock、scheduler stop event、publisher manager 与应用服务只挂在 `app.state.forwin_runtime`；不存在模块级可变 API 状态。
- 所有 HTTP 路由位于 `forwin.http.adapters`。项目/Genesis/章节/review 调用 `ProjectApplicationService`，项目聚合读模型归 `forwin.application.read_models`，任务 mutation 调用 `TaskApplicationService`，任务读模型由 `forwin.application.task_center.TaskCenterService` 提供，project-control 调用 `ProjectControlApplicationService`，publisher/extension 调用 `PublisherApplicationService`；适配器不拥有业务状态机。
- `forwin.api` 只公开 `app`、`create_app` 与 `lifespan`。旧 `api_core`、根 `api_route_registry.py`、根 `api_*_routes.py`、`ModuleType` 代理、`globals().update()`、`api_project_ops`、`api_publisher_ops`、`api_project_policy` 和 `project_ops` 已删除。
- `/api/generate` 与 `GenerateRequest` 已删除。合法写作路径只有 `project_create -> Genesis generate/refine/lock -> project_start_writing`，以及 writing 项目的 `project_continue_generation`。
- 决策事件契约归 `forwin.audit.events`，持久化归 `forwin.models.audit`；任务契约、约束与 checkpoint 归 `forwin.planning`，草稿规则归 `forwin.review`，project-control 应用用例归 `forwin.application.project_control`，Codex 受控动作归 `forwin.codex_bridge.governed_actions`。生产代码不再使用泛化 `governance` namespace。

## Review 决策层

LLM 正文评审与 repair escalation 使用完整拼接后的 `WriterOutput.body`。`scene_outputs` 中的拼接前草稿不得作为第二份正文送审；原场景产物保留供诊断及结构化地图检查。摘要、状态、事件、时间候选与 Canon invariants 用于交叉核验，不替代最终正文。

章节 review 详情固定返回五个有序、互不代替的 `decision_layers`：

1. `draft_review`：草稿证据、问题与 verdict。
2. `repair`：修复尝试、范围与独立 verification。
3. `residual_eligibility`：repair 耗尽后的残余资格，不代表 Canon 已写入。
4. `gate_delegation`：仅由 `GATE_DELEGATION_*` 事件表示；未调用时明确为 `not_delegated`。
5. `canon`：只由最新 `CandidateDraftRecord.status/canon_status/canon_commit_id` 和 Canon 事件表示；通用 review approval 不构成 Canon 成功。

操作台使用专用五层 Review 模态窗展示这些状态、证据、修复历史与审计跳转，不再把 review 详情拼成 `window.alert` 文本。
- 生产代码禁止星号导入和隐式依赖仓库。拆分遗留的 2,336 个未使用导入已清除，runtime 模块必须直接声明依赖。

## Quality Profile

ForWin 只支持 `RuntimePolicy.quality_profile=standard|pulp`。

`standard` 是默认长篇质量策略；`pulp` 是低成本高节奏策略，使用精简 review signal、`pulp_fatal` canon quality gate、world-only BookState extraction、短上下文窗口和低成本 trope 选择。不存在 `premium`、operation/progression/reckless mode，也不存在请求级策略覆盖。

## Canon Commit Path

新章节只有经过以下候选与原子提交路径才能成为 accepted canon：

```text
immutable CandidateDraftRecord
-> CanonPreparationService
   -> eligibility / quality / EntityAdmissionPlan verification
   -> BookStateGraphDeltaExtractor -> BookStateReviewGate
   -> frozen CanonCommitPlan
-> CanonAdmissionService.commit_plan
   -> project/candidate locks + stale revalidation
   -> BookStateCompiler + entity/alias + obligations + chapter acceptance
   -> CanonCommitRecord + deterministic outbox rows
-> projection / phase3 maintenance / publisher outbox consumers
```

`forwin.canon.CanonAdmissionService` 是唯一把 candidate 转为 accepted/canon 状态的入口；generation pipeline 与人工接受都提交持久化的 `CanonCommitPlan`。旧 `commit()`、`BookStateDirectCommitService`、`BookStateCanonPort`、`_commit_book_state_canon`、`_apply_world_v4_gate` 和恒成功的 `_compile_world_model_after_acceptance` 已删除。运行期世界编辑 proposal 也只能经 `CanonAdmissionService.commit_world_edit` 写 BookState。

post-Canon maintenance 按 planning → arc → world → feedback 顺序运行，并完成 order controls。第2章起，Canon 提交强制要求前章四步与 controls 成功且没有未解除的 future/checkpoint/manual 阻断；它不同于可重建的知识投影。当前 trace 上传失败也可能使维护 step 失败，这项耦合尚未删除。

旧 `world_model_v4` / world-v4 compatibility projection 与 `StateUpdater.apply_*` 写入已经从 accepted chapter runtime 删除。`state_changes`、`new_events`、`thread_beats`、`time_advance` 和 EntityAdmissionPlan 先转成同一 GraphDelta 合约，再经 BookState review/compile 一次落盘；后续只保留 Knowledge Projection refresh 等当前检索投影。

## 兼容层

- `forwin.world_model`：已物理删除；可重建页面、proposal 与 Obsidian 能力归 `forwin.knowledge_system` / `forwin.obsidian`。
- `/world-model/*` HTTP 路径：仅保留传输契约名，适配器直接读取 BookState snapshot、Knowledge Projection page 与 CanonQualitySignal，不对应同名领域包或状态库。
- `world_model_v4`：已删除的旧 compatibility projection / debug-export bridge；不得重新作为 runtime 写入路径引入。
- `forwin.book_state.extraction`：BookState candidate extraction、deterministic gate 与 GraphDelta conversion 的唯一 owner；不是章节草稿 reviewer。
- legacy `entity_states / relation_edges / canon_events / event_entity_links / plot_threads / plot_thread_beats / story_time_points / chapter_timelines`：ORM 与表定义均已删除；`entities / entity_aliases` 只作为 Canon 实体准入提交后的身份唯一性索引。
- Provisional Band Preview runtime：已物理删除；不存在 policy 开关、第二 writer、preview service、执行/ledger 表、审计事件、HTTP/UI 入口或 repair callback。`provisional_window` / `provisional_band_size` 仅表示 Arc sizing 的近端 ChapterPlan 窗口，`ProvisionalPromotionRecord` 仅记录 accepted feedback，普通 writer 失败仍可调用 `write_preview_chapter()`。

## 投影层

`Knowledge Projection`、`Obsidian Vault`、Karpathy-style `LLM KB`、chapter memory index 和 World Studio 视图都必须可从 BookState 重建。它们不是 canon writer；章节接纳只写 deterministic outbox，投影失败重试且不能回滚 accepted state。Obsidian 是单向 export 投影，保留的人工 section 独立进入 human index；Canon 编辑必须通过 generic proposal，不存在 reverse import。

## Schema 基线

生产 schema 只由 Alembic 管理，当前唯一 revision 为 `0001_v5_baseline`。应用启动只校验 v5 revision，不执行 `create_all`、手写 `ALTER TABLE` 或自动升级。`init_db` 仅供 disposable PostgreSQL 测试库按当前 metadata 建表；旧数据库和旧项目不迁移。

## 地图红线

`SubWorld` 只表示大陆、星球、位面、异世界、星区等大尺度地图容器。城市、宗门外门、客栈、遗迹入口、炼丹塔等局部舞台必须进入 `Region / MapNode / site_state`，不得作为新的 `SubWorld` 语义写回设计或代码。

## 2026-07 Integrated Runtime Updates

- Semantic retrieval defaults to the LAN embedding gateway (`EMBEDDING_BACKEND=gateway`) with 384-dimensional `all-MiniLM-L6-v2` vectors. `scripts/reembed_memory_index.py` is the supported maintenance path for backfilling accepted chapter memories into the memory index.
- Pulp BookState extraction keeps the low-cost single-writer path but now emits a light structured world delta when extraction is deferred or degraded and the chapter otherwise has no world facts. Canon remains guarded by `BookStateReviewGate`.
- Arc continuation planning builds an `ArcActivationReviewPack` from accepted chapter summaries, current BookState snapshots, open obligations, recent decisions, audience signals, and faction/group state. The handoff records `ARC_ACTIVATION_REVIEW_PACK_BUILT`; degraded fallback chapter plans are marked `needs_review`.
- The pulp canon profile uses `pulp_fatal`: expanded fatal continuity signals still block admission, while lower-priority obligations remain review warnings unless they are hard blockers. Auto-review retry refuses hard canon, SubWorld, and active-rule blockers instead of looping blindly.
- The runtime trope registry expands seed and markdown libraries to a minimum usable 50-template pulp set with genre, audience, platform, payoff, and execution metadata. The chapter scheduler enforces a two-use-per-20-chapters template cooldown and prompt injection includes per-trope execution constraints.
- The operator task drawer surfaces stop-reason distribution, auto-continue chain health, `needs_review` / `repair_exhausted` queues, repair attempts, canon risk, review decision chains, retry actions, soft-accept actions, and proposal-backed narrative-obligation repairs. Entity admission conflicts remain fail-closed review evidence instead of entering a parallel SubWorld repair path.
