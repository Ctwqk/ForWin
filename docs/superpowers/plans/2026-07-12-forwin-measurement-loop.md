# ForWin Measurement Loop 统一方案（度量回路：让数据决定喂谁、饿谁）

> **For agentic workers:** 按 Slice 顺序实施，每个 Slice 独立可交付、独立可回滚。
> 全程遵守 Global Constraints。任务使用 checkbox（`- [ ]`）跟踪。

**日期：** 2026-07-12
**性质：** 设计评审后的统一整改方案，覆盖评审提出的全部六类问题（经代码验证后拆为 P1-P8）：门禁效力账本、规则出身制度、成本与人时预算、读者回路补全、多样性遥测、认知层打点、体验层校准、运营预算治理，合为一个系统：**Measurement Loop**。
**前置状态：** v5 收敛已完成（RuntimePolicy / ChapterPipeline / CanonAdmissionService.commit_plan 唯一原子写者 / outbox）。本方案不改写作与 canon 主链，只在其事件与数据之上建立度量与治理层。

---

## 1. 要解决的问题（来自 2026-07 设计评审）

| # | 问题 | 表现 | 对应 Slice |
|---|---|---|---|
| P1 | **防御机制事故驱动累积，无效力核算**（2026-07-12 验证后修正表述） | 7-8 道门。已存在**快照式计数**：`application/project_control/support.py:614` 的 `build_audit_insights` 对最近 1000 条事件计数 forced-accept/override 原因/hard gate/checkpoint 状态——但无 per-gate fire/block/override 归一化率、无 regret（误拦/漏拦）核算、无全量历史窗口、无跨书视图、无生命周期判定 | S1（扩展 insights 为账本）+ 治理政策 |
| P2 | **规则内容过拟合到单次生成任务**（已有 git 直接证据） | `reference_classifier.py` 在 2026-07-07 一天内 14 连发 commit（04:00–11:48，"Normalize XXX character annotations/aliases"）——100 章长跑每遇一种名字形态就加一条全局词条/模式；"馆员/追踪者"等故事色彩词条至今在 global 常量中 | S2 |
| P3 | **无成本标尺** | 每章 LLM 调用横跨写作/审查/抽取/审计，无按章成本视图；新增 LLM 门无预算约束 | S3 |
| P4 | **读者回路半闭合** | 评论链路已通（raw comments → signal candidates → window aggregates → audience calibration → band 计划），但：无平台侧留存/追读数据；audience 恶化不触发任何门；回路无效果核算 | S4 |
| P5 | **千章尺度长程重复/风格漂移无防御** | trope cooldown 只管短程；跨百章的句式/结构/情绪曲线趋同（LLM 连载典型死法）无度量 | S5 |
| P6 | **认知层成本未验证** | reader cognition / false belief / reveal ladder 每章付抽取与审查成本，无证据表明被消费、改善产出 | S6 |
| P7 | **体验层静态、无学习能力**（2026-07-12 代码验证后对"体验层薄"的修正版） | 体验闭环结构完整（`ChapterExperiencePlan` 契约 → `_experience_overlay_section` 注入写作提示词 → webnovel reviewer 打分 → 体验 patch；pulp 另有 `pulp_beat` 确定性检查），但：判定阈值全是无出处常数（`review/webnovel.py` 的 hook<0.4、stall>=0.45、progress>=0.34）；跨章情绪/张弛曲线零建模（全仓无 tension/curve）；`llm_eval` harness 只有 CLI 入口，提示词改动无 A/B 纪律 | S7 |
| P8 | **无人化目标与运营面张力，人时无预算** | 生成期 7 类门已可 spark 代批；无委托路径的人工面：硬阻断章节 needs_review 批准、Obsidian proposal 审批、publisher QR/CAPTCHA/风险事件、Genesis 设计期操作。预算无定义、无统计 | S8（统计半边在 S3） |

**统一原则：** 所有 Slice 都是**从既有事件/数据派生判断力**，不新增阻断路径；一切新信号先 observe/warn，晋升为阻断必须走 S1 的生命周期制度。

---

## 2. 已核实的基础设施（实施锚点）

实施前已逐项核实（2026-07-12，HEAD @ `4b6102f`）：

