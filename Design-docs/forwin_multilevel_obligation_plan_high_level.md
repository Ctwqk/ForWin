# ForWin 强状态叙事义务与多层级计划偿还机制：高层改进计划

生成日期：2026-05-13  
用途：交给 Codex 执行的高层设计计划。  
目标：在 ForWin 的强状态管理写作系统中，建立一种“当前章不必就地补丁，但必须通过强状态账本与 chapter / band / arc / book 计划约束来偿还缺口”的机制。

---

## 1. 核心判断

当前系统已经有真实的 plan / review / rewrite / checkpoint 基础，但如果所有 review 问题都倾向于“修当前章”或“塞到下一章 chapter plan”，长篇会逐渐变成一本补丁书。

合理的机制不是简单放宽 review，也不是默认改后续 chapter plan，而是建立：

```text
叙事义务账本 = 缺口源头真相
计划补丁 = 偿还路径
chapter / band / arc / book plan = 计划投影
context injection = 后续执行约束
verifier = 偿还验证
canon admission gate = 当前章准入裁判
final gate = 全书清算
```

也就是说：

```text
可以允许当前章留下缺口；
但缺口必须进入强状态；
强状态必须绑定计划；
计划必须有截止章与偿还测试；
后续章节必须看到并偿还；
未偿还时必须阻断、升级重计划或进入人工审查。
```

---

## 2. 当前系统基础与改进方向

### 2.1 当前已有基础

根据 ForWin 代码结构，当前系统已经具备下列基础：

- `ChapterPlan`：有章节标题、单行摘要、目标、任务合同、体验计划、残留 review issue、canon risk 等字段。
- `ArcPlanVersion`：有 arc 编号、章节范围、arc synopsis、计划目标规模、状态等字段。
- `BandCheckpoint`：有 arc、band、章节边界、状态、问题列表、resolved_at 等治理字段。
- `NarrativeConstraint`：有 arc、band、约束类型、生效章、保护截止章、状态等字段。
- `repair_coordinator`：已有 review 后重写链路，并区分 scene / band / escalation。
- `canon_quality` / review 相关改造方向：已经在围绕占位符、倒计时、artifact、角色状态、reveal、重复、identity、style 等建立 deterministic 检查。

这些基础说明：ForWin 不缺“局部修补能力”，真正缺的是一个能决定**问题应该落到哪一级计划偿还**的高层控制层。

### 2.2 本次要新增的关键能力

本次不只是增加规则，而是新增一套调度与治理机制：

1. `NarrativeObligationLedger`：叙事义务账本，作为缺口源头真相。
2. `ObligationScopeRouter`：判断缺口应该落到 chapter、band、arc 还是 book 层。
3. `NarrativePlanPatch`：对计划的可审计补丁，分 chapter / band / arc / book 层。
4. `PlanPatchValidator`：验证计划补丁是否真的能偿还义务。
5. `PlanBackedDeferredAcceptance`：计划背书式延后接受事务。
6. `ContextInjection`：将 active obligations 注入后续 writer / reviewer / gate。
7. `ObligationResolutionVerifier`：验证后续章节是否真正偿还。
8. `ObligationBudget`：限制债务数量，防止“以后再圆”滥用。
9. `FinalObligationGate`：终章或全书完成前清算 P0/P1 义务。

---

## 3. 设计原则

### 3.1 义务账本是源头，计划是偿还路径

不要把缺口只写进 chapter plan、band plan 或 arc plan。计划会变化，义务不能丢。

正确关系：

| 层级 | 职责 |
| --- | --- |
| NarrativeObligation | 记录“当前留下了什么缺口，为什么允许延后，必须何时偿还” |
| PlanPatch | 记录“准备用哪一级计划、哪些未来章节偿还这个缺口” |
| Chapter/Band/Arc/Book Plan | 作为执行投影，驱动 writer 和 reviewer |
| CanonAdmissionGate | 判断当前章能否 clean commit 或 with obligation commit |
| FinalGate | 判断全书是否仍有未偿还主线义务 |

### 3.2 不是所有问题都能延后

