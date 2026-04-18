# 第三版 v2.3：面向几百章连载的开源长篇网文系统方案（Writer 调用策略 + 黑箱/人工双模式 + 错误恢复 + 多项目隔离）

## 0. 本版相对 v2.2 的新增重点

v2.3 在 v2.2 的基础上，补全四个之前还不够明确的工程问题：

1. **Writer 的 LLM 调用策略**
   - 一章目标字数
   - 单次生成还是分 scene 生成
   - output limit 超限怎么办
   - 基于你当前的 MiniMax M2.7 调用预算来设计默认策略

2. **人工介入点**
   - 不再只说 human-in-the-loop
   - 明确哪些节点可人工介入、怎么介入
   - 同时支持 **完全黑箱模式**

3. **错误恢复 / 重试策略**
   - WriterOutput 解析失败怎么办
   - ReviewVerdict 失败怎么办
   - State Updater 事务失败怎么办
   - 给 0.5 阶段和正式阶段都定义默认策略

4. **多项目隔离**
   - 两本书并行时，PG / Qdrant / MinIO / Redis 怎么隔离
   - 命名空间、事务隔离、缓存隔离、任务隔离怎么做

---

## 1. 仍然保持不变的总原则

v2.3 不改变 v2.2 的大方向，仍然坚持：

- 弱规划，不做全书死大纲
- 强状态账本，而不是强 planner
- 双审查分离：Continuity / Pacing
- 时间是一等公民
- Retrieval Broker 提供任务视图，而不是裸查
- Keeper 必须拆层
- agent 协议必须硬定义
- replan 必须分级与冷却

所以 v2.3 是 **补全实现细节**，不是推翻架构。

---

## 2. Writer 的 LLM 调用策略

这是 v2.3 的第一项关键补充。

---

## 2.1 先给结论

### 在你的当前条件下：
你每小时有 **300 次 MiniMax M2.7 调用额度**。

这意味着系统不必被迫走“极端省调用”的路线。  
因此我不推荐“一章只调用一次模型”的最简方案作为长期默认方案。

### 我的建议是：

#### 阶段 0.5
- **单章单次生成**
- 目标：先跑通闭环
- 每章目标长度：**1500~2200 中文字**
- 只验证：
  - 能写出来
  - 能回写状态
  - 不立即崩

#### 正式阶段（阶段 1 起）
- **分 scene 生成，再拼接**
- 每章目标长度：**2000~4000 中文字**
- 推荐每章 2~4 个 scene
- 每个 scene 单独生成
- 再做一个轻量 stitch / polish pass

这是最适合网文的平衡点。

---

## 2.2 为什么不建议长期使用“整章单次生成”

虽然整章单次生成简单，但会有这些问题：

1. 本章长度越大，单次输出越不稳
2. 中间容易丢 must_progress_points
3. 结尾 hook 容易弱
4. 同一章里多个冲突点的节奏难控制
5. output limit 或截断更麻烦
6. 出错时要整章重来，代价高

所以：

> **阶段 0.5 可以整章单次生成，长期不建议。**

---

## 2.3 推荐的正式 Writer 生成流程

### 输入
Writer 不再只吃一个 ChapterPlan，而是吃：

- ChapterPlan
- ChapterTimeline
- ChapterContextPack
- ScenePlan（可由 Writer 内部轻量生成，或由 Director/Assembler 预生成）

### 流程
#### Step 1：Scene Breakdown
把一章拆成 2~4 个 scene。

每个 scene 只包含：
- 本 scene 目标
- 必须推进点
- 当前 scene 的时间/地点
- 参与角色
- scene ending micro-hook

#### Step 2：Scene Generation
逐个 scene 生成正文。

#### Step 3：Scene Stitch / Continuity Pass
把 scenes 合并成章节草稿，并检查：
- 人称/文风一致
- 时间/地点衔接
- scene 之间逻辑连贯
- 章末 hook 是否到位

#### Step 4：Structured Extraction
从整章 draft 中抽取：
- chapter_summary
- event_candidates
- state_change_candidates
- thread_beat_candidates
- lore_candidates
- timeline_hints

---

## 2.4 推荐的章节长度与 scene 数量

### 默认网文章节目标
- **2000~3500 中文字**：默认目标
- **3500~4500 中文字**：重剧情章可允许
- **1500 以下**：只适合 MVP 或特定快节奏短章