| 设施 | 现状 | 位置 |
|---|---|---|
| DecisionEvent | 含 32 个 GATE/CHECKPOINT/AUDIT 事件类型；事件带 `actor_type`（human/system/worker）与 `chapter_number`/`band_id` 列（regret 关联依据） | `forwin/audit/events.py` |
| **每道门的事件覆盖（S1 前提，已逐一核对）** | hard floor→`HARD_GATE_HIT`；canon 门→`CANON_COMMIT_BLOCKED`；future audit→`FUTURE_PLAN_AUDIT_RUN`/`FUTURE_PLAN_PATCH_APPLIED`；rehearsal→`SCENARIO_REHEARSAL_EVALUATED/BLOCKED`；band checkpoint→`BAND_CHECKPOINT_HIT/APPROVED/OVERRIDDEN`；manual→`MANUAL_CHECKPOINT_HIT`；generation audit→`GENERATION_AUDIT_CHECKPOINT_REACHED`；委托→`GATE_DELEGATION_*`；残余放行→`FORCED_ACCEPT_APPLIED`；repair 链→`REPAIR_STARTED/SUCCEEDED/FAILED`。**结论：S1 无需新增事件类型，regret 的"override→同章后续 repair"关联可计算** | `forwin/audit/events.py:37-86,152` |
| 既有统计（S1 的种子，非从零起建） | `build_audit_insights`：近 1000 事件 + 近 20 checkpoint 的 forced-accept/override 原因/hard gate/checkpoint 状态计数。缺：per-gate 归一化率、regret、全量窗口、跨书、生命周期 | `forwin/application/project_control/support.py:614` |
| 规则消费收口点（S2 前提） | ActiveRule 实际存储于 `CanonQualitySignalRow`（active_rule_store 查询该表）；消费集中在 `query_active_as_of` / `apply_pre_write_active_rules` 两个入口——**status/scope 过滤只需改这一个收口点即可全局生效** | `forwin/canon_quality/active_rule_store.py:54,126`、`active_rules_handler.py:16` |
| 过拟合的 git 证据（P2 实证） | 2026-07-07 一天 14 个 "Normalize …" commit 逐条向 `reference_classifier` 加词条/模式（04:00-11:48） | `git log --follow -- forwin/checker/reference_classifier.py` |
| PromptTrace | **无 token 字段**。`attempts_json` 内含 `input_chars`/`output_chars`/`duration_ms`/`model`/`stage_key`/`task_family`（见 `TelemetryMixin._record_llm_attempt` 签名）——字符级成本代理现成，真实 token 用量需新增采集 | `forwin/models/genesis.py:32`、`forwin/writer/llm/telemetry.py` |
| ActiveRule 存储 | per-project 规则：`rule_key` + `valid_from/until_chapter` + `trigger_quote`，已是"项目级规则档案"雏形 | `forwin/canon_quality/active_rule_store.py` |
| 过拟合词表（待迁移对象） | `GENERIC_CHARACTER_REFERENCES` 等硬编码在代码常量 | `forwin/checker/reference_classifier.py` |
| 读者评论链路 | `publisher_comment_sync_jobs` → `publisher_raw_comments` → `comment_signal_candidates` → `SignalWindowAggregate`（score_v1）→ `AudienceTrendView` → `build_audience_calibration_profile` → band 计划 | `forwin/models/publisher.py:306-363`、`forwin/audience_metrics.py`、`forwin/experience/service.py:85`、`forwin/planning/band_plan_service.py:87` |
| 平台留存数据 | **不存在**（retention/追读/收藏 全仓零命中）——S4 的缺口 | — |
| 章节向量 | `ChapterMemoryIndex` 已将章节记忆嵌入 Qdrant `chapter_memories` | `forwin/retrieval/memory_index.py` |
| PlanHealth | future audit / patch validation / rehearsal 已统一汇入 | `forwin/planning/`（PlanHealthService） |
| Gate 委托 | human/spark 委托已统一（`GateDelegationService`），所有暂停门经它 | `forwin/generation/gate_delegation.py` |

---

## 3. Global Constraints

- 不修改 `CanonAdmissionService.commit_plan` 的事务语义与任何现有阻断门的判定逻辑。
- 本方案所有新信号初始均为 observe/warn；**任何新阻断必须经 S1 生命周期晋升**，本方案自身不引入新阻断。
- 不自动改稿、不自动改计划；audience/多样性信号最多触发既有 checkpoint（走 gate delegation），不直接生成 patch。
- 派生表/报表可随时重建（projection 性质），删除即回滚；不迁移旧项目数据。
- 每个 Slice 单独 commit、单独可 revert。

---

## 4. Slice 设计

### Slice 1 — GateOutcome 契约 + 门禁效力账本（解决 P1）

