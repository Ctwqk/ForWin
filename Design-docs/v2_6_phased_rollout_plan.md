# 第三版 v2.6 分阶段落地计划（Phased Rollout Plan）

## 文档目的

这份文档不是新的总架构设计，也不是替代 v2.6 的完整规范。  
它的作用是：

> **把当前已有的评论反馈层设计，收敛成一条现实可执行的分阶段实现路线。**

它回答的是：

- 当前代码库从“20 行关键词匹配”出发，下一步应该先做什么
- 哪些能力可以先做
- 哪些能力应该延后
- 当前 Phase A / B / C 是否能覆盖 v2.6 的目标
- 哪些点还需要补齐，才能算“完整覆盖 v2.6”

---

## 1. 总结论

当前的 **Phase A / B / C** 拆法是合理的。  
它比直接一步实现完整 v2.6 更符合当前代码库状态。

### 总体判断
- **Phase A**：先从“关键词匹配”升级为“多标签信号提取 + 最小结构化存储”
- **Phase B**：再加“窗口聚合 + 读者规模代理 + 冷却期”
- **Phase C**：最后加“Action Mapper + AudienceHintPack + 可选 ML pipeline”

### 功能覆盖判断
如果三阶段都完成，这条路线可以覆盖 **v2.6 的绝大多数核心能力**。

### 但如果要说“完整覆盖 v2.6”，还需要显式补齐 3 个点：
1. `signal_type` 的 schema 从 Day 1 就预留完整 6 类
2. Phase B 增加 trend 派生层
3. Phase C 明确 `score_v1` 为正式目标，而不是“以后再上”

---

## 2. v2.6 目标能力清单

为了判断分阶段路线是否覆盖 v2.6，需要先把 v2.6 的核心目标列清楚。

### v2.6 目标能力
1. 一条评论可以命中多个 `signal_key`
2. 评论反馈不是单条即动作，而是通过聚合形成 signal
3. signal 需要按窗口聚合
4. signal 阈值不能只看评论数，还要按读者规模 / 曝光阶段分层
5. signal 应该具备：
   - 去重用户
   - 读者渗透率
   - 评论占比
   - 持续性
   - 热度
   - 内部共振
   - 严重度
6. 不同类型信号有不同动作映射
7. Writer 不直接看原始评论，而看压缩后的 hint
8. Arc Director / Pacing / Reviewer 才是主要使用评论信号的模块
9. prediction 不直接驱动剧情
10. risk 可以快响应
11. 评论区反馈要与现有 provisional / arc envelope 体系兼容
12. 模型路线不应该默认走重型 LLM 主链，而应支持更轻的 NLP / 分类器演进路线

---

## 3. Phase A：LLM 替换关键词 + 最小结构化存储

## 目标
把当前“20 行关键词匹配”升级为：

- 一条评论可提取多个信号
- 信号可落库存储
- 不引入新的本地 ML 模型依赖
- 不改变现有 Writer / ContextPack 主链结构
- 保留 fallback，不破坏现有行为

---

## 3.1 这一阶段做什么

### 1. 新增一张表：`CommentSignalCandidate`
建议结构：

```text
CommentSignalCandidate
- id
- source_comment_id
- signal_key
- signal_type
- target_type
- target_name
- severity
- confidence
- evidence_span
- created_at
```

### 2. 给 `PublisherRawComment` 增加基础热度字段
```text
- like_count
- reply_count
```

### 3. 用现有 LLM 基础设施替换关键词解析
这里的准确表述应该是：

> **不引入新的本地 ML/NLP 模型依赖，复用现有 llm_client 能力。**

也就是说，这一阶段是：
- 复用现有 `llm_client.chat`
- 复用现有 `parse_llm_json`
- 不新加 embedding 模型、分类器、聚类框架

### 4. 支持一条评论命中多个 signal
这是 Phase A 必须具备的关键能力。

### 5. 保留 fallback
LLM 失败时：
- 回退到关键词匹配
- 不中断主链路

### 6. 输出仍然先走现有 `ReaderFeedbackView`
不改 `ChapterContextPack` 结构。  
只是把原来粗糙的 reader feedback，替换成更有信息量的结果。

---

## 3.2 Phase A 不做什么
- 不建 `SignalWindowStats`
- 不建 `AudienceTrend`
- 不建 `ReaderScaleTier`
- 不做评分公式
- 不做窗口聚合
- 不做 Action Mapper
- 不做 AudienceHintPack
- 不引入新的本地模型依赖

