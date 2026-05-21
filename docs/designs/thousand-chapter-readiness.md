# Thousand-Chapter Readiness Gap Analysis

## 背景

目标：稳定生成 1000 章以上、面向下沉市场的网文连载。

本文综合三方评审：

- **GPT Pro 外部评审**：从产品/架构视角列出 8 项差距
- **代码核查（Claude）**：逐条对照源码验证，并修正部分判断
- **Codex 二次评审**：补充被前两方漏掉的事实，校正优先级

三方在大方向上一致：现在系统不是"能不能生成"的问题，而是"能不能在千章规模上稳定证明低成本、不漂移、可运营"的问题。

---

## 1. 已验证的代码事实

| 主张 | 文件:行号 | 状态 |
|---|---|---|
| `pulp` profile 真实接入 | `forwin/config.py:607-636` | 属实 |
| `pulp_pressure_test.py` 是 placeholder | `scripts/pulp_pressure_test.py:71-75,92` | 属实，只生成空指标行 |
| `hard_floor.py` 只查 6 项基础底线 | `forwin/checker/hard_floor.py:46-72` | 无 payoff/反应/收益验收 |
| hard floor fail 后 `continue` 下一章 | `forwin/orchestrator_loop_core/project_chapters.py:294-318` | 同一 run 内不停 |
| memory upsert 失败 `raise` 让章节失败 | `forwin/orchestrator_loop_core/project_chapters.py:561` | 由 `:778` 兜底标 failed |
| `_extract_structured_part` fallback 截断到 1800 字 | `forwin/writer/chapter_writer.py:802` | 章末 payoff 易丢 |
| `_pick_memories` 默认 `max_memories=3` | `forwin/retrieval/broker_core/broker.py:85` | 单一 query，不分类型 |
| `_create_continue_generation_task` 是 daemon thread | `forwin/api_core/generation.py:461,524` | 进程死后无人续 |
| `TropeTemplate` 已有 `desire_setup/payoff/aftermath` 等字段 | `forwin/protocol/trope_library.py:15-38` | 字段就位，缺验收 |
| Trope 注入 writer prompt（"本章爽点指令"） | `forwin/writer/prompt_core/sections.py:141-170` | 属实 |
| **`target_total_chapters` 默认值 3** | `forwin/models/project.py:20` | **生产级脚手架问题** |
| **创建上限 200 章** | `forwin/api_schema/project.py:217` | **千章不可能在一个 contract 内** |
| **extend 单次上限 100 章** | `forwin/api_schema/project.py:257` | 到 1000 至少需要 8 次 extend |
| 跨 band trope 去重不存在 | `forwin/experience/band_scheduler.py:43` | `used_template_ids` 是局部变量 |
| 重启不会 resume，只标 failed/paused | `forwin/api_core/tasks.py:595-641` | 任务状态在 DB 但执行不接续 |
| auto-continue 看到 `failed_chapters` 会停 | `forwin/generation/auto_continue.py:196-197` | 跨 run 阻断生效，同 run 内不阻断 |
| 无 Saga/Volume 层 | `forwin/models/project.py` | Arc 是当前最大执行单元 |

---

## 2. 三方共识：核心差距

按重要性排序：

### 2.1 千章 long-run contract 缺失（最根本）

整条链路按短篇设计：

- schema 默认 `target_total_chapters=3`
- 创建上限 200，extend 单次上限 100
- auto-continue 看到 `accepted_max >= target_total_chapters` 就 `target_total_reached` 停掉
- 没有"目标 1000 章 / 每批 N 章 / 当前 M 章"这一类一等公民对象

后果：1000 章项目必须人工管理多次 extend，没有协议保证全局连续性。

### 2.2 爽点没有硬验收

`hard_floor.py` 只检查"能不能交付"，不检查"爽不爽"。`TropeTemplate` 的 `visible_payoff / aftermath / anti_patterns` 等字段已经存在并注入 prompt，但**没有一个 deterministic 验收回路**确认每章真的兑现了。