可以延后的是“解释、铺垫、偿还、反转、误导、伏笔”。  
不能延后的是“硬错误、正文污染、状态破坏、终章主线未闭合”。

允许延后的例子：

- 人物动机暂时不明，但可以后续用行动或对白补证据。
- 身份疑点被明确设计成谜团。
- 倒计时异常被设定为系统干预，但需要后续解释。
- artifact 数量疑点被设计成记录污染，但需要后续修正。
- 某个 reveal 需要跨 band 才能完成 escalation。
- 读者承诺需要在 band 内或 arc 内支付。

不能延后的例子：

- `相关人员` 等占位符进入正文。
- 正文截断、JSON 泄漏、空正文。
- 同章存在互斥结局。
- 明确死亡后无桥接继续行动。
- artifact 数量在同一段内自相矛盾。
- 终章仍有 P0/P1 主线倒计时、hook、artifact、身份债务未清。
- 数据持久化、章节顺序、任务状态等工程硬错误。

### 3.3 不默认落到最近的下一章

系统不能默认把所有缺口塞进下一章 chapter plan。  
必须先判断缺口影响范围：

```text
单章可偿还 -> chapter plan
需要 2-6 章铺垫/兑现 -> band plan
跨 band 或触及主线结构 -> arc plan
全书尾声/续作 hook -> book/final plan
```

若范围不确定，应优先选择更高层级或人工审查，而不是压成局部补丁。

---

## 4. ObligationScopeRouter：多层级计划选择

### 4.1 职责

`ObligationScopeRouter` 是本机制的核心。它负责回答：

```text
这个缺口是否可以延后？
如果可以，应由哪一级计划负责偿还？
需要影响哪些未来章节？
截止章是什么？
是否需要 band / arc 级重计划？
```

它不写正文，也不直接修改计划。它只做范围判断和治理决策。

### 4.2 输入维度

ScopeRouter 至少应考虑：

- 缺口类型：动机、身份、倒计时、artifact、reveal、角色状态、风格、payoff、世界规则。
- 缺口严重度：P0 / P1 / P2 / P3。
- 缺口硬度：可留白、设计债、canon 风险、硬阻断。
- 当前章节位置：chapter、band、arc、final band、final chapter。
- 未来剩余章节数量。
- 同类义务是否已经多次出现。
- 是否触及中心角色、主线 artifact、倒计时、终局 hook。
- 是否需要多个章节铺垫。
- 是否已经接近 deadline。
- 是否已有过失败修复或重复补丁。
- 是否会影响已 accepted canon。

### 4.3 Chapter 层适用场景

落到 chapter plan 的情况应当有限且明确：

- 下一章即可补清的人物动机。
- 单个 reveal 证据缺失。
- 轻微转场缺口。
- 低风险读者疑问。
- 小型伏笔下一章必须支付。
- 当前章创造了悬念，但下一章开头解释即可。

要求：

- affected chapters 通常为 1-2 章。
- deadline 不应超过当前 band。
- payoff test 必须具体。
- 如果下一章没有偿还，立即升级 band 或阻断。

### 4.4 Band 层适用场景

落到 band plan 的情况：

- 当前缺口需要一组章节共同偿还。
- 本 band 的爽点、payoff、悬念、关系推进节奏需要重排。
- 连续几章出现重复场景功能。
- 某个角色关系需要 2-6 章完成转折。
- reader promise 需要在 band 内支付。
- 当前章制造了 suspense，但不适合下一章马上解释。
- 本 band 的 reward cadence 已经失衡。

Band plan patch 关注：

- band payoff contract。
- band reveal schedule。
- band reward cadence。
- band-level obligations。
- must_resolve_by_band_end。
- allowed_carry_forward。
- affected future chapters。
- 角色/关系/场景功能分配。

### 4.5 Arc 层适用场景

落到 arc plan 的情况：

- 身份谜团或身份漂移。
- 亲属关系、阵营关系、中心角色角色定位冲突。
- 主线 artifact / 档案数量 ledger 需要结构性修正。
- 倒计时规则、重置规则、世界规则需要解释。
- 角色死亡、牺牲、救援、处决链条跨 band 冲突。
- 反派系统规则需要重定义。
- 当前 band 无法独立修复。
- 需要多个 band 铺垫和兑现。
- 已有多次局部修复失败或补丁痕迹。

