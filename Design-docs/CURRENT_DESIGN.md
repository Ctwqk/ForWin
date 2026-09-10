# ForWin 当前设计

> 当前路线图：[三阶段改进设计](../docs/superpowers/specs/2026-09-09-forwin-three-stage-design.md)。本页描述开发分支当前实现，生产部署和真实长跑结果另见[实施记录](../docs/operations/three-stage-implementation-2026-09-09.md)。旧 L200 与历史矩阵由本轮 smoke + 全新离线 L100 取代。

更新：2026-09-10。范围：`codex/three-stage-improvements` 的版本身份、发布冻结、完整后缀修订、5% 存稿、职责重构、合格反馈链路、地图约束与 Genesis 来源事实传递修复。最终全量回归、角色镜像、长跑及生产切换状态以执行计划为准。

源码起点是 `master@521228871a5752ebe8572c057caa9f4944bb0295`。前轮[收口验证记录](../docs/operations/v5-closure-reassessment-2026-09-04.md)和[自主性修复记录](../docs/operations/v5-autonomy-fixes-2026-09-04.md)只解释历史依据；本轮工作包、独立评审和未完成项见[执行计划](../docs/superpowers/plans/2026-09-09-forwin-three-stage.md)。

## 1. 系统用途与边界

ForWin 是有持久状态的长篇小说生产系统。它把写前设定、分层计划、草稿生成、质量检查、修复、正式接纳、知识投影和平台发布连成一条流程。

系统围绕一本书和顺序章节运行。不同项目可以并行生成；同一项目不能让相邻章节抢先提交。网页、World Studio、MCP、CLI 和定时生产只是不同入口，不各自拥有一套生成逻辑。

当前没有多租户/RBAC/计费体系，也没有为了本轮拆分数据库或重做微服务拓扑。现存数据库必须先备份、在隔离副本验证，再使用 Alembic 向前迁移；不重写已部署 baseline，不丢弃旧项目或历史引用。应用启动只检查 schema，不偷偷升级。

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
| 当前章节版本 | 稳定 `ChapterPlan.id`、`active_commit_id`、逐章 `acceptance_revision` 和 `Project.book_revision` | 历史提交号、正文身份与增量保留；不以负章节号归档旧版本 |
| 实体身份准入 | `EntityRegistrar` 产出 `EntityAdmissionPlan`，Canon 事务落实 | 草稿期分类不得先写 Entity/EntityAlias；新名字须有完整准入决定，未解决、冲突、歧义 fail-closed |
| accepted 世界事实 | `BookState + GraphDelta + Snapshot` | Context/review/repair 通过 `BookStateQuery` 读取；投影不是另一个 Canon |
| 地图拓扑与可达性 | `BookMap / Scheme C` | SubWorld 是大陆/位面等大尺度容器，局部舞台归 Region/MapNode/site state |
| candidate → accepted | `CanonAdmissionService.commit_plan` | 人工/LLM 审批和 pipeline 都不能直接写 accepted |
| 写前世界编辑 | generic proposal → `CanonAdmissionService.commit_world_edit` | 仅限尚无 accepted/active 章节的项目；已有正式历史须走章节修订及完整后缀核验，Obsidian 不能反向导入 |
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

`RuntimeContainer` 是 pipeline 构造点，按运行角色构建所需服务。`ChapterPipeline` 保留顺序协调和任务控制；`WriterExecution`、`CandidateReviewService`、`RepairPlanPatchService` 与 `CanonPreparationService` 接收有限请求并承担具体职责。真实调用方已替换，旧 Writer/Review/Repair Stage 路径已删除；没有以完整 Pipeline 或万能回调袋作为新 owner 的依赖。

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

Genesis 包含 brief、world、map、story_engine、book_blueprint、bootstrap 六阶段。用户可以在写前生成、修订、锁定；handoff 后它变成原始蓝图档案。后续情节安排以运行计划为准，已确定的历史与根规则仍保留来源约束，未来目标不被当成已发生事实。

PlanningService 组织 Arc/Band/Chapter 的创建、激活和修订。PlanningQuery 是读侧；PlanHealthService 的实际职责是把 future-plan audit 转成 typed health 结果，供生成控制决定是否阻断。Patch validation 由现有计划修订流程执行，没有经过一个统一健康聚合器。FuturePlanAuditor 可以对未来计划提出并应用合法修正，不能改已接纳章节来抹平历史。

