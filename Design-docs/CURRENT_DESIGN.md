# ForWin 当前设计

更新：2026-09-04；2026-10-21 更新世界引擎大修 P2 的现状（见第 13 节）。范围：当前源码实际实现及本轮已验证的收口修改。

当前设计以已集成的 `master@0a06cfa` 为基础，包含随后按外部评审落实的修复验证、trace补传和同日调度修复。分支整合与前轮消融见[收口验证记录](../docs/operations/v5-closure-reassessment-2026-09-04.md)，本轮边界见[运行自主性修复设计](../docs/superpowers/specs/2026-09-04-v5-autonomy-fixes-design.md)，本地证据见[修复验证记录](../docs/operations/v5-autonomy-fixes-2026-09-04.md)。

## 1. 系统用途与边界

ForWin 是有持久状态的长篇小说生产系统。它把写前设定、分层计划、草稿生成、质量检查、修复、正式接纳、知识投影和平台发布连成一条流程。

系统围绕一本书和顺序章节运行。不同项目可以并行生成；同一项目不能让相邻章节抢先提交。网页、World Studio、MCP、CLI 和定时生产只是不同入口，不各自拥有一套生成逻辑。

当前没有多租户/RBAC/计费体系，也没有为了 v5 拆分数据库或重做微服务拓扑。v5 使用新 schema，不迁移旧项目、旧 task payload 和旧设置。正式版本发布是否完成与“代码已有实现”分开判断。

## 2. 一条主流程

```text
创建项目
  → Genesis 六阶段生成 / 修订 / 锁定
  → start-writing：冻结 Genesis revision，物化运行计划
  → durable generation task：数据库入队 / claim / lease / heartbeat
  → Arc → Band → Chapter 计划与上下文
  → Scene 分解 / 生成 → Stitch 最终正文 → 结构化候选
  → draft review → 有限 repair / verification
  → residual eligibility → 必要的 human / spark 委托门
  → CanonPreparation：冻结提交计划
  → CanonAdmission.commit_plan：原子接纳
  → post-Canon maintenance / durable outbox
  → 下一章所需计划准备、知识投影、记忆索引、publisher jobs
```

`project_create` 只创建 Genesis 项目。只有 `project_start_writing` 能把 Genesis 交给章节生产。继续生成前要确认不存在活跃生成任务。业务状态由支持的 MCP/API 查询，不能靠运行日志猜测，也不允许直接改数据库绕过工作流。

## 3. 事实与决策归谁所有

| 事实或操作 | 当前 owner | 副本或消费方的限制 |
| --- | --- | --- |
| 写前书本设定 | `forwin.genesis` 的 active revision | handoff 后永久冻结；运行计划修改不能回写 Genesis |
| Arc/Band/Chapter 运行计划 | 持久化计划及其版本；`PlanningService` 为门面、`PlanningQuery` 为读侧 | Genesis 物化、repair patch、post-Canon replan 仍有直接写路径，尚未完全收成一个写入口 |
| 运行行为 | 项目版本化 `RuntimePolicy`；任务保存不可变 snapshot | 环境配置只供基础设施、凭据和模型目录，不再有可变全局 runtime settings |
| 生成任务状态 | 数据库持久任务、lease、epoch、heartbeat | 进程内对象、task drawer 和日志不能覆盖数据库事实 |
| 当前待审叙述 | 最终 `WriterOutput.body` | stitch 前 scene 文本是中间产物，不再作为并列正文送给主 LLM reviewer |
| 草稿及修复历史 | `ChapterDraft`、`ChapterReview`、不可变 candidate 版本、rewrite attempts | 修复产生新候选，不能改写已接纳 Canon 来制造成功 |
| 实体身份准入 | `EntityRegistrar` 产出 `EntityAdmissionPlan`，Canon 事务落实 | 草稿期分类不得先写 Entity/EntityAlias；新名字须有完整准入决定，未解决、冲突、歧义 fail-closed |
| accepted 世界事实 | `BookState + GraphDelta + Snapshot` | Context/review/repair 通过 `BookStateQuery` 读取；投影不是另一个 Canon |
| 地图拓扑与可达性 | `BookMap / Scheme C` | SubWorld 是大陆/位面等大尺度容器，局部舞台归 Region/MapNode/site state |
| candidate → accepted | `CanonAdmissionService.commit_plan` | 人工/LLM 审批和 pipeline 都不能直接写 accepted |
| 运行时世界编辑 | generic proposal → `CanonAdmissionService.commit_world_edit` | Obsidian 不能反向导入直接写 Canon |
| 外部发布动作 | publisher job/attempt/lease/receipt + browser journal/reconciliation | 生成流程不能直接操作第三方网页；未知外部结果先核对，不盲目重复 |