**目标：** 每道门用拦截记录付房租；"该不该删 rehearsal"这类问题由报表回答，不再靠争论。

**设计：**

1. 定义 `GateOutcome` 统一事件 payload 契约（新模块 `forwin/audit/gate_outcome.py`）：

```python
class GateOutcome(BaseModel):
    gate_id: str            # "hard_floor" | "canon_quality" | "future_plan_audit" | ...
    scope: str              # chapter | band | arc | project
    chapter_number: int
    fired: bool             # 是否产生发现
    blocked: bool           # 是否阻断
    overridden_by: str      # "" | "human" | "spark" | "force_accept"
    rule_keys: list[str]    # 命中的规则（关联 S2 规则档案）
    evidence_refs: list[str]
```

2. 各门在**现有 DecisionEvent 之内**附加此 payload（§2 已逐门核对事件类型齐备，不新增事件类型，只收敛 payload 结构）。涉及：hard floor（`pipeline_core/project_chapters.py`）、canon quality gate、form gate（`pipeline_core/quality_gates.py`）、future audit（`pipeline_core/audit_control.py`）、rehearsal blocking（`pipeline_core/run_control.py`）、band/manual/generation-audit checkpoint、gate delegation 结果。
3. 派生账本 `GateLedgerService`：**以现有 `build_audit_insights`（`project_control/support.py:614`）为种子扩展**，而非从零起建。现状已有 forced-accept/override/hard-gate/checkpoint 计数，需补四点：per-gate 归一化率（fire per chapter）、regret 核算、突破近 1000 事件的全量/分 band 窗口、跨书视图。扩展后 insights 端点保持兼容或由账本取代：

```text
每 gate × band 统计：
  fire_rate / block_rate / override_rate
  regret_false：阻断 → 被 override 放行 → 该章后续无 repair/retcon 记录
  regret_miss ：放行 → 该章后续因该门职责域的问题进入 repair
  llm_cost    ：该门关联 PromptTrace 的字符成本（S3 提供）
```

   regret 的判定完全来自既有事件链（override 事件 → 同章后续 repair/canon 事件），无需人工标注。
4. 报表出口：MCP 查询工具 `gate_ledger_report` + operator UI 一页（band 结束时生成）。

**任务：**
- [ ] `GateOutcome` 模型 + 契约测试
- [ ] 8 个门的 payload 接入（每门一个小 commit）
- [ ] `GateLedgerService` 派生查询 + regret 判定
- [ ] MCP 工具 + band 报表页
- [ ] 用 100 章项目的历史事件回放验证账本可计算（容忍旧事件缺 payload → 记为 unknown）

**验收：** 对任一在跑项目，一条 MCP 调用返回全部门的 fire/block/override/regret 统计；rehearsal 与 generation audit 的历史命中率首次可见。

### Slice 2 — 规则出身与作用域制度（解决 P2，过拟合的直接解）

**目标：** 任何因单次生成事故而生的规则，默认只属于那个项目；跨书生效必须凭证据晋升。

**设计：**

1. 扩展 `ActiveRule`（`canon_quality/active_rule_store.py`）。ActiveRule 的持久化载体是 `CanonQualitySignalRow`，新字段落在 payload；ActiveRule 审计行不得进入通用 open-signal 查询，运行时消费只认 `query_active_as_of` 返回的 `active` 规则：

```python
class ActiveRule(BaseModel):
    ...
    origin_event_id: str = ""        # 因哪次事故而生
    origin_project_id: str = ""
    status: Literal["observing", "active", "suspended", "retired"] = "observing"
    promotion_evidence: list[str] = []  # 跨书真阳性 event ids
```

所有 runtime `ActiveRule` 永远 project-scoped，不提供 global runtime 存储。

2. **词表迁移**：`reference_classifier.py` 的全局确定性集合只保留语言学上无歧义的泛称（"路人/守卫/众人"类）；技术 ID、职业/组织和状态形态仅产出 `genre_candidate` 特征，不得直接丢弃实体；带故事色彩的词条完全退出生产 classifier，交给项目内 Entity admission。确需保留的项目例外必须写入该项目的 observing rule，而不是另建全局词表。
3. **事故响应阶梯**写入 `Design-docs/DESIGN_STATUS.md` 作为治理条款：修成因（prompt/抽取契约）＞ 冻结 fixture 回归测试 ＞ 运行时规则（observing 起步）。
4. **换书体检**：`start-writing` handoff 时自动记录一条 DecisionEvent，列出全部非 global 规则及其状态（继承 global、其余 observing）；MCP 工具 `rule_provenance_report` 可随时列出"哪些规则是哪本书的伤疤"。