Writer 目前保留 Scene 分解、场景生成、stitch 和结构化抽取。这能组织长文本和场景目标，但引入了多个叙述表示。最终 body、scene 原稿、摘要、事件和时间候选不一定天然一致，必须明示优先级，不能让 reviewer 混读新旧稿。

本轮已删除主 LLM review 和 repair escalation 的旧 scene 正文输入，改为完整最终 body；结构化状态/事件/时间和 Canon invariants 仍用于核验。场景原始产物没有被销毁，地图检查仍可使用位置等结构化数据。

上下文由 Genesis、当前运行计划、BookState、BookMap、accepted 摘要、检索投影、人物技能及项目规则组装。Skill Runtime 是指令层，可影响 prompt 并留下 trace，但不拥有 Canon 写权限。

`knowledge_system_context` 是不进入 Writer prompt 的溯源副本，保留在 context 中，但不参与 Writer 软预算，避免挤掉实际可见的摘要和记忆。其余预算、裁剪优先级及超限语义保持；这仍是字符估算，不是精确 token 限额。主 BODY reviewer 接收已经保留的完整前章摘要，证据 `history:summary:N` 绑定本次输入序号，不推断绝对章号。摘要未记载的细节保持未知。

五种 Writer 提示共用世界页渲染，读取可见页面的 `Canon Summary`；具备实体来源的单个世界/地图页还读取 `Current State`，共享每页 220 字内容限额。书籍、overview 等聚合页的状态可能混有隐藏信息，只保留概要；frontmatter、人工笔记或待批准修订不当作 Canon。页面属性与内嵌 frontmatter 任一标记为隐藏时都不展开，秘密页仍不开放；可见性及真假关系随内容呈现，读者可见不等于所有角色知情。

WorldNode/MapNode 的隐藏状态判断由一个 BookState owner 供检索、导出和读侧共用。导出不丢弃源节点的隐藏 status/tags；知识上下文用请求时点的 Canon 复核旧页面显隐，缺失的实际实体来源保持隐藏，书籍合成 ID 与地图 ID 分别处理。未来页面在排序和限额前排除；旧投影已经被覆盖而没有历史页时保持缺失，不把当前状态当成历史状态。只调整上下文副本，已存页面、正文和 hash 不改写，主 reviewer 仍取得完整保留页面。上述接线不能补回摘要、状态抽取或现有 memory 前 500 字之外未保留的事实，也不能证明后续计划和真实正文一致；见[已接纳连续性失败记录](../docs/superpowers/reports/2026-09-10-accepted-continuity-failure.md)。

`GenesisContextProvider` 从冻结 revision 提供一份只读来源事实视图：`world_bible.history_slice`、`axioms` 和具名 `core_cast.secret`，保留完整原文、类别、人物和字段路径。相同视图经过 Chapter/Review context 进入各写作模式及主 BODY reviewer，审查证据引用绑定 Genesis revision 与原字段。视图最多 64 条、序列化条目合计 12000 字符，超限整条省略并显式记录遗漏数，不把截断句当完整事实；章计划提及的人物优先于其余 cast。实际 RetrievalBroker 再按调用者总预算的四分之一裁剪整条来源并累计遗漏；总预算仍紧张时，章节与当前人物均未涉及的秘密先于当前 Canon 上下文撤掉。完整 Genesis、文化词库和未来场景模板不随之灌入。

这些信息不直接创建 Canon、人物知情或揭示许可：`character_secret` 是作者背景，仍遵守当前知情状态与 `must_not_reveal`。当前状态以已接纳 BookState 为准；同一历史事件不能被新计划静默改写。主审查同时收到现有的禁止揭示、揭示阶梯、人物认知、观察者可见状态和允许误导线索，并以完整最终正文核对来源，区分客观陈述、角色谎言/猜测与不同事件，不通过年份关键词判错。缺失来源表示证据不完整。[历史事实漏传失败报告](../docs/superpowers/reports/2026-09-10-stage1-genesis-reference-failure.md)区分输入传递验证与真实生成验收。