“单一事实源”是每种事实有明确 owner，不是所有状态都塞进一张表。地图拓扑、计划、候选、accepted 世界、任务和外部发布回执承担不同职责；它们之间的引用、版本和提交身份必须一致。

## 4. 应用层与运行配置

`forwin.http.create_app()` 创建每个 App 自己的 `HttpRuntime`。应用服务持有用例，HTTP adapter 只做传输适配。当前实际入口包括：

- `ProjectApplicationService`：项目、Genesis、章节和 review 用例。
- `GenerationApplicationService`：生成任务入队与 worker 执行边界。
- `TaskApplicationService` / `TaskCenterService`：任务控制与任务读模型。
- `ProjectControlApplicationService`：项目控制与策略相关操作。
- `PublisherApplicationService`：publisher/extension 用例。

这与早期设计里把 Genesis/Review 各写成独立 application class 的名字不同；当前实现由 ProjectApplicationService 归口，不能把旧类名当缺失功能。

`RuntimeContainer` 是 pipeline 构造点，按运行角色构建所需服务。`ChapterPipeline` 通过显式协作者与 stage owner 组合；不存在旧 `WritingOrchestrator` 的跨模块函数赋值、动态模块代理或隐藏全局 API 状态。

RuntimePolicy v2 是冻结的类型模型，拒绝未知字段。用户维度只有质量 profile、模型 profile、章节长度和 pause/delegate 设置。任务执行使用创建时的 snapshot；后续项目设置不能悄悄改变已在跑的任务。

| 默认策略 | standard | pulp |
| --- | --- | --- |
| 章节长度 min/target/max | 2500 / 2800 / 3200 | 1800 / 2400 / 3000 |
| review signals | experience、lint、world_consistency、personality、canon_quality、publisher | lint、world_consistency、publisher |
| review.travel_tolerance | 0.2 | 0.2 |
| 常规重写预算 | 3 | 0 |
| blocking 重写预算 | 使用常规预算 | 1 |
| repair scope | local/chapter/band/arc/book/obligation | 常规scope为空，严重阻断仍有受限恢复路径 |
| BookState layers（固定启用，非策略开关） | world/map/cognition/narrative | world/map/cognition/narrative |
| Canon quality policy | strict | pulp_fatal |
| 默认 band checkpoint | pause_on_warn | continue |
| future constraints / plan health / LLM simulation | 开启 | 关闭 |
| planning recency window | 0 | 50 |
| manual checkpoints | 开启 | 关闭 |

这些是 `RuntimePolicy.for_profile()` 的默认值，不等于每本书的有效 snapshot。未交付的计划目标（`plan_goal_missing`）与 `world_consistency` 错误都是 blocking 问题：pulp 在转人工前有一次 blocking 重写，standard 沿用常规预算。抽取失败或抽取到的事实全部丢失正文证据时，hard floor 记 `chapter_delta_unavailable` 警告而不判章节失败，由 draft review 的 `chapter_delta_missing` / `chapter_delta_unverified` 走重写，预算用尽后转 needs_review。人工接受时，若 review 的错误全部是这两种（及其导致的 `repair_unfixed`），canon 会重新抽取，再由 translator、gate v6 与义务检查判定，accept 事件记录 `accepted_past_extraction_errors`；仍抽取失败则阻断。其余错误照旧拒绝。`pulp` 不是无门禁模式，实体准入、结构化 Canon、严重事实矛盾等仍有硬边界。

## 5. Genesis、计划和写作

Genesis 六阶段按 `brief → world → map → story_engine → book_blueprint → world_seed` 运行，旧 `bootstrap` / `execution_bootstrap` 已删除。用户可以在写前生成、修订、锁定；handoff 后它变成原始蓝图档案，后续写作以运行计划为准。创建项目必须显式给出 genre，没有默认题材。模型生成或修订失败时的 Genesis fallback 只返回结构壳，并把 `stage_states[stage].degraded` 置位；后续模型生成、修订或 patch 会清除该标记。`world_seed` 由 LLM 生成，经 `forwin/genesis/world_seed.py` 归一化与确定性校验，有 validation error 时拒绝锁定；交接时再次校验。其 fallback 按 story_engine id 为每个 core_cast / opposition / faction 建 actor 空壳，不编造内容。