### scene 数建议
- 2000~2500 字：2 scenes
- 2500~3500 字：3 scenes
- 3500~4500 字：4 scenes

不要一章拆太多 scene，否则调用开销和 stitch 成本都上升。

---

## 2.5 基于你的调用额度，Writer 的默认预算建议

你有 300 次 / 小时调用额度。

### 推荐的单章调用预算
#### 正常章节
- 1 次 scene breakdown
- 2~4 次 scene generation
- 1 次 stitch / polish
- 1 次 structured extraction

合计：
- **5~7 次调用 / 章**

#### 复杂章节
- 额外 1 次 rewrite / patch
- 合计 6~8 次

这意味着理论上每小时可以处理几十章级别的流水，但实际还会被延迟、上下游逻辑、review 限制。

### 结论
在你的额度下：
> **“分 scene 生成 + 轻量 stitch”是完全可承受的默认策略。**

---

## 2.6 output limit / 超长输出怎么办

这是必须明确的。

### 策略 1：场景拆分优先
首选方法不是“要求模型一次吐完整章”，而是：
- 把章拆成多个 scene
- 每个 scene 单独生成

### 策略 2：scene continuation
如果单个 scene 仍然超长：
- 允许 scene 分段续写
- scene 内最多两段 continuation

### 策略 3：stitch pass 不重写正文
stitch pass 只做：
- scene 衔接
- 轻量润色
- 小范围统一
不要让它重新大写一遍整章，否则 token 成本会上升。

### 策略 4：超限降级
如果某章预算过大：
- 降低 scene 数
- 缩小每个 scene 的描述粒度
- 允许 chapter summary 更粗
- 必要时把某个 must_progress_point 延后到下一章

---

## 2.7 WriterOutput 结构要为 scene 模式预留

v2.2 里的 WriterOutput 还不够细，v2.3 建议升级成：

```text
WriterOutput
- chapter_plan_id
- draft_blob_path
- chapter_summary
- scene_outputs
- event_candidates
- state_change_candidates
- thread_beat_candidates
- lore_candidates
- timeline_hints
- writer_notes
- generation_meta
```

其中：

```text
SceneOutput
- scene_no
- scene_objective
- scene_time_point
- scene_location_id
- involved_entities
- text_blob_path
- micro_summary
```

这样以后不管你是：
- 单章单次生成
- 分 scene 生成
都能兼容。

---

## 3. 人工介入点：支持黑箱模式和可控模式

这是 v2.3 第二项关键补充。

---

## 3.1 先给结论

系统应该同时支持三种运行模式：

### Mode A：完全黑箱模式
- 默认不需要人工参与
- 系统自动完成：
  - 规划
  - 写作
  - 审查
  - 回写
  - 重试
  - 小范围 replan

这满足你希望的：
> **完全黑箱，不需要人工参与的模式**

---

### Mode B：检查点模式
- 平时自动跑
- 只在关键节点停下来等用户确认

适合：
- 半自动创作
- 用户想掌控节奏但不想每步都介入

---

### Mode C：编辑共驾模式
- 允许用户在多个节点手动修改
- 更像“AI 创作台”

适合：
- 产品化给普通 Windows 用户用
- 用户不接受纯黑箱

---

## 3.2 建议开放的人工介入节点

### 节点 1：Writer 产出后、Reviewer 之前
用户可以：
- 直接编辑 draft
- 只编辑某个 scene
- 改章节标题
- 改章末 hook

### 节点 2：Reviewer 给出 patch / rewrite 后
用户可以：
- 接受系统修改建议
- 改写 rewrite instruction
- 选择“保留当前稿件，强行通过”

### 节点 3：Replan 触发时
用户可以：
- 接受新 plan
- 编辑新 plan
- 否决 replan，继续使用旧计划

### 节点 4：State Updater 提交前
用户可以查看：
- state_change_candidates
- event_candidates
- thread_beat_candidates
- timeline_hints

并做：
- 接受 / 拒绝 / 修改

### 节点 5：阶段总结后
用户可以：
- 修改 arc direction
- 提升/降低某条 thread 的优先级
- 决定是否开启新支线

---

## 3.3 黑箱模式如何设计