Genesis 提供明确地点路线时，handoff 在现有 BookMap owner 内保留原始端点、方向、独立平行路线、显隐状态、发现状态和通行规则。行程的明确时长转为小时；登记、排队和许可等条件保留原文，无法解析的耗时保持未知。只有没有明确地点路线的输入才使用程序化地图生成；不因“门禁”字样生成传送门，也不为通过连通性检查补造路线。全部跨区路线落库后在同一 savepoint 内核验结构弱连通性，失败完整回滚；实际寻路仍按单向/双向限制。旧作品地图不被自动重建。

路线原文的 `control/access` 与 `hazard/risk` 在同一导入入口归一化到现有来源 metadata；不同字段同时出现时保留全部不同原文，不用后者覆盖前者。Genesis 预览复用该归一化，Writer 展示通行条件和风险说明，主审查沿现有地图 metadata 读取。自然语言条件不转成权限 ID 或风险分数，不代表人物已经获得许可或风险事件已经发生；隐藏路线仍遵守原可见性边界。

Genesis 路线使用同一 typed contract：新完整 Map 必须明确提供 `overview / topology_rules / submaps / regions / nodes / edges` 六字段，每条路线显式声明端点、耗时原文 `duration_text`、交通方式 `mode`、条件 `conditions`、风险 `risks` 及方向/显隐/通行规则引用。旧 `travel_time` 数值按小时解释，带单位字符串与有限旧别名由唯一 parser 处理；原始字典和字段路径随来源 metadata 保留。费用文案不冒充耗时，范围/约数保持未知；多个耗时来源冲突或不可比较、未知字段及错误类型明确报出路径，不能静默替换成程序化路线。生成、完整/定向修订、patch、Map 锁定和导入均执行相关校验；失败保留原 revision。旧 revision 可读取，预览对无法解析的可见路线显示信息不完整，隐藏路线仍过滤。SubWorld 级显式路线也保留独立身份、类型和显隐状态；程序化默认连接与现存已部署地图不因此重建。[路线合同修复记录](../docs/superpowers/reports/2026-09-10-genesis-route-contract.md)区分输入传递验证与真实生成验收。

完整 Map 的模型 schema 描述六字段及既有层级结构；新生成在默认值补齐前验证字段、ID、端点和 SubWorld/Region/Node 归属，完整修订保留有限旧路线输入形式。定向修订和 patch 检查合并后的地图；Map 锁定、归一化和导入共用引用校验。子 Region 可以先于父 Region 出现，ID 优先于重名地点；无效引用、重复 ID、错误容器不能通过补默认地理或改换父级变成成功。初始 World 和手工输入仍可补齐省略的稳定 ID，明确空集合保持为空，历史读取不新增硬门。[完整地图修复记录](../docs/superpowers/reports/2026-09-10-genesis-map-completeness.md)保留写前失败证据与验收边界。

Writer 获取当前可见 BookMap 路线和写前来源约束，即使人物位置未解析或地图被裁剪也保留已有路线，并标明不完整。主 BODY reviewer 获取当前地图及来源证据，以当前 BookMap 优先；客观隐藏路线不等于角色已知。确定性移动检查识别运行地点 ID、唯一 Genesis 来源 ID/名称，以及明确的中文/数字复合时长；复合自由文本位置不猜测映射。它仍不能证明最终正文的每一段移动都正确，真实长跑须独立审读最终 BODY。[已暂停 L100 的失败与修复边界](../docs/superpowers/reports/2026-09-10-stage1-map-failure.md)记录了这一限制。

## 6. Review、修复和接纳资格

DraftReviewService 聚合不同职责的信号：文本/体验、计划契约、地图移动、人物行为、lint、Canon quality、publisher 约束。多个检查器共存的合理性来自职责不同；“都能发现某种连续性问题”仍可能产生重复成本，是下一轮消融要检验的部分。

界面/API 固定展示五层，不能把其中一层的成功当另一层成功：

1. draft_review：草稿问题与 verdict。
2. repair：修复范围、尝试、预算和 verification。
3. residual_eligibility：修复耗尽后是否仍有资格继续。
4. gate_delegation：本应交给人的合格候选暂停门，由 human 或 spark 处理。
5. canon：真正已接纳的 candidate/commit 身份。

RepairService 选择 scope，必要时修改未来计划，再重写、重新 review、验证 must_fix / must_preserve 和新风险。预算在当前 candidate/repair cycle 内计算。警告不自动变成必须修复；内容错误、合同错误与基础设施失败不能混为同一“重试”。

