Provisional / Scenario Rehearsal 逻辑说明（4.1 代码核对版）

> 4.1 修订：旧 `provisional preview` 已从核心机制降级为 legacy compatibility。新的主流程把“写之前判断计划能不能写”交给 `Scenario Rehearsal`，把“写之后判断正文能不能入 canon”交给 `Candidate Draft Review`。旧 `ProvisionalBandExecution` / `ProvisionalChapterLedger` / `ProvisionalPromotionRecord` 仍保留，用于历史兼容、legacy preview 和审计展示，但不再默认阻断正式写作。
>
> 4.1 完整架构补齐：`Candidate Draft` 已成为独立语义记录层，链接 `ChapterDraft` / `ChapterReview` / repair attempts / canon commit 状态；`Scenario Rehearsal` 已拆出 trigger matrix、deterministic rule pack、hybrid director simulation、plan patch approve / rerun API。legacy provisional 不参与新主线判断，除非显式开启 legacy 开关。
>
> V4.5.1 口径：本文档只保留 legacy provisional 边界说明。BookState canon 优先路径、Scheme C map gate、movement reviewer policy 的残余设计收束见 `V4.5.1_markstone.md`；World Studio、dashboard、脚本型 Skill runtime 不写入本文档。

## 0. 4.1 术语边界

```text
Scenario Rehearsal：Writer 下笔前的结构化叙事/世界/认知/爽点预检。
Candidate Draft：Writer 产出的候选正文及其 scene/state/event/thread candidates。
Candidate Draft Review：候选正文入 canon 前的 review/repair/final acceptance。
Canon Commit：通过 review 后才正式改变 canonical world/timeline/thread state。
Legacy Preview：旧 provisional band preview，仅在 legacy 开关启用时运行。
Scenario Plan Patch：Scenario Rehearsal 产出的计划补丁记录，可自动应用、等待人工批准，或 rerun 后转为 pass/replan/block。
```

4.1 runtime 顺序：

```text
Book Genesis / Arc Plan
  -> Scenario Rehearsal
  -> pass | auto patch + rerun | replan | checkpoint/block
  -> Chapter Context Assembly
  -> Writer
  -> Candidate Draft Review
  -> Repair Loop
  -> Canon Commit
  -> World / Cognition / Reader State Update
```

