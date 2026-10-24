# ForWin Current Architecture

更新时间：2026-10-22（世界引擎大修 P4a 收口）

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
- 决策事件契约归 `forwin.audit.events`，持久化归 `forwin.models.audit`；任务契约、约束与 checkpoint 归 `forwin.planning`，草稿规则归 `forwin.review`，project-control 应用用例归 `forwin.application.project_control`，Codex 世界冲突裁定归 `forwin.world_engine.conflict_delegate`。生产代码不再使用泛化 `governance` namespace。

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

post-Canon maintenance 按 world → plan → feedback 顺序运行；world 只运行 `WorldTickService.run`，feedback 在聚合前分析新读者评论；三步之后完成 order controls。第2章起，Canon 提交强制要求前章三步与 controls 成功且没有未解除的 future/checkpoint/manual 阻断；它不同于可重建的知识投影。world / feedback 及 order_controls 在各自事务内提交结果、完成状态与冻结 trace outbox。plan 使用普通 session：卷激活、节拍表、剩余计划审计 / 重规划、下一 band 准备分别提交，最后经 completion fence 同事务提交 step 成功状态与 trace outbox。plan 未完成或失败时可已有持久化规划结果；重试保留并按条件复用这些工作，不保证整步回滚。raw-artifact 在相应工作单元内同步写入，存储失败可使当前单元 / step 失败；异步 outbox 上传失败仅补传，不回滚已提交业务或重跑模型，内容 SHA 区分不同 trace 对象。

旧 `world_model_v4` / world-v4 compatibility projection 与 `StateUpdater.apply_*` 写入已删除。当前统一 chapter delta 经 `ChapterDeltaTranslator` 与 BookState review/compile 落盘；后续只保留 Knowledge Projection refresh 等当前检索投影。

## 兼容层

- `forwin.world_model`：已物理删除；可重建页面、proposal 与 Obsidian 能力归 `forwin.knowledge_system` / `forwin.obsidian`。
- `/world-model/*` HTTP 路径：仅保留传输契约名，适配器直接读取 BookState snapshot、Knowledge Projection page 与 CanonQualitySignal，不对应同名领域包或状态库。
- `world_model_v4`：已删除的旧 compatibility projection / debug-export bridge；不得重新作为 runtime 写入路径引入。
- `forwin.book_state.extraction`：BookState candidate extraction 与 GraphDelta conversion 的唯一 owner；包含 `chapter_delta_extractor.py`、`chapter_delta_schema.py`、`chapter_input.py`、`evidence.py`、`ref_check.py`、`translator.py` 和 `world_review.py`。旧 `contract.py` / `graph_delta.py`、writer-contract builder 与正则兜底均已删除。
- legacy `entity_states / relation_edges / canon_events / event_entity_links / plot_threads / plot_thread_beats / story_time_points / chapter_timelines`：ORM 与表定义均已删除；`entities / entity_aliases` 只作为 Canon 实体准入提交后的身份唯一性索引。
- Provisional Band Preview runtime：已物理删除；不存在 policy 开关、第二 writer、preview service、执行/ledger 表、审计事件、HTTP/UI 入口或 repair callback。`provisional_window` / `provisional_band_size` 仅表示 Arc sizing 的近端 ChapterPlan 窗口，`ProvisionalPromotionRecord` 及其表已在 P4a 删除，普通 writer 失败仍可调用 `write_preview_chapter()`。

## 投影层

`Knowledge Projection`、`Obsidian Vault`、Karpathy-style `LLM KB`、chapter memory index 和 World Studio 视图都必须可从 BookState 重建。它们不是 canon writer；章节接纳只写 deterministic outbox，投影失败重试且不能回滚 accepted state。Obsidian 是单向 export 投影，保留的人工 section 独立进入 human index；Canon 编辑必须通过 generic proposal，不存在 reverse import。

## Schema 基线