---

## 3.3 Phase A 的价值
这一步的价值不是“完成评论反馈层”，而是完成一个关键跃迁：

> **从“无结构的关键词命中”，变成“可多标签、可存储、可后续聚合的评论信号候选层”。**

如果这一步没做，Phase B / C 就没有可靠输入。

---

## 3.4 对 v2.6 的覆盖情况
Phase A 已经覆盖了这些 v2.6 能力：

- 一条评论可命中多个 `signal_key`
- `CommentSignalCandidate` 作为中间层落库
- signal_type / target_type / severity / confidence / evidence_span 进入结构化输出
- 保留 Writer 不直接读原始评论的边界
- 不让评论直接改 canon

### 结论
**Phase A 覆盖了 v2.6 的“输入层”和“最小结构化层”。**

---

## 4. Phase B：窗口聚合 + 读者规模代理 + 冷却期

## 目标
在有了 `CommentSignalCandidate` 之后，开始把评论候选信号变成真正可判定的窗口级信号。

---

## 4.1 这一阶段做什么

### 1. 新增窗口聚合表
建议：

```text
SignalWindowAggregate
- id
- project_id
- signal_key
- window_type
- window_chapter_start
- window_chapter_end
- hit_comment_count      # M
- unique_user_count      # U
- total_comment_count    # C
- reader_estimate        # R
- signal_level
- created_at
```

### 2. 新增读者规模快照表
```text
ReaderScaleSnapshot
- id
- project_id
- chapter_number
- reader_estimate
- estimation_method
- tier
- created_at
```

### 3. 引入多窗口聚合
至少三种窗口：

- short：最近 3 章
- medium：最近 8~10 章
- long：最近 20 章 或 当前 arc

### 4. 引入 reader estimate
在没有平台 API 时，先用评论量代理。  
但必须持久化 `estimation_method`，因为以后接平台真实读者数据时要能校准。

### 5. 引入 feedback cooldown
避免同一类评论信号章章触发动作。

### 6. 扩展 `ReaderFeedbackView`
让现有 Writer 链路开始能看到：
- confirmed_signals
- reader_tier
- 更像“过滤后的反馈摘要”

---

## 4.2 Phase B 仍然不做什么
- 不做完整评分公式
- 不做 Action Mapper
- 不引入 AudienceHintPack
- 不要求 Writer 改读新对象
- 不接入复杂 ML pipeline

---

## 4.3 Phase B 的价值
这一步完成的是从“单点评价”到“窗口级判断”的跃迁。

它让系统第一次具备：

- 不被单条评论带偏
- 能看跨章节持续性
- 能按读者规模看信号强弱
- 能给风险类和普通类不同处理节奏

---

## 4.4 对 v2.6 的覆盖情况
Phase B 已经覆盖了这些 v2.6 能力：

- 多窗口聚合
- 读者规模 / 曝光阶段分层
- `M / U / C / R` 核心量化基础
- cooldown
- 读者规模感知阈值的基础设施

### 结论
**Phase B 覆盖了 v2.6 的“聚合层”和“规模感知层”。**

---

## 5. Phase C：Action Mapper + AudienceHintPack + 可选 ML

## 目标
把聚合出来的 confirmed signal 真正接入系统动作层。

---

## 5.1 这一阶段做什么

### 1. 新增 `ActionMapper`
把 signal 映射成动作，例如：
- `patch_current_band`
- `reband_candidate`
- `clarification_backlog`
- `character_weight_adjustment`
- `urgent_repair_queue`

### 2. 引入 `AudienceHintPack`
到这一步，Writer 不再依赖粗糙的 `ReaderFeedbackView`，而是吃一个更明确的：
- pacing_hints
- clarity_hints
- risk_flags
- character_heat_changes

### 3. 接入 Arc Director
让 Director 能用：
- 长窗口热度趋势
- 长窗口 confusion/risk
- arc 成功与否的外部反馈

### 4. 接入 Serial Pacing Strategist
让 Pacing 模块能用：
- medium window pacing signal
- 最近 2~3 章的风险/拖沓趋势

### 5. 可选：ML pipeline 替换 LLM 评论解析
只有当评论量足够大、LLM 成本/延迟不可接受时再做：
- embedding
- 多标签分类器
- 聚类
- topic discovery