story_engine 的实体有 id，缺省生成 `cast-n` / `opp-n` / `faction-n`；`relationship_axes` 与 `long_arcs` 为结构化数据，core_cast 的秘密使用 `secrets` 列表。开写交接顺序为 arcs → map bootstrap → `WorldSeedCommitter` → arc-1 ChapterPlan。`arc_plan_versions` 持久化 `title` / `goal` / `stakes` / `payoff_direction`；章节规划仍调用 `launch_arc_N`，留到 P4 替换。种子先通过 `BookStateReviewGate`，再经 `CanonAdmissionService.commit_world_seed` 以 `source_type="genesis_seed"` 的第 0 章 GraphDelta 写入 BookState；该入口用 `FOR NO KEY UPDATE` 锁定项目。第 0 章 subworld 角色生成路径已删除。地图 / 种子交接失败分别抛出 `HandoffStageError` 子类 `GenesisMapBootstrapError` / `WorldSeedCommitError`，并记录 `MAP_GENERATION_FAILED` / `WORLD_SEED_COMMIT_FAILED`。

续写（extend-generation）追加一个 planned `ArcPlanVersion`，由现有 arc activation 为其规划章节；Genesis blueprint 中没有该卷时，materializer 用 `ArcPlanVersion` 合成 payload。读模型把有计划卷但尚无章节计划的卷报告为可恢复。未命名的 planned 槽位不会被激活，也不再被赋予编造的名字。

PlanningService 组织 Arc/Band/Chapter 的创建、激活和修订。PlanningQuery 是读侧；PlanHealthService 的实际职责是把 future-plan audit 转成 typed health 结果，供生成控制决定是否阻断。Patch validation 由现有计划修订流程执行，没有经过一个统一健康聚合器。FuturePlanAuditor 可以对未来计划提出并应用合法修正，不能改已接纳章节来抹平历史。

Writer 目前保留 Scene 分解、场景生成和 stitch，用于组织长文本和场景目标。最终 body 是审查的正文依据，scene 原稿和摘要是写作产物，不能让 reviewer 混读新旧稿。结构化事实由 writer 之后的单一 `chapter_delta_extraction` stage 从最终 body 抽取，供 checkers、`world_consistency` 和 canon preparation 共用。

主 LLM review 和 repair escalation 使用完整最终 body，场景原始产物仍作为写作档案保留。核验使用共享章节 delta 与 Canon invariants；`world_consistency` 通过 `ChapterDeltaTranslator` 与 `BookStateReviewGate` v6 检查抽取事实。

上下文由 Genesis、当前运行计划、BookState、BookMap、accepted 摘要、检索投影、人物技能及项目规则组装。Skill Runtime 是指令层，可影响 prompt 并留下 trace，但不拥有 Canon 写权限。

## 6. Review、修复和接纳资格

DraftReviewService 聚合不同职责的信号：文本/体验、计划契约、`world_consistency`（共享 translator + gate v6）、人物行为、lint、Canon quality、publisher 约束。多个检查器共存的合理性来自职责不同；“都能发现某种连续性问题”仍可能产生重复成本，是下一轮消融要检验的部分。

界面/API 固定展示五层，不能把其中一层的成功当另一层成功：

1. draft_review：草稿问题与 verdict。
2. repair：修复范围、尝试、预算和 verification。
3. residual_eligibility：修复耗尽后是否仍有资格继续。
4. gate_delegation：本应交给人的合格候选暂停门，由 human 或 spark 处理。
5. canon：真正已接纳的 candidate/commit 身份。

RepairService 选择 scope，必要时修改未来计划，再重写、重新 review、验证 must_fix / must_preserve 和新风险。预算在当前 candidate/repair cycle 内计算。警告不自动变成必须修复；内容错误、合同错误与基础设施失败不能混为同一“重试”。

标题属于修复合同里的可保留元数据：若合同精确保护当前标题，且本次计划没有显式改名，重写者应保留该标题。显式改名与旧 must_preserve 冲突时仍拒绝；本轮不靠删除 verifier 约束获得通过。