下沉爽文的核心 KPI（压迫/出手/反转/收益/惩罚/围观/钩子）目前完全靠 LLM 自觉。

### 2.3 量产稳定性无真实压测证明

`pulp_pressure_test.py` 是 placeholder，所有指标字段都是 `None`，README 自承"future versions can replace placeholder rows with live ForWin generation metrics"。

没有这层，下面所有改动都没有 A/B 基线。

### 2.4 失败处理对连续性危险

- hard floor fail → 当前章 failed → `continue` 下一章。**第 58 章可能基于第 57 章断裂上下文继续写**。
- memory index upsert 失败 → `raise` → 整章 failed。Qdrant 抖动会让全线停摆。
- 重启时未完成任务被标 failed，不重启执行。

### 2.5 长程记忆不分桶

`_pick_memories` 用单一 query search、limit=3。1000 章下面会漏掉"低频但关键"的仇恨、承诺、道具、旧敌。但**仓库已有 `narrative_obligations` 和 BookState 派生设施**，不需要从零造 ledger。

### 2.6 durable resume 不存在

进程死后任务不会接续。但仓库已有：

- `generation_tasks` 表带状态字段
- `publishers` 表已用 `last_heartbeat_at + claimed_at` 跑租约模式（`forwin/models/publisher.py:24,131`）

把同一套抬到 `generation_tasks` 即可，**不需要引入 Kafka 或新基础设施**。

### 2.7 抽取窗口对章末事实不友好

`_extract_structured_part` 失败时截取 `chapter_body[:1800]`，再失败降级为空 metadata。爽文 payoff 往往在章末——打脸结果、战利品、身份变化、敌人损失会被静默丢失。

### 2.8 无跨 band trope 冷却

`band_scheduler.py` 的 `used_template_ids` 是单次调用的局部变量。1000 章 ≈ 100-200 个 band，188 个模板会被反复使用。

---

## 3. 三方意见的校正

### 3.1 GPT Pro 的方向错误

**主张引入 Saga/Volume 层。** 经核：

- Arc 本身跨度 30-100 章（`arc_envelope_resolver.py:114,348` band 钳在 [4,12]），1000 章就是 10-30 个 arc
- Arc 已经是"主角进阶单元"，加 Volume 等于建第二套坐标系
- GPT Pro 想要的 `status_ladder / wealth_ladder / enemy_tier` 应该作为**结构化字段加到 `ArcPlanVersion`**，加上 `Project` 上的全局规则表
- 不引入新概念

**主张"从零建 promise/wealth/enemy ledger"。** 经核：

- `forwin/narrative_obligations/` 已存在
- `forwin/book_state/` 已有 `compiler/projection/query_interface`
- 正确做法是扩展现有体系（加 hard floor 维度 + BookState projection），不是新建表

**漏掉 200/100 创建上限。** 这是 Codex 抓到的。

### 3.2 GPT Pro 的方向正确但优先级偏

把"补 PulpBeatVerifier"和"补 Saga 层"列在同一优先级。实际上 verifier 是 P1，Saga/ledger 是 P3。**没有 KPI 之前先做大架构是反模式**。

### 3.3 我（Claude）最初的偏差

- 跟着 GPT Pro 的"Saga"命名走了一步。修正：不引入新概念
- 把 recovery 描述成"任务丢了"。Codex 更准：任务状态在 DB，但只是被标 failed，**resume 路径不存在**——这影响修复方案的工作量评估（加 3-4 列即可，不是从零）

### 3.4 Codex 的补充

- 创建上限 200 章 / extend 上限 100 章（关键事实，前两方都漏）
- recovery 表述精化（任务落库但不接续）
- 失败 cascade 更准（同 run 内 hard-floor fail 才会 continue；跨 run 时 auto-continue 看 `failed_chapters` 已停）