当前重写通过 `ChapterContextPack.repair_contract` 接收与 verifier 同源的完整 `must_fix / must_preserve / must_not_reveal` 三列表。它在各 scope 完成计划重建后附加，纳入已有软上下文预算，再由单章、预演、场景拆分、场景生成和 stitch 共用一处完整渲染。合同不可按前三项截断；次要上下文依原规则裁剪，必需内容超过软预算时不静默删除合同，也不新增质量门。下一轮替换当前合同，普通初稿不携带；不再将通用纠错文本累积进持久化计划的规则锚点。现有倒计时专用提示和 Canon 优先级保持不变。此修复解决输入覆盖，不能保证模型输出或 reviewer 建议本身正确；真实 smoke 和 L100 仍需独立验收。

标题属于修复合同里的可保留元数据：若合同精确保护当前标题，且本次计划没有显式改名，重写者应保留该标题。显式改名与旧 must_preserve 冲突时仍拒绝；本轮不靠删除 verifier 约束获得通过。

普通岗位称呼及其动作不等同姓名占位。确定性检查只在明确身份栏位或预期主角缺失等有依据的场景报告占位；不能凭“工作人员说”或独立一行岗位名硬判，更不能全局替换成编造的别名。已核实的 Canon 人名修正仍会重新评审；未知身份通过已有有限修复处理，局部执行器没有确定性替换时交给 Writer，不把原样正文算作成功重写。实际样本及支持边界见[连续性失败记录](../docs/superpowers/reports/2026-09-10-accepted-continuity-failure.md)。

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

历史修订先保存候选，原 accepted 主线继续有效；在隔离 BookState 中重新抽取修改章并核验直到 accepted 尾章的完整后缀。旧 GraphDelta 只作为证据，不能以重放成功替代正文核验。结果绑定 base revision、完整范围、候选 hash 和引用；fail 或关键 unknown 拒绝自动替换。短事务内再次核对主线、冻结事实及发布 attempt，然后原子切换整个修订集合。正文未变但接纳上下文变化的后继也产生新接纳身份，保留旧证据。支持边界及并发/失败回归见[修订报告](../docs/superpowers/reports/2026-09-09-p1-2-revision-evidence.md)。

正式摘要、评审、Arc 激活材料、节奏分析和 Band 核验沿 `ChapterPlan.active_commit_id → CanonCommitRecord → CandidateDraftRecord → ChapterDraft` 读取；评审使用 candidate 绑定的 `review_id`。窗口先按稳定章节限额，再读取对应稿件。保存或拒绝修订不改变正式输入，成功接纳后才切换。accepted 身份损坏时不回退到最新候选：章节正文 API 返回 409，Band 核验保留阻断；摘要缺失保持未知，不以计划概要补造事实。

世界编辑的局部 BookState gate 不能替代完整后缀核验；即使标记未来章节，编辑也可能修改历史读取共用的实体元数据。因此当前 `commit_world_edit` 在项目锁内拒绝任何已有 accepted/active 章节的项目，人工强制批准不能绕过。未来计划调整仍走 Planning；世界编辑接入同一完整后缀核验前不开放这一入口。写前世界编辑仍可使用，章号解析由一个 owner 同时供 envelope、delta 和新建事实使用。

## 8. Canon 后的维护、投影和恢复

post-Canon maintenance 按 planning → arc → world → feedback 顺序执行，分别更新计划、下一章 Arc resolution、世界压力和反馈。这些结果被下一章的 planning/personality/context 消费。四步全部成功后，还需完成 order controls，包括 obligation 验证、future-plan audit、generation-audit report 和 band checkpoint。第2章及以后提交 Canon 都强制检查前章这一屏障；未解除的 future-contract、checkpoint 或人工阻断会使提交等待恢复。pulp 的 continue 只放过 checkpoint warn，fail 仍阻断。

四步维护及order_controls的trace已与对象存储上传分开：业务结果、成功状态和冻结脱敏trace的OutboxEvent同事务提交，上传在事务后由现有worker处理。存储失败重试上传，不重跑已成功的维护模型。对象key含内容SHA，旧trace延迟补传不会覆盖后来重跑order_controls的trace；上传状态归outbox，维护结果保留引用。原始payload保留在outbox中，尚未增加自动清理策略。