**生命周期政策（与 S1 账本联动，数字为初值可调）：**
- observing → active：≥1 次真阳性且 override_rate < 50%
- project → global recommendation：≥2 本书各有真阳性；真正 global 仍需静态代码修改、逐书 frozen fixture、owner review 与 full suite
- 连续 300 章零真阳性 或 override_rate > 70%：→ suspended，报表提示
- suspended 满一本书未复活：→ retired（删除，教训转 fixture）

**任务：**
- [x] ActiveRule 扩展字段（JSON payload；不增加 global runtime schema）
- [x] reference_classifier 词表拆分与迁移
- [x] handoff 换书体检事件 + MCP `rule_provenance_report`
- [x] 生命周期状态机 + 基于 S1 账本的自动降级建议（建议而非自动执行）
- [x] DESIGN_STATUS 治理条款

**验收：** 新书开跑时，一条命令列出"上一本书的伤疤规则，本书仅观察不阻断"；100 章项目的故事专有词条不再出现在 global 路径。

### Slice 3 — 成本账本（解决 P3；含对先前方案的事实修正）

**事实修正：** PromptTrace 无 token 字段。方案分两步：

1. **字符代理账本（零采集改动）**：从 `attempts_json` 的 `input_chars/output_chars` × `stage_key`/`task_family` 派生按章成本视图：

```text
每章：写作 : 审查 : 抽取 : 审计 : 门委托 的字符成本占比 + 调用次数 + duration
每 band：趋势 + 与 profile 预算线的对比
```

2. **真实 token 采集（小改动）**：`writer/llm/adapter.py` 在响应处理处抓 provider `usage` 字段，追加进 attempt event（`prompt_tokens/completion_tokens`，缺失时留空回退字符代理）。Codex bridge 路径同理（bridge 返回体已含 model/trace，补 usage 透传）。
3. 预算定义放入 RuntimePolicy 之外的观测配置（**不是**新 policy 字段，避免旗标回潮）：per-profile 参考线写在报表层。
4. **人时账本**：从带 `actor_type="human"` 的事件（REVIEW_APPROVED、checkpoint 处理、retry）统计每 100 章人工介入次数与分布，同一报表呈现。

**任务：**
- [ ] 字符代理成本查询 + 章/band 报表
- [ ] adapter usage 采集（含缺失回退）
- [ ] 人时统计
- [ ] MCP `cost_report` 工具

**验收：** 任一项目可回答"每章多少 token（或字符），花在哪个环节，人工介入几次"；S1 账本的 `llm_cost` 列由此填充。

### Slice 4 — 读者回路补全（解决 P4）

**事实基线：** 评论回路已闭合到 band 计划校准（见 §2 表）。本 Slice 只补三个缺口，不重建已有链路：

1. **平台侧留存类指标采集**：扩展 publisher extension 同步任务，按章采集平台可得的公开指标（阅读/收藏/追更数，因平台而异，schema 允许稀疏）；新表 `chapter_platform_metrics`（章号 × 指标 × 采集时间），outbox 消费落表。浏览器侧复用现有 comment sync job 的 claim/风险门机制，**不新增自动化风险面**。
2. **audience 恶化门（observe 起步）**：`AudienceHealthCheck` 在 band 结束时运行——留存环比跌幅、score_v1 恶化趋势超阈值 → 产生 `audience_drop` PlanHealth 信号 → 按 S1 制度先 observing；晋升后触发既有 band checkpoint（走 gate delegation，human/spark 决定）。
3. **回路效果核算**：记录"audience calibration 实际改变了 band 计划的哪些字段"（diff 事件），并在下一 band 报表中对照读者曲线变化——回答"这个回路有没有用"，为 P6 类问题建立范式。

**任务：**
- [ ] `chapter_platform_metrics` 模型 + extension 采集任务 + outbox 落表
- [ ] `AudienceHealthCheck`（observing）+ PlanHealth 接入
- [ ] calibration diff 事件 + band 对照报表
- [ ] 报表并入 S1 的 band 页

**验收：** band 报表同时呈现：计划节奏、实际读者曲线、calibration 做了什么、门是否该触发（observing 影子判定）。

### Slice 5 — 长程多样性遥测（解决 P5）

**设计（全部 warn-only，消费方是计划层不是审查层）：**