生产 schema 只由 Alembic 管理。当前 head 为 `0006_planning_v6`（规划表与 P4a 删除项），父 revision 为 `0005_world_plan_conflicts`（新增 `world_plan_conflicts`），再前为 `0004_drop_parallel_ledgers`（删除 `countdown_ledgers` / `character_state_transitions`），再前为 `0003_book_state_v6`、`0002_drop_dead_tables` 与 `0001_v5_recovery`（文件 `0001_v5_baseline.py`）；已合入的迁移不得原地修改。`0003` 新增 `story_calendars` / `book_lexicons`、GraphDelta 整数分钟时间列、节点身份版本列、world snapshot 的 narrative 数据和 arc 目标字段，并删除 `book_reader_promises`（承诺改存 narrative `promise` 节点）。`0002` 删除 `world_projection_deltas`、`story_obligations`、`reveal_registry_entries`、`artifact_collection_ledgers`、`book_reader_experience_deltas` 五张无读写的死表，还会 `DROP TABLE IF EXISTS` 只存在于生产库的 15 张 v4/scenario 遗留表（不在 ORM metadata 中，外键指向 `projects` 且不级联，会阻塞项目删除）；不使用 CASCADE，downgrade 不重建它们。应用启动只校验 head revision，不执行 `create_all`、手写 `ALTER TABLE` 或自动升级。`init_db` 仅供 disposable PostgreSQL 测试库按当前 metadata 建表；旧数据库和旧项目不迁移。代码 head 不代表生产部署已完成。

## 地图红线

`SubWorld` 只表示大陆、星球、位面、异世界、星区等大尺度地图容器。城市、宗门外门、客栈、遗迹入口、炼丹塔等局部舞台必须进入 `Region / MapNode / site_state`，不得作为新的 `SubWorld` 语义写回设计或代码。

## 2026-07 Integrated Runtime Updates

- Semantic retrieval defaults to the LAN embedding gateway (`EMBEDDING_BACKEND=gateway`) with 384-dimensional `all-MiniLM-L6-v2` vectors. `scripts/reembed_memory_index.py` is the supported maintenance path for backfilling accepted chapter memories into the memory index.
- Pulp 与 standard 共用统一 chapter-delta extraction / translation / gate v6；旧 `pulp_light` regex fallback 已删除。
- Arc continuation now uses `ArcProgressionService`, beat sheets and just-in-time `BandPlanner`; the retired activation/envelope/structure planner path is removed.
- The pulp canon profile uses `pulp_fatal`: expanded fatal continuity signals still block admission, while lower-priority obligations remain review warnings unless they are hard blockers. Auto-review retry refuses hard canon, SubWorld, and active-rule blockers instead of looping blindly.
- The runtime trope registry expands seed and markdown libraries to a minimum usable 50-template pulp set with genre, audience, platform, payoff, and execution metadata. The chapter scheduler enforces a two-use-per-20-chapters template cooldown and prompt injection includes per-trope execution constraints.
- The operator task drawer surfaces stop-reason distribution, auto-continue chain health, `needs_review` / `repair_exhausted` queues, repair attempts, canon risk, review decision chains, retry actions, soft-accept actions, and proposal-backed narrative-obligation repairs. Entity admission conflicts remain fail-closed review evidence instead of entering a parallel SubWorld repair path.

## 2026-10 世界引擎大修 P0

设计见 [世界引擎大修](../docs/designs/2026-10-20-world-engine-overhaul.md)。本节记录 P0 的护栏与纯删除；P1 的当前实现见下一节。