Generation Audit 已 report-only；FuturePlanAudit 和 band checkpoint 仍可阻断，不能混称为同一个审计。

自动 Band checkpoint 的创建事件保存其实际输入身份及结果摘要。复查仍调用同一组确定性检查；正式接纳身份、相关计划、义务或约束变化后，旧 PASS/override 不再放行，重新核验追加记录而不覆盖原证据。维护恢复、继续生成及人工/Spark 批准均复核当前输入，失效或缺证据视为待核验。已成功的维护模型不因 checkpoint 失效而重跑；短事务在模型工作开始前释放项目锁。`continue` 主动跳过且从未创建检查时不等待不存在的记录，已存在的有效失败及未接纳前章仍阻断。

这一边界保障顺序一致性，但增加延迟和实现复杂度。不能仅因模块名含 projection 就整体删除：必须先证明下游消费者能接受落后数据，并按步骤做消融。Canon 已接纳的本章不因投影故障回滚。

Knowledge Projection、Obsidian export、LLM KB、chapter memory 和 World Studio 读视图从 Canon 派生，失败通过 outbox/checkpoint/replay 恢复。Qdrant 是检索索引，MinIO 是产物存储，都不是独立 Canon。Obsidian 的人工 section 进入单独 human index；写前事实编辑走 generic proposal，已有正式章节的事实变更须通过章节修订及完整后缀核验。

小说 Markdown + manifest 是独立的 outbox 导出：先从保留 Canon 历史冻结目标 book revision 和内容身份，再原子写本地版本文件并推进 current。重试复用冻结快照，旧事件不回退当前版本；只读 rebuild 可重建已导出的版本。接纳事务不执行文件 IO，导出失败不回滚 Canon。发布回执引用明确是捕获时观察值，不伪称过去时点的完整发布状态。当前没有每书 Git 仓库、远端同步或绕过 proposal 的正文导入；边界见[导出报告](../docs/superpowers/reports/2026-09-09-novel-export.md)。

反馈步骤先独立提交带来源/版本的评论分析，零信号也完成；后续聚合或计划事务失败不撤销已经完成的分析。唯一 aggregation owner 以全部评论为分母，区分方向与平台作者，冻结来源发布版本和证据。行动分别记录提议、选择、未来计划应用、裁剪后实际 Writer 输入、正文观察与后续变化；单一读者、低置信度、风险 watchlist 或相反方向不足以自动改纲。未来计划使用现有版本 CAS，拒绝已写/预约/接纳/发布历史，保留既定目标。合格提示的 canonical provider 已恢复，旧全局校准、世界规则自动改写及 review 反馈阻断仍禁用。正文观察默认 unknown，后续信号比较只称关联；[有限样本](../docs/superpowers/reports/2026-09-09-stage3-feedback-finite-loop.md)明确区分真实 owner 与冻结模型/发布输入。

## 9. Publisher 与自动生产

Publisher 与生成是独立运行角色。Canon 后物化具有确定身份的发布任务；worker/browser 使用 attempt token、lease fencing、receipt、浏览器持久 journal 和只读 reconciliation 处理崩溃后的未知结果。

外部动作前已经持久化章节版本保护；任意平台确认公开即永久冻结，未知结果或未核对的远端草稿不能因超时、任务删除或重启解除保护。冻结包括正文、读者标题和章节顺序。修订与发布按同一 Project/Chapter 锁序竞争并复核不可变载荷，旧版本 pending job 不会被误发。

`daily_serial` 用显式主平台的连续公开前缀 P 计算存稿，当前 accepted 连续尾号 G 加持久预约 R 满足 `G + R - P <= min(N, max(3, ceil(N × 0.05)))`。入队、worker 开始新章和 Canon 接纳均有责任边界；预约服从任务 lease/epoch，重试同章不重复占位。达到上限是正常等待，发布仍可推进；N 下调不会删除旧存稿。明确隔离的 `factory_batch/soak_test` 不受发布前缀约束，也不会因此获得真实发布权限。

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