1. 每章接受后（outbox 消费者，不进 canon 事务）计算：
   - Qdrant `chapter_memories` 内与历史章节的 top-k embedding 相似度及爬升趋势；
   - 与滚动窗口（近 100 章）的 n-gram/句式复用率（simhash 量级）;
   - 场景结构指纹（writer output 结构字段免费可得）与 trope 使用直方图熵（trope registry 现有数据）。
2. 新表 `chapter_diversity_metrics`；band 报表页给出：相似度曲线、最像历史章节对 top5、高频句式 top10。
3. 消费方：超阈值时向 experience planner 注入规避约束（提示词层，如"近 20 章高频出现 X 结构，本 band 规避"），并产生 observing 状态的 `diversity_drift` PlanHealth 信号。**永不阻断、永不触发 repair**——重复是趋势不是错误，用计划纠偏。

**任务：**
- [ ] 指标计算 outbox 消费者 + `chapter_diversity_metrics`
- [ ] band 多样性报表
- [ ] experience planner 规避约束注入（带 S2 出身档案：origin=diversity telemetry）
- [ ] 100 章项目历史回放，标定初始阈值

**验收：** 对 100 章项目回放能画出多样性曲线并复现已知的重复段落；新生成 band 的报表含规避建议。

### Slice 6 — 认知层使用率打点（解决 P6）

**设计：** 只测消费，不改行为。context 组装与 reviewer 提示词构建处打点：每章有多少条 cognition/narrative 层事实被检索、多少条真正进入最终提示词、进入后与 review 结果的相关性。两周数据回答三选一：加深 / 冻结 / 降层（如 pulp 已做的 world-only）。决策由人做，本 Slice 只出数据。

**任务：**
- [ ] context/reviewer 打点（事件或 metrics 表）
- [ ] 消费率报表（按层：world/map/cognition/narrative）

**验收：** 能回答"cognition 层每章抽取 N 条、消费 M 条、M/N 与 review 结果的关系"。

### Slice 7 — 体验层校准与实验纪律（解决 P7）

**已核实的基线（2026-07-12）：** 体验闭环结构完整——`protocol/experience.py:217` 的 `ChapterExperiencePlan`（reward tags / hook_type / question_hook / immersion anchors / progress channels）→ `writer/prompt_core/sections.py:177` 注入写作提示词 → `review/webnovel.py` 按计划打分（hook/immersion/stall/reward 落地）→ `pipeline_core/repair_patches.py` 体验 patch；pulp 档走 `checker/pulp_beat.py` 确定性检查。**缺的是校准与实验，不是机制。**

**设计：**

1. **体验阈值进 S2 规则档案**：`review/webnovel.py` 与 `pulp_beat` 的判定阈值（hook<0.4、stall>=0.45、progress>=0.34、boring_setup_ratio 等）从代码常数迁为带出身档案的规则（初值=现常数，scope=global，origin=heuristic）。S4 的读者数据接入后，band 报表并列呈现"hook_score 分布 vs 该 band 读者曲线"，阈值调整从此有依据、有记录。
2. **band 级张弛曲线（observe 起步）**：`BandExperiencePlan` 增加轻量 tension 目标（枚举序列：铺垫/升压/爆点/回落 的位置分配），`experience/chapter_planner.py` 按曲线位置分配 hook_type 与 reward 类型（现在只按 reward_tags 查表，如 "emotion"→"emotional_knife"）；webnovel reviewer 已产出的 stall/immersion/hook 分数按章聚合成 band **实际**曲线，报表对照计划曲线。只报表、不阻断；是否升级为 PlanHealth 信号走 S1 晋升。
3. **提示词 A/B 纪律**：把 `llm_eval`（`cases.py`/`profiles.py` 底座现成）从 CLI 孤岛接入变更流程——writer prompt 模板 / skill 层 / experience overlay 的改动，提交前跑 eval suite 并把结果与模板版本号一起存档（PromptTrace 已有 `template_id/template_version` 字段可关联）；上线后用 S4 读者数据做后验。
4. **治理条款**：写入 DESIGN_STATUS——"一致性门冻结新增（第九道门默认拒绝，须走 S1 晋升）；体验层投入优先于一致性层，直至 S4 数据表明相反"。

**任务：**
- [ ] webnovel/pulp_beat 阈值迁入规则档案（S2 依赖）
- [ ] BandExperiencePlan tension 目标 + chapter_planner 按位分配
- [ ] band 实际曲线聚合 + 计划/实际对照报表
- [ ] llm_eval 接入提示词变更流程（最小版：一条 CI/脚本约定 + 结果存档）
- [ ] DESIGN_STATUS 治理条款

