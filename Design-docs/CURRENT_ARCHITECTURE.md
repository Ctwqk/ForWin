# ForWin Current Architecture

更新时间：2026-10-21（世界引擎大修 P2 收口）

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

唯一 canon source 是 `BookState DB Canon`。章节生产仍由 `Arc -> Band -> Chapter -> Scene` 驱动；入 canon 的最终路径是 `ChapterDeltaExtractor -> ChapterDeltaTranslator -> BookStateReviewGate v6 -> BookStateCompiler -> GraphDelta ledger -> Snapshot`。

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

RepairVerifier使用完整原稿/修复稿及全部合同，逐条记录pass/fail/unknown和可核对引用；聚合字段为true/false/null。证据不足或超时不冒充通过，也不创建新质量门；已证实失败、主review和hard residual的阻断仍有效。有证据语义反对最多复核一次，未验证信息透传API/UI。

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

`standard` 是默认长篇质量策略；`pulp` 是低成本高节奏策略，使用精简 review signal、`pulp_fatal` canon quality gate、短上下文窗口和低成本 trope 选择。两个 profile 的 BookState 均固定启用 world/map/cognition/narrative 四层，`canon.book_state_layers` 已删除。不存在 `premium`、operation/progression/reckless mode，也不存在请求级策略覆盖。

## Canon Commit Path

新章节只有经过以下候选与原子提交路径才能成为 accepted canon：

```text
immutable CandidateDraftRecord
-> CanonPreparationService
   -> eligibility / quality / EntityAdmissionPlan verification
   -> ChapterDeltaExtractor -> ChapterDeltaTranslator -> BookStateReviewGate v6
   -> frozen CanonCommitPlan
-> CanonAdmissionService.commit_plan
   -> project/candidate locks + stale revalidation
   -> BookStateCompiler + entity/alias + obligations + chapter acceptance
   -> CanonCommitRecord + deterministic outbox rows
-> projection / phase3 maintenance / publisher outbox consumers
```

`forwin.canon.CanonAdmissionService` 是唯一把 candidate 转为 accepted/canon 状态的入口；generation pipeline 与人工接受都提交持久化的 `CanonCommitPlan`。旧 `commit()`、`BookStateDirectCommitService`、`BookStateCanonPort`、`_commit_book_state_canon`、`_apply_world_v4_gate` 和恒成功的 `_compile_world_model_after_acceptance` 已删除。运行期世界编辑 proposal 也只能经 `CanonAdmissionService.commit_world_edit` 写 BookState。

post-Canon maintenance 按 planning → arc → world → feedback 顺序运行，并完成 order controls。第2章起，Canon 提交强制要求前章四步与 controls 成功且没有未解除的 future/checkpoint/manual 阻断；它不同于可重建的知识投影。trace以冻结payload同事务进入既有outbox，异步上传失败不回滚已成功业务；补传不重跑模型，内容SHA区分不同trace对象。

旧 `world_model_v4` / world-v4 compatibility projection 与 `StateUpdater.apply_*` 写入已删除。当前统一 chapter delta 经 `ChapterDeltaTranslator` 与 BookState review/compile 落盘；后续只保留 Knowledge Projection refresh 等当前检索投影。

## 兼容层

- `forwin.world_model`：已物理删除；可重建页面、proposal 与 Obsidian 能力归 `forwin.knowledge_system` / `forwin.obsidian`。
- `/world-model/*` HTTP 路径：仅保留传输契约名，适配器直接读取 BookState snapshot、Knowledge Projection page 与 CanonQualitySignal，不对应同名领域包或状态库。
- `world_model_v4`：已删除的旧 compatibility projection / debug-export bridge；不得重新作为 runtime 写入路径引入。
- `forwin.book_state.extraction`：BookState candidate extraction 与 GraphDelta conversion 的唯一 owner；包含 `chapter_delta_extractor.py`、`chapter_delta_schema.py`、`chapter_input.py`、`evidence.py`、`ref_check.py`、`translator.py` 和 `world_review.py`。旧 `contract.py` / `graph_delta.py`、writer-contract builder 与正则兜底均已删除。
- legacy `entity_states / relation_edges / canon_events / event_entity_links / plot_threads / plot_thread_beats / story_time_points / chapter_timelines`：ORM 与表定义均已删除；`entities / entity_aliases` 只作为 Canon 实体准入提交后的身份唯一性索引。
- Provisional Band Preview runtime：已物理删除；不存在 policy 开关、第二 writer、preview service、执行/ledger 表、审计事件、HTTP/UI 入口或 repair callback。`provisional_window` / `provisional_band_size` 仅表示 Arc sizing 的近端 ChapterPlan 窗口，`ProvisionalPromotionRecord` 仅记录 accepted feedback，普通 writer 失败仍可调用 `write_preview_chapter()`。