- **防过拟合护栏**：`tests/overfit_guard.py`、`tests/overfit_allowlist.toml`、`tests/test_no_story_content_in_runtime.py` 用 AST 扫描 `forwin/` 中的故事内容。命中项必须在 allowlist 中分类（`language_lexicon`、`genre_lexicon`、`catalog`、`prompt_text`、`numeric_policy`、`eval_fixture`）；尚未删除的故事内容登记为 `pending_removal` 并带 `remove_in` 阶段。当前 `meta.completed_phase = "P4"`，`pending_removal` 为空。`ALLOWED_PRODUCTION_MECHANISM_FILES` 已清空。
- **清库工具**：`scripts/purge_all_projects.py` 默认 dry-run，仅 `--yes-delete-all-projects` 才删除；存在非终态生成任务或上传任务时拒绝执行，保留全局 publisher 扩展与浏览器会话表。每个项目在独立事务中删除；dry-run 执行同样的删除后一律回滚，以提前暴露外键阻塞。任一项目删除失败时，已删项目保持删除，所有外部存储都不清理，退出码为 4。报告列出各本地根目录是否存在。`_delete_project` 现在同时删除 publisher 子表行与项目 outbox 事件。
- **v4 抽取链路已删除**：见“兼容层”。`WriterOutput` 的 v4 自报字段与 `RepairScope` 的 `world_model` 已移除。
- **0002 迁移**：见“Schema 基线”。
- **world contracts 已删除**：P4a 移除了合同生成器及合同表，由节拍表与 band 计划承接规划；P0 当时只清除了写死的 homeworld 样例。
- **续写**：extend-generation 追加一个 planned `ArcPlanVersion`，由 ArcProgressionService 生成节拍表，再即时规划 band，不再写入硬编码节拍；blueprint 缺少该卷时 materializer 从 `ArcPlanVersion` 合成 payload；读模型把“有计划卷但无章节计划”的卷报告为可恢复。
- **Genesis fallback 只返回结构壳**：不再编造人名、地名、职业；`stage_states[stage].degraded` 标记走了 fallback 的阶段。
- **运行代码移除的故事内容**：writer prompt 里的剧情示例、ArcDirector 默认世界、SubWorldManager 名字池、WorldSimulator 的 `chapter>=3` 规则；未命名的 planned 槽位不再被激活。canon_quality、checker（pulp profile 只按显式 genre 选择）、canon_names（锚点不分角色；P1 已接入 BookState 来源）、future-plan audit 中泄漏的第二套剧本词汇已清除。
- **genre 创建项目必填**：API、ORM、CLI、MCP 与首页表单均无默认题材。
- **llm_eval**：fixture 移到 `tests/fixtures/llm_eval`，CLI 必须传 `--context-file` / `--cases-file`。
- **剩余清理**：P1 的地图角色词表、题材人物 token 与关系 personality 映射已清理；P4a 后 `pending_removal` 已清空，分类清单以 `tests/overfit_allowlist.toml` 为准。

## 2026-10 世界引擎大修 P1

- **Genesis**：阶段顺序为 `brief → world → map → story_engine → book_blueprint → world_seed`，`bootstrap` / `execution_bootstrap` 已删除。`world_seed` 由 LLM 生成，经 `forwin/genesis/world_seed.py` 确定性校验；有 error 时拒绝锁定，fallback 只返回标记 degraded 的结构壳。story_engine 的 core_cast / opposition / faction 有 id（缺省生成 `cast-n` / `opp-n` / `faction-n`），`relationship_axes` / `long_arcs` 为结构化数据，core_cast 使用 `secrets` 列表。种子须覆盖每个 actor；fallback 只按这些 id 建壳。
- **开写交接**：先物化 arcs（`arc_plan_versions` 保存 `title` / `goal` / `stakes` / `payoff_direction`），再 map bootstrap、`WorldSeedCommitter`，最后生成 arc-1 节拍表与 protection，不创建 ChapterPlan。种子转换为 `source_type="genesis_seed"` 的第 0 章 GraphDelta，经 `BookStateReviewGate` 与 `CanonAdmissionService.commit_world_seed` 编译；后者以 `FOR NO KEY UPDATE` 锁住项目。`HandoffStageError` 的 `GenesisMapBootstrapError` / `WorldSeedCommitError` 分别记录 `MAP_GENERATION_FAILED` / `WORLD_SEED_COMMIT_FAILED`。第 0 章 subworld 角色生成路径已删除。
- **BookState v6**：故事时间为自纪元起的整数分钟，存于 `graph_deltas.story_time_start/end`；`story_calendars` 提供本书日历，`format_story_time` 负责显示。`book_lexicons` 保存时间单位别名、等级阶梯、货币、称谓、人物泛称、交通模式与消息速度。节点的 name / aliases / profile / is_active 随章存于 `world_node_states`，`world_nodes` 是最新投影；narrative 层进入 world snapshot。向已有章节编译以该章为基底，世界编辑钳位到最新 canon 章并在项目锁内复核。
- **结构与认知**：新增 `agenda` / `intent` / `clock` 节点及 v6 值校验；character 使用 `alive_status` / `restraint` / `travel_mode_ids`，位置仍为 `state.location_id`；actor 使用 `actor_tier` / `next_turn_at` / `resources`；rule 使用 `hardness` / `statement` / `checkable`，社交边支持 `rival_of`。认知经知识事件写 `knowledge_by_ref`，默认可见性由事实敏感度决定。读者承诺存为 narrative `promise` 节点，不再使用独立表或 `metadata.reader_promise_patches`。
- **内容来源**：人物泛称来自本书 lexicon；canon name anchors 从 BookState 的 kinship 边及 `metadata.appellation` 读取，方向为“source 的 appellation → target”。地图生成从 Genesis 结构推导区域、地形和路线，不再嗅探题材关键词。
- **与设计的取舍**：P1 当时延后的章节时间与 v6 世界检查已在 P2 实现；当前 P4a 实现与 P4b 范围见后文。