1. 文档目的
这份文档只回答一件事：
> 当前设计里，`provisional` 到底是什么，它和 `canonical`、`Writer`、`Review Hub`、`State Updater`、`Audience Feedback`、`Arc/Band/Chapter` 的关系是什么，以及运行顺序是什么。
目标不是重新讨论方案优劣，而是给现有代码做核对基线。
---
2. 一句话定义
`provisional` 是正式真值提交前的预写/预审状态层。
它的作用是：
让系统先写出一批“可被审查、可被分析、可被重写”的正文与派生对象
但这些对象还不进入正式 canonical 真值
只有通过当前阶段要求后，才 `promote` 到 canonical
所以：
`draft`：尚未通过基本生成/抽取
`provisional`：已可用于审查、分析、重写，但未正式入真值
`canonical`：正式主线真值
---
3. provisional 不是什么
3.1 它不是整本书预演
不是先把整本书都写一遍，再决定要不要用。
3.2 它不是单章独立双写
不是“每章都先完整预演一遍，再完整正式写一遍”这种双倍成本流程。
3.3 它不是评论系统 Phase A/B/C
评论系统的 Phase A/B/C 是评论反馈层的实现路线，不是正文 provisional 的执行阶段。
3.4 它不是新的主链
主链仍然是：
`WritingOrchestrator -> Writer -> Review Hub -> State Updater`
provisional 只是这条主链里、正式写入 canonical 之前增加的一层状态与分支管理。
---
4. provisional 在整体架构中的位置
4.1 在主写作链里的位置
按当前设计，`provisional` 位于：
`Arc/Band/Chapter` 计划已经存在
`Writer` 已经生成正文与结构化候选
`Review Hub` 已经可以审查这些结果
但 `State Updater` 还没有把它们正式写入 canonical
也就是说，它位于：
> `Writer / Review Hub` 之后，`canonical State Updater` 之前
---
4.2 在分层结构里的位置
当前分层可简化为：
大层：Arc
负责：
当前阶段目标
当前 arc 的边界/包络
当前 arc 的功能（铺垫/爆发/转折/收束）
中层：Band
负责：
当前近端 3~8 章左右的可执行章节带
是 `provisional` 的主要作用域
小层：Chapter / Scene
负责：
逐章、逐 scene 的实际正文生成
是 `Writer` 的执行粒度
关键结论
`provisional` 不是整书级，也不是单章孤立级，主要是 band 级：
band 内逐章生成
band 内逐章审查
band 作为一个整体决定是否 promote
---
5. provisional 的对象范围
下列对象允许先存在于 `provisional` 层，而不是直接 canonical：
5.1 正文对象
`ChapterDraft`
`SceneOutput`
`chapter_summary`
5.2 事件与状态候选
`event_candidates`
`state_change_candidates`
`thread_beat_candidates`
`timeline_hints`
5.3 计划层派生物
当前 band 的近端 `ChapterPlan` 调整
当前 arc 的局部 envelope 修正建议
review 产生的 `repair_instruction`
5.4 review 元数据
`review_notes`
`experience_scores`
`lint_signals`
`evidence_refs`
`proposed_design_patch`
5.5 重要限制
下列东西不应该因为 provisional 就直接改写 canonical：
正式 current entity state
正式 canon event chain
正式 timeline
正式 thread state
已 canonical 历史章节
---
6. provisional 和 canonical 的边界
6.1 canonical 的定义
`canonical` 是正式真值层，意味着：
可被后续章节默认引用
可被 `Context Assembler` 当作真实历史
可被 `Timeline Manager` 视为正式事件
可被 `State Repository` 视为当前状态
6.2 provisional 的定义
`provisional` 是待确认层，意味着：
可以被当前 band 的 review / analysis 使用
可以产生临时状态快照
可以用于判断是否重写、扩 arc、缩 arc
但不能自动成为后续所有章节的正式历史
6.3 promote 的定义
`promote` 是从 provisional 到 canonical 的唯一正式入口。
只有在 promote 之后，以下内容才算正式成立：
canonical chapter draft
canonical event
canonical entity state update
canonical thread beat
canonical timeline change
---
7. runtime 先后关系（主流程）
下面是当前最接近设计意图的运行顺序。
---
Step 0：项目/阶段前提已存在
前提：
Project 已存在
active arc 已确定
当前 band 已生成
Writer/Review/State Updater 主链已可运行
如果是 v2.7 语义，则：
overlay / experience plan / reader promise 等当前 active overlay 已存在
但这些不改变 provisional 的位置
---
Step 1：激活当前 active arc
系统先确定当前正在写哪个 active arc。
此时会拿到：
当前 arc 目标
当前 arc 约束
当前 arc 的近端 band
当前 band 内各章的 ChapterPlan
如果采用 v2.4 的 envelope 语义，则此时至少已有：
`base_target_size`
`base_soft_min`
`base_soft_max`
---
Step 2：形成当前 band 的执行上下文
`Context Assembler` 为当前章构建：
`ChapterContextPack`
`ReviewContextBuilder` 为 reviewer 构建：
`ReviewContextPack`
这一步读取的是：
canonical 历史
当前 active arc/band 计划
当前 overlay / audience hints / confirmed signals
最近 canon events / entity snapshots / rule entities
注意
这里默认读取的是：
canonical 历史
加上当前 band 内已存在的 provisional 局部信息（如果系统支持 band 内连续写作共享 provisional）
---
Step 3：Writer 生成当前章
Writer 按当前模式运行：
0.5 阶段
单章单次生成
正式阶段
scene breakdown
scene generation
stitch / polish
structured extraction
输出：
draft text
summary
scene_outputs
event_candidates
state_change_candidates
thread_beat_candidates
lore_candidates
timeline_hints
当前状态
这时这些输出还不是 canonical。
它们首先进入：
`draft`
然后进入 `provisional`
---
Step 4：Review Hub 审查当前 provisional 章
Review Hub 不审 canonical，而是审：
当前 provisional draft
当前章的 provisional extraction
当前 band 的既有 review/meta
当前 canonical 历史上下文
在 v2.3 语义下，至少包括：
continuity review
pacing/review logic（后续阶段增强）
在 v2.7 语义下，会增强为：
continuity
lint signals
WNER / webnovel experience review
merged review verdict
这一阶段的作用
不是正式写入，而是决定：
`pass`
`patch`
`rewrite`
`replan`（或 band/arc patch）
---
Step 5：根据 verdict 处理 provisional
5.1 pass
当前章 provisional 内容可进入 promote 候选。
5.2 patch
只修当前章或当前 scene，不改 canonical 历史。
5.3 rewrite
重写当前章，但仍然是在 provisional 层进行。
5.4 replan / band patch / arc patch
如果问题已经不是当前章节能解决的，就提升到：
chapter 级设计修补
band 级 patch
arc 级 patch
关键点
这些动作都优先发生在 provisional 层，不是直接回滚 canonical。
---
Step 6：band 级分析与 envelope/overlay 修正
当当前 band 有足够 provisional 章节后，系统可以做 band 级判断：
当前 band 节奏是否成立
当前 arc 是否需要扩张/压缩
当前 overlay / reward mix / curiosity beats 是否要重排
当前 audience hints 是否需要进入下一 band
这一层决定的是：
未来 band 怎么改
当前 provisional band 是否够资格 promote
它不应该直接改：
已 canonical 历史
---
Step 7：promote 当前 band（或当前章）
当满足 promote 条件后，才由 `State Updater` 正式提交：
canonical chapter
canonical event
canonical state
canonical thread beat
canonical timeline
推荐 promote 粒度
从设计意图看，更合理的是：
band 为主
单章也可以 promote，但不应把 provisional 简化成“写完一章立刻 canonical”
设计上的核心判断
如果系统完全退化成“章写完立刻 canonical”，那么 provisional 的价值会大幅下降。
---
Step 8：进入下一个 band / 下一个章节
这时后续章节默认读取的历史真值，已经是 promote 后的 canonical。
---
8. 各模块与 provisional 的关系
8.1 Arc Director
作用
定当前 arc / band 的目标与约束
在 band/arc patch 时调整未来计划
与 provisional 的关系
不直接生成 provisional 正文
但它的计划决定 provisional 的写作范围
它会消费 provisional analysis 结果，修正未来 band/arc
---
8.2 Writer
作用
逐章、逐 scene 生成正文
输出结构化候选
与 provisional 的关系
Writer 的直接产物默认先进入 provisional
Writer 不直接写 canonical
---
8.3 Review Hub
作用
对 provisional 内容做通过/修补/重写/升级判断
与 provisional 的关系
它的主要审查对象就是 provisional
它是 provisional 到 canonical 之间的主要闸门
---
8.4 State Updater
作用
负责真正的事务写入
与 provisional 的关系
在 promote 前，它不应该把 provisional 候选直接当 canonical 写入
它只在 promote 时把 provisional 确认为正式真值
---
8.5 Timeline Manager
作用
管故事内时间点和时间范围
检查时间一致性
与 provisional 的关系
可以基于 provisional timeline hints 做临时判断
但正式 timeline 只在 promote 后成立
---
8.6 Audience Feedback Layer / v2.6
作用
读取评论，形成 signal / hint / trend
与 provisional 的关系
它不属于 provisional 主流程
但可以在 band patch / next band planning 时影响：
future band
clarification placement
pacing tweak
character weight adjustment
关键边界
评论信号可以影响：
future band
reband
next active arc
不能直接因为 provisional 或评论就改：
canonical 历史
世界核心规则
核心真相
---
8.7 WNER / v2.7 Experience Layer
作用
用证据化 reviewer 判断：
reward 是否交付
问题梯子是否存在
规则可读性是否断裂
当前章/当前 band 的网文体验是否达标
与 provisional 的关系
WNER 主要也是在审 provisional
它产生的 `review_notes / repair_instruction / design_patch`
应先作用于 provisional 层
只有通过后，相关章节才 promote
关键点
v2.7 增强的是 provisional 的审查质量，不改变 provisional 的位置。
---
9. 开发阶段先后关系（不是 runtime）
这一部分说的是“代码实现先做什么”。
9.1 v2.3 阶段
先把主链跑通：
Writer
Review
State Updater
错误恢复
黑箱/检查点/共驾
多项目隔离
此时
可以先没有完整 provisional branch 管理，但至少逻辑上要保留：
candidate 不直接污染 canon
review 在 canonical 之前发生
---
9.2 v2.4 阶段
明确：
arc target / range / envelope
active arc 的 provisional 预演
resolved envelope
promote 之前的 band 级分析
这是 provisional 正式成形的阶段
---
9.3 v2.6 阶段
增强评论反馈层：
但不改变 provisional 的基本位置
只是给后续 band/arc patch 增加外部反馈信号
---
9.4 v2.7 阶段
增强 review 质量：
ReviewContextPack
evidence-anchored WNER
lint signals
merged repair patch
它对 provisional 的作用
让 provisional 的通过标准更完整、更证据化。
---
10. 代码核对时应该重点检查什么
如果你要用这份文档检查现有代码，建议按下面顺序看。
10.1 是否存在明确的 provisional / canonical 边界
检查：
Writer 输出后是否直接入正式状态
Review 是否真的发生在 canonical 写入前
State Updater 是否只在 promote 时改正式真值
10.2 promote 粒度是否合理
检查：
是不是退化成“章写完就立刻 canonical”
band 级 promote 是否有实现或至少有接口预留
10.3 patch / rewrite / replan 是否优先作用于 provisional
检查：
当前修补是否先改 provisional 对象
是否存在直接改 canonical 历史的危险路径
10.4 provisional 是否能承载 review 元数据
检查：
review_notes
repair_instruction
experience_scores
lint_signals
evidence_refs
这些是否能先附着在 provisional 阶段，而不是只能在 canonical 后保存
10.5 评论系统是否错误地直接驱动 Writer 或 canonical
检查：
原始评论是否直接喂给 Writer
confirmed signals 是否直接改写真值
Action Mapper 是否只影响 future band / planning 层
10.6 v2.7 reviewer 增强是否仍然在 provisional 闸门内
检查：
WNER / lint / continuity 是不是都作用于 provisional review
还是已经绕过 review gate 直接影响 state
---
11. 最终结论
当前设计里，`provisional` 的准确定位是：
> **它是 active arc 的近端 band 在正式 canonical 写入前的预写、预审、预修补状态层。**
它与其他部分的关系是：
`Arc Director`：决定 provisional 的作用范围
`Writer`：产出 provisional 内容
`Review Hub`：审 provisional
`State Updater`：只在 promote 时把 provisional 写成 canonical
`Audience Feedback`：影响 future band / reband，不直接改 canonical
`v2.7 WNER`：增强 provisional review，不改变 provisional 的位置
它的先后关系是：
> **active arc 激活 → band 形成 → 逐章写入 provisional → review/patch/rewrite/replan → band 分析 → promote → canonical → 下一 band**
---
12. 一句话版
如果只用一句话概括：
> **provisional 不是整书预演，也不是单章双写，而是“当前 active arc 的近端 band 在正式写入 canonical 之前的预写/预审/预修补层”；正文在 band 内逐章生成，review 在 provisional 上发生，只有 promote 后才进入正式真值。**