Arc plan patch 关注：

- arc synopsis 或 arc contract 的调整。
- central identity contract。
- artifact ledger contract。
- countdown / reset rule contract。
- world rule contract。
- cross-band reveal schedule。
- hard deadlines。
- 不能破坏已 accepted canon 的 preserve list。

### 4.6 Book / final 层适用场景

Book/final 层非常谨慎，只适合：

- 续作 hook。
- 非主线世界余波。
- 风格或体验层面的轻债。
- 可开放但不影响当前主线闭合的问题。

不允许 book/final 延后：

- P0/P1 主线义务。
- active countdown。
- 核心 artifact ledger。
- 中心人物身份未清。
- 终局反派规则未解释。
- terminal hook 未关闭。

---

## 5. PlanPatch：不同层级的计划补丁

### 5.1 总体要求

每个 PlanPatch 都必须：

- 绑定一个或多个 obligation。
- 指明 target scope。
- 指明 affected future chapters。
- 指明 deadline。
- 指明 payoff test。
- 指明 writer context injection。
- 指明 reviewer context injection。
- 指明 must preserve / must not change。
- 通过 PlanPatchValidator。
- 被应用后才能允许当前章 `commit_with_obligation`。

### 5.2 ChapterPlanPatch

用途：

```text
把缺口放到一个或少数未来章节中偿还。
```

必须覆盖：

- 目标未来章节。
- 该章节新增目标。
- 新的 payoff test。
- writer 必须补出的证据。
- reviewer 必须检查的点。
- 如果未完成时的升级策略。

### 5.3 BandPlanPatch

用途：

```text
把缺口转化为 band 内的一组节奏、payoff、reveal、关系推进任务。
```

必须覆盖：

- affected chapters。
- band payoff contract。
- band reveal schedule。
- reward cadence 调整。
- 角色关系或场景功能分配。
- 本 band 结束前必须清掉的 obligations。
- 可带入下个 band 的 obligations。
- band checkpoint 检查条件。

### 5.4 ArcPlanPatch

用途：

```text
把缺口转化为 arc 级结构调整。
```

必须覆盖：

- affected chapters / affected bands。
- arc synopsis 或 arc contract 的变化。
- 中心身份、关系、阵营、世界规则的约束。
- artifact / countdown / reveal / terminal-state 等核心 ledger 的调整。
- 不得破坏的 accepted canon facts。
- hard deadlines。
- arc checkpoint 或 final gate 检查条件。

### 5.5 BookPlanPatch

用途：

```text
处理非主线开放结尾、续作 hook、全书余波。
```

限制：

- 不能承接 P0/P1 主线义务。
- 不能绕过 final gate。
- 需要人工确认或强 governance policy。

---

## 6. Plan-Backed Deferred Acceptance 工作流

### 6.1 正常流程

完整流程应为：

```text
Review / CanonQualityAnalyzer 发现问题
  -> ReviewOutcomeRouter 判断可延后
  -> 创建 NarrativeObligation
  -> ObligationScopeRouter 选择偿还层级
  -> 生成对应层级 PlanPatch
  -> PlanPatchValidator 验证
  -> 应用 PlanPatch
  -> NarrativeObligation 进入 planned 状态
  -> CanonAdmissionGate 允许当前章 commit_with_obligation
  -> 当前章 accepted 后 NarrativeObligation 进入 active 状态
  -> 后续 writer/reviewer/gate 全部接收 active obligation
  -> ObligationResolutionVerifier 验证偿还
  -> obligation resolved 或 expired / blocked
```

### 6.2 当前章准入要求

当前章只有在以下情况才能带 obligation 入 canon：

```text
NarrativeObligation 已创建
PlanPatch 已创建
PlanPatch 已验证
PlanPatch 已应用
obligation 已绑定 PlanPatch
obligation 有 deadline
obligation 有 payoff test
obligation 没有超预算
obligation 不是 hard blocker
```

任何一个条件不满足：

```text
当前章不得 commit_with_obligation
```