## 2026-10 世界引擎大修 P2（2026-10-21）

P2 的抽取与审查已落地；P3 基础与 P4a 增量见后文。

- **单次抽取与翻译**：每个候选正文由 `ChapterDeltaExtractor` 在 `chapter_delta_extraction` stage 完成一次统一抽取（校验失败可重试；引用了输入表之外的 ref 时，按 translator 的规则逐条指出并重试一次）；`ensure_chapter_delta` 只在 `body_sha256` 与 `plan_fingerprint` 同时匹配时复用，任一变化都重抽。输入包括 BookState as-of N−1 的最小实体表、地点表、计划目标 / 义务 / reward targets、intents、secrets、calendar units、time aliases 和 travel modes，不是 ChapterWorldView。`ChapterDeltaTranslator` 将 `WriterOutput.chapter_delta` 翻译为 `source_type="chapter_extraction"` 的 GraphDelta，再经 `BookStateReviewGate` v6；draft review 和 canon preparation 共用这份抽取，最终仍由 `CanonAdmissionService.commit_plan` 原子提交。canon preparation 遇到缺失或过期的抽取时，先核验实体准入，再由 BookState preparation 重抽，之后才跑 canon quality gate，到期义务按新抽取判定。抽取到的事实全部丢失正文证据时，canon 以 `chapter-delta-unverified-blocked` 阻断；没有事实被丢弃的空抽取（包括只丢弃了实体提及或计划履行条目的）仍提交无 patch 的 delta。
- **世界审查**：`WorldConsistencyReviewer` 的 `world_consistency` draft-review 信号运行相同 translator 与 gate，把错误送进 repair；它替代已删除的 `MapMovementReviewer` / `forwin/review/map_movement.py`。v6 检查时间跨度、移动偏移、已知路径可达性、`review.travel_tolerance` 下的速度、受限角色移动、章前已死亡实体行动、物品持有者、告知来源和读者揭示时间表。地图边 `travel_time` 为小时，乘 60 换成整数分钟；本书时间单位统一经 `duration_to_minutes` 换算。
- **结构合同**：移动存于 delta `metadata["movements"]`；物品只认 item 的 `state.holder_id`。条目级 `flashback: bool` 加 `time.evidence` 表示闪回；闪回只产生事实与知识事件，不改变当前 state。非 create 结构补丁带 `old_value` 与 `field_path`。计划 goals 与 narrative obligations 由有正文证据的结构化 `plan_fulfillment` 判定，已删除关键词子串检查。
- **删除与 schema**：`WriterContractDeltaBuilder`、`BookStateGraphDeltaExtractor`、`pulp_light` 正则兜底、旧分阶段 extraction、倒计时/羁押 future-plan audits 和 canon_quality 的 countdown / character-state ledgers 已删除。P2 的 `0004_drop_parallel_ledgers`（父为 `0003_book_state_v6`）删除 `countdown_ledgers` / `character_state_transitions`。chapter review form 继续作为 LLM 信号，其行来自 BookState，回答只产出 quality signals，不再写任何平行账本。
- **Genesis 与原子开写**：Genesis map 提供结构化 submaps / regions / nodes / edges；`forwin.map.genesis_adapter` 将其转换为 BookMap。handoff 依次物化 arcs → map → chapter-0 world_seed → arc-1 节拍表 / protection，不创建 ChapterPlan，service / seed commit 只 flush，由调用方提交；`HandoffStageError` 回滚交接写入后只保留失败事件。成功时才锁定 Genesis revision 并设为 writing。
- **护栏收口**：`tests/overfit_allowlist.toml` 的 `meta.completed_phase = "P4"`，`pending_removal` 为空；canary 的时间单位 ignore 及过时注释已删除。

- **当前揭示约定**：world_seed 的 `location_ref` 在提交时换成数据库地图节点 id；active 节拍表的 reader `reveal_targets.range` 起点优先；未覆盖的事实回退冻结 Genesis `reveal_candidates`，映射到当前 `ArcPlanVersion.chapter_start`（包括续写卷）。本章 `reveal_directives` 豁免 `premature_reveal`；无时间表时向读者推进仍记 warning。