### 完全黑箱模式下：
- 默认跳过所有人工节点
- 只有在以下严重错误时才暴露人工兜底：
  - Writer 连续失败
  - Reviewer 连续无法解析
  - State Updater 连续事务失败
  - replan 超过 cooldown 仍无法收敛

也就是说：

> 黑箱模式是“人工可用但默认不打扰”，不是“系统完全没有人工接口”。

---

## 3.4 推荐的产品默认值

### 默认模式建议
- **普通用户默认：检查点模式**
- **高级用户可切到：完全黑箱模式**
- **编辑型用户可切到：共驾模式**

因为“给 Windows 用户用”的产品，如果一开始完全黑箱，很多人会不放心。

---

## 4. 错误恢复 / 重试策略

这是 v2.3 第三项关键补充。

---

## 4.1 错误类型分类

系统中最常见的错误分成四类：

### A. LLM 调用错误
- 超时
- 空响应
- provider error
- safety refusal
- 非法 JSON

### B. 协议解析错误
- WriterOutput 不合法
- ReviewVerdict 不合法
- 字段缺失
- 类型不匹配

### C. 真值写入错误
- State Updater 事务失败
- schema validation 失败
- timeline conflict 无法自动解决
- event link 不完整

### D. 编排错误
- 某节点重复执行
- 某任务状态卡住
- 并发冲突
- replan 冷却冲突

---

## 4.2 Writer 调用失败怎么办

### 默认策略
#### 第 1 次失败
- 同 prompt 原样重试 1 次

#### 第 2 次失败
- 走 repair strategy：
  - 缩短上下文
  - 去掉低优先 recall
  - 降低 scene 粒度
  - 要求更严格结构化输出

#### 第 3 次仍失败
- 标记任务为 `writer_needs_attention`
- 黑箱模式下进入自动降级方案
- 非黑箱模式下抛给用户

---

## 4.3 WriterOutput 解析失败怎么办

### 推荐策略
1. 尝试 strict parse  
2. 失败后走 repair parse  
3. repair parse 失败，再请求模型只重发结构化部分  
4. 仍失败，则整次 Writer task 重试最多 2 次  
5. 仍失败，进入人工兜底或黑箱降级

### 黑箱降级
黑箱模式下允许：
- 暂时只保留正文 blob
- 用一个 lightweight extractor 再抽 summary / events / states

也就是：
> 正文生成成功但结构化输出失败，不必整章废掉。

---

## 4.4 ReviewVerdict 解析失败怎么办

默认：
1. 严格 parse
2. repair parse
3. 让 Reviewer 只重发 verdict 和 issues
4. 连续失败 2 次后：
   - 降级成 minimal rule-based continuity checks
   - 或进入人工审查

这样 Reviewer 不会成为整个系统的单点崩溃源。

---

## 4.5 State Updater 事务失败怎么办

### 必须有事务边界
State Updater 写入时必须做到：

- 先验证
- 再开事务
- 写入 event / state / relation / thread / timeline
- 任一步失败即 rollback

### 失败后的默认动作
1. 记录失败原因
2. 把本次 WriterOutput 和 ReviewVerdict 冻结为 artifact
3. 不污染 canon
4. 允许：
   - 自动重试 1 次
   - 或要求人工 review candidate

### 关键原则
> **candidate 永远可以失败，canon 绝不能半写成功。**

---

## 4.6 阶段 0.5 的简化策略

在阶段 0.5，可以简单采用：

- Writer 失败：重试最多 2 次
- Review 失败：降级成最简规则检查
- Updater 失败：rollback + 人工兜底

先跑通闭环，再做更精细的错误恢复。

---

## 5. 多项目隔离

这是 v2.3 第四项关键补充。

---

## 5.1 为什么要提前设计

如果用户同时写两本书：

- 不能让 A 书的 Qdrant recall 混进 B 书
- 不能让 MinIO 的 artifact 路径冲突
- 不能让 Redis cache 相互污染
- 不能让 Writer / Reviewer 拿错 project 的 active plan

所以多项目隔离必须尽早做，不然以后重构非常痛。

---

## 5.2 总原则

### 所有运行时和存储对象都必须有：
- `project_id`
- 必要时有 `workspace_id`
- 统一命名空间

---

## 5.3 PostgreSQL 隔离

PostgreSQL 里所有核心表都带：
- `project_id`

并且：
- 查询默认必须 project-scoped
- 不允许无 project_id 的跨项目默认查询

