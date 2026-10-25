# Feature: 世界引擎大修——让世界驱动剧情（破坏性）

日期：2026-10-20；2026-10-25 更新实施状态。状态：已批准，P0–P4（P4a + 审计 follow-up + P4b）代码已实现。生产部署由独立 Task 14 runbook 执行，P5 真实生成验收尚未执行。

本文保留批准时的设计目标、实施分期及历史问题清单，不能把这些历史问题当作当前缺口。当前代码边界与实施取舍以 [CURRENT_ARCHITECTURE](../../Design-docs/CURRENT_ARCHITECTURE.md#p4b-已落地)、[CURRENT_DESIGN](../../Design-docs/CURRENT_DESIGN.md) 和 [DESIGN_STATUS](../../Design-docs/DESIGN_STATUS.md#post-deploy-cleanup) 为准。源码核查起点分支已合入 master（`f81d542`），随后实施了本次大修。

实施取舍：正文残留冲突使用 `chapter_extraction`，提供 `rewrite_chapter` / 人工 `amend_plan`，从不 auto；最新章重写退役重跑 tick，失效 open conflicts 标为 `expired`。`world_variable` / `world.drift`、五种 `checkable` 谓词与幕后新 minor actors 已实现；物品类谓词与 soft protection 未实现。task_contract_json 和 `narrative_constraints` 已删除，抽取输入已按 ChapterWorldView 收窄，按章成本 / 自主性报告已落地。部署后兼容清理与 P5 调参见上述状态文档。

## Goal

### 意图

ForWin 要用一个世界框架降低长篇创作的难度。世界模型是背后的框架，应当尽量大而周密，在有所限制的同时保留充分的自由度：世界里的行动者按规则自我演化，剧情规划从世界已经发生和正在酝酿的事情里取材，而不是每一章都凭空发明。账本（BookState）是这一切的地基：世界依据的状态必须完整、可按章节回放，并且与正文一致。

两者都要，但优先级是“世界推动剧情”；账本补全作为地基一起完成。

### 设计起点的问题（历史，清单见附录 A）

- `WorldContractPlanningService` 和 v4 抽取链路写死了一个“母星被围 / 殖民地”样例剧本；另一套“记忆重置 / 档案清理 / 倒计时 / 父亲留下的后门 / 盟友被羁押”剧本散落在倒计时与羁押审计、续写节拍、名字锚点、质量检查、Genesis fallback 和 writer prompt 示例中。
- 认知层（谁知道什么）不进入写作；正文不会回写关系、地图和认知；故事时间只是一个字符串标签；只有 `state` 带时间版本，`profile` 是最新值覆盖。
- start-writing 不把 Genesis 的角色、势力、关系、秘密写入 BookState；第 0 章角色由 `plan_subworld_delta` 再调一次 LLM 重新编造，输入不含 Genesis 的 core_cast。
- `WorldSimulator` 只根据剧情线停滞和读者评论算一个压力值，不读取世界节点、地图或势力，也不写 BookState。
- `ReplanGovernor` 在两条以上剧情线停滞时直接 rearc，并生成缺少 arc_number 和章节范围的 `ArcPlanVersion`；`macro_progression_json` 从无写入，宏观进度审计永不触发。
- 剧情规划几乎不读世界：每卷一次性生成全部章节目标，卷结构草案不看世界状态，band 规划完全确定性。

### 成功标准（用户确定）

1. 生成一部 100 章的小说，没有逻辑硬伤。
2. 有新意。
3. 最重要：整个过程中不把写作内容过拟合进系统。运行代码里不得出现任何具体故事的内容（反例：“母星被围”）。

### Non-goals

- 不改动 publisher、浏览器扩展与发布调度。
- 不改动生成任务租约、Canon 原子提交的事务模型、五层 review 展示契约。
- 不做数值化的战斗、经济模拟引擎（这类引擎天然绑定题材，违反防过拟合原则）。
- 不迁移任何旧项目数据。
- 不为多租户、RBAC、计费做任何工作。
- 不新增 World Studio SPA 功能；只适配被删除或改变的接口。

### Anti-goals（技术上能跑，但算失败）

- 任何样例剧本、角色名、地名、题材专有词进入 `forwin/` 运行代码的决策逻辑。
- 世界演化不受约束、把大纲冲垮；或者反过来，演化被锁死成摆设。
- writer 在写作时看到不该看到的隐藏真相。
- 冲突全部堆给人工，生成无法自主推进。
- BookState 之外继续维护平行的世界真相（倒计时账本、羁押状态账本、角色状态账本等）。

## Scope

包含：

1. BookState v6：账本补全（故事时间、全字段时间版本化、新节点类型、认知写入、关系与地图回写）。
2. Genesis：`bootstrap` 阶段改为 `world_seed`；start-writing 用 Genesis 种出初始世界。
3. 世界引擎：新包 `forwin/world_engine/`。
4. 剧情规划：卷节拍表、band 规划器、可行性审计、世界简报。
5. 写作与回写：POV 世界视图、章节增量抽取、世界审查门、review 栈调整。
6. 世界与计划冲突的审查机制。
7. 防过拟合护栏，以及两套泄漏剧本的清除。
8. 删除清单（附录 B）。
9. 数据库迁移、清库、部署、验收运行。

不包含：publisher 与发布调度；World Studio 新功能；读者留存等 S4–S8 度量。

## Design

### 0. 总体结构

```text
Genesis：brief → world → map → story_engine → book_blueprint → world_seed
   │ start-writing：种子 GraphDelta（第 0 章）+ 卷 1 节拍表
   ▼
BookState v6（world / map / cognition / narrative + StoryClock + lexicon）
   │                                          ▲
   │ 世界简报                                  │ GraphDelta（CAS，经审查门）
   ▼                                          │
卷节拍表 → band 规划器 → ChapterPlan            ├── 世界引擎 tick（每章入 canon 后）
   │                                          │     结算到点意图 / 消息传播 / 行动者回合
   ▼                                          │
POV 世界视图 → Writer（场景）→ 章节增量抽取 → 世界审查门 → draft review / repair → Canon 提交
                                                │
                    冲突（世界 vs 计划）→ auto / Codex 审稿代理 / 用户
```

### 1. BookState v6：账本补全

#### 1.1 保留

- 类型化属性图的四层：world、map、cognition、narrative。
- 只追加的 `GraphDelta` 及其 patch，`old_value` 作为前置条件做 compare-and-set。
- 每次提交写全量快照；按“最近快照 + 重放后续 delta”查询任意章节的状态。
- `CanonAdmissionService` 是唯一的 canon 写入口；`BookStateCompiler` 是唯一的 delta 编译器。
- 历史重写机制（`canon/historical_rewrite.py`）。

#### 1.2 四层永远全开

删除 `RuntimePolicy.canon.book_state_layers` 和 `_filter_graph_delta_layers`。pulp 与 standard 共用同一套世界模型，成本差异只通过 §8 的旋钮体现。

#### 1.3 StoryClock

- 每本书一份日历：新表 `story_calendars(project_id, epoch_label, opening_minute, units_json)`。`units_json` 形如 `[{"name": "时辰", "minutes": 120}, {"name": "日", "minutes": 1440}]`，单位名称由 world_seed 按题材生成。代码只内置“分钟”这一个基准单位，不内置任何单位名。
- 故事时间 = 自纪元起的整数分钟（bigint）。`graph_deltas` 新增 `story_time_start`、`story_time_end`；章节同样记录本章起止时间。
- 显示标签由日历格式化（`format_story_time(calendar, minutes)`），不再把 LLM 写的自由文本当作真值。
- 抽取输出时长用 `{amount, unit_name}`，`unit_name` 必须属于本书日历，换算由代码完成。
- 规则：本章开始时间 ≥ 上一章结束时间。闪回只能创建发生于过去的 fact 与知识事件，不能修改当前 state。

#### 1.4 全字段时间版本化

- `profile`、`name`、`aliases` 的变更与 `state` 一样经 patch 写入、可按章节回放；`world_nodes` 行只作为“最新值”投影。
- narrative 层纳入快照，加载时不再全量重放日志。
- 快照里的 promise 统计以 narrative `promise` 节点为准（修正现在统计 `reader_promise` world 节点、几乎恒为空的问题）。

#### 1.5 新节点类型与字段（schema 白名单同步更新）

- `agenda`（行动者议程）：`owner_ref`、`goal`、`motivation`、`means_refs`、`constraints`、`priority`(1–5)、`secrecy_refs`（知情者）、`status`(active / achieved / abandoned / failed)、`horizon`。
- `intent`（意图）：`owner_ref`、`agenda_ref`、`action_type`、`summary`、`target_refs`、`location_ref`、`scheduled_at`（故事时间）、`preconditions`、`expected_effects`（结构化 patch 预览）、`impact_level`、`requires_onstage`(bool)、`slip_count`、`status`(planned / due / resolved / disrupted / vetoed / held)。`action_type` 是题材中立的枚举：move、confront、negotiate、ally、betray、acquire、transfer、investigate、spread_info、deceive、build、recruit、hide、train、decree、other。
- `clock`（时限）：`label`、`due_at`、`owner_ref`、`scope`(public / secret)、`on_expire`（效果描述或意图引用）。它取代现在的 countdown ledger 和所有倒计时特判：剩余时间 = `due_at - now`，由代码确定性计算。
- 角色 state 规范字段：`alive_status`(alive / dead / unknown)、`location_ref`、`restraint`（free / restrained / imprisoned 这类泛化状态，加 `by_ref`、`since`）。`restraint` 取代现在的羁押专用词表。
- 行动者属性：character、faction、organization、group 节点的 state 增加 `actor_tier`(major / minor / background) 和 `next_turn_at`。
- `rule` 节点：`hardness`(hard / soft)、`statement`，以及可选的 `checkable`（可由代码检查的结构化谓词）。
- 每本书一份词表：新表 `book_lexicons`，随 world_seed 生成，包含时间单位别名（如“一炷香”= 30 分钟）、有序的等级体系、货币、称谓、出行方式。确定性检查只能使用这里的词，代码里不得写题材词。
- 出行方式（题材中立）：lexicon 中的 `travel_modes[{id, name, speed_multiplier, allowed_edge_types}]`，名称由题材决定（马、飞剑、穿梭机……），代码只认 id 和数值。角色 state 增加 `travel_mode_ids`。瞬移类能力用地图上的 portal / world_gate 边表达，不作为出行方式。地图边的 `travel_time` 仍是生成器给出的客观小时数，加载时换算为分钟；某次移动的最短耗时 = 最短路线耗时 ÷ 移动者可用出行方式中的最大 `speed_multiplier`（该方式必须允许路线上的边类型）。

#### 1.6 认知层

- 知识事件是认知层唯一的写入方式：`{observer_ref, fact_ref, state, channel, source_ref, at}`。
  - `state`：heard / suspects / believes / knows / disbelieves / misled。
  - `channel`：witnessed / told / rumor / document / inferred / seeded。
  - 翻译为 cognition patch，并写入 `evidence_by_ref`。
- 虚假信念 = 观察者对一个 `truth_value=false` 的 fact 处于 believes 或 knows，或者存在 `field_override`。
- `can_see` 的默认规则改为：sensitivity 为 hidden / secret 的事实默认不可见，公开事实默认可见（修正现在“默认全部可见”）。
- `reader` 是一个观察者，读者认知只由正文中的揭示推进（抽取出的 `observer=reader` 的知识事件）。

#### 1.7 关系与地图从正文回写

- 关系变化 → edge patch（create / end / set）。
- 移动 → `location_ref` 更新，外加移动记录（from、to、出发与到达时间）。
- 新的局部地点 → 挂在已有 Region 下的 MapNode 加 `site_state`。“SubWorld 只表示大尺度容器”的红线不变。
- 路线被发现 → 对应观察者的认知追加 `map_edge:<id>`。

### 2. Genesis：world_seed 阶段与开写种子

#### 2.1 阶段

阶段顺序改为 brief → world → map → story_engine → book_blueprint → **world_seed**。删除 `execution_bootstrap`（它没有运行时消费者）。需要修改的位置见附录 C。顺手把 7 处重复定义的 `_GENESIS_STAGE_ORDER` 收敛为 `genesis/constants.py` 一处。

#### 2.2 story_engine 结构化

- `core_cast`、`opposition` 增加稳定 id（现在没有 id 字段）。
- `relationship_axes` 从字符串列表改为结构化关系：`{source, target, edge_type, note, visibility}`。
- `long_arcs` 改为 `{id, summary, owner_refs}`。
- 单个 `secret` 字符串改为 `secrets` 列表，交由 world_seed 展开。

#### 2.3 world_seed 输出

由 LLM 生成，经确定性校验，用户可以修订和锁定：

- `calendar`（单位、纪元、开篇时间）与 `lexicon`（含出行方式和消息传播速度倍率）；各 actor 的可用出行方式。
- `actors`：每个 core_cast / opposition / faction 的 `actor_tier`、初始 `location_ref`（引用 map 节点 id）、资源、1–3 个 agenda、可选的初始 intent。
- `relations`：初始关系边。
- `facts` 与 `secrets`：`truth_value`、sensitivity、知情者名单；以及初始知识分布（谁知道什么，包括误信）。
- `rules`：区分 hard / soft。
- `clocks`：开篇时已存在的时限。
- `reader_promises`：落为 narrative `promise` 节点。
- `reveal_candidates`：哪些秘密适合在哪一卷揭示，供卷节拍表参考，不做硬绑定。

校验规则：引用的 id 必须存在于 map 或 story_engine；每个 major actor 至少有一个 agenda；每个秘密至少有一个知情者，或明确标为“无人知晓”。fallback 只能产出结构正确的空壳并标记 degraded，**不得编造题材内容**（现在 `genesis/fallbacks.py` 写死的地名、人名、职业前缀一并删除）。

#### 2.4 start-writing 交接顺序

1. 现有的前置检查和 `START_WRITING_REQUESTED` 事件。
2. `materialize_book_arcs`：`ArcPlanVersion` 额外持久化 `goal`、`stakes`、`payoff_direction`（现在被丢弃）。
3. 地图 bootstrap（沿用 `GenesisMapBootstrap`）。
4. **`WorldSeedCommitter`**：把 world_seed 编译成第 0 章的 GraphDelta（`source_type="genesis_seed"`），经 `BookStateReviewGate` 和 `BookStateCompiler` 写入；写 `story_calendars` 与 `book_lexicons`；角色经 `CharacterCreationHelper` 分配人格。可以参考 legacy 的 `finalization._seed_state` 作模板，完成后删除 `_seed_state`。
5. 生成卷 1 节拍表（§4.1）。
6. 交接时不再生成全卷章节计划；第一个 band 的章节计划由 run control 在首章前生成（§4.2）。
7. revision 标记 locked，`creation_status` 设为 writing。

删除 `subworld_manager` 中 `ensure_initial_registry_for_active_arc`、`plan_subworld_delta`、`apply_arc_delta` 的角色生成路径，以及名字池和 `fallback_slot_name`。SubWorld 作为地图容器的注册保留（`sub_worlds` 是地图表的外键目标）；`sub_world_roster_items` 删除。

### 3. 世界引擎（`forwin/world_engine/`）

#### 3.1 时机

作为 post-Canon 维护的 `world` 步骤运行（步骤顺序改为 world → plan → feedback，见 §9.3）。输入是章节 N 提交后的 BookState（as-of N）；`now` = 章节 N 的 `story_time_end`。

#### 3.2 Tick 流程（确定性编排，嵌入 LLM 节点）

1. **应用正文对意图的影响**：先应用抽取出的 `intent_interactions`（被正文打断、推进或兑现的意图）。
2. **结算到点意图**：对 `status=planned`、`scheduled_at ≤ now`、`requires_onstage=false` 的意图批量调用 `world.adjudicate`。输入是意图本身、相关各方的状态与资源、规则和随机种子；输出是结果 patch、事件和可见性。
3. **onstage 意图**：`requires_onstage=true` 且到点的意图不在幕后结算，而是标记为 due 交给规划。规划没有安排时，`scheduled_at` 顺延，`slip_count` 加 1；超过 `world.max_onstage_slips`（默认 2）就产生冲突。
4. **公共事件传播**（确定性）：一个 `visibility=public` 的新事件在地点 X、时间 t 发生，每个有据点的 actor 在 `t + 最短路线耗时(X, actor 位置) ÷ lexicon.news_speed_multiplier`（默认 1.0）时获得 heard 知识事件；时间未到的进入在途消息队列，下一次 tick 再判断。秘密事件只给参与者和目击者。
5. **行动者回合**：`next_turn_at ≤ now` 的 actor 按 tier 和与主角的相关度排序，取前 `world.max_actor_turns_per_tick` 个，调用 `world.actor_turn`（每个 actor 一次，或按 `world.actor_turn_batch_size` 小批量）。
   - 输入：该 actor 的**认知视图**（只给它知道或相信的）、agenda、资源、位置、可达地点及耗时、规则、它知道的 clocks，以及叙事边界摘要（只列出“不可触碰”的引用，不解释剧情原因）。
   - 输出：0–2 个新 intent，或修改 / 放弃 agenda，以及下一次回合的时间。
6. **世界级变量**（可选，`world.drift`，按故事时间频率触发）：Genesis 中 institution / resource_economy 定义的世界变量发生变化（天灾、市场、制度等），同样产出 intent 或事件。
7. **校验**：每个产出都转换为 GraphDelta 候选，经过世界审查门（§5.4 的确定性检查，含 CAS）和约束检查（§3.3）。不通过的意图带着错误重试一次；仍不通过就丢弃并记录事件。
8. **提交**：通过的变化经新入口 `CanonAdmissionService.commit_world_tick` 提交（与 `commit_world_edit` 同一模式：锁项目、编译、记录），`source_type="world_engine"`，默认对读者隐藏。
9. **冲突**：产生的冲突交给 §6。冲突涉及的变化不提交，状态为 held。

#### 3.3 约束（“有所限制”）

- **硬规则**（确定性，违反即拒绝）：
  - 死亡的实体不能行动。
  - 意图地点必须能在 `scheduled_at` 之前到达。
  - 转移或消耗的资源必须已经持有。
  - 受限者不能自由移动，除非这个意图本身就是脱困。
  - hard 规则节点上的 `checkable` 谓词。
  - 时间单调。
- **叙事边界**（来自计划，见 §4）：
  - `protection` 约束，存为 narrative `review_constraint` 节点：`{ref, kind, until_chapter | until_milestone, source}`。`kind` ∈ must_exist / must_be_alive / must_hold_state / must_not_know(observer, fact)；`source` ∈ beat_sheet / band_plan / human。
  - 揭示时间表：每个秘密向读者推进的最早章节。
  - 演化违反叙事边界时产生冲突，不直接修改剧情。
- **影响等级**（确定性分类）：
  - minor：小的状态变化。
  - moderate：关系改变、资源大量转移、受伤。
  - major：命名角色死亡、势力覆灭或易主、地点毁灭、秘密公开。
  - critical：触及卷里程碑所需的引用或卷 exit_state。
  - major 及以上必须经过审查（§6）。
- **预算**：每次 tick 的 actor 回合上限、每个 actor 的新意图上限、每次 tick 的 major 事件上限（超出的顺延）。

#### 3.4 与历史重写的配合

历史重写的用途是运营者重试某个已接受的旧章（保留其后的章节）。世界 tick 产生的 delta 记录来源章节（`source_chapter`），排在该章自身的章节 delta 之后。重写第 k 章时：

- 第 k 章之后的章节 delta 和 tick delta **按原记录原样重放**，不重新调用 LLM。
- `canon/historical_rewrite.py::validate_contribution` 需要接受 `source_type="world_engine"` 的 delta。
- 重放时 CAS 失败，就产生一条冲突（`source=historical_rewrite`），暂停后续生成，直到冲突解决。

#### 3.5 自由度

LLM 在约束内自由提出方案；随机种子写入 delta 的 metadata，保证可重放；actor 可以放弃或新建 agenda；意图可以引出新的次要 actor（经实体准入）。

#### 3.6 与正文的衔接

- 已结算的幕后事件对读者隐藏，成为规划素材（“幕后发生了什么”）。
- POV 角色获得知识（在途消息到达、亲眼目击）时，规划可以把“消息到达”排进章节内容。
- 主角的行动可以打断意图（抽取中的 `intent_interactions`：disrupted / advanced / fulfilled）。

### 4. 剧情规划

#### 4.1 卷节拍表（`ArcBeatSheet`，新表 `arc_beat_sheets`，带版本）

- 生成：卷激活时（开写，或上一卷完成时）调用 LLM `plan.arc_beats`。输入：Genesis 中该卷的 synopsis / goal / stakes / payoff_direction / 章节范围 / 软上下限、world_seed 的 reveal_candidates、当前世界简报（§4.4）、未了义务、上一卷的收尾状态。
- 内容：
  - `phases[{name, chapter_range, purpose}]`
  - `milestones[{id, description, target_range, required_refs, hard}]`
  - `protections`：写入为叙事边界节点。
  - `reveal_targets[{fact_ref, observer, target_state, range}]`：observer 可以是 reader 或某个 actor。
  - `exit_state`：用世界状态描述（主角等级、势力地位、关键关系等），代码可以直接比较。
  - `band_breakdown`：band 边界。
- `exit_state` 取代 `macro_progression`，由可行性审计对照 BookState 检查。
- “改大纲”= 生成新版本节拍表（由用户或审查决定），旧版本保留。
- 卷长在 Genesis 的 soft_min / soft_max 之内，由节拍表给出。删除 `ArcEnvelope`、`ArcEnvelopeAnalysis`、`ArcStructureDraft`，以及 `analyze_arc_envelope`、`draft_arc_structure`。

#### 4.2 Band 规划器（`plan.band`，每个 band 一次）

- band 大小由节拍表给出，默认 6 章（policy `planning.band_size`，允许 4–12），不跨卷。
- 时机：post-Canon 的 `plan` 步骤在 band 最后一章入 canon 后规划下一个 band；run control 发现缺计划时同步补规划（首章也走这条路）。两条路径调用同一个服务。
- 输入：
  - 节拍表的当前 phase，以及窗口内和邻近的里程碑。
  - 读者体验排期：保留确定性的 `BandExperienceScheduler`、`TropeSelector` 和 trope 冷却，作为输入。
  - 世界简报。
  - 上一个 band 的实际结果摘要。
  - 停滞剧情线信号、未了义务、到点的 onstage 意图。
- 输出（每章一个 ChapterPlan）：
  - `title`、`one_line`、`goals`、`pov_ref`、`onstage_refs`、`location_refs`
  - `expected_span`：预计故事时长
  - `world_bindings[{kind, ref, how}]`：kind ∈ intent / event / news / clock / relation / secret
  - `reveal_directives[{id, fact_ref, observer, target_state}]`
  - `hint_directives[{id, fact_ref, hint_text}]`：hint_text 不得包含真相本身
  - `intended_changes`：预期的状态变化
  - reward slot 与 trope id、`hook`
- 校验：可行性审计（§4.3）不通过 → 带着错误重试 1 次 → 仍不通过就产生冲突（§6）。
- `ChapterExperiencePlan` 由 band 计划和体验排期确定性派生（保留 `ChapterExperiencePlanner`，输入改为 band 计划）。
- band 切窗改为按卷章节范围和 `band_breakdown` 计算；删除 `BandWindowResolver` 对既有 ChapterPlan 行的依赖，删除 world contracts 的两次调用，用 `onstage_refs` 取代 `subworld_manager.plan_band_activation` 的入场角色绑定。

#### 4.3 可行性审计（确定性）

取代 `FuturePlanAuditor` 中倒计时、羁押、宏观进度相关的部分。对已规划但未写的章节检查：

- onstage 角色存活且未受限，或者计划本身包含脱困。
- 地点存在，并且能在 `expected_span` 内到达。
- `reveal_directives` 不早于揭示时间表，也不违反 protection。
- 里程碑覆盖：卷末之前，每个 hard milestone 都必须由某一章的计划承担。
- `exit_state` 的可达性（警告级）。
- clock 到期前，计划是否有所回应（警告级）。

运行时机：每章入 canon 后对本 band 剩余章节运行一次。不可行时自动重规划本 band 剩余章节（上限 `planning.max_replans_per_band`，默认 2）；仍失败则产生冲突。写前检查（`_audit_current_plan_before_write`）的调用点保留，改用此审计。`NarrativePlanPatch` 与叙事义务的绑定能力保留，只保留题材无关的部分。

#### 4.4 世界简报（`WorldDigest`，确定性构建，供卷和 band 规划使用）

按以下顺序组织，超出预算时从末尾开始裁剪（`planning.digest_budget_chars`，默认 8000）：

1. 主角处境：位置、状态、资源、等级、关键关系。
2. 相关行动者：按关系距离、地图距离、意图是否指向主角或其关联对象排序，取前 K 个，附带它们的可见与隐藏意图及预计时间。规划者是作者视角，可以看到隐藏意图。
3. 在途消息：会在 band 时间窗内到达主角所在地的消息。
4. 近期幕后事件。
5. 秘密与揭示进度：reader 认知状态与 reveal target 的对照。
6. 紧张关系：敌对、债务、背叛风险等。
7. clocks。
8. 停滞剧情线、未了义务。

#### 4.5 删除自动重规划器

删除 `StageAnalyzer`、`PacingStrategist`、`ReplanGovernor`、`ProjectStageAnalysis`、`ProjectReplanEvent`。剧情线停滞只作为世界简报里的一个信号。

### 5. 写作与回写

#### 5.1 POV 世界视图（`ChapterWorldView`，确定性构建）

它在 writer 上下文中取代 world contracts、world_model pack 和 world_pressure，内容包括：

- 场上实体的客观可感知状态：外观、位置、明显伤势、随身物品。
- POV 角色的知识：知道的事实、怀疑、误信（标明“POV 以为……”）。如果 POV 知道一个读者按时间表还不该知道的秘密，writer 拿到真相，但会标注“POV 知道，本章不得向读者明示”；是否泄漏由 §5.4 的揭示检查兜底。band 规划允许把 POV 交给持有未揭示秘密的角色，但同时必须为该秘密生成对应的保留指令。
- 读者已知事实中与本章相关的部分。
- 本章允许揭示的内容：`reveal_directives` 对应事实的真实内容和目标状态。
- 暗示指令：只给 `hint_text`。
- 禁止定论的话题：对读者尚未知、揭示时间未到、POV 也不知道的秘密，只列出**话题**（例如“不要给出 A 与 B 关系的确定说法”），不提供真相内容。writer 编造出的矛盾“真相”由 §5.4 的揭示检查拦截。
- 地图：POV 已知的路线与耗时，当前地点与邻近地点（只含 writer 可见的边）。
- 时间：当前故事时间（按日历格式化）与本章预期时长。
- POV 知道的 clocks。

预算：`writer.world_view_budget_chars`。揭示指令、暗示指令、禁止话题、POV 位置属于不裁剪部分，其余可裁剪。场景标记 `<<FORWIN_LOCATION>>` 改为输出地图节点 id（从视图提供的地点表中选择；新地点写作 `new:<名称>`）。

删除的写作输入：`_world_intent_section`、world_pressure 段、world_model 段中的 v4 字段；`knowledge_system_context` 里未被渲染的部分，要么并入视图，要么不再组装。顺手修复 single-draft prompt 把 canon_quality 段渲染两次的问题。

#### 5.2 章节增量抽取（`extract.chapter_delta`，取代现在的 3 次抽取）

- 时机：writer 产出最终正文之后、draft review 之前。修复产生的每个新候选正文都重新抽取；结果以正文 hash 缓存，canon 准备阶段复用。
- 输入：最终正文、`ChapterWorldView` 中的实体表（id、名字、别名）、地点表、本章计划（bindings / reveal / hint）、日历单位名、相关意图，以及**本章相关秘密清单**（禁止话题背后的秘密，加上 POV 知道的未揭示秘密，含真相文本）。抽取器只用这份清单给条目打标签，不把它当成正文事实。
- 输出（JSON schema，每个条目都带 `evidence` 原文引用）：
  - `time`：`elapsed{amount, unit}`、`flashback_spans`
  - `entity_changes[{ref | new_entity, field, new_value}]`
  - `relation_changes[{source_ref, target_ref, edge_type, op}]`
  - `knowledge_events[{observer_ref, fact_ref | new_fact{proposition, truth_value}, state, channel, source_ref, touches_secret_ref, contradicts_ref}]`：observer_ref 可以是 reader；`touches_secret_ref` 表示这条知识涉及秘密清单中的哪一项，`contradicts_ref` 表示它与哪个已有事实矛盾
  - `movements[{entity_ref, from_ref, to_ref, depart_offset, arrive_offset}]`
  - `new_sites[{name, near_ref | region_ref, kind}]`
  - `item_transfers[{item_ref, from_ref, to_ref}]`
  - `events[{summary, participant_refs, location_ref, significance, visibility}]`
  - `thread_beats[{thread_ref | new_thread, beat_type}]`
  - `intent_interactions[{intent_ref, effect}]`
  - `plan_fulfillment[{target_id, status, ...}]`：target_id 是目标序号或指令 id，status ∈ done / partial / missing
  - `entity_mentions`：供实体准入使用
- 可以拆成 2 次调用（世界事实 / 认知加叙事），由实现者按抽取质量决定（开关 `extract.split_calls`）。
- 证据校验（确定性）：`evidence` 必须是正文的子串（空白规范化后）；不通过的条目丢弃，并记为抽取问题。
- 翻译：`ChapterDeltaTranslator`（确定性）把抽取结果转成一个 GraphDelta（`old_value` 读 as-of N−1）；新实体走 `EntityAdmissionPlan`。它取代 `WriterContractDeltaBuilder`、v4 路径和正则兜底。抽取失败意味着本章不能入 canon，不再用正则制造节点。
- 章节摘要仍取自 writer 的 `<<FORWIN_SUMMARY>>`。

#### 5.3 Review 栈调整

- `MapMovementReviewer` 的检查并入世界审查门（基于结构化的 movements 与故事时间），删除它的中文时长词表。
- `ContinuityChecker` 的死亡检查改读 BookState 的 `alive_status`，删除 `DEAD_STATUS_KEYWORDS`。
- `PlanContractReviewer` 改为依据 `plan_fulfillment` 判定，删除关键词子串匹配和 `task_contract_json`。
- canon_quality：删除平行的 countdown / character_state 账本；时限读 clock 节点，角色状态读 BookState。chapter review form 继续作为 LLM 信号，但行数据改为从 BookState 构建。
- 叙事义务保留（延迟修复的债务），验证改用抽取结果中的 `plan_fulfillment` 和义务兑现字段，删除“因为 / 真相 / 身份”这类标记词判定。

#### 5.4 世界审查门（`BookStateReviewGate` v6，确定性）

在现有的 schema 校验、CAS、规则定义不可改的基础上新增：

- **时间**：时间单调；闪回不修改 state。
- **移动**：每次移动的耗时，与“该角色已知路线（没有认知信息时用客观路线）÷ 其最快可用出行方式”得出的最短耗时比较：快出 `review.travel_tolerance`（默认 20%）以上为 error，容差以内为 warning。不可达为 error。受限者移动必须伴随脱困事件，或者计划允许。
- **生死**：dead 实体不得出现在参与者、移动或对话事件中（闪回除外）。
- **物品**：同一物品同一时刻只能有一个持有者或位置。
- **知识**：channel=told 的知识事件，其 source 必须已经知道该事实；角色对一个它没有任何获知途径的秘密处于 knows 状态，记为 warning。
- **揭示**：reader 认知推进超出揭示时间表为 error（本章 `reveal_directive` 允许的除外）；`new_fact` 与隐藏事实矛盾为 error。事实是自由文本命题，代码无法直接比较，所以这两条检查依据抽取器打的 `touches_secret_ref` / `contradicts_ref` 标签做确定性判定；标签本身的准确性由抽取质量负责（见 Risk）。
- **protection**：违反叙事边界时产生冲突（交 §6），不直接判失败。
- 删除基于 `role` / `source_type` 的 writer hidden-truth 检查，由揭示检查取代。

这些检查的结果作为 draft review 的 `world_consistency` 信号进入修复循环（error 进入 must_fix）；canon 准备阶段再运行一次，作为最终守门。

### 6. 世界与计划的冲突审查

#### 6.1 数据模型

新表 `world_plan_conflicts`：

| 字段 | 说明 |
| --- | --- |
| `id`、`project_id` | |
| `source` | world_tick / chapter_extraction / plan_feasibility / band_planning |
| `detected_after_chapter`、`story_time` | |
| `involved_refs` | |
| `violated` | protection / milestone / reveal_schedule / feasibility / onstage_slip |
| `proposed_change` | GraphDelta 候选或计划补丁 JSON |
| `impact_level` | |
| `options` | 可选的处理方式 |
| `required_decider` | auto / codex / human |
| `blocks_from_chapter` | |
| `status` | open / resolved / expired |
| `decision` | `{option, decided_by, actor_id, rationale, decided_at}` |
| `result_refs` | |

#### 6.2 处理选项

- `veto_world`：否掉这次演化。意图标为 vetoed，并把约束注入该 actor 的下一次回合；重掷上限为 `world.max_rerolls`。
- `rewrite_chapter`：（来源是正文时）退回修复，冲突约束作为 must_fix。
- `amend_plan`：生成新版本节拍表，或重规划 band，接受这次世界变化（解除相关 protection）。
- `plot_responds`：保留世界变化（意图保持 planned 或 held），在下一次 band 规划中加入回应它的目标，例如让主角去阻止。
- `accept_with_obligation`：仅限 minor / moderate，接受并记录一条叙事义务。

#### 6.3 决策路由（确定性）

- minor 或 soft 约束 → auto，默认 `veto_world`（静默重掷）。
- moderate / major，且不触及卷的 hard milestone 与 exit_state → 模型审稿：新角色 `review.world_conflict`，默认模型 **`gpt-6-astra`**（`models.role_preferences["review.world_conflict"] = ("gpt-6-astra",)`），经 Codex 通道做一次结构化调用，复用 gate delegation 中 Spark 的调用方式和审计写入；输入是冲突记录、相关 BookState 片段、卷节拍表、受影响的计划，输出所选选项和理由。超时、解析失败，或选择了改卷节拍表的 `amend_plan` → 升级给用户。
- critical（触及卷 hard milestone 或 exit_state，或需要用 `amend_plan` 修改卷节拍表）→ 用户。
- policy `world.conflict_delegate: "codex" | "human"`，默认 codex（即 gpt-6-astra）；设为 human 时，moderate 及以上全部交给用户。
- 路由前提：现在判断“是否 Codex 模型”靠写死的子串 `codex` / `gpt-5.3`（`forwin/llm/router.py:563-566`、`forwin/writer/llm/routing.py:381`），`gpt-6-astra` 会被当成普通模型而走不了 Codex 通道。需要改成由配置声明的 Codex 模型清单（例如 `InfrastructureConfig.codex_models`），并确认 Codex bridge 能调用 `gpt-6-astra`。

#### 6.4 暂停与恢复

- 世界 tick 中出现的冲突不阻塞 tick 本身：涉及的变化不提交，状态为 held。
- run control 在每章开始前检查：若存在 `status=open`、`required_decider=human` 且 `blocks_from_chapter ≤ 下一章` 的冲突，任务以 `needs_review` 结束，reason 为 `world_conflict`（复用现有的 needs_review 终态和 continue 流程）。
- 解决入口：
  - HTTP：`GET /api/projects/{id}/world-conflicts`、`POST /api/projects/{id}/world-conflicts/{cid}/resolve`。
  - MCP：`world_conflict_list`、`world_conflict_get`、`world_conflict_resolve`。现有的 `world_conflict_list` 读的是 CanonQualitySignal，改为新语义。
  - 服务端控制台的任务抽屉增加冲突列表和选项按钮。
- 选项执行（提交 delta / 生成新版节拍表 / 重规划）之后，由用户 continue-generation。
- 每一步都写 DecisionEvent：`WORLD_CONFLICT_DETECTED`、`WORLD_CONFLICT_ESCALATED`、`WORLD_CONFLICT_RESOLVED`。

### 7. 防过拟合

#### 7.1 原则

运行代码只处理结构。所有故事内容都来自 Genesis、world_seed、BookState 或 LLM 输出；题材相关词只能来自本书的 lexicon。

#### 7.2 护栏测试

`tests/test_no_story_content_in_runtime.py` 配合 `tests/overfit_allowlist.toml`，用 AST 和正则扫描 `forwin/**/*.py`（排除 `migrations/versions`）：

1. **id 字面量**：禁止 `^(line|gap|world_line|hidden_line|fact|thread|intent|agenda)_[a-z0-9_]+$` 形式的字面量；禁止向 `world_line_id=`、`gap_id=` 等关键字传非空字面量（allowlist 中登记的通用默认值如 `"main"` 除外）；禁止 `"<字面量>" in x.must_not_reveal`。
2. **章节号**：禁止名字匹配 `chapter(_number|_start|_end|_hint)?` 的变量与 ≥2 的整数字面量比较；禁止 `chapter_hint=<int>`。通用策略常量以 `path:NAME` 登记在 allowlist。
3. **嗅探 premise**：禁止 `"<字面量>" in <名字含 premise / all_text / synopsis / setting / title / genre 的表达式>`。
4. **CJK 决策词表**：CJK 字符串出现在模块级集合、元组、字典，`re.compile` 的模式，或 `"…" in x` 比较中，即判失败，除非该文件或符号登记在 allowlist。日志、异常消息、`description=` / `reason=` 等说明性参数，以及构造 LLM messages 的文本不受此限。
5. **测试剧本泄漏金丝雀**：从 `tests/` 和 `data/` 的 fixture 中自动收集专有名词与 id，出现在 `forwin/` 中即失败；另外保留一份种子黑名单（附录 A 中两套剧本的特征词）。

allowlist 条目格式：`path[:SYMBOL]`、category（language_lexicon / catalog / prompt_text / numeric_policy / eval_fixture）、justification、`max_literals`（数量增长需要复审）。规则：题材词表只能按项目显式的 genre 选择，禁止从正文推断题材（删除 `pulp_beat._infer_track`）。

过渡安排：P0 落地护栏时，允许在 `pending_removal` 区段登记尚未清理的条目；进入 P5 之前该区段必须为空，由测试强制检查。

#### 7.3 清除两套泄漏剧本

完整清单见附录 A，要点：

- world contracts 与 v4 抽取。
- 续写节拍 `_EXTENSION_BEATS`：extend-generation 改为增加卷或章节目标，内容交给节拍表和 band 规划器生成。
- 倒计时、羁押审计以及对应的修复补丁。
- `canon_names` 的“母亲”锚点：改为从 BookState 的亲属关系边生成“称谓 → 名字”锚点。
- canon_quality 的 placeholder / readability 中的角色词；pulp_beat 中的剧本标记词。
- `genesis/fallbacks.py` 写死的地名、人名、职业前缀、文化默认值（fallback 只产出结构空壳）。
- `subworld_manager` 名字池；`arc_director` 的默认世界。
- writer prompt 中的剧本示例（“系统巡检员”、倒计时示例、追踪器措辞）。
- `llm_eval/cases.py` 的剧本移到 `tests/fixtures`。
- 题材词：删除 map_movement 的时长表（改用 lexicon）；地图生成器和 `genesis_adapter` 中的仙侠路线与地形词，改为由 map_atlas / lexicon 提供，或使用题材中立的默认值；默认 genre “玄幻” 改为必填、无默认值。

#### 7.4 多题材验证

`tests/fixtures` 中至少放 3 个题材迥异的 Genesis / world_seed 样例（例如仙侠、星际、都市悬疑）。集成测试用桩 LLM 依次跑 world_seed → 种子提交 → tick → band 规划 → 抽取与翻译 → 审查门，三个样例必须走完全相同的代码路径。

### 8. LLM 角色、路由与成本

#### 8.1 新角色

| 角色（stage_key） | 调用时机 | 次数 |
| --- | --- | --- |
| `genesis.world_seed` | Genesis 阶段 | 每本书 1 次（另加修订） |
| `plan.arc_beats` | 卷激活、改大纲 | 每卷 1 次 |
| `plan.band` | band 规划、重规划 | 每个 band 1 次（另加重试） |
| `world.actor_turn` | 每次 tick | 每章 ≤ `max_actor_turns_per_tick` |
| `world.adjudicate` | 每次 tick | 每章 0–1 批 |
| `world.drift` | 按故事时间频率 | 可选 |
| `extract.chapter_delta` | 每个候选正文 | 每个候选 1–2 次 |
| `review.world_conflict` | 冲突 | 按需 |

删除的角色：`subworld_delta`、`arc_plan`（结构草案）、`arc_envelope_analysis`、`launch_arc_N` 的全卷章节生成、`world_pressure`，以及 3 个旧抽取调用。

#### 8.2 路由

- 每个角色在 `writer/llm/routing.py::_llm_task_route` 注册路由，沿用现有的 fallback chain。
- RuntimePolicy 新增 `models.role_preferences: dict[role, tuple[model, ...]]`，用来调整各角色的优先模型；这是对现有 `ReviewPolicy.repair_models` 模式的推广。
- 每个调用点都必须保存 PromptTrace 和 LLM DecisionEvents，不得像 `ArcDirector._call_json` 那样漏记。

#### 8.3 成本旋钮

以下参数放在 RuntimePolicy，standard 与 pulp 的默认值不同，在测试中调整：

- `world.max_actor_turns_per_tick`、`world.actor_turn_batch_size`、`world.drift_every_story_minutes`、`world.max_major_events_per_tick`
- `planning.band_size`、`planning.max_replans_per_band`、`planning.digest_budget_chars`
- `writer.world_view_budget_chars`
- `extract.split_calls`

删除 `planning.use_llm_simulation` 和 `canon.book_state_layers`。

### 9. 流水线变化汇总

#### 9.1 单章流程

1. run control：检查暂停与取消；补跑前章的 post-Canon 维护；**检查阻塞性冲突**；**确保本章的 ChapterPlan 存在（不存在则同步执行 band 规划）**；写前可行性审计。
2. 上下文：Genesis 摘要、ChapterPlan、POV 世界视图、人格、体验计划、读者反馈提示、检索记忆、叙事义务。删除 world contracts、world pressure、world_model pack。
3. writer：场景模式，未涉及的部分保持不变。
4. **章节增量抽取 → 翻译 → 世界审查门**（新位置）。
5. draft review（新增 `world_consistency` 信号）→ 修复（修复后回到第 4 步）。
6. 残留判定 → 委托门。
7. canon 准备（复用抽取结果、实体准入计划，审查门复核）→ `commit_plan`。
8. post-Canon 维护：world → plan → feedback。

#### 9.2 order controls

依次为：义务核验、可行性审计（取代 future plan audit 中的对应部分）、generation audit 报告、band checkpoint。

#### 9.3 post-Canon 步骤

`POST_CANON_STEP_NAMES = ("world", "plan", "feedback")`：

- **world**：世界 tick。
- **plan**：本 band 剩余章节的可行性审计，必要时重规划；band 末规划下一个 band；卷末激活下一卷的节拍表。
- **feedback**：不变，仍排在最后并承载 order_controls。

#### 9.4 工作集

“应写哪些章节”由卷的章节范围决定，ChapterPlan 按 band 物化。`continue_workset` 改为根据卷范围和现有计划状态计算；缺计划时由 run control 触发 band 规划，不再返回 `future_arc_materialization_required` 交给上层另行处理。卷的切换在 plan 步骤或 run control 内完成。

### 10. 数据库与上线

用户已同意线上生成立即停止，因此先停写、先清库，再逐期合入。

- **开工前（P0 的第一步）**：
  1. 暂停线上全部生成任务（优先 `task_pause`），关闭每日自动调度。
  2. pg_dump 备份数据库，同时备份外部存储。
  3. 运行清库脚本（见下）。
  4. 从此到验收运行之前，不创建新书（测试用书除外），自动调度保持关闭。
- **迁移**：每一期合入 master 时附带该期自己的前向迁移（`0002_…`、`0003_…`，依次接在 `0001_v5_recovery` 之后），合入后按 AGENTS.md 通过 150 sync 部署。已合入的迁移**不得再修改**，0001 同样不得原地修改。因为数据已经清空，迁移不需要搬运旧数据。
  - 删除附录 B 所列的表和列。
  - 新增 `story_calendars`、`book_lexicons`、`arc_beat_sheets`、`world_plan_conflicts`、`graph_deltas` 的时间列、`chapter_plans` 的新列。band 计划是新建 `band_plans` 表还是扩展现有表，由实现者决定。
- 清库脚本 `scripts/purge_all_projects.py`（默认 dry-run）：
  - 先执行 pg_dump 备份。
  - 删除全部项目数据，包括现在 `_delete_project` 漏掉的 `publisher_upload_attempts`、`publisher_upload_receipts`、`publisher_operator_actions`、`publisher_milestones`，以及按 aggregate 关联的 `outbox_events`。
  - 清理外部存储：artifacts（含 MinIO）、Qdrant 的 `chapter_memories` 与 `llm_kb_vectors`、`data/llm_kb`、`data/world_vaults`、`data/publisher_covers`。
  - 保留全局的 publisher 扩展与浏览器会话表（其中保存登录态）。
  - 顺手修复 `_delete_project` 的漏删。
- 每期的部署顺序：合入 master → 备份 → `alembic upgrade head` → 通过 150 sync 部署 → 冒烟测试。清库和每次部署在执行前都需要用户确认。
- 已经发布在番茄等平台上的书不受影响，但 ForWin 中的绑定关系会被删除（用户已同意删除旧书）。

### 11. 实施分期

各期依次开发，每期在全量测试通过后合入 master 并部署（线上已停写，中间状态不影响生产）。

- **P0 停写、清库、护栏与纯删除**：停写并清库（§10）；落地护栏测试（含 `pending_removal` 区段）；删除不需要替代物就能删的内容：样例剧本内容、续写节拍、llm_eval 剧本、Genesis fallback 的编造内容、writer prompt 剧本示例、死表。
- **P1 账本 v6**：world_seed 阶段、种子提交、StoryClock、lexicon。
- **P2 回写**：章节增量抽取、翻译、审查门 v6、review 栈调整；删除旧抽取、v4 路径、正则兜底、map_movement 词表、倒计时与羁押的平行账本。
- **P3 世界引擎**：世界引擎、冲突审查（auto / codex / human）、HTTP / MCP / 控制台入口。
- **P4 规划**：节拍表、band 规划器、可行性审计、POV 世界视图；删除 envelope / structure / 重规划器 / world contracts / WorldSimulator / subworld 角色路径。
- **P5 验收**：重新开启生成，按 Verification 跑验收（运行成本需用户批准）。

## Risk

- **范围巨大**：触及 Genesis、规划、写作、审查、canon、维护、HTTP / MCP / UI，大量测试需要重写。缓解：分期推进，每期结束时全量测试通过；护栏测试防止回退。
- **抽取质量**：id 引用错误或遗漏会让账本失真。缓解：证据子串校验、实体表限定、可拆分调用的开关、审查门。
- **世界引擎的成本与延迟**：每章增加若干次调用。缓解：按故事时间调度、预算旋钮、低成本模型优先。
- **冲突过多导致人工成为瓶颈**。缓解：影响分级，auto 和 codex 先处理；度量冲突率并调整阈值。
- **演化失控或空转**。缓解：影响等级预算、protection、世界素材绑定率指标。
- **时间换算错误**：LLM 给错单位或数量级，会让旅行时间检查误报。缓解：单位必须属于本书日历；数量级异常记为 warning 并交给修复。
- **历史重写与世界 tick 的交互**：重写早期章节后，原样重放的 tick delta 可能与新内容冲突。缓解：按 §3.4 原样重放，CAS 失败时转为冲突，不重新运行 LLM。
- **揭示与矛盾检查依赖抽取标签**：抽取器漏标时，泄漏或矛盾可能漏检。缓解：draft review 的 LLM 审稿表单同时拿到秘密清单做交叉检查；验收后的连续性审计专门统计这一类问题。
- **JIT 的 band 规划会增加首章和 band 边界处的延迟**。
- **清库不可逆**。缓解：先备份、dry-run，并经用户确认。

## Verification

- 每个新组件都有单元测试和契约测试；护栏测试；多题材桩 LLM 集成测试；扩展现有的架构边界测试（`tests/test_architecture_boundaries.py`），禁止已删除的模块和表回归。
- 全量测试：`pytest -q`（PostgreSQL 测试库）。
- 验收运行（P5，用户批准后执行）：
  - 主力：题材 A，standard，100 章。
  - 对照：题材 B、题材 C 各 30 章，其中一本用 pulp。
- 运行报告中的指标：
  1. **硬伤**：
     - canon 中被强制接受的 `world_consistency` error 数 = 0。
     - 全书完成后做一次独立连续性审计：由 LLM 按窗口阅读章节摘要与 BookState 时间线，列出矛盾，再人工核对；确认的硬伤数 = 0。
  2. **新意**（代理指标，初始目标可调）：
     - ≥ 40% 的章节计划至少绑定一条由世界引擎产生的素材。
     - trope 冷却违规数为 0。
     - 统计实际行动过的不同 actor 数量。
  3. **防过拟合**：护栏测试通过；三本书运行时走的是同一套代码路径。
  4. **自主性**：冲突总数，以及按 auto / codex / human 的分布、人工介入次数。
  5. **成本**：每章的 token 与费用（CostLedger），按角色拆分。
- 用户抽读若干章节。

## Rollback

- **单期回滚**：revert 该期的合入提交，恢复该期部署前的备份，再重新部署。新 schema 与旧代码不兼容（`require_v5_schema` 会拒绝启动），所以代码必须连同数据库一起回滚。
- **整体回滚**（放弃大修、找回旧书）：恢复 P0 清库前的 pg_dump 与外部存储备份，并重新部署 `f81d542`（合入 Fanqie 修复后的 master）。

## 决策记录

用户确定：

- 世界模型的两个角色都要：一本严格的账本，以及能推动剧情的自我演化世界；必须二选一时偏向后者，账本作为地基一起修。
- 世界框架的目的是降低创作难度，形式是有所限制的自我演化；世界模型作为背后的框架，应当尽量大而周密，在限制中保留充分的自由度。
- 世界演化与大纲冲突时走审查机制，并扩大审查者的权限：由模型审稿或用户决定是重写还是改大纲；默认交给 `gpt-6-astra`。
- 线上生成可以立即停止；`codex/fanqie-publish-recovery` 的 3 个提交先合入 master（已完成，`f81d542`），大修从 master 开分支。
- 正在写的书直接删除，不迁移旧数据。
- 成本上限在后续测试中调整；每个 LLM 调用点都有 fallback chain，调整优先模型即可改变成本，可以使用 MiniMax 等低成本模型。
- 成功标准：100 章、没有逻辑硬伤、有新意，最重要的是不把写作内容过拟合进系统。
- 已认可方案的整体轮廓：账本补全、行动者 + 意图 + 按故事时间调度、三层约束、卷节拍表 + band 规划、POV 视图、冲突选项与决策路由、防过拟合、验收、清理与上线。

我的决定（可以推翻）：

- 模型审稿是单次结构化调用（与现有 Spark 委托门同一模式），不做能自行调用工具的代理会话。
- “改大纲”指修改运行期的卷节拍表或章节计划，Genesis 在开写后保持冻结。
- 小冲突由代码或模型审稿处理，只有触及卷级里程碑或 exit_state 的冲突才停下来等用户。
- pulp 与 standard 共用同一套四层世界模型，差别只在旋钮。
- 用 world_seed 阶段取代空转的 bootstrap，开写前用户可以审阅初始世界。
- 意图与议程作为 BookState 节点，而不是单独的表，从而自动获得 CAS、快照和认知能力。
- 公共事件按地图最短路线的耗时传播给各方。
- 揭示控制：writer 只有在 POV 知道、或本章允许揭示时才拿到真相；其余秘密只给“禁止定论的话题”和暗示指令。抽取器拿到本章相关秘密清单并打标签，审查门据此确定性判定泄漏与矛盾。
- 历史重写时，后续章节与 tick 的 delta 原样重放，不重新调用 LLM；CAS 失败转为冲突。
- 移动速度：地图边保留客观耗时；题材中立的出行方式（lexicon `travel_modes`）提供速度倍率；超出最快可能速度 20% 以上判为 error，以内为 warning。
- 章节增量抽取移到 draft review 之前，让世界一致性问题进入修复循环。
- 数据库：先停写清库，之后每期带自己的前向迁移逐期合入部署；用清库脚本而不是删库重建（以保住 publisher 的登录态）；已合入的迁移和 0001 都不原地修改。
- 验收另跑两个题材各 30 章；新意代理指标的初始目标定为 40%。
- 默认数值：band 6 章、onstage 意图最多顺延 2 次、每个 band 最多重规划 2 次、世界简报 8000 字。

## 留给实施者

- 各张新表的具体列类型和索引；band 计划是新建表还是扩展 `band_experience_plans`。
- `world.actor_turn` 与 `world.adjudicate` 的 prompt 措辞与批量大小；抽取是否拆成两次调用。
- 影响等级的具体分类规则表（在 §3.3 的定义范围内）。
- 世界简报和 POV 视图的格式与裁剪细节。
- `<<FORWIN_LOCATION>>` 新地点的准入流程细节。
- 服务端控制台冲突面板的样式。
- 三个多题材 fixture 的具体内容（只能放在 `tests/` 下）。
- 各期内部的任务拆分与合并节奏。

## 附录 A：设计起点已核实的泄漏与缺陷（历史）

路径相对仓库根目录；`fpa/` = `forwin/planning/future_plan_audit/`。

**样例剧本一：母星被围 / 殖民地**

- `forwin/planning/world_contract_service.py:50-180`：用 母星 / 殖民 关键词嗅探 premise，写死第 22/23/25/28 章、`father_sieged`、`gap_homeworld_siege`、`line_colony_defense` 及剧情文本。
- `forwin/book_state/extraction/delta_extractor.py:70,88,108,127-152,171`；`cognitive.py:44-48`；`reveal.py:18,46-50`；`evidence_rules.py:35-56`。
- 后果：对任何题材，每一章的可见事件都会挂到 `line_colony_defense` 这条世界线上。

**样例剧本二：记忆重置 / 档案 / 倒计时 / 后门 / 羁押**

- `forwin/application/projects/common.py:222-336`：`_EXTENSION_BEATS`（12 个写死的情节节拍）与续写护栏文本，经 `POST /extend-generation` 和 MCP `project_extend_generation` 使用。
- `fpa/helpers.py:16-61,228-260,342-388`：羁押标记词与改写文本、倒计时键（memory_reset / archive_cleanup / terminal_audit_window / core_access_window / public_countdown）；`fpa/patches.py:203`；`fpa/custody.py:48,142`。
- `forwin/generation/pipeline_core/repair_patches.py:126-166`。
- `forwin/context/assembler_core/canon_quality_context.py:84-94`：memory_reset 剩余 ≤ 180 分钟时覆盖主倒计时。
- `forwin/canon_quality/placeholder.py:9-15,187,196-198`；`readability.py:10-42`。
- `forwin/canon_names.py:11-94,166-212,285-325`：“母亲”锚点与剧本名词。
- `forwin/checker/pulp_beat.py:52,111-113,154-184,264-306`。
- `forwin/genesis/fallbacks.py:40-50,106-128,175,212-234,274`；`forwin/genesis/helpers.py:341-362`。
- `forwin/planning/contracts.py:78-99`。
- `forwin/writer/prompt_core/sections.py:74-81`；`forwin/writer/prompt_core/constraints.py:253-255`。
- `forwin/checker/reference_classifier.py:106-134`。

**其它泄漏**

- `forwin/subworld_manager.py:35-40,918-923`（名字池）、`:798-803`（写死的章节窗口）。
- `forwin/director/arc_director.py:75-76,194-200`（默认的雨夜都市世界）。
- `forwin/llm_eval/cases.py:17-60`（eval 剧本放在运行包内）。
- `forwin/simulation/world.py:738`（`chapter_number >= 3` 强制 rising）。
- 题材词：`forwin/review/map_movement.py:14-57`、`forwin/map/generator.py:438-472`、`forwin/map/genesis_adapter.py:13,217,250-274`、`forwin/book_state/extraction/graph_delta.py:287-474`（正则造节点）、默认 genre “玄幻”（`models/project.py:18`、`api_schema/project.py:224` 等）。

**结构缺陷**

- `forwin/book_state/reviewer.py:91`：只认 writer / writing / writer_pack，而 writer 产出的 delta 的 source_type 是 `writer_output`（`book_state/writer_contract.py:541`），hidden-truth 检查对主路径不生效。
- `forwin/retrieval/broker_core/broker.py:325-329`：认知相关输入被写死为空列表；`:125-134` 的合并异常被静默吞掉。
- `forwin/protocol/writer.py:67-74`：v4 字段从来没有被填充过。
- `forwin/planning/stage_analysis.py:149-151,304-316,429-518`：两条剧情线停滞即 rearc，生成残缺的 ArcPlanVersion。
- `macro_progression_json` 没有写入点（`models/project.py:61` 只有默认值）。
- `forwin/subworld_manager.py:285`：第 0 章角色由 LLM 重新编造，不使用 Genesis 的 core_cast。
- 组装了但从未渲染给 writer 的字段：active_relations、task contracts、knowledge_system_context、认知状态等（`forwin/context/` 与 `forwin/writer/prompt_core/` 之间）。

## 附录 B：删除清单

- **world contracts**：`planning/world_contracts.py`、`planning/world_contract_service.py`、`models/world_contract.py`；表 `arc_world_contracts`、`band_world_contracts`、`chapter_world_delta_intents`；调用方包括 planning service / arc_envelope / band_plan_service、experience_provider、assembler、`protocol/context.py` 的 world 字段、`canon/preparation.py`、broker、writer `_world_intent_section`、`checker/hard_floor.py:72`。
- **v4 抽取**：`book_state/extraction/{delta_extractor,evidence_rules,cognitive,reveal,world_delta,reader_cognition,gate,types}.py`、`book_state/adapter.py`、`protocol/world_v4.py`、`api_schema/world.py` 中的 V4 schema、空目录 `forwin/world_v4_review_gate/` 与 `forwin/extractor/`；精简 `extraction/contract.py` 和 `graph_delta.py`（删除 v4 步骤与正则兜底）；删除 WriterOutput 的 v4 字段、`protocol/review.py` 中的 world_model 修复 scope 及相关字段。
- **broker 世界模型包**：`build_world_model_pack`、`_load_reveal_ladder`、`_filter_writer_safe_world_context`、`_merge_writer_world_model_pack`，以及 `protocol/context.py` 中的 `WorldModelRetrievalPack` 及其子类；`GET /context-pack/{role}` 与 `llm_kb/compiler.py` 的按角色打包同步删除或改写。`_augment_v46_context` 提供的 BookState / Obsidian / LLM-KB 内容，在删除前确认要么并入 POV 视图，要么明确舍弃。
- **WorldSimulator**：`simulation/world.py`（含无调用方的 `CommentAnalyzer`）、表 `world_simulation_turns`、`WorldPressureView` 及其所有消费者（state_provider、personality_provider、writer 段、review、read models、api_schema）。读者反馈聚合保留在 feedback 步骤。
- **重规划器**：`planning/stage_analysis.py`，表 `project_stage_analyses`、`project_replan_events`，以及 read models、api_schema 和 UI 中的 replan / pacing 字段。
- **宏观进度**：`planning/macro_progression.py`、`fpa/macro_progression.py`、`book_state/macro_status.py`、`arc_plan_versions.macro_progression_json`。
- **subworld 角色路径与 ArcDirector**：`subworld_manager` 的初始注册、roster、`plan_band_activation`、名字池；`director/arc_director.py` 整体删除（`draft_arc_structure` 与 `analyze_arc_envelope` 随 envelope 一起删除）；表 `sub_world_roster_items`。
- **Envelope 与结构草案**：`ArcEnvelope`、`ArcEnvelopeAnalysis`、`ArcStructureDraft`，`planning/arc_envelope.py` 与 `arc_envelope_resolver.py` 中被节拍表取代的部分，`BandWindowResolver` 中依赖既有计划的逻辑。
- **平行账本**：canon_quality 的 countdown 与 character_state 账本，以及对应表（`countdown_ledgers`、`character_state_transitions`）和审计；future plan audit 中的倒计时与羁押逻辑。
- **死表**：`world_projection_deltas`、`story_obligations`、`reveal_registry_entries`、`artifact_collection_ledgers`、`book_reader_experience_deltas`、`book_reader_promises`（reader promise 改由 narrative 节点承载）。
- **legacy 种子路径**：`finalization._seed_state`、`RunControlStage.run` / `run_existing_project`、`ArcDirector.plan_arc`、`apply_initial_arc_plan`（唯一调用方是 llm_eval，需要同步改造 llm_eval）。
- **其它**：`execution_bootstrap`、`RecencyTruncateGate`、`CodexGovernedActionProcessor`、`DraftReviewService` 中未使用的 reviewer 字段、WriterOutput 中无读者的 `scene_continuation` / `timeline_hints` / `writer_notes`、孤立的 structured_extraction 延迟维护记录、恒为 None 的 `reader_feedback`。

## 附录 C：bootstrap → world_seed 涉及的位置

`genesis/constants.py:10-25,65-72,90-147`、`genesis/messages.py:105-120`、`genesis/llm.py:39,70-71,139`、`genesis/fallbacks.py:640`、`genesis/helpers.py:234`、`genesis/names_paths.py:221`、`mcp/models.py:6-23`、`mcp/client.py:686,903`、`mcp/http.py:192-222`、`cli.py:57-80`、`http/routes.py:617-655`、`application/projects/genesis.py:138,187,238,289,343`、`application/read_models/{common,generation,arc_snapshot,runtime_maps,project_summary,project_detail,genesis}.py` 中的 `_GENESIS_STAGE_ORDER`、`api_schema/genesis.py:22,62`、`http/request_support.py:166`、`writer/llm/routing.py:258`、`ui_assets/home/app_state.js:23-70`、`ui_assets/home/app_genesis.js:3-9`、`forwin_skills/genesis/bootstrap/SKILL.md`。