### 6. 正式上评分公式 `score_v1`
这是 v2.6 真正完整化的关键。

---

## 5.2 Phase C 的价值
这一步完成的是从“看懂评论信号”到“让系统真的吸收评论信号”的跃迁。

---

## 5.3 对 v2.6 的覆盖情况
Phase C 覆盖了这些 v2.6 能力：

- Action Mapping
- AudienceHintPack
- Director / Pacing 接入
- 可选模型升级路线
- 评分公式正式落位

### 结论
**Phase C 覆盖了 v2.6 的“动作层”和“模型演进层”。**

---

## 6. 当前分阶段路线与 v2.6 的差距

如果说“Phase A/B/C 最终能否完整覆盖 v2.6”，答案是：

> **能，但还要显式补 3 个点。**

---

## 6.1 差距 1：signal_type schema 目前只实现 4 类，不是完整 6 类
当前路线里，Phase A 实际要做的是：
- confusion
- pacing
- character_heat
- risk

但 v2.6 的完整信号集是 6 类，还包括：
- relationship_interest
- prediction

### 建议
不要等到 Phase C 再改 schema。  
从 **Day 1** 开始：
- `signal_type` 的枚举就预留 6 类
- Phase A / B 只启用前 4 类
- Phase C 再打开其余两类

这样以后不用为了扩枚举再做迁移和兼容处理。

---

## 6.2 差距 2：trend 还没有显式对象
Phase B 已经有多窗口聚合，但还没有明确的：
- `AudienceTrend`
- `score_delta`
- `rising / falling / stable`

而 v2.6 里：
- `character_heat`
- `relationship_interest`
都需要趋势判断

### 建议
至少在 Phase B 增加一个轻量 trend 派生层：

```text
AudienceTrendView
- signal_key
- previous_score
- current_score
- delta
- trend_type   # rising / falling / stable
```

可以先做成派生视图，不一定先落库。

---

## 6.3 差距 3：评分公式不能只是“以后再上”
如果要说“这条路线最终覆盖 v2.6”，就必须在文档里明确：

- Phase C 默认实现 `score_v1`
- 不是“有时间再做”
- 而是“这是正式落地的必选项”

否则就还是停留在：
- 规则系统 + reader tier 分档  
还没真正到：
- v2.6 的统一量化层

---

## 7. 推荐的正式表述

后续文档里不要写：

> “这就是 2.6 的实现方案”

而应该写：

> **“这是 v2.6 的 phased rollout plan。  
> Phase A 提供多标签信号候选层，  
> Phase B 提供窗口聚合与读者规模分层，  
> Phase C 提供动作映射、HintPack 与正式评分模型。  
> 三个阶段合起来，完整覆盖 v2.6。”**

---

## 8. 推荐的工程约束

### 8.1 Phase A 不能破坏现有行为
必须保证：
- LLM 失败时 fallback 到关键词匹配
- Writer 仍通过现有 `ReaderFeedbackView` 收到摘要
- 不引入新的主链结构依赖

### 8.2 Phase B 不要过早改 Writer 接口
优先把：
- 聚合
- 冷却
- 读者规模代理  
做稳，再改 prompt 接口。

### 8.3 Phase C 才动主动作层
不要在 Phase A / B 就急着做：
- patch_current_band
- reband_candidate
- AudienceHintPack  
否则会太重。

---

## 9. 最终判断

### 这套 Phase A / B / C 合理吗？
**合理，而且很强。**

它最大的优点是：
- 工程上现实
- 不会一下跳到 full v2.6
- 每一步都有明确中间产物
- 前一阶段的代码不会被后一阶段推翻

### 它能覆盖 v2.6 吗？
**能，但要满足下面这个说法：**

> **Phase A / B / C 是 v2.6 的分阶段落地路线，而不是 v2.6 的单次实现。**

### 现在还需要补什么？
为了确保“完整覆盖 v2.6”，请再补三点：

1. `signal_type` schema 从 Day 1 就支持完整 6 类
2. Phase B 增加 trend 派生层
3. Phase C 明确 `score_v1` 是正式目标

---

## 10. 一句话总结

**这套分段方法是有道理的，而且基本覆盖了 2.6；要确保“完整覆盖”，只需要再显式补齐三件事：六类 signal 的 schema 预留、trend 的显式建模、以及 Phase C 中 `score_v1` 的正式落位。**
