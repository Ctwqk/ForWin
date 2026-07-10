# ForWin Current Architecture

更新时间：2026-07-09

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
- 任务入口：API、worker、scheduler、CLI、Genesis handoff、continue 和 auto-continue 统一经过 `GenerationApplicationService`；`RuntimeContainer` 是唯一 orchestrator 装配点。
- 运行时计划：`forwin.planning.PlanningService` 是写侧门面，`PlanningQuery` 读取 active arc/chapter/band 计划，future audit、patch validation 与 scenario rehearsal 统一投影为 `PlanHealth`。
- 实体准入：`EntityRegistrar` 只构建并验证候选稿上的 `EntityAdmissionPlan`，不会写 `Entity` / `EntityAlias`；分类器异常、遗漏、别名歧义和唯一性冲突均 fail-closed。只有 `CanonAdmissionService` 通过 `EntityAdmissionCommitter` 在 Canon 事务中落实无冲突计划。
- review 主链：`review.DraftReviewService` 聚合章节文本、体验、治理、地图、人格和 lint；draft review 与 canon gate 通过 `QualityAnalysisRunRow` 共享 primary quality 分析，cache key 包含正文内容、chapter plan、prior-canon 分析上下文、模式/版本与模型指纹；`review.repair.RepairService` 是 draft/canon repair 的两个显式入口；`review.decision.FinalResidualPolicy` 只评估 repair 耗尽后的残留，不决定 canon；`CanonAdmissionService` 直接编排 quality、BookState 与 projection helper，不经 `WritingOrchestrator` canon 方法注入；`BookStateReviewGate` 是 GraphDelta 入 canon 前的 deterministic guardrail。
- skill runtime：仅作为 prompt / workflow instruction layer，参与 PromptTrace，不写 canon，不绕过 DecisionEvent 或 BookState gate。

## Quality Profile

ForWin 只支持 `RuntimePolicy.quality_profile=standard|pulp`。

`standard` 是默认长篇质量策略；`pulp` 是低成本高节奏策略，使用精简 review signal、`pulp_fatal` canon quality gate、world-only BookState extraction、短上下文窗口和低成本 trope 选择。不存在 `premium`、operation/progression/reckless mode，也不存在请求级策略覆盖。

## Canon Commit Path

新章节 accepted 后，canon success 只以 BookState direct path 为准：

```text
WriterOutput / chapter body
-> EntityAdmissionPlan verification
-> WriterOutput structured contract -> GraphDelta
-> BookStateGraphDeltaExtractor
-> BookStateReviewGate
-> BookStateCompiler
-> EntityAdmissionPlan commit
-> projection refresh
```

`forwin.canon.CanonAdmissionService` 是唯一把 candidate 转为 accepted/canon 状态的入口；generation pipeline 与人工接受都调用其强类型 `commit()`。内部 BookState 写路径名为 `_commit_book_state_canon`。`_apply_canon_candidate`、兼容 outcome coercer、`_apply_world_v4_gate` 和恒成功的 `_compile_world_model_after_acceptance` 已删除；`FinalResidualPolicy` 的 force-accept 候选仍必须经过上述 canon admission。

旧 `world_model_v4` / world-v4 compatibility projection 与 `StateUpdater.apply_*` 写入已经从 accepted chapter runtime 删除。`state_changes`、`new_events`、`thread_beats`、`time_advance` 和 EntityAdmissionPlan 先转成同一 GraphDelta 合约，再经 BookState review/compile 一次落盘；后续只保留 Knowledge Projection refresh 等当前检索投影。

## 兼容层

- `world_model`：legacy wiki/export/projection/read path；不作为新 canon 语义来源。
- `world_model_v4`：已删除的旧 compatibility projection / debug-export bridge；不得重新作为 runtime 写入路径引入。
- `reviewer_v4`：world_v4 extraction compatibility gate；不是 `reviewer` 的新版替代品。
- legacy `entity_states / relation_edges / CanonEvent / PlotThreadBeat / ChapterTimeline`：已退出 accepted-state 读写路径，等待 v5 baseline 物理删表；`entities / entity_aliases` 只作为 Canon 实体准入提交后的身份唯一性索引。
- legacy provisional：历史预演、审计和 compatibility preview，不默认阻断正式写作。

## 投影层

`Obsidian Vault`、Karpathy-style `LLM KB`、legacy wiki/export 和 World Studio 视图都必须可从 BookState 或兼容投影重建。它们不是 canon writer。

## 地图红线

`SubWorld` 只表示大陆、星球、位面、异世界、星区等大尺度地图容器。城市、宗门外门、客栈、遗迹入口、炼丹塔等局部舞台必须进入 `Region / MapNode / site_state`，不得作为新的 `SubWorld` 语义写回设计或代码。

## 2026-07 Integrated Runtime Updates

- Semantic retrieval defaults to the LAN embedding gateway (`EMBEDDING_BACKEND=gateway`) with 384-dimensional `all-MiniLM-L6-v2` vectors. `scripts/reembed_memory_index.py` is the supported maintenance path for backfilling accepted chapter memories into the memory index.
- Pulp BookState extraction keeps the low-cost single-writer path but now emits a light structured world delta when extraction is deferred or degraded and the chapter otherwise has no world facts. Canon remains guarded by `BookStateReviewGate`.
- Arc continuation planning builds an `ArcActivationReviewPack` from accepted chapter summaries, current BookState snapshots, open obligations, recent decisions, audience signals, and faction/group state. The handoff records `ARC_ACTIVATION_REVIEW_PACK_BUILT`; degraded fallback chapter plans are marked `needs_review`.
- The pulp canon profile uses `pulp_fatal`: expanded fatal continuity signals still block admission, while lower-priority obligations remain review warnings unless they are hard blockers. Auto-review retry refuses hard canon, SubWorld, and active-rule blockers instead of looping blindly.
- The runtime trope registry expands seed and markdown libraries to a minimum usable 50-template pulp set with genre, audience, platform, payoff, and execution metadata. The chapter scheduler enforces a two-use-per-20-chapters template cooldown and prompt injection includes per-trope execution constraints.
- The operator task drawer surfaces stop-reason distribution, auto-continue chain health, `needs_review` / `repair_exhausted` queues, repair attempts, canon risk, review decision chains, retry actions, soft-accept actions, and proposal-backed narrative-obligation repairs. Entity admission conflicts remain fail-closed review evidence instead of entering a parallel SubWorld repair path.
