# ForWin Current Architecture

更新时间：2026-07-10

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
- 任务入口：API、worker、scheduler、CLI、Genesis handoff、continue 和 auto-continue 统一经过 `GenerationApplicationService`；`RuntimeContainer` 是唯一 `ChapterPipeline` 构造点。
- 运行时计划：`forwin.planning.PlanningService` 是写侧门面，`PlanningQuery` 读取 active arc/chapter/band 计划，future audit、patch validation 与 scenario rehearsal 统一投影为 `PlanHealth`。
- 实体准入：`EntityRegistrar` 只构建并验证候选稿上的 `EntityAdmissionPlan`，不会写 `Entity` / `EntityAlias`；分类器异常、遗漏、别名歧义和唯一性冲突均 fail-closed。只有 `CanonAdmissionService` 通过 `EntityAdmissionCommitter` 在 Canon 事务中落实无冲突计划。
- review 主链：`review.DraftReviewService` 聚合章节文本、体验、治理、地图、人格和 lint；draft review 与 canon gate 通过 `QualityAnalysisRunRow` 共享 primary quality 分析；`review.repair.RepairService` 是 draft/canon repair 的两个显式入口；`review.decision.FinalResidualPolicy` 只评估 repair 耗尽后的残留，不决定 canon。`CanonPreparationService` 在事务外完成资格、quality、实体计划与 BookState review，`CanonAdmissionService.commit_plan` 是唯一 accepted-chapter 原子写入口；`BookStateReviewGate` 是 GraphDelta 入 canon 前的 deterministic guardrail。
- skill runtime：仅作为 prompt / workflow instruction layer，参与 PromptTrace，不写 canon，不绕过 DecisionEvent 或 BookState gate。

## 模块与入口边界

- 章节生产入口是 `forwin.generation.pipeline.ChapterPipeline`。它静态组合 run control、governance、review、repair planning、chapter execution、writer、finalization 等 stage owner，构造器只接收具体类型协作者；类体不再做跨模块函数赋值。旧 `WritingOrchestrator`、模块回注、伪造 `__module__` 和完整 pipeline 反向注入均已删除。
- `forwin.generation.pipeline_core` 以 stage owner class 保存 pipeline 行为，以模块私有函数保存纯计算；不通过 `common.py` 转发外域类型。`RepairExecution` 与 `CanonPreparationContext` 是冻结的窄能力集，review/canon 域不依赖 `ChapterPipeline`。
- Genesis 只有 `forwin.genesis` 一个包，workspace 与 handoff 是其子域；`book_genesis.py`、`book_genesis_core`、`genesis_workspace`、`genesis_handoff` 旧入口均已删除。
- 项目/Genesis/章节/review 的传输适配统一落到 `ProjectApplicationService`；publisher HTTP/extension 动作统一落到 `PublisherApplicationService`；生成任务统一落到 `GenerationApplicationService`。
- `forwin.api` 只公开 `app` 与 `lifespan`。旧 `ModuleType` 代理、`api_core.exports`、`globals().update()`、`api_project_ops`、`api_publisher_ops`、`api_project_policy` 和 `project_ops` 根包已删除。
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
-> post-commit knowledge / memory / publisher workers
```

`forwin.canon.CanonAdmissionService` 是唯一把 candidate 转为 accepted/canon 状态的入口；generation pipeline 与人工接受都提交持久化的 `CanonCommitPlan`。旧 `commit()`、`BookStateDirectCommitService`、`BookStateCanonPort`、`_commit_book_state_canon`、`_apply_world_v4_gate` 和恒成功的 `_compile_world_model_after_acceptance` 已删除。运行期世界编辑 proposal 也只能经 `CanonAdmissionService.commit_world_edit` 写 BookState。

旧 `world_model_v4` / world-v4 compatibility projection 与 `StateUpdater.apply_*` 写入已经从 accepted chapter runtime 删除。`state_changes`、`new_events`、`thread_beats`、`time_advance` 和 EntityAdmissionPlan 先转成同一 GraphDelta 合约，再经 BookState review/compile 一次落盘；后续只保留 Knowledge Projection refresh 等当前检索投影。

## 兼容层

- `forwin.world_model`：已物理删除；可重建页面、proposal 与 Obsidian 能力归 `forwin.knowledge_system` / `forwin.obsidian`。
- `/world-model/*` HTTP 路径：仅保留传输契约名，适配器直接读取 BookState snapshot、Knowledge Projection page 与 CanonQualitySignal，不对应同名领域包或状态库。
- `world_model_v4`：已删除的旧 compatibility projection / debug-export bridge；不得重新作为 runtime 写入路径引入。
- `world_v4_review_gate`：BookState extraction deterministic gate；不是章节草稿 reviewer。
- legacy `entity_states / relation_edges / canon_events / event_entity_links / plot_threads / plot_thread_beats / story_time_points / chapter_timelines`：ORM 与表定义均已删除；`entities / entity_aliases` 只作为 Canon 实体准入提交后的身份唯一性索引。
- legacy provisional：历史预演、审计和 compatibility preview，不默认阻断正式写作。

## 投影层

`Knowledge Projection`、`Obsidian Vault`、Karpathy-style `LLM KB`、chapter memory index 和 World Studio 视图都必须可从 BookState 重建。它们不是 canon writer；章节接纳只写 deterministic outbox，投影失败重试且不能回滚 accepted state。

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