## 投影层

`Knowledge Projection`、`Obsidian Vault`、Karpathy-style `LLM KB`、chapter memory index 和 World Studio 视图都必须可从 BookState 重建。它们不是 canon writer；章节接纳只写 deterministic outbox，投影失败重试且不能回滚 accepted state。Obsidian 是单向 export 投影，保留的人工 section 独立进入 human index；Canon 编辑必须通过 generic proposal，不存在 reverse import。

## Schema 基线

生产 schema 只由 Alembic 管理。当前 head 为 `0004_drop_parallel_ledgers`（删除 `countdown_ledgers` / `character_state_transitions`），父 revision 为 `0003_book_state_v6`，再前为 `0002_drop_dead_tables` 与 `0001_v5_recovery`（文件 `0001_v5_baseline.py`）；已合入的迁移不得原地修改。`0003` 新增 `story_calendars` / `book_lexicons`、GraphDelta 整数分钟时间列、节点身份版本列、world snapshot 的 narrative 数据和 arc 目标字段，并删除 `book_reader_promises`（承诺改存 narrative `promise` 节点）。`0002` 删除 `world_projection_deltas`、`story_obligations`、`reveal_registry_entries`、`artifact_collection_ledgers`、`book_reader_experience_deltas` 五张无读写的死表，还会 `DROP TABLE IF EXISTS` 只存在于生产库的 15 张 v4/scenario 遗留表（不在 ORM metadata 中，外键指向 `projects` 且不级联，会阻塞项目删除）；不使用 CASCADE，downgrade 不重建它们。应用启动只校验 head revision，不执行 `create_all`、手写 `ALTER TABLE` 或自动升级。`init_db` 仅供 disposable PostgreSQL 测试库按当前 metadata 建表；旧数据库和旧项目不迁移。代码 head 不代表生产部署已完成。

## 地图红线

`SubWorld` 只表示大陆、星球、位面、异世界、星区等大尺度地图容器。城市、宗门外门、客栈、遗迹入口、炼丹塔等局部舞台必须进入 `Region / MapNode / site_state`，不得作为新的 `SubWorld` 语义写回设计或代码。

## 2026-07 Integrated Runtime Updates

- Semantic retrieval defaults to the LAN embedding gateway (`EMBEDDING_BACKEND=gateway`) with 384-dimensional `all-MiniLM-L6-v2` vectors. `scripts/reembed_memory_index.py` is the supported maintenance path for backfilling accepted chapter memories into the memory index.
- Pulp 与 standard 共用统一 chapter-delta extraction / translation / gate v6；旧 `pulp_light` regex fallback 已删除。
- Arc continuation planning builds an `ArcActivationReviewPack` from accepted chapter summaries, current BookState snapshots, open obligations, recent decisions, audience signals, and faction/group state. The handoff records `ARC_ACTIVATION_REVIEW_PACK_BUILT`; degraded fallback chapter plans are marked `needs_review`.
- The pulp canon profile uses `pulp_fatal`: expanded fatal continuity signals still block admission, while lower-priority obligations remain review warnings unless they are hard blockers. Auto-review retry refuses hard canon, SubWorld, and active-rule blockers instead of looping blindly.
- The runtime trope registry expands seed and markdown libraries to a minimum usable 50-template pulp set with genre, audience, platform, payoff, and execution metadata. The chapter scheduler enforces a two-use-per-20-chapters template cooldown and prompt injection includes per-trope execution constraints.
- The operator task drawer surfaces stop-reason distribution, auto-continue chain health, `needs_review` / `repair_exhausted` queues, repair attempts, canon risk, review decision chains, retry actions, soft-accept actions, and proposal-backed narrative-obligation repairs. Entity admission conflicts remain fail-closed review evidence instead of entering a parallel SubWorld repair path.