建议：
- repository 层统一要求 project_id 入参
- SQLAlchemy / service 层对 project_id 做 guard

---

## 5.4 Qdrant 隔离

推荐两种方式：

### 方案 A：共享 collection + project_id payload 过滤
适合：
- 项目多
- 运维简单
- collection 数量不爆炸

例如：
- `chapter_memories`
- payload 里带 `project_id`

查询时必须 filter：
- `project_id == X`

### 方案 B：每项目独立 collection
适合：
- 项目很少
- 希望物理隔离更强

我更推荐：
> **阶段 1 / 2 先用共享 collection + project_id filter**
这样更简单。

---

## 5.5 MinIO 隔离

路径必须 namespace 化：

```text
projects/{project_id}/chapters/{chapter_id}/drafts/v{version}.json
projects/{project_id}/chapters/{chapter_id}/raw/llm_response_v{version}.json
projects/{project_id}/plans/arc/{plan_version}.json
projects/{project_id}/exports/{timestamp}.md
```

如果以后支持多用户/多 workspace：

```text
workspaces/{workspace_id}/projects/{project_id}/...
```

---

## 5.6 Redis 隔离

Redis key 统一命名：

```text
novel:{project_id}:orchestrator:task:{task_id}
novel:{project_id}:lock:chapter:{chapter_id}
novel:{project_id}:cache:context:{chapter_id}
novel:{project_id}:pacing:window:{window_id}
```

### 关键点
- key 必须 project-scoped
- lock 也必须 project-scoped
- 不允许通用 `context:chapter:12` 这种没有命名空间的 key

---

## 5.7 任务隔离

Orchestrator 的任务对象也必须带：

```text
Task
- task_id
- project_id
- task_type
- chapter_id_nullable
- arc_id_nullable
- status
- retry_count
- created_at
```

这样多本书并行写作时，任务状态不会串。

---

## 6. v2.3 推荐的默认运行模式

结合你的额度和产品目标，我建议：

---

## 6.1 默认 Writer 策略
### 阶段 0.5
- 单章单次生成
- 1500~2200 中文字

### 正式阶段
- 分 scene 生成
- 每章 2000~3500 中文字
- 默认 2~4 scenes
- 5~7 次调用 / 章

这对你现在的 300 次 / 小时额度是可承受的。

---

## 6.2 默认人工模式
### 默认：
- 检查点模式

### 可选：
- 完全黑箱模式
- 共驾编辑模式

---

## 6.3 默认错误恢复
- Writer：最多 2 次重试 + 1 次 repair
- Reviewer：最多 2 次 repair / fallback
- Updater：事务失败 rollback + 冻结 artifact

---

## 6.4 默认项目隔离
- PG：project_id
- Qdrant：共享 collection + project filter
- MinIO：project path namespace
- Redis：project-scoped keys

---

## 7. 实施顺序（v2.3 版）

### 阶段 0.5
- 最简 Writer
- 最简 State Repository + State Updater
- 最简 Context Assembler
- 最简 continuity rule checks
- 单章单次生成
- 不要求人工参与，但保留人工兜底口

### 阶段 1
- 加 Arc Director
- 加 typed state schema
- 硬定义协议对象
- 前 10 章闭环
- 项目隔离全打通

### 阶段 2
- 加 Retrieval Broker
- 加 Qdrant
- 加 MinIO
- ContextPack token budget
- 分 scene 生成

### 阶段 3
- 加 Pacing Strategist
- 加 patch/reband/rearc
- 加 cooldown
- 加时间驱动的阶段分析

### 阶段 4
- 加 NPC Intent Generator / World Simulator
- 加平台接入
- 加黑箱模式优化

---

## 8. v2.3 最终结论

第三版 v2.3 在 v2.2 的基础上补齐了四个实际工程必须回答的问题：

1. **Writer 怎么调模型**
2. **人在哪些地方能介入，同时如何支持完全黑箱**
3. **失败了怎么恢复**
4. **多本书同时运行怎么隔离**

如果只用一句话概括 v2.3：

> **第三版 v2.3 是一个“阶段导演 + 拆层状态系统 + 类型化协议 + 受预算控制的上下文组装 + 分 scene 写作 + 可切换黑箱/人工模式 + 可恢复执行 + 多项目隔离”的长篇连载系统。**
