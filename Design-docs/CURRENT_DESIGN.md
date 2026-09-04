# ForWin 当前设计

更新：2026-09-04。范围：当前源码实际实现及本轮已验证的收口修改。

代码基线从 `codex/v5-r9-integration-candidate@fdaeaa6` 延续到本轮收口修改，集成目标为远端默认分支 `master`。初次核对时 master 落后147个提交，因此本轮以领先候选复评。测试与发布状态见 [本轮验证记录](../docs/operations/v5-closure-reassessment-2026-09-04.md)。

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
| review signals | experience、lint、map movement、personality、canon quality、publisher | lint、publisher |
| 常规重写预算 | 3 | 0 |
| blocking 重写预算 | 使用常规预算 | 1 |
| repair scope | local/chapter/band/arc/book/obligation | 常规scope为空，严重阻断仍有受限恢复路径 |
| BookState layers | world/map/cognition/narrative | world |
| Canon quality policy | strict | pulp_fatal |
| 默认 band checkpoint | pause_on_warn | continue |
| future constraints / plan health / LLM simulation | 开启 | 关闭 |
| planning recency window | 0 | 50 |
| manual checkpoints | 开启 | 关闭 |

这些是 `RuntimePolicy.for_profile()` 的默认值，不等于每本书的有效 snapshot。`pulp` 不是无门禁模式，实体准入、结构化 Canon、严重事实矛盾等仍有硬边界。

## 5. Genesis、计划和写作

Genesis 包含 brief、world、map、story_engine、book_blueprint、bootstrap 六阶段。用户可以在写前生成、修订、锁定；handoff 后它变成原始蓝图档案，后续写作以运行计划为准。

PlanningService 组织 Arc/Band/Chapter 的创建、激活和修订。PlanningQuery 是读侧；PlanHealthService 的实际职责是把 future-plan audit 转成 typed health 结果，供生成控制决定是否阻断。Patch validation 由现有计划修订流程执行，没有经过一个统一健康聚合器。FuturePlanAuditor 可以对未来计划提出并应用合法修正，不能改已接纳章节来抹平历史。

Writer 目前保留 Scene 分解、场景生成、stitch 和结构化抽取。这能组织长文本和场景目标，但引入了多个叙述表示。最终 body、scene 原稿、摘要、事件和时间候选不一定天然一致，必须明示优先级，不能让 reviewer 混读新旧稿。

本轮已删除主 LLM review 和 repair escalation 的旧 scene 正文输入，改为完整最终 body；结构化状态/事件/时间和 Canon invariants 仍用于核验。场景原始产物没有被销毁，地图检查仍可使用位置等结构化数据。

上下文由 Genesis、当前运行计划、BookState、BookMap、accepted 摘要、检索投影、人物技能及项目规则组装。Skill Runtime 是指令层，可影响 prompt 并留下 trace，但不拥有 Canon 写权限。

## 6. Review、修复和接纳资格

DraftReviewService 聚合不同职责的信号：文本/体验、计划契约、地图移动、人物行为、lint、Canon quality、publisher 约束。多个检查器共存的合理性来自职责不同；“都能发现某种连续性问题”仍可能产生重复成本，是下一轮消融要检验的部分。

界面/API 固定展示五层，不能把其中一层的成功当另一层成功：

1. draft_review：草稿问题与 verdict。
2. repair：修复范围、尝试、预算和 verification。
3. residual_eligibility：修复耗尽后是否仍有资格继续。
4. gate_delegation：本应交给人的合格候选暂停门，由 human 或 spark 处理。
5. canon：真正已接纳的 candidate/commit 身份。

RepairService 选择 scope，必要时修改未来计划，再重写、重新 review、验证 must_fix / must_preserve 和新风险。预算在当前 candidate/repair cycle 内计算。警告不自动变成必须修复；内容错误、合同错误与基础设施失败不能混为同一“重试”。

标题属于修复合同里的可保留元数据：若合同精确保护当前标题，且本次计划没有显式改名，重写者应保留该标题。显式改名与旧 must_preserve 冲突时仍拒绝；本轮不靠删除 verifier 约束获得通过。

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

当前 trace 产物上传也位于维护 step 成功标记之前，上传失败可能让业务步骤回滚并阻塞下一章。这是仍存在的诊断与业务耦合，尚未在本轮删除。Generation Audit 已 report-only；FuturePlanAudit 和 band checkpoint 仍可阻断，不能混称为同一个审计。

这一边界保障顺序一致性，但增加延迟和实现复杂度。不能仅因模块名含 projection 就整体删除：必须先证明下游消费者能接受落后数据，并按步骤做消融。Canon 已接纳的本章不因投影故障回滚。

Knowledge Projection、Obsidian export、LLM KB、chapter memory 和 World Studio 读视图从 Canon 派生，失败通过 outbox/checkpoint/replay 恢复。Qdrant 是检索索引，MinIO 是产物存储，都不是独立 Canon。Obsidian 的人工 section 进入单独 human index；正式事实编辑仍走 generic proposal。

## 9. Publisher 与自动生产

Publisher 与生成是独立运行角色。Canon 后物化具有确定身份的发布任务；worker/browser 使用 attempt token、lease fencing、receipt、浏览器持久 journal 和只读 reconciliation 处理崩溃后的未知结果。