下列内容不能再作为现行方案引用：多种 operation/checkpoint/copilot/reckless 模式、premium 空profile、RuntimeSettingsStore、governance_json、request-level policy override、旧review cutover flags、WritingOrchestrator动态拼装、旧world_model facade、legacy accepted-state双写、独立Scenario rehearsal、Provisional Band Preview、Obsidian reverse import、旧API/private alias、手写schema升级器。旧部署 baseline 仅作为经过 schema 指纹检查的 Alembic 向前桥接来源保留，不作为第二套生产 schema。

保留的普通 writer preview fallback 与 Arc sizing `provisional_window` 不是已经删除的第二套 Provisional writer；保留的 BookState extraction gate 不是旧主 reviewer；两者不能仅凭旧名字相似误删。

## 12. 复杂度与消融重点

本轮先以固定响应验证 Writer、Review/Repair 和 Canon 准备拆分的行为等价，再进行单假设消融。A1 因两个已知严重漏拦保留 verifier；A2 只有输入字符节省而缺少质量/真实 token 对照，证据不足；A3 没有证明消费者能使用陈旧结果，保留四步 barrier。smoke 已显示 Scene/stitch 调用成本和时间矛盾，但没有同输入、同预算的单章 Writer 对照，A4 不据此替换现行 Writer。具体证据与缺失度量见[消融报告](../docs/superpowers/reports/2026-09-09-stage2-ablation.md)。未保留实验 mode 或第二套实现。

前轮仍有效的取舍如下；它们不是本轮新测量：

| 对象 | 可证伪的问题 | 保留/删除依据 |
| --- | --- | --- |
| 拼接前scene正文送审 | 是否带入最终正文不存在的冲突？ | 已用真实R27产物及fixture证实；删除重复输入，真实最终正文error仍能阻断 |
| repair标题重新生成 | 是否与must_preserve合同冲突且无收益？ | 已复现，修复owner保留受保护标题；不削弱verifier |
| 无调用PlanHealth聚合接口 | 是否只有测试在制造存在理由？ | 已删除 from_patch_validation / combine 和专用测试，净删47行；from_future_audit 的真实阻断调用保留 |
| RepairVerifier头尾摘要与总体布尔判断 | 是否遗漏正文中段和未覆盖语义合同？ | 改为完整正文、逐合同证据及三态覆盖；无证据/超时不伪称通过 |
| Generation Audit摘要 | 是否提供S1/任务查询没有的运营信息？ | 目前低成本report-only；删除会损失cadence摘要，先做对照，不恢复成门 |
| post-Canon四步barrier | 哪些步骤确实必须阻塞下一章？ | 25种内存输入对照确认当前硬依赖；保留业务四步，trace改为持久outbox补传，故障不重跑已成功业务 |
| release harness验真层 | 哪些规则防真实错误，哪些只是反复证明脚本自身？ | 保留来源/镜像/状态/回执真实性；停止新增外部签名等前置门，后续按实验裁剪 |

真实验收使用约 20 章隔离 smoke，再使用全新离线 L100，要求固定源码/镜像/策略、有限修复及预先确定的收尾。smoke 已发现评审未拦住的连续性缺陷，运行中没有热修复；本地回归、accepted 行数和冻结模型 fixture 都不能替代真实完成结尾的证据。当前进度见[smoke 报告](../docs/superpowers/reports/2026-09-09-stage1-smoke.md)。

## 13. 文档入口

- 本文：现有设计的完整叙述。
- [CURRENT_ARCHITECTURE](CURRENT_ARCHITECTURE.md)：精简的架构与代码边界。
- [DESIGN_STATUS](DESIGN_STATUS.md)：旧文档权威性与当前状态。
- [前轮收口修订](https://github.com/Ctwqk/ForWin/blob/521228871a5752ebe8572c057caa9f4944bb0295/docs/superpowers/specs/2026-09-04-v5-closure-design.md)：架构收口与发布验收顺序。
- [前轮验证记录](../docs/operations/v5-closure-reassessment-2026-09-04.md)：历史分支整合、测试、消融和未完成发布证明。
- [本轮修复设计](../docs/superpowers/specs/2026-09-04-v5-autonomy-fixes-design.md)及[验证记录](../docs/operations/v5-autonomy-fixes-2026-09-04.md)：外部评审三个问题的实现、证据与边界。

发生冲突时先核对实际源码与通过的契约测试，再更新本文和精简架构入口。旧计划上的 Approved/checkbox 不足以推翻当前 owner，也不足以证明功能通过真实运行验证。