RepairVerifier 对全部 must_fix、must_preserve、must_not_reveal 条件分别记录 pass / fail / unknown，并提供理由、判断方法和原稿/修复稿引用。规则只判断自己实际覆盖的条件；标题没变不等于人物认知等语义条件仍成立。语义验证使用完整原稿和最终正文，引用校验来源、逐字内容和字符偏移。有证据的语义反对最多交给同一verifier复核一次；超时不额外重试，不为补证据调用writer。

完整正文和合同超过输入预算、输出截断、缺引用或复核分歧时，相关条件保持unknown。既有两个聚合字段使用true / false / null表示已验证通过 / 证实失败 / 未验证；API、修复记录和UI保留区别。unknown不生成新的质量错误或人工门，仍由主review、hard residual与Canon约束决定能否接纳。因此“候选已接纳”不意味着每条语义修复条件都已验证通过。

FinalResidualPolicy 只判资格，不提交 Canon。fail/error 或不可接纳 candidate 不能交给 Spark 放行。Spark 只有写 trace/event 的窄能力，不能修改 eligibility 或直接调用 Canon 写入口。JSON/schema/model/timeout 等失败按现有契约关闭该委托路径。

## 7. Canon 原子事务

事务外的 CanonPreparationService 收集 review/eligibility、质量分析、实体计划及 BookState extraction 结果，冻结 CanonCommitPlan。共享 QualityAnalysisRunRow 用于避免 draft review 与 Canon 准备重复执行同一候选的 primary quality 分析。

事务内的 commit_plan 锁定项目、章节和 candidate，复核版本、正文/计划指纹、实体别名和 worker lease 等前提，再原子提交：

- BookStateCompiler 的 GraphDelta、状态快照与相关事实。
- Entity/EntityAlias 身份与叙事义务更新。
- candidate/章节 accepted 状态与 CanonCommitRecord。
- 同一提交身份派生的确定性 outbox 事件。

中间失败应整体回滚；事务后的 worker 崩溃不能产生第二次 Canon 接纳。stale candidate、旧 lease owner、重复提交和重复 outbox 必须被拒绝或幂等处理。旧 StateUpdater accepted-state 双写已删除。

## 8. Canon 后的维护、投影和恢复

post-Canon maintenance 按 planning → arc → world → feedback 顺序执行，分别更新计划、下一章 Arc resolution、世界压力和反馈。这些结果被下一章的 planning/personality/context 消费。四步全部成功后，还需完成 order controls，包括 obligation 验证、future-plan audit、generation-audit report 和 band checkpoint。第2章及以后提交 Canon 都强制检查前章这一屏障；未解除的 future-contract、checkpoint 或人工阻断会使提交等待恢复。pulp 的 continue 只放过 checkpoint warn，fail 仍阻断。

四步维护及order_controls的trace已与对象存储上传分开：业务结果、成功状态和冻结脱敏trace的OutboxEvent同事务提交，上传在事务后由现有worker处理。存储失败重试上传，不重跑已成功的维护模型。对象key含内容SHA，旧trace延迟补传不会覆盖后来重跑order_controls的trace；上传状态归outbox，维护结果保留引用。原始payload保留在outbox中，尚未增加自动清理策略。

Generation Audit 已 report-only；FuturePlanAudit 和 band checkpoint 仍可阻断，不能混称为同一个审计。

这一边界保障顺序一致性，但增加延迟和实现复杂度。不能仅因模块名含 projection 就整体删除：必须先证明下游消费者能接受落后数据，并按步骤做消融。Canon 已接纳的本章不因投影故障回滚。

Knowledge Projection、Obsidian export、LLM KB、chapter memory 和 World Studio 读视图从 Canon 派生，失败通过 outbox/checkpoint/replay 恢复。Qdrant 是检索索引，MinIO 是产物存储，都不是独立 Canon。Obsidian 的人工 section 进入单独 human index；正式事实编辑仍走 generic proposal。

## 9. Publisher 与自动生产

Publisher 与生成是独立运行角色。Canon 后物化具有确定身份的发布任务；worker/browser 使用 attempt token、lease fencing、receipt、浏览器持久 journal 和只读 reconciliation 处理崩溃后的未知结果。