## 2026-10 世界引擎大修 P3（2026-10-22）

状态：P3 代码收口；代码 head 与生产部署验收分开记录。

- **tick 与提交边界**：`POST_CANON_STEP_NAMES = ("world", "plan", "feedback")`。world 只运行 tick；feedback 在聚合前分析新评论。world step `result_json` 保存 `tick_id`、`graph_delta_ids`、`conflict_ids`、`dropped_candidate_ids`、`postponed_candidate_ids`、`warnings`、`skipped_reason`。
- **tick 流程**：`tick-<commit id>` 使用本章 `story_time_start..story_time_end` 整数分钟窗口和确定性 seed；world disabled、非最新章、同 tick 已提交或该章已有 tick 时跳过。`target_total_chapters > 0` 且本章达到或超过目标时以 `skipped_reason="book_complete"` 跳过。候选来自 intent interactions、clock 到期、onstage 顺延记账（按本章绑定与 band 规划机会判定）、`WorldAdjudicator` 及 `ActorTurnRunner`。候选逐个过 hard rules / `BookStateReviewGate`，LLM 候选失败重试一次，actor feedback 按自身视图脱敏；仍失败记录 `WORLD_TICK_CANDIDATE_DROPPED`。随后做 impact / budgets / narrative boundaries，需审查者进入冲突；对本章事件、待提交事件和在途消息做一轮新闻传播，再由 `WorldTickDeltaBuilder` 构建 delta。LLM 不可用跳过相关节点，不使 tick 失败。
- **Canon 准入**：world engine 只产候选和 `source_type="world_engine"` delta，不直接写四层状态。提交前 `RunningStateCheck`（`world_engine/running_state.py`）依次检查 base candidates，再按组检查 in-tick resolutions：hard rules 读取前序已接受变化留下的 runtime，world gate 检查累计 delta。base candidate 不再成立则以 `constraint_violation` 丢弃；resolution 不再成立则撤回该组、以 `failure_reason="conflict_stale"` 升级人工，冲突保持 open。`CanonAdmissionService.commit_world_tick` 以 `tick_id` 幂等，在最新 canon 章以 `FOR NO KEY UPDATE` 项目锁串行化写入。无补丁且无 resolution 的 quiet tick 不取项目锁；其他 tick 在 commit SAVEPOINT 前取锁并复核所读 world snapshot（P27），canon 变化则抛 stale、回滚 step 后重试。非 create 补丁保留 `old_value` / `field_path`；在途消息保存在 delta `metadata["news_in_transit"]`。reader 认知补丁被 gate 以 `world_engine_reader_knowledge` 拒绝。
- **历史重写**：`historical_rewrite.py` 对已登记的 world_engine delta（tick 或直接 `conflict-<id>` resolve）按 CAS 重放；失败者从 ledger 退役并产生 `historical_rewrite` / `replay_cas` 人工冲突。依赖被丢弃 delta 所创建引用的后续 tick 级联丢弃，非 tick delta 则拒绝整次重写。被丢弃 delta 承载的 resolutions 被 supersede，原冲突仍为 `resolved`；其未结义务经 `NarrativeObligationRepository.cancel_obligation` 进入终态 `cancelled`，记录 `metadata.cancel_reason`，不计入 open 列表、预算或 gate。只回撤目标为 `planned` / `failed` ChapterPlan 的计划补丁，并追加 `plan_patch_revert` 记录，accepted 等其他状态计划不变。回撤移除义务 / form instruction 写入的目标、合同条目和标记，但保留同一计划上其他仍有效 resolution 补丁共同贡献的条目（P34）；五项上限曾挤出的旧条目不能恢复。`replay_cas.proposed_change` 总含 `superseded_conflict_ids`、`cancelled_obligation_ids`、`reverted_plan_patch_ids`，有 supersede 时摘要列出三项数量。
- **冲突模型与路由**：`world_plan_conflicts` 保存 source、violated、impact、proposed change、options、required_decider、blocks_from_chapter、decision 与结果引用。historical_rewrite 或 critical 交 human；minor 自动 `veto_world`；moderate / major 在 `world.conflict_delegate="codex"` 时交 Codex，否则交 human。历史重写仅可 veto；protection / milestone / reveal_schedule 边界冲突仅可 veto / amend；其余可 veto / plot_responds / amend，minor / moderate 另可 accept_with_obligation。
- **Codex 与选项**：`WorldConflictDelegate` 默认按 `models.role_preferences["review.world_conflict"]` 使用 `gpt-6-astra`，与 Spark 门共用 `run_codex_structured_call`，审计独立事务保存；Codex 模型目录由 `FORWIN_CODEX_MODELS` 声明。任何失败或选择 `amend_plan` 都升级人工。`veto_world` 将未结意图标为 vetoed，并在 `[latest - world.max_rerolls, latest]`（两端均含）检测到的冲突中，统计该 actor 的 `veto_world` 决定次数；次数小于 `world.max_rerolls` 时安排 actor 再行动；`accept_with_obligation` 提交变化并记 `world_change_response` 义务，deadline 为 `min(latest+6, target)`（`latest` 为最新 canon 章，`target` 为 `project.target_total_chapters`）；`plot_responds` 提交变化并给下一个未接受 ChapterPlan 加回应目标；人工 `amend_plan` 提交变化并解除被破坏的 protections，随后由 PlanAmender 重规划剩余 band。直接 resolve 的 delta 经 `commit_world_tick(tick_id="conflict-<id>")`，变化、义务 / 计划副作用和关闭冲突同在一个 SAVEPOINT。
- **阻塞与恢复**：open human conflict 在进入 `blocks_from_chapter` 及以后章节前阻塞；run control 发出 `paused_for_review`，任务为 `needs_review`，派生 `BlockingReasonCode` 为 `world_conflict_pending`。任务 / 章节没有新增 reason 列。continue / review-approve（`continue_generation=true`）的 409 守卫读取该阻断；解决冲突后重新读取项目、确认无活跃任务，再显式继续，其他门仍有效。HTTP `/api/projects/{id}/world-conflicts[/{cid}[/resolve]]`、MCP `world_conflict_list` / `world_conflict_get` / `world_conflict_resolve` 与控制台 task drawer 提供处理入口；旧 CanonQualitySignal 使用 `world_model_signal_list`，保留 `/world-model/conflicts` 路径。
- **裁定上下文与分批**：完整节点正文仅含本次 due intents、其 owner / targets、它们持有的 item / resource，以及同地且 active 的 actor / item / resource / site_state；event 与非本次 due intent 不给完整正文，compact index 排除 event 和已结 intent，保留 pending intents。facts 保留全部 scoped standing facts，事件事实按 `created_at_chapter` 降序、id 升序选最新 `MAX_ADJUDICATION_EVENT_FACTS = 20` 条；允许值含 `ended_status`。每 tick 先尝试一批最多 20 个 due offstage intents；超 prompt guard 后逐 intent 调用，单个仍超限则 DROPPED `prompt_too_large`，由后续 due intent 补位；超 cap 未询问者以 `adjudication:<intent>` 进入 `postponed_candidate_ids`。
- **actor 回合上限**：视图按 priority 降序、id 升序保留最多 `MAX_ACTOR_AGENDAS = 8` 条 active agendas；每回合最多 `MAX_AGENDA_CHANGES_PER_TURN = 3` 项 agenda_changes，超限以 `too_many_agenda_changes` 反馈重试，prompt 含 `max_agenda_changes_per_actor`。每个实际回合 actor 的最新五条 `veto_world` resolutions 由 SQL 限量读取。
- **影响与 protection**：major 包括 faction / organization / group / site_state 的 `state.status` 从未结束进入 `destroyed` / `dissolved` / `disbanded` / `inactive`，faction / group 的 `leader_id` 或 site_state 的 `controller_faction_id` 变化，以及 `knows` / `believes` 将 hidden / secret / must_not_reveal 事实给予此前未持有该认知的 observer；新隐藏事实的 `known_by` 不算再次暴露。`must_exist` 也以进入上述 ended status 判定。解除 protection 设置 `payload.status = "released"`，保留 old_value / field_path；缺省 until_chapter=0 表示 open，不用它解除保护。
- **直接 resolve 与 writer 文本**：直接 resolve 紧随冲突行锁取得 `FOR NO KEY UPDATE` 项目锁，在锁内读 canon / news queue，再用相同 running-state 检查复核选项；hard-rule / gate error 抛 `ConflictStale`（HTTP/MCP 409），保持 open，veto 不受被扣留变化的失效影响。Canon 拒绝写入也返回 409。context 与 delta 的 `story_time_start` 使用最新章 extraction 起点至当前故事分钟的窗口。`plot_responds` / `accept_with_obligation` 的目标、progress marker、义务 summary / payoff_test、plan-patch contract 及 writer / reviewer injections 只取 held change 的 public event summaries；无公开事件用 `PLOT_RESPONSE_GENERIC_GOAL`，onstage_slip 用 `ONSTAGE_SLIP_RESPONSE_GOAL`，冲突摘要不进入这些写作文本。
- **P3 已完成的配套**：确定性地图物化使用 `sha256(f"{project_id}|{subworld_id}")` 种子，Genesis 地图边按声明端点、方向和 hidden 属性物化；已有 subworld 地图不会自动重建。clock producers 产到期状态 / 事件或催办 intent；form 只保护剩余时间为正的最近六个 running clocks（时间、key 排序），逾期项可裁剪。secret clock 的信号及后续 writer 约束不暴露 label / node id。移动时间共用 `estimate_travel`，消费本书 travel modes 与已知路径。