### 6.3 到期处理

到期时若未偿还：

- 如果仍可局部修复：当前章或 deadline 章重写。
- 如果是 band 级问题：band replan。
- 如果是 arc 级问题：arc replan。
- 如果接近终章或影响主线：manual review 或 block。
- 如果已到 final chapter：final gate 阻断完成。

---

## 7. ReviewOutcomeRouter 更新

### 7.1 新增动作

ReviewOutcomeRouter 应输出以下动作：

- `commit_clean`
- `commit_with_obligation`
- `local_rewrite`
- `chapter_replan_then_rewrite`
- `defer_with_chapter_plan_patch`
- `defer_with_band_plan_patch`
- `defer_with_arc_plan_patch`
- `defer_with_book_plan_patch`
- `band_replan_then_rewrite`
- `arc_replan_then_rewrite`
- `manual_review_required`
- `block`

### 7.2 关键区别

| 动作 | 当前章是否重写 | 是否创建 obligation | 是否修改计划 |
| --- | --- | --- | --- |
| `local_rewrite` | 是 | 否 | 否 |
| `chapter_replan_then_rewrite` | 是 | 可选 | 当前章计划 |
| `defer_with_chapter_plan_patch` | 否 | 是 | 后续 chapter plan |
| `defer_with_band_plan_patch` | 否 | 是 | band plan |
| `defer_with_arc_plan_patch` | 否 | 是 | arc plan |
| `commit_with_obligation` | 否 | 是 | 必须已完成 |
| `manual_review_required` | 不确定 | 不确定 | 人工决定 |
| `block` | 否 | 否 | 阻断 |

### 7.3 取消“按次数决定范围”

当前 “第一次 scene、第二次 band、第三次 escalation” 可以保留为 fallback，但不应作为主决策。

主决策必须来自：

```text
问题类型 + 状态强度 + 影响范围 + 计划层级 + 债务预算
```

---

## 8. 防止系统退化成“下一章补一句”

### 8.1 Scope 保护规则

必须加入以下保护规则：

1. identity ambiguity 默认不能只落到下一章，除非它只是单章误写。
2. artifact count explanation 默认至少 band，涉及主线计数则 arc。
3. countdown explanation 默认至少 band，涉及重置规则则 arc。
4. repeated scene pattern 默认 band。
5. terminal state bridge 默认 arc，除非同章可本地修复。
6. final hook closure 不允许 book-level 延后，必须终章前清零。
7. 多次出现的 chapter-level obligation 自动升级 band。
8. 同一 subject 多个 obligation 聚集时自动升级 band 或 arc。
9. deadline 逼近时不再允许新增同类 obligation。
10. 如果 PlanPatch 只修改一个未来 chapter 但 obligation 被判定为 band/arc，验证必须失败。

### 8.2 ObligationBudget

建议设置预算：

- 每章可新增 obligation 数量上限。
- 每个 band 可同时打开 P1/P2 obligation 上限。
- 每个 arc 可同时打开核心结构 obligation 上限。
- 同一个中心角色不能堆多个 identity / motivation obligation。
- final band 开始时 P0 obligation 必须为 0。
- final chapter 前 P1 obligation 必须为 0。
- 超预算时禁止 `commit_with_obligation`。

### 8.3 Plan Coherence Check

每次应用 PlanPatch 后，需要检查：

- 是否造成未来章节目标互相冲突。
- 是否把太多 payoff 挤到同一章。
- 是否破坏 band rhythm。
- 是否让 arc finale 过载。
- 是否把 P1 主线债推得太晚。
- 是否修改了已 accepted canon。

---

## 9. 后续上下文注入

### 9.1 Writer 必须看到

后续 writer context 应显示：

- active obligations。
- 当前章必须推进或解决哪些 obligation。
- deadline。
- payoff test。
- 不得提前解决的 obligation。
- 必须保留的 canon facts。
- 当前 PlanPatch 对本章的具体约束。

### 9.2 Reviewer 必须看到

Reviewer context 应显示：

- 当前章应解决的 obligation。
- 当前章应推进但不解决的 obligation。
- 过期 obligation。
- 计划补丁预期。
- payoff test。
- 不能接受的伪解决方式。