## 2026-10 世界引擎大修 P0

设计见 [世界引擎大修](../docs/designs/2026-10-20-world-engine-overhaul.md)。本节记录 P0 的护栏与纯删除；P1 的当前实现见下一节。

- **防过拟合护栏**：`tests/overfit_guard.py`、`tests/overfit_allowlist.toml`、`tests/test_no_story_content_in_runtime.py` 用 AST 扫描 `forwin/` 中的故事内容。命中项必须在 allowlist 中分类（`language_lexicon`、`genre_lexicon`、`catalog`、`prompt_text`、`numeric_policy`、`eval_fixture`）；尚未删除的故事内容登记为 `pending_removal` 并带 `remove_in` 阶段。当前 `meta.completed_phase = "P2"`，`remove_in` 不得为 P0、P1 或 P2。`ALLOWED_PRODUCTION_MECHANISM_FILES` 已清空。
- **清库工具**：`scripts/purge_all_projects.py` 默认 dry-run，仅 `--yes-delete-all-projects` 才删除；存在非终态生成任务或上传任务时拒绝执行，保留全局 publisher 扩展与浏览器会话表。每个项目在独立事务中删除；dry-run 执行同样的删除后一律回滚，以提前暴露外键阻塞。任一项目删除失败时，已删项目保持删除，所有外部存储都不清理，退出码为 4。报告列出各本地根目录是否存在。`_delete_project` 现在同时删除 publisher 子表行与项目 outbox 事件。
- **v4 抽取链路已删除**：见“兼容层”。`WriterOutput` 的 v4 自报字段与 `RepairScope` 的 `world_model` 已移除。
- **0002 迁移**：见“Schema 基线”。
- **world contracts 只产出通用合同**：写死的“母星被围”样例（homeworld fixture）已删除。
- **续写**：extend-generation 追加一个 planned `ArcPlanVersion`，由 arc activation 为其规划章节，不再写入硬编码节拍；blueprint 缺少该卷时 materializer 从 `ArcPlanVersion` 合成 payload；读模型把“有计划卷但无章节计划”的卷报告为可恢复。
- **Genesis fallback 只返回结构壳**：不再编造人名、地名、职业；`stage_states[stage].degraded` 标记走了 fallback 的阶段。
- **运行代码移除的故事内容**：writer prompt 里的剧情示例、ArcDirector 默认世界、SubWorldManager 名字池、WorldSimulator 的 `chapter>=3` 规则；未命名的 planned 槽位不再被激活。canon_quality、checker（pulp profile 只按显式 genre 选择）、canon_names（锚点不分角色；P1 已接入 BookState 来源）、future-plan audit 中泄漏的第二套剧本词汇已清除。
- **genre 创建项目必填**：API、ORM、CLI、MCP 与首页表单均无默认题材。
- **llm_eval**：fixture 移到 `tests/fixtures/llm_eval`，CLI 必须传 `--context-file` / `--cases-file`。
- **剩余清理**：P1 的地图角色词表、题材人物 token 与关系 personality 映射已清理；`pending_removal` 剩余条目按 P4 清理，完整清单以 `tests/overfit_allowlist.toml` 为准。

## 2026-10 世界引擎大修 P1