### P4a：节拍表、即时 band 规划与 POV 视图

- `arc_beat_sheets`（`plan.arc_beats`）保存 phases、milestones、protection、reveal_targets、exit_state、protagonist_ref、reader_promise 与 arc_payoff_map。取舍 **2 / 3**：固定 Genesis 卷范围和总章数，只切 band；主角必须为种子 character，体验层改读 phase purpose / milestone description，保留 ReaderPromise / ArcPayoffMap。
- protection 仅来自 beat_sheet，经 `CanonAdmissionService.commit_plan_constraints` 以节拍表 id 幂等写最新章（handoff 为 0）的 plan_constraint delta；替换时同一 delta 解除旧保护，设置 `payload.status = "released"`（R14）。历史重写将 release 的 old_value 重置为当前 status，跳过缺失或已 released 的目标；create 保留意图，节点 / fact 引用缺失则抛 `HistoricalRewriteInvalid` 拒绝整次重写（R20）。world_engine delta 单独走 CAS，失败或依赖已丢弃 tick 的创建引用时退役，产生 `replay_cas` 人工冲突并 supersede 其 resolutions。hard milestone / exit_state 引用驱动 critical；仅实际状态变化计入触及，排期键不计（R28）。
- handoff 不创建章节计划；`ArcProgressionService` 向前推进卷，`BandPlanner` 即时写版本化 `band_plans`（`plan.band`）。重规划保留 ChapterPlan id、重放有效补丁，不改 accepted/drafted/needs_review。legacy 行（band_plan_id 为空）有草稿 / candidate 历史时保留计划，从未起草的 legacy planned/failed 行在写前补规划；写前直接复用 band-owned 行，但其中 planned/failed 行即使有草稿历史，仍可被后续显式或自动重规划改写。run control 直接复用 active arc 之前的已有行，包括从未起草的 legacy 行；completed arc 不重新激活，退化卷范围无可写章节并记 warning（R27/R31/R34）。发布只覆盖调用方所见版本，旧调用不覆盖胜者（R33）。
- 可行性审计覆盖 onstage 角色死亡/受限、地点与已知路线/时间、揭示/protection、writer 泄密及 hard milestone；exit_state、未回应 public clock、未绑定 due onstage intent 是 warning。post-Canon 自动重规划有上限；仍有 error 或写前不通过产生 human plan_feasibility；band 失败不写劣质计划，写前重试仍失败产生 band_planning。两种冲突只提供 `("amend_plan",)`；PlanAmender 对 milestone/exit_state 先修订节拍表，其余携用户 rationale 强制重规划。
- 无下一计划时 plot_responds 保留待回应世界变化，accept_with_obligation 保留义务；后续 band 消费并追加 plan_patch: 结果引用。onstage 顺延按本章绑定及 band 内规划机会判定；最后 tick 才首次到期的 intent 交下一 band（R29）。
- 取舍 **14**：`ChapterWorldView` 供 writer 使用章前 POV 认知、已记录 reader 认知、writer-visible 路线、onstage restraint/持有物、clocks、允许揭示/hints 与不含真相的禁止话题；预算 standard 6000/pulp 4000 字符。只加载 onstage 性格，实体表控制命名；scene 地点输出视图 id 或 new:<名称>，抽取只作非证据提示。作者 WorldDigest/bindings 不进入 writer；goals 承载绑定行动，payoff map 按章投影，writer 文本由 `writer_text_states_secret` 等校验保护（R21/R24/R26/R36），改写表达仍依赖 prompt / gate。
- 抽取仅在正文 hash 与 plan fingerprint 同时匹配时复用（R25）；抽取输入收窄留给 P4b。取舍 **18**：独立 access rules、team speed、edge multipliers 已由出行方式取代（lexicon travel_modes / estimate_travel）。
- 顺序为 `POST_CANON_STEP_NAMES = ("world", "plan", "feedback")`，然后 order controls；plan 负责卷推进、审计/重规划与下个 band，CommentAnalyzer 在 feedback 聚合前运行。两个规划 stage 均记 PromptTrace / LLM DecisionEvents；raw-artifact 存储失败重跑 step，事务后 outbox 失败仅重试上传。详细字段、失败/并发及裁定见 [CURRENT_DESIGN 第 13 节](CURRENT_DESIGN.md)。