防重的目标是同一 Canon/job 不产生重复外部效果；不能把数据库事务能力推导成第三方网页的全局 exactly-once 保证。上传后回执丢失等情况要核实外部状态。CAPTCHA/MFA/账号风险进入明确暂停，必须经受支持的人工恢复，不能自动绕过验证。

Daily automation 继续保留，通过应用层入队同一 durable generation task。它没有第二条直接执行 pipeline 的通道。Server-rendered 控制台、World Studio SPA、browser extension 三个界面/客户端仍共存，各自对应运营、世界/章节工作区和第三方发布。

每日调度保留“每天一次成功批次”。last_scheduler_at记录检查，last_scheduler_date记录成功预留；尚未完成工作时，active task、waiting review和idle不会耗掉当天机会，解除后可同日补调度。项目级数据库调度锁避免并发tick；review回调先在没有Project行锁或写入的区间执行，再重新读取设置和backlog。任务入队、Canon发布job释放和日槽共同提交，重启或任务终态不会重新获得同日批次额度。这尚不是全天按各类剩余额度滚动补量的控制器，也不是平台确认发布的完成记录。

成功的同步review也消耗该日批次；其后人工批准或新增发布job不承诺触发当天第二批。review独立事务后的恢复依赖持久章节状态，还没有跨崩溃累计review额度账本。并发自动化设置在Project锁后读取，保留已提交日槽。

## 10. 度量与当前保留的审计

DecisionEvent 记录决策、阶段、失败和人工动作；PromptTrace/原始产物说明模型调用及其输入输出；性能记录说明耗时。B0 的三个只读账本已实现：

- S1 GateLedger：机会数、触发/阻断/覆盖，无法恢复 denominator 时为 unknown；后续事故只称 incident proxy，不伪称真实误报/漏报。
- S3 CostLedger：provider usage、Codex usage、估算或缺失分别记录；人工动作是次数，没有显式 duration 不推测人时。
- S2 RuleProvenance：运行规则 project-scoped，observing/active/suspended/retired 需显式生命周期；跨书数据只能建议全局提升，不能自动生成全局运行规则。

Generation Audit 每6个 accepted DB chapter 写一次 report-only 摘要，不 pause/delegate/block。它引用已发生的 FuturePlanAudit 结果，不是第二次修改计划的决定器。R27只读证据有13个 checkpoint，均未产生 gate 阻断。

S4-S8 的读者留存、多样性、认知/张力及完整体验度量仍是未来产品方向，不作为当前缺失实现来补建。

## 11. 已删除的设计

下列内容不能再作为现行方案引用：v4 fixture 驱动的 BookState 抽取链路（delta extractor、确定性 gate、reviewers、`BookStateDeltaAdapter`、`forwin.protocol.world_v4`、`WriterOutput` 的 v4 自报字段、`RepairScope.world_model`）、world contracts 中的“母星被围”样例、Genesis fallback 与 writer prompt 里的样例剧本、五张死表（见 `0002_drop_dead_tables`）、多种 operation/checkpoint/copilot/reckless 模式、premium 空profile、RuntimeSettingsStore、governance_json、request-level policy override、旧review cutover flags、WritingOrchestrator动态拼装、旧world_model facade、legacy accepted-state双写、独立Scenario rehearsal、Provisional Band Preview、Obsidian reverse import、旧API/private alias、旧迁移链和手写schema升级器。

保留的普通 writer preview fallback 与 Arc sizing `provisional_window` 不是已经删除的第二套 Provisional writer；保留的 `BookStateReviewGate`（GraphDelta 入 canon 前的确定性护栏）不是旧主 reviewer；两者不能仅凭旧名字相似误删。

## 12. 复杂度与消融重点

在本轮修复前后的小幅变化范围内，代码规模约为：生产 Python 582文件/13.9万行，主测试约8.4万行，release harness约3.16万行实现及2.89万行测试。物理行数包含schema、空行和注释；它衡量维护表面积，不直接证明无用或运行开销。

从旧 master 到领先候选，生产 Python 相关树约增2.35万、删1.18万行；release harness新增约6.3万行（含Markdown和测试）。架构owner减少与代码总量增加同时发生，主要新增来自恢复/发布和验证工程。

当前优先做可本地验证的消融，而非再建复杂框架：