- **Genesis**：阶段顺序为 `brief → world → map → story_engine → book_blueprint → world_seed`，`bootstrap` / `execution_bootstrap` 已删除。`world_seed` 由 LLM 生成，经 `forwin/genesis/world_seed.py` 确定性校验；有 error 时拒绝锁定，fallback 只返回标记 degraded 的结构壳。story_engine 的 core_cast / opposition / faction 有 id（缺省生成 `cast-n` / `opp-n` / `faction-n`），`relationship_axes` / `long_arcs` 为结构化数据，core_cast 使用 `secrets` 列表。种子须覆盖每个 actor；fallback 只按这些 id 建壳。
- **开写交接**：先物化 arcs（`arc_plan_versions` 保存 `title` / `goal` / `stakes` / `payoff_direction`），再 map bootstrap，再 `WorldSeedCommitter`，最后生成 arc-1 ChapterPlan。种子转换为 `source_type="genesis_seed"` 的第 0 章 GraphDelta，经 `BookStateReviewGate` 与 `CanonAdmissionService.commit_world_seed` 编译；后者以 `FOR NO KEY UPDATE` 锁住项目。`HandoffStageError` 的 `GenesisMapBootstrapError` / `WorldSeedCommitError` 分别记录 `MAP_GENERATION_FAILED` / `WORLD_SEED_COMMIT_FAILED`。第 0 章 subworld 角色生成路径已删除。
- **BookState v6**：故事时间为自纪元起的整数分钟，存于 `graph_deltas.story_time_start/end`；`story_calendars` 提供本书日历，`format_story_time` 负责显示。`book_lexicons` 保存时间单位别名、等级阶梯、货币、称谓、人物泛称、交通模式与消息速度。节点的 name / aliases / profile / is_active 随章存于 `world_node_states`，`world_nodes` 是最新投影；narrative 层进入 world snapshot。向已有章节编译以该章为基底，世界编辑钳位到最新 canon 章并在项目锁内复核。
- **结构与认知**：新增 `agenda` / `intent` / `clock` 节点及 v6 值校验；character 使用 `alive_status` / `restraint` / `travel_mode_ids`，位置仍为 `state.location_id`；actor 使用 `actor_tier` / `next_turn_at` / `resources`；rule 使用 `hardness` / `statement` / `checkable`，社交边支持 `rival_of`。认知经知识事件写 `knowledge_by_ref`，默认可见性由事实敏感度决定。读者承诺存为 narrative `promise` 节点，不再使用独立表或 `metadata.reader_promise_patches`。
- **内容来源**：人物泛称来自本书 lexicon；canon name anchors 从 BookState 的 kinship 边及 `metadata.appellation` 读取，方向为“source 的 appellation → target”。地图生成从 Genesis 结构推导区域、地形和路线，不再嗅探题材关键词。
- **与设计的取舍**：P1 当时延后的章节时间与 v6 世界检查已在 P2 实现；当前 P3/P4 范围见下节。

## 2026-10 世界引擎大修 P2（2026-10-21）

状态：P2 代码收口。