### P4a 删除清单

以下逐项对应 `tests/test_architecture_boundaries.py::test_p4a_deleted_modules_and_tables_stay_gone`。`0006_planning_v6` 新增 arc_beat_sheets / band_plans 及 ChapterPlan 的结构字段，并删除下列表；不恢复旧导入 alias。

删除路径：

- `forwin/simulation`
- `forwin/models/phase4.py`
- `forwin/retrieval/broker_core/helpers.py`
- `forwin/retrieval/broker_core/visibility.py`
- `forwin/planning/world_contracts.py`
- `forwin/planning/world_contract_service.py`
- `forwin/models/world_contract.py`
- `forwin/planning/stage_analysis.py`
- `forwin/planning/macro_progression.py`
- `forwin/planning/future_plan_audit/macro_progression.py`
- `forwin/book_state/macro_status.py`
- `forwin/director`
- `forwin/experience/arc_experience_planner.py`
- `forwin/genesis/handoff/chapter_materializer.py`
- `forwin/planning/arc_envelope.py`
- `forwin/planning/arc_envelope_resolver.py`
- `forwin/planning/arc_structure_service.py`
- `forwin/planning/band_window.py`
- `forwin/planning/band_plan_service.py`
- `forwin/planning/arc_activation_service.py`
- `forwin/subworld_manager.py`
- `forwin/context/gates/recency_truncate.py`
- `forwin/codex_bridge/governed_actions.py`