防重的目标是同一 Canon/job 不产生重复外部效果；不能把数据库事务能力推导成第三方网页的全局 exactly-once 保证。上传后回执丢失等情况要核实外部状态。CAPTCHA/MFA/账号风险进入明确暂停，必须经受支持的人工恢复，不能自动绕过验证。

Daily automation 继续保留，通过应用层入队同一 durable generation task。它没有第二条直接执行 pipeline 的通道。Server-rendered 控制台、World Studio SPA、browser extension 三个界面/客户端仍共存，各自对应运营、世界/章节工作区和第三方发布。

## 10. 度量与当前保留的审计

DecisionEvent 记录决策、阶段、失败和人工动作；PromptTrace/原始产物说明模型调用及其输入输出；性能记录说明耗时。B0 的三个只读账本已实现：

- S1 GateLedger：机会数、触发/阻断/覆盖，无法恢复 denominator 时为 unknown；后续事故只称 incident proxy，不伪称真实误报/漏报。
- S3 CostLedger：provider usage、Codex usage、估算或缺失分别记录；人工动作是次数，没有显式 duration 不推测人时。
- S2 RuleProvenance：运行规则 project-scoped，observing/active/suspended/retired 需显式生命周期；跨书数据只能建议全局提升，不能自动生成全局运行规则。

Generation Audit 每6个 accepted DB chapter 写一次 report-only 摘要，不 pause/delegate/block。它引用已发生的 FuturePlanAudit 结果，不是第二次修改计划的决定器。R27只读证据有13个 checkpoint，均未产生 gate 阻断。

S4-S8 的读者留存、多样性、认知/张力及完整体验度量仍是未来产品方向，不作为当前缺失实现来补建。

## 11. 已删除的设计

下列内容不能再作为现行方案引用：多种 operation/checkpoint/copilot/reckless 模式、premium 空profile、RuntimeSettingsStore、governance_json、request-level policy override、旧review cutover flags、WritingOrchestrator动态拼装、旧world_model facade、legacy accepted-state双写、独立Scenario rehearsal、Provisional Band Preview、Obsidian reverse import、旧API/private alias、旧迁移链和手写schema升级器。

保留的普通 writer preview fallback 与 Arc sizing `provisional_window` 不是已经删除的第二套 Provisional writer；保留的 BookState extraction gate 不是旧主 reviewer；两者不能仅凭旧名字相似误删。

## 12. 复杂度与消融重点

在本轮修复前后的小幅变化范围内，代码规模约为：生产 Python 582文件/13.9万行，主测试约8.4万行，release harness约3.16万行实现及2.89万行测试。物理行数包含schema、空行和注释；它衡量维护表面积，不直接证明无用或运行开销。

从旧 master 到领先候选，生产 Python 相关树约增2.35万、删1.18万行；release harness新增约6.3万行（含Markdown和测试）。架构owner减少与代码总量增加同时发生，主要新增来自恢复/发布和验证工程。

当前优先做可本地验证的消融，而非再建复杂框架：

| 对象 | 可证伪的问题 | 保留/删除依据 |
| --- | --- | --- |
| 拼接前scene正文送审 | 是否带入最终正文不存在的冲突？ | 已用真实R27产物及fixture证实；删除重复输入，真实最终正文error仍能阻断 |
| repair标题重新生成 | 是否与must_preserve合同冲突且无收益？ | 已复现，修复owner保留受保护标题；不削弱verifier |
| 无调用PlanHealth聚合接口 | 是否只有测试在制造存在理由？ | 已删除 from_patch_validation / combine 和专用测试，净删47行；from_future_audit 的真实阻断调用保留 |
| Generation Audit摘要 | 是否提供S1/任务查询没有的运营信息？ | 目前低成本report-only；删除会损失cadence摘要，先做对照，不恢复成门 |
| post-Canon四步barrier | 哪些步骤确实必须阻塞下一章？ | 25种内存输入对照确认当前硬依赖；实际planning/personality消费者存在，保留四步；trace上传耦合尚待故障注入验证 |
| release harness验真层 | 哪些规则防真实错误，哪些只是反复证明脚本自身？ | 保留来源/镜像/状态/回执真实性；停止新增外部签名等前置门，后续按实验裁剪 |

本轮不运行几十或几百章真实测试。消融结果与本地全量测试用于决定本轮可推送的代码；历史全新L200、恢复演练和正式部署门仍在发布设计中，但不伪称已经完成，也不作为本任务继续长跑的理由。

## 13. 文档入口

- 本文：现有设计的完整叙述。
- [CURRENT_ARCHITECTURE](CURRENT_ARCHITECTURE.md)：精简的架构与代码边界。
- [DESIGN_STATUS](DESIGN_STATUS.md)：旧文档权威性与当前状态。
- [收口修订](../docs/superpowers/specs/2026-09-04-v5-closure-design.md)：本次范围和具体设计调整。
- [验证记录](../docs/operations/v5-closure-reassessment-2026-09-04.md)：分支、测试、消融、未完成发布证明。

发生冲突时先核对实际源码与通过的契约测试，再更新本文和精简架构入口。旧计划上的 Approved/checkbox 不足以推翻当前 owner，也不足以证明功能通过真实运行验证。