**验收：** 任一 band 报表能并列展示"计划张弛曲线 / 实际分数曲线 / 读者曲线（S4 后）"；任一次 writer prompt 改动能查到对应的 eval 结果与版本。

### Slice 8 — 运营预算治理（解决 P8）

**设计：**

1. **定预算并写入治理条款**：standard 每 100 章人工介入 ≤ N 分钟（N 由 owner 定，建议 30）、pulp ≈ 0；预算不是软目标，是功能取舍的判据。
2. **S3 人时账本按人工面分解**：needs_review 批准 / band-manual checkpoint / Obsidian proposal / publisher 风险事件 / 其他，每面统计次数与（事件间隔估算的）耗时。
3. **每个人工面预置降级路径决策表**：
   - 硬阻断 needs_review 批准：**保留人批**（canon 安全线，不委托）；但非硬阻断已有 `review_auto_retry`，其命中率进 S1 账本，覆盖不足时扩大 auto-retry 资格而非扩大人工；
   - Obsidian proposal 审批：统计使用频次，**连续一本书为零 → 冻结回写入口**（导出保留），World Studio 只读化同判据；
   - publisher QR/CAPTCHA/风险：本质人工（平台约束），只统计不优化；
   - 任何面持续超预算：二选一——扩 spark 委托范围（该 gate 走 S1 observing→active 晋升）或砍掉产生该负担的功能。
4. **验收报表**：每本书完结出"实际人时 vs 预算，超支在哪个面，建议动作"。

**任务：**
- [ ] 人工面分类的事件统计（S3 之上的分组视图）
- [ ] 预算与降级决策表写入 DESIGN_STATUS
- [ ] 书级人时报表（并入 S1 的书级复盘页）

**验收：** 跑完一本书能回答"实际花了多少人时、花在哪个面、哪个面该降级"。

---

## 5. 实施顺序与依赖

```text
S1 GateOutcome+账本 ──┬─→ S2 规则出身制度（账本供生命周期判据）──→ S7 体验阈值迁入规则档案
                      ├─→ S3 成本账本（gate 的 llm_cost 列）──→ S8 人时分面统计
                      └─→ S4/S5/S7 的信号晋升通道
S3 独立可先行（字符代理部分零依赖）
S4 → S7 的阈值校准（读者曲线是校准依据）
S5/S6 相互独立，可并行
```

建议节奏：S1+S3 先行（纯派生，风险≈0）→ S2（治过拟合）→ S4（读者数据）→ S7（体验校准，依赖 S2 档案 + S4 数据）→ S5 → S6 → S8（治理条款可随 S3 提前落地）。全部完成后，**200 章 no-hotfix 验证跑**同时成为 Measurement Loop 的首次全量实战：跑完应产出门禁绩效、成本、读者、多样性、张弛曲线、人时六份报表。

## 6. 与 v5 发布验证计划（V1-V6）的接口

发布验证计划（见 `2026-07-12-forwin-v5-final-roadmap.md` Track C）的长跑统计层**由本计划提供，不另建一次性脚本**：

- **V4 长跑矩阵的统计需求 → S1 + S3 报表**：repair attempt 分布、Canon block 类型、Spark approve/reject/failure、PlanHealth blocker 命中，全部是 S1 门禁账本的直接输出；每章 LLM 调用数/延迟/成本是 S3 成本账本的直接输出。
- **V4 要统计 token 的前置**：PromptTrace 无 token 字段（本计划 §2 已核实），S3 的 usage 采集是 V4 token 统计的必要前置。
- **长跑同时为门禁去留供数**：30/60/100 章期间 S1 账本积累的 rehearsal / generation audit / provisional 命中率，直接决定瘦身计划里"数据决策项"的档位。
- **实施顺序含义**：S1 + S3 必须在 V4 之前完成；S2 建议同批（长跑前完成换书体检，避免上一本书的伤疤规则污染验证跑）；S4-S8 不阻塞验证，可在长跑之后按数据继续。

## 7. Non-goals

- 不引入任何新的默认阻断门；不改 canon 原子写者；不自动改稿/改计划。
- 不做企业级 metrics 平台（Prometheus/Grafana 等）；报表走现有 operator UI + MCP。
- 不迁移旧项目历史数据；旧事件缺 GateOutcome payload 记为 unknown。
- 不在本方案内删除 rehearsal 或任何现有门——删除决策等 S1 账本出数（这正是本方案存在的意义）。