### 9.3 Gate 必须看到

CanonAdmissionGate 应显示：

- 当前章新增了哪些 obligation。
- 当前章解决了哪些 obligation。
- 是否过期。
- 是否超预算。
- 是否 final gate 清零。
- 是否存在未验证 resolution。

---

## 10. 偿还验证

### 10.1 不允许模型自称已解决

系统不能仅凭正文说“事情解释清楚了”就清账。

必须通过 verifier：

- 对动机缺口：是否有行为、对白或事件证据。
- 对身份疑点：是否明确 truth value、lie、reveal 或误导机制。
- 对 countdown：是否 ledger 一致。
- 对 artifact：是否数量 ledger 一致。
- 对 terminal state：是否有 bridge event。
- 对 reveal：是否有 escalation 或 payoff。
- 对 final hook：是否 terminal obligation resolved。

### 10.2 偿还状态

义务生命周期：

```text
proposed
planned
active
resolved
expired
waived
blocked
```

只有 `resolved` 或人工 `waived` 才能从 active 清出。  
`waived` 必须有人工原因和审计记录。

---

## 11. 与 band / arc 的具体集成建议

### 11.1 Band 集成

因为当前系统已有 `BandCheckpoint` 和 band 相关治理字段，band-level obligation 应该集成到：

- band checkpoint。
- band issue summary。
- band payoff contract。
- band end verification。
- task center / project detail 中的 band status。

Band-level obligation 应在 band end checkpoint 时强制检查：

```text
must_resolve_by_band_end 是否清零
allowed_carry_forward 是否符合规则
是否需要升级 arc
```

### 11.2 Arc 集成

Arc-level obligation 应集成到：

- `ArcPlanVersion`。
- arc synopsis / arc contract。
- chapter range。
- arc-level identity / artifact / countdown / reveal contracts。
- arc completion gate。

如果 arc-level obligation 在 arc 末未解决，应进入：

```text
arc replan
manual review
block final continuation
```

而不是继续写下一 arc。

---

## 12. 实施阶段

### Phase 1：概念与数据层

交付：

- NarrativeObligationLedger。
- NarrativePlanPatch。
- obligation lifecycle。
- PlanPatch lifecycle。
- obligation 与 plan patch 的绑定关系。
- 基础审计记录。

验收：

- 当前章可以创建 obligation。
- obligation 必须有 deadline 和 payoff test。
- obligation 可以绑定 plan patch。
- 未绑定 plan patch 时不能进入 active。

### Phase 2：ScopeRouter 与 PlanPatchValidator

交付：

- ObligationScopeRouter。
- PlanPatchValidator。
- chapter / band / arc / book target scope。
- scope 保护规则。
- 计划一致性检查。

验收：

- 动机小缺口落 chapter。
- band payoff 问题落 band。
- identity / artifact / countdown / terminal state 落 arc。
- 错误落级会被 validator 拒绝。

### Phase 3：Deferred Acceptance 事务

交付：

- Plan-Backed Deferred Acceptance transaction。
- 当前章 `commit_with_obligation`。
- AdmissionGate 更新。
- 事务失败回滚。

验收：

- obligation + plan patch + validator 都通过，当前章才能带 obligation accepted。
- 任一环节失败，当前章不能 accepted。

### Phase 4：Context Injection 与后续执行

交付：

- writer context 注入 active obligations。
- reviewer context 注入 active obligations。
- gate context 注入 active obligations。
- must_resolve_now 标记。

验收：

- 后续章节 writer 能看到 obligation。
- reviewer 能检查 obligation。
- 到 deadline 章未解决则阻断。

### Phase 5：Resolution Verifier 与 Budget

交付：

- ObligationResolutionVerifier。
- ObligationBudget。
- expired obligation handling。
- waiver / manual approval。

验收：

- 不能靠模型一句“已解决”清账。
- 超预算不能继续带债。
- final gate 前 P0/P1 清零。

### Phase 6：Band / Arc 深度集成

交付：

- band-level obligation contract。
- arc-level obligation contract。
- band checkpoint 检查。
- arc completion gate。
- band/arc replan report。