---

## 4. 已定调的设计决策

| 决策 | 原因 |
|---|---|
| **不引入 Saga/Volume 新层** | Arc 已是这个粒度，加新层会让 `ArcEnvelopeResolver/BandPlanService/future_plan_auditor` 背两套坐标系 |
| **不引入 Kafka** | Kafka 不解决长时任务 resume；现有 `generation_tasks` 表 + 租约模式（已在 publisher 用）就够 |
| **不从零建 ledger** | `narrative_obligations` 和 BookState projection 已是雏形，扩展即可 |
| **抽取改三窗口（头/中/尾）** | 章末 payoff 不再被截断；全失败时入 deferred 队列 |
| **失败处理分级** | fatal chapter fail → 停 run；index/抽取/观测失败 → deferred task，章节先 accept |

---

## 5. 优先级与工作量估算

### P0：本周内能做的封堵（每项 1-3 天）

| # | 改动 | 文件 | 工作量 |
|---|---|---|---|
| 1 | `target_total_chapters` 默认 3 → 删默认 / 提到 50 | `forwin/models/project.py:20` | 半天 + 迁移 |
| 2 | 创建上限 200 → 5000 或去掉；extend 100 → 500 或去掉 | `forwin/api_schema/project.py:217,257` | 1 行 |
| 3 | 引入 `LongRunGenerationContract`（轻量：project 加 `contract_target_chapters / contract_batch_size / contract_status` 三列）；auto-continue 看 contract | `forwin/models/project.py`, `forwin/generation/auto_continue.py` | 2-3 天 |
| 4 | memory upsert 失败 → deferred 队列，不 raise | `forwin/orchestrator_loop_core/project_chapters.py:561` | 1 天（含 deferred 任务类型） |
| 5 | hard floor fail → 当前 run break，不 continue 下一章 | `forwin/orchestrator_loop_core/project_chapters.py:318` | 半天 |
| 6 | `pulp_pressure_test.py` 改成真实采集器（回放历史 task / decision_event / chapter_plan / performance_span / candidate_draft / prompt_trace） | `scripts/pulp_pressure_test.py` | 2-3 天 |

P0 完成后即可证明"30 章稳定"。

### P1：2-4 周（先证明，再扩面）

| # | 改动 | 备注 |
|---|---|---|
| 7 | 最小 `PulpBeatVerifier`（词典+规则起步）：pressure/action/payoff/reaction/gain/punishment/audience/hook 七项 | 复用 `narrative_obligations` 的"是否兑现"，不另起表 |
| 8 | 抽取改三窗口（头 1200 / 中 1200 / 尾 1600），全失败入 `deferred_extraction_task` | `forwin/writer/chapter_writer.py:689-823` |
| 9 | 跑 100 章真实压测，建 KPI 基线（LLM calls/章、prompt slope、reward gap p95、payoff missing rate） | 用 #6 的采集器 |

### P2：准备无人值守前（1-2 月）

| # | 改动 | 备注 |
|---|---|---|
| 10 | `generation_tasks` 加 `lease_owner / lease_expires_at / heartbeat_at / last_completed_chapter` 四列；worker 跑 Postgres `SKIP LOCKED` claim；进程死 5 分钟租约过期自动接管 | 参考 `forwin/models/publisher.py:24,131` 现有模式 |
| 11 | 跨 band trope 冷却：`used_template_ids` 持久化到 BookState，按"最近 N band 不重用 + 同 category 至少跳 K"硬约束 | `forwin/experience/band_scheduler.py:43` |
| 12 | `RetrievalBroker` 改按类型预算：promise / enemy / wealth / status / recent 各自 quota 从 BookState 取 | `forwin/retrieval/broker_core/broker.py:85,813-833` |
| 13 | 跑 300 章压测，验证 context slope、角色漂移、重复套路 | |

### P3：千章质量问题（不是证明问题）