| 对象 | 可证伪的问题 | 保留/删除依据 |
| --- | --- | --- |
| 拼接前scene正文送审 | 是否带入最终正文不存在的冲突？ | 已用真实R27产物及fixture证实；删除重复输入，真实最终正文error仍能阻断 |
| repair标题重新生成 | 是否与must_preserve合同冲突且无收益？ | 已复现，修复owner保留受保护标题；不削弱verifier |
| 无调用PlanHealth聚合接口 | 是否只有测试在制造存在理由？ | 已删除 from_patch_validation / combine 和专用测试，净删47行；from_future_audit 的真实阻断调用保留 |
| RepairVerifier头尾摘要与总体布尔判断 | 是否遗漏正文中段和未覆盖语义合同？ | 改为完整正文、逐合同证据及三态覆盖；无证据/超时不伪称通过 |
| Generation Audit摘要 | 是否提供S1/任务查询没有的运营信息？ | 目前低成本report-only；删除会损失cadence摘要，先做对照，不恢复成门 |
| post-Canon四步barrier | 哪些步骤确实必须阻塞下一章？ | 25种内存输入对照确认当前硬依赖；保留业务四步，trace改为持久outbox补传，故障不重跑已成功业务 |
| release harness验真层 | 哪些规则防真实错误，哪些只是反复证明脚本自身？ | 保留来源/镜像/状态/回执真实性；停止新增外部签名等前置门，后续按实验裁剪 |

本轮不运行几十或几百章真实测试。消融结果与本地全量测试用于决定本轮可推送的代码；历史全新L200、恢复演练和正式部署门仍在发布设计中，但不伪称已经完成，也不作为本任务继续长跑的理由。

## 13. 世界引擎大修 P2 之后（2026-10-21）

设计见[世界引擎大修](../docs/designs/2026-10-20-world-engine-overhaul.md)。P0–P2 已落地；Canon 写入统一经过 `CanonAdmissionService`。

- **单次抽取与翻译**：每个候选正文由 `ChapterDeltaExtractor` 在 `chapter_delta_extraction` stage 完成一次统一抽取（校验失败可重试；引用了输入表之外的 ref 时，按 translator 的规则逐条指出并重试一次）；`ensure_chapter_delta` 按 `body_sha256` 复用结果，正文改变才重抽。输入包括 BookState as-of N−1 的最小实体表、地点表、计划目标 / 义务 / reward targets、intents、secrets、calendar units、time aliases 和 travel modes，不是 ChapterWorldView。`ChapterDeltaTranslator` 将 `WriterOutput.chapter_delta` 翻译为 `source_type="chapter_extraction"` 的 GraphDelta，再经 `BookStateReviewGate` v6；draft review 和 canon preparation 共用这份抽取，最终仍由 `CanonAdmissionService.commit_plan` 原子提交。canon preparation 遇到缺失或过期的抽取时，先核验实体准入，再由 BookState preparation 重抽，之后才跑 canon quality gate，到期义务按新抽取判定。抽取到的事实全部丢失正文证据时，canon 以 `chapter-delta-unverified-blocked` 阻断；没有任何条目被丢弃的空抽取仍提交无 patch 的 delta。
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

## 14. 文档入口

- 本文：现有设计的完整叙述。
- [世界引擎大修设计](../docs/designs/2026-10-20-world-engine-overhaul.md)：P0–P2 已落地，P3/P4 为目标设计；推迟事项以第 13 节为准。
- [CURRENT_ARCHITECTURE](CURRENT_ARCHITECTURE.md)：精简的架构与代码边界。
- [DESIGN_STATUS](DESIGN_STATUS.md)：旧文档权威性与当前状态。
- [前轮收口修订](../docs/superpowers/specs/2026-09-04-v5-closure-design.md)：架构收口与发布验收顺序。
- [前轮验证记录](../docs/operations/v5-closure-reassessment-2026-09-04.md)：历史分支整合、测试、消融和未完成发布证明。
- [本轮修复设计](../docs/superpowers/specs/2026-09-04-v5-autonomy-fixes-design.md)及[验证记录](../docs/operations/v5-autonomy-fixes-2026-09-04.md)：外部评审三个问题的实现、证据与边界。

发生冲突时先核对实际源码与通过的契约测试，再更新本文和精简架构入口。旧计划上的 Approved/checkbox 不足以推翻当前 owner，也不足以证明功能通过真实运行验证。