验收：

- band obligation 不会被压到单章补丁。
- arc obligation 不会被压到 band 或 chapter。
- band/arc plan patch 会影响后续多个章节 context。

### Phase 7：可观测性与测试

交付：

- project detail 展示 active obligations。
- chapter detail 展示 obligation provenance。
- task center 展示 blocked reason。
- replay report 支持查看 obligation 生命周期。
- 完整测试套件。

验收：

- 能解释为什么当前章被允许带债通过。
- 能解释后续哪一章必须偿还。
- 能解释为什么过期后阻断。
- 能回放 60 章项目中的缺口生命周期。

---

## 13. 测试计划

建议新增测试：

- `test_narrative_obligation_ledger.py`
- `test_plan_patch_scope_router.py`
- `test_plan_patch_validator.py`
- `test_plan_backed_deferred_acceptance.py`
- `test_obligation_context_injection.py`
- `test_obligation_resolution_verifier.py`
- `test_obligation_budget.py`
- `test_band_plan_obligation_patch.py`
- `test_arc_plan_obligation_patch.py`
- `test_final_gate_obligation_clearance.py`

重点测试场景：

1. 小动机缺口落到后续 chapter plan。
2. band payoff 缺口落到 band plan，而不是下一章。
3. identity drift 落到 arc plan。
4. artifact count drift 落到 arc 或 band，不能只下一章补一句。
5. countdown reset rule 落到 arc。
6. terminal state bridge 落到 arc，除非本章本地修。
7. obligation 无 PlanPatch 不允许 accepted。
8. PlanPatch 验证失败不允许 accepted。
9. 到 deadline 未偿还阻断。
10. final chapter 前 P0/P1 obligation 未清零阻断。
11. repeated chapter-level obligations 自动升级 band。
12. 同一 subject 多 obligation 自动升级 arc 或 manual。

---

## 14. Codex 执行要求

请 Codex 按本计划实现时遵守：

1. 不要只实现“往下一章 chapter plan 写 payoff_test”。
2. 必须实现 ObligationScopeRouter。
3. 必须允许 target scope 为 chapter / band / arc / book。
4. 必须有 scope 保护规则，防止结构问题被压成单章补丁。
5. 必须新增 NarrativeObligationLedger 作为源头强状态。
6. 必须新增 NarrativePlanPatch 作为偿还路径。
7. 必须新增 PlanPatchValidator。
8. `commit_with_obligation` 必须依赖 obligation + plan patch + validator。
9. active obligations 必须注入 writer / reviewer / gate。
10. 到期未偿还必须阻断或升级。
11. band-level obligation 必须进入 band checkpoint / band contract。
12. arc-level obligation 必须进入 arc plan / arc completion gate。
13. final gate 必须清算 P0/P1 obligations。
14. 不允许 hard blocker 伪装成 obligation。
15. 不允许纯 prompt 文案替代强状态。

---

## 15. 最终验收标准

实现完成后，系统应满足：

```text
当前章可以不就地重写；
但只有在缺口被强状态记录、范围被正确路由、计划被对应层级修改、后续上下文被强制注入、偿还被验证、到期可阻断的情况下，当前章才能带义务进入 canon。
```

更具体地说：

- 单章缺口可以落 chapter。
- band 节奏缺口必须落 band。
- arc 结构缺口必须落 arc。
- final 主线缺口不能延后。
- 计划补丁不能降级处理结构问题。
- 债务不能丢失在计划重写中。
- 后续章节不能无视 active obligations。
- 全书完成不能绕过 obligation 清算。

---

## 16. 简短结论

如果只把缺口塞进后续 chapter plan，仍然会变成局部补丁系统。

正确做法是：

```text
NarrativeObligationLedger 作为缺口源头；
ObligationScopeRouter 决定 chapter / band / arc / book；
PlanPatch 修改对应层级计划；
PlanPatchValidator 防止错误落级；
ContextInjection 强制后续执行；
Verifier 验证偿还；
FinalGate 清算债务。
```

这样系统既能保留长篇小说需要的悬念和延后解释，又不会把整本书写成无法清偿的补丁堆。