| # | 改动 | 备注 |
|---|---|---|
| 14 | `ArcPlanVersion` 加结构化字段：`status_promise / wealth_tier_from-to / enemy_tier_from-to / market_space / ladder_rung_target` | 把 GPT Pro 的 Saga 字段并回 Arc |
| 15 | `ProjectProgressionRule` 表：`rule_type / chapter_threshold / payload_json`，trope scheduler 选模板时过滤 | 全局禁令机制 |
| 16 | `BookState` 派生 `protagonist_macro_status` projection：从已接受章节的 state_change 和 narrative_obligation 派生 wealth/enemy/status tier | 纯派生，不存新表 |
| 17 | `future_plan_auditor` 加 arc 边界审计：arc 末章实际 tier 必须达到 `to`，否则阻断 | `forwin/planning/future_plan_auditor.py` |
| 18 | 跑 1000 章 dry run，验证状态机、任务恢复、成本曲线 | |

### P4（GPT Pro 和 Codex 都没列）

| # | 改动 | 备注 |
|---|---|---|
| 19 | 发布反馈回路：追读率/章评/留存信号回流到 `BandExperienceScheduler` 的 calibration | `forwin/publishers/` 和 `publisher_runtime/` 已有发布基础 |

---

## 6. 关键指标定义（供压测使用）

| 指标 | 含义 | 目标 |
|---|---|---|
| `avg_llm_calls_per_chapter` | 平均每章 LLM 调用次数 | ≤ 3（pulp 模式） |
| `prompt_char_count_slope` | prompt 字符数随章节的线性增长率 | ≈ 0 |
| `context_pack_char_count_slope` | context pack 字符数增长率 | ≈ 0 |
| `reward_gap_p95` | 距上次 reward beat 的章节数 p95 | ≤ 2 |
| `visible_payoff_missing_rate` | 缺爽点章节占比 | < 15% |
| `hard_floor_fail_rate` | hard floor 失败章节占比 | < 5% |
| `repeat_trope_similarity_p95` | 重复套路相似度 p95 | < 0.6 |
| `canon_extraction_failure_rate` | 结构化抽取失败率 | < 2% |
| `task_resume_success_rate` | 进程重启后任务接续成功率 | > 99%（P2 后） |
| `wall_time_per_chapter_p95` | 单章端到端耗时 p95 | TBD by load test |

---

## 7. 风险与反模式提醒

1. **不要先做大架构（Saga/ledger/durable runner）再做证明**。没有 KPI 闭环，再漂亮的架构都是负担。
2. **不要在没有 contract 概念前去刷"千章"指标**。`target_total_chapters` 默认 3 + 上限 200 的组合会让你以为系统能跑 1000 章，实际是堆 8 个 200 章短篇。
3. **不要把 memory/index 失败和 chapter 失败绑定**。可恢复后台任务不应污染章节状态机。
4. **不要在没有真实指标采集前升级 verifier 复杂度**。先词典 + 规则，证明可行再上 LLM evaluator。
5. **不要为 long-run resume 引入 Kafka/Celery/Temporal**。Postgres 租约（你们 publisher 已经在用）量级匹配、零新基础设施、200 行代码搞定。

---

## 8. 决策日志

| 决策 | 提出方 | 状态 |
|---|---|---|
| 引入 Saga/Volume 新层 | GPT Pro | **否决**（Arc 已是此粒度） |
| 用 Kafka 解决 resume | 用户提问 | **否决**（DB 租约更贴场景） |
| 抽取改三窗口 | GPT Pro | **采纳** |
| 复用 `narrative_obligations` 做 ledger | Claude | **采纳** |
| `target_total_chapters` 默认/上限改造 | Claude + Codex | **采纳**（P0） |
| 先做真实压测，再做 verifier | Codex | **采纳**（P0 → P1） |
| `LongRunGenerationContract` 一等公民对象 | Claude（基于 Codex 200 章发现） | **待评审** |