删除表：

- `world_simulation_turns`
- `arc_world_contracts`
- `band_world_contracts`
- `chapter_world_delta_intents`
- `project_stage_analyses`
- `project_replan_events`
- `arc_envelopes`
- `arc_envelope_analyses`
- `arc_structure_drafts`
- `provisional_promotion_records`
- `sub_world_roster_items`

### P4b 范围（尚未实现）

以[批准的 P4b 计划](../docs/superpowers/plans/2026-10-22-world-engine-p4b.md)为准：任务 0–2 补冲突前置字段、保护消息重排 / veto / 快照新鲜度等 P3 小修及 runtime 复用、快照排序、义务与路径成本；任务 3–6 做正文叙事边界检测、残留 `chapter_extraction` 冲突、章内暂停 / `rewrite_chapter` 恢复、最新章 tick 退役重跑、历史消失 ref 检查与控制台 held-change 卡片；任务 7 删除 `task_contract_json`；任务 8–10 实现 `checkable` 谓词、组织 / 地点状态枚举、`world_variable` / `world.drift` 与幕后新行动者；任务 11 清理 P2 死信号 / ledger drift、生产 `director_setup_without_delivery` 并按 ChapterWorldView 收窄抽取；任务 12–13 做多题材端到端验证与文档收尾。任务 14 单独负责生产 runbook，本文不提供部署步骤，也不宣称生产验收完成。

兼容标记仅记录旧边界：P3 当时的“writer prompt 不携带 clock / restraint state；替代物是设计 §5.1 的 `ChapterWorldView`”已在 P4a 落地。当前最新章重写仍跳过已有 tick；retained tick 引用、消息重排、世界变量、规则谓词和新行动者按上述 P4b 范围处理。