- **单次抽取与翻译**：每个候选正文由 `ChapterDeltaExtractor` 在 `chapter_delta_extraction` stage 完成一次统一抽取（校验失败可重试；引用了输入表之外的 ref 时，按 translator 的规则逐条指出并重试一次）；`ensure_chapter_delta` 按 `body_sha256` 复用结果，正文改变才重抽。输入包括 BookState as-of N−1 的最小实体表、地点表、计划目标 / 义务 / reward targets、intents、secrets、calendar units、time aliases 和 travel modes，不是 ChapterWorldView。`ChapterDeltaTranslator` 将 `WriterOutput.chapter_delta` 翻译为 `source_type="chapter_extraction"` 的 GraphDelta，再经 `BookStateReviewGate` v6；draft review 和 canon preparation 共用这份抽取，最终仍由 `CanonAdmissionService.commit_plan` 原子提交。canon preparation 遇到缺失或过期的抽取时，先核验实体准入，再由 BookState preparation 重抽，之后才跑 canon quality gate，到期义务按新抽取判定。
- **世界审查**：`WorldConsistencyReviewer` 的 `world_consistency` draft-review 信号运行相同 translator 与 gate，把错误送进 repair；它替代已删除的 `MapMovementReviewer` / `forwin/review/map_movement.py`。v6 检查时间跨度、移动偏移、已知路径可达性、`review.travel_tolerance` 下的速度、受限角色移动、章前已死亡实体行动、物品持有者、告知来源和读者揭示时间表。地图边 `travel_time` 为小时，乘 60 换成整数分钟；本书时间单位统一经 `duration_to_minutes` 换算。
- **结构合同**：移动存于 delta `metadata["movements"]`；物品只认 item 的 `state.holder_id`。条目级 `flashback: bool` 加 `time.evidence` 表示闪回；闪回只产生事实与知识事件，不改变当前 state。非 create 结构补丁带 `old_value` 与 `field_path`。计划 goals 与 narrative obligations 由有正文证据的结构化 `plan_fulfillment` 判定，已删除关键词子串检查。
- **删除与 schema**：`WriterContractDeltaBuilder`、`BookStateGraphDeltaExtractor`、`pulp_light` 正则兜底、旧分阶段 extraction、倒计时/羁押 future-plan audits 和 canon_quality 的 countdown / character-state ledgers 已删除。当前 Alembic head 为 `0004_drop_parallel_ledgers`（父为 `0003_book_state_v6`），删除 `countdown_ledgers` / `character_state_transitions`。chapter review form 继续作为 LLM 信号，其行来自 BookState，回答只产出 quality signals，不再写任何平行账本。
- **Genesis 与原子开写**：Genesis map 提供结构化 submaps / regions / nodes / edges；`forwin.map.genesis_adapter` 将其转换为 BookMap。handoff 依次物化 arcs → map → chapter-0 world_seed → arc-1 ChapterPlan，service / seed commit 只 flush，由调用方提交；`HandoffStageError` 回滚交接写入后只保留失败事件。成功时才锁定 Genesis revision 并设为 writing。
- **护栏收口**：`tests/overfit_allowlist.toml` 的 `meta.completed_phase = "P2"`，`pending_removal` 只能指向未来阶段；canary 的时间单位 ignore 及过时注释已删除。

### 与设计的取舍 / P3–P4 延后项

- **P3，确定性地图物化**：每次 build 重新生成地图；seed 包含随机 Genesis revision id。atlas edges 目前只贡献放在连续 anchors 之间的 kind，没有按声明端点物化，所以 movement checks 仍针对随机生成地图；subworld 内的 atlas `hidden` 边虽被携带，但尚未消费。
- **P3，clock producers**：逾期而仍为 running 的 clocks 在 review form 中仍是 active，不裁剪且无数量上限；secret-scope clock node ids 可能经 open-signal descriptions 到达 writer。
- **P3，地图规则**：旧 map_movement reviewer 的 access rules、team speed、edge multipliers、false routes 从未在生产运行，也不在 gate v6 中；不能把旧设计描述为已实现。
- **P4，writer 世界视图**：平行账本删除后，writer prompt 不携带 clock / restraint state；替代物是设计 §5.1 的 `ChapterWorldView`。writer 的 `<<FORWIN_LOCATION>>` 标记暂时保留。
- **P3/P4，未闭环信号与补丁**：关键词检查删除后 `director_setup_without_delivery` 无 producer；`form_invariant_drift` 无 producer；`ledger_state_drift_pre_write` patches 虽持久化但从未应用。
- **P3/P4，protection 与规划**：未来约束的角色可用性、秘密保留、关系保持、地点存在及规则保持关键词判定直接删除，P2 不提供替代；protection / `review_constraint` 等 P4 卷节拍表生产约束、P3 冲突审查。`task_contract_json` 存储与 ArcDirector / band 规划保留到 P4；`launch_arc_N`、`finalization._seed_state`、`RunControlStage.run`、`SubWorldManager.apply_initial_arc_plan`、`sub_world_roster_items` / `plan_band_activation` 同期替换。
- **当前揭示约定**：world_seed 的 `location_ref` 在提交时换成数据库地图节点 id；`reveal_candidates` 保留在冻结 Genesis pack，P2 的 `reveal_schedule.earliest_reveal_chapters` 取对应卷的 `chapter_start`；无时间表时向读者推进记 warning。`reveal_directives` 留到 P4。

生产 rollout 项目统一见 P2 deploy runbook；本文只记录代码现状与后续设计，不宣称生产验收完成。
