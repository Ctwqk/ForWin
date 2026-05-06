# ForWin 三层 Plan 与人机边界解耦计划

> 代码基线：`Ctwqk/ForWin@0f7b802b171b7c60c8d111a1844f231584a65c79`  
> 目标：把 Arc / Band / Chapter 三层计划机制、Genesis 设计器、写作更新节奏规划器、发布/上传自动化的职责边界拆清楚，让“人类交互模块”和“后台自动化模块”不再互相穿透。

---

## 1. 设计基线

当前设计文档已经给出了清晰边界：

1. 系统对象层级是：

```text
BookGenesis -> Project -> Arc -> Band -> Chapter -> Scene
```

2. 运行主链是：

```text
Genesis -> Start Writing -> Arc Envelope / Provisional -> Writer -> Review -> Repair -> Canon -> Post-acceptance -> Governance / Feedback
```

3. `Band` 是一等治理单元，不应只是 `ArcEnvelopeManager` 或 repair fallback 里的临时概念。

4. Genesis 是 Project 根层真值，`start-writing` 是 Genesis 与写作链的唯一交接点。Genesis 完成后应默认停在 `genesis_ready`，不能自动写章。

5. 写作任务状态机明确把 Genesis 排除在正式写作任务之外；正式写作任务入口是 `/api/generate`、`/start-writing`、`/continue-generation`。

6. Repair 链当前固定为三次：

```text
draft -> chapter_plan -> band_plan
```

其中 `band_plan` 语义是“调整当前 band 的节奏、payoff 和章节执行约束，然后重写当前章”，不是全局 arc 重排。

这几个原则决定了本次重构的方向：**三层 plan 必须成为明确服务；人类输入必须通过 Human Workspace/Action 进入；后台自动化只能消费已锁定或已批准的计划。**

---

## 2. 当前主要耦合点

### 2.1 `ArcEnvelopeManager._persist_experience_overlay()` 把 Band Plan 混进 Arc Envelope

当前 `_persist_experience_overlay()` 同时做了以下事情：

```text
1. 根据 activation_chapter 和 detailed_band_size 计算当前 band window
2. 调 _derive_band_delight_schedule() 生成 BandDelightSchedule
3. 调 subworld_manager.plan_band_activation() 生成 active_subworld_ids / chapter_entry_targets
4. 删除并重写 BandExperiencePlan
5. 为 active_band 每一章生成 ChapterExperiencePlan
6. 直接修改 ChapterPlan.experience_plan_json
7. 再次调用 _persist_world_contracts()
```

问题：

- `ArcEnvelopeManager` 应该负责 arc envelope sizing / resolution，不应该负责 band 爽点计划、subworld activation、chapter experience overlay、world contracts。
- `BandExperiencePlan` 现在虽然有表，但生成逻辑没有独立成 `BandPlanner` 或 `BandPlanService`。
- `Band` 作为一等治理单元的设计，在代码里被压成了 `ArcEnvelopeManager` 的副作用。

应拆出：

```text
forwin/experience/band_planner.py
forwin/experience/chapter_overlay.py
forwin/planning/band_plan_service.py
forwin/planning/world_contract_service.py
```

---

### 2.2 `ArcEnvelopeManager.ensure_active_arc_resolution()` 过度编排

当前 `ensure_active_arc_resolution()` 同时负责：

```text
1. 激活当前 arc
2. 初始化 subworld registry
3. 读取或创建 ArcEnvelope
4. 读取 / 创建 ArcStructureDraft
5. 根据 Genesis persisted sizing 或旧逻辑计算 base target / soft min / soft max
6. 运行 ScenarioRehearsalCoordinator
7. 可选运行 legacy provisional preview
8. 解析 envelope resolution
9. 写 ArcEnvelope / ArcEnvelopeAnalysis / ProvisionalBandExecution
10. 写 experience overlay
11. 写 world contracts
```

问题：

- 这是一个混合了 arc sizing、arc structure、scenario rehearsal、legacy provisional、band overlay、world contracts、subworld registry 的超级流程。
- 设计上 `Arc Envelope / Provisional` 是写作侧一段流程，但不是整个三层计划系统本身。

应拆成：

```text
ArcActivationService
ArcEnvelopeResolver
ArcStructurePlanner
ScenarioRehearsalService
ProvisionalPreviewService
BandPlanService
WorldContractService
```

`ArcEnvelopeManager` 最终应只保留一个 facade：

```python
class ArcEnvelopeManager:
    def ensure_active_arc_resolution(...):
        active_arc = arc_activation.activate(...)
        envelope = arc_envelope_resolver.resolve(...)
        arc_structure = arc_structure_service.ensure(...)
        band_plan_service.ensure_current_band_plan(...)
        scenario_rehearsal_service.run_if_needed(...)
        return envelope
```

---

### 2.3 `ArcEnvelopeManager._build_structure_draft()` 把 Arc Structure 和 Experience Design 混在一起

当前 `_build_structure_draft()` 会调用 `director.draft_arc_structure()`，并返回：

```text
phase_layout
key_beats
thread_priorities
hotspot_candidates
compression_candidates
reader_promise
arc_payoff_map
```

其中前 5 项偏 arc 结构，后 2 项是明确的读者体验 / 爽点设计。

问题：

- `ArcDirector` 与 `ArcEnvelopeManager` 共同承担了 Arc 结构和 Experience 设计。
- 爽点设计没有作为独立模块出现，导致 band/chapter experience 只能跟着 envelope 生成。

应拆成两个服务：

```text
forwin/planning/arc_structure_planner.py
  -> ArcStructureDraftData without reader_promise / arc_payoff_map

forwin/experience/arc_experience_planner.py
  -> ReaderPromise + ArcPayoffMap
```

新的数据流：

```text
ArcPlanVersion + ChapterPlan seed + audience trends
  -> ArcStructurePlanner
  -> ArcStructureDraft

ArcStructureDraft + Genesis book_brief/core_delight + audience trends
  -> ArcExperiencePlanner
  -> ReaderPromise + ArcPayoffMap
```

---

### 2.4 `api_project_ops.start_project_writing()` 把人类交接动作和后台副作用混在一个 API handler

当前 `start_project_writing()` 在一个函数里做了：

```text
1. 校验 genesis_ready
2. 校验模型配置
3. 校验 active generation task
4. 写 START_WRITING_REQUESTED DecisionEvent，actor_type=manual_ui
5. materialize_book_arcs()
6. materialize_arc_chapter_plans()
7. 从 Genesis world_bible 写 project.setting_summary
8. 从 Genesis map_atlas 生成 BookMap
9. 出错时 rollback 并写 MAP_GENERATION_FAILED
10. 创建 continue generation task
```

问题：

- “人类点击启动写作”应该只是一条明确的 command，不应直接散落执行所有 materialization、map generation、task enqueue。
- Genesis 设计器和写作自动化的边界被 API handler 连接得太紧。
- Map generation 是 start-writing handoff 的一部分，但应该是独立 `GenesisHandoffService` 的步骤，不应写在项目 API ops 里。

应拆成：

```text
forwin/genesis/workspace_service.py      # 人类编辑 / generate / refine / lock
forwin/genesis/handoff_service.py        # start-writing handoff
forwin/genesis/materializer.py           # arcs / chapter plans materialization
forwin/genesis/map_handoff.py            # Genesis map_atlas -> BookMap
forwin/production/task_enqueue.py        # 创建生成任务
```

API handler 最终只应做：

```python
command = StartWritingCommand(project_id=project_id, actor="manual_ui")
result = genesis_handoff_service.start_writing(command)
return StartWritingResponse(...)
```

---

### 2.5 `BookGenesisService` 同时承担 Genesis 设计器、LLM 生成器、命名器、规范化器和 materializer

`book_genesis.py` 当前集中包含：

```text
1. Genesis 六阶段定义
2. 每阶段 prompt / hard rules
3. fallback world / map / story_engine / book_blueprint
4. pack normalize / stage path patch / lock 判断
5. LLM 调用、parse、PromptTrace
6. culture name generation
7. materialize arcs / chapter plans
8. map service import 相关入口
```

问题：

- Genesis 作为“需要人类输入的设计器”应该优先是 workspace/domain service。
- LLM stage generation、字段 refine、命名建议、start-writing materialization 是不同能力。
- 现在这个服务太大，后续任何 Genesis UI、自动补全、handoff、回滚都会继续堆进同一文件。

建议拆分：

```text
forwin/genesis/
  schema.py                 # GENESIS_STAGE_ORDER, stage keys, pack helpers
  workspace_service.py      # create/patch/generate/refine/lock/build_detail
  stage_generator.py        # LLM prompt + fallback + parse
  name_service.py           # CultureNameGenerator facade
  normalizer.py             # world/map/story_engine/book_blueprint normalize
  materializer.py           # BookGenesisPack -> ArcPlanVersion/ChapterPlan
  handoff_service.py        # start-writing 状态迁移
  trace_service.py          # PromptTrace/DecisionEvent 写入
```

---

### 2.6 `api_automation.run_automation_scheduler_pass()` 还不是独立写作更新节奏规划器

当前自动化调度直接在 `api_automation.py` 里：

```text
1. 读取所有 project
2. 读取 automation_json
3. 判断 enabled / daily_start_time / last_scheduler_date
4. 聚合 pending_review / total_plans / pending_numbers / active_generation_project_ids
5. 若 pending_review 则暂停
6. 按 daily_chapter_quota 创建 initial generation 或 continue generation task
7. 写 last_scheduler_action/message/task_id
```

问题：

- 这是“每天生成几章”的 API helper，不是完整的“写作更新节奏规划器”。
- 还没有独立表达：每天规划几章、写几章、review 几章、发布几章。
- human-configured rhythm 和 automation executor 没有拆开。
- `auto_publish` 与 `publish_bindings` 还没有被纳入一个统一的 production plan。

建议新建：

```text
forwin/production/
  policy.py              # ProductionPolicy / StageQuota
  backlog.py             # ProductionBacklog
  planner.py             # ProductionPlanPlanner
  scheduler.py           # ProductionScheduler
  executor.py            # ProductionPlanExecutor
  repository.py          # ProductionPlan / run history persistence
```

核心模型：

```python
class StageQuota(BaseModel):
    plan: int = 0
    write: int = 1
    review: int = 0
    publish: int = 0

class ProductionPolicy(BaseModel):
    enabled: bool = False
    daily_start_time: str = "09:00"
    quota: StageQuota = StageQuota(write=1)
    stop_when_review_pending: bool = True
    auto_publish: bool = False
    max_active_generation_tasks: int = 1
    max_active_upload_tasks: int = 1

class ProductionBacklog(BaseModel):
    needs_plan: list[int]
    planned_unwritten: list[int]
    drafted_unreviewed: list[int]
    reviewed_unpublished: list[int]
    failed: list[int]

class ProductionPlan(BaseModel):
    project_id: str
    date: str
    plan_chapters: list[int]
    write_chapters: list[int]
    review_chapters: list[int]
    publish_chapters: list[int]
    blocked_reason: str = ""
```

`api_automation.run_automation_scheduler_pass()` 应降级为 facade：

```python
production_scheduler.run_due_projects(now=utcnow())
```

---

### 2.7 `ContextAssembler` 同时做上下文拼装、Genesis 投影、BookState overlay、Map graph、人格 integrity gate

当前 `assemble_context()` 及其 helper 负责：

```text
1. 读取 project
2. 读取 active Genesis revision 并生成 genesis_context_refs / world overview / map overview / story_engine summary
3. 读取 allowed entities / relations / threads / summaries / timeline
4. 读取 NPC intents / world pressure / arc envelope / reader promise / payoff map / band schedule
5. 构造 map context / review graph
6. 构造 BookState overlay 并合并 active_locations
7. 检查 personality integrity 并可能写 DecisionEvent
```

问题：

- “上下文拼装”与“前置 gate / integrity 检查”混在一起。
- Genesis、BookState、Map、Experience、Personality 都是独立 context provider，但现在全写在一个 assembler。
- 写 DecisionEvent 是 side effect，不应该发生在纯 context assembly 主函数里。

建议拆成 provider chain：

```text
forwin/context/providers/
  genesis_provider.py
  state_provider.py
  experience_provider.py
  map_provider.py
  book_state_provider.py
  personality_provider.py
  feedback_provider.py

forwin/context/gates/
  personality_integrity_gate.py
  context_integrity_gate.py
```

目标接口：

```python
class ContextProvider(Protocol):
    def contribute(self, request: ContextRequest, draft: ContextDraft) -> None: ...

class ContextGate(Protocol):
    def validate(self, context: ChapterContextPack) -> list[ContextIssue]: ...
```

---

### 2.8 `WebNovelExperienceReviewer` 混合了体验审查、Map movement、人格审查、LLM WNER 和 heuristic fallback

当前 `WebNovelExperienceReviewer` 同时包含：

```text
1. planned vs delivered reward 检查
2. stall / hook / immersion / understanding / emotion scoring
3. scheduled_reward_missing
4. rule consistency
5. map movement issue
6. personality context payload
7. LLM reviewer prompt/repair/trace
8. heuristic fallback
```

问题：

- Map movement 不是 webnovel experience，应该是 `MapMovementReviewer`。
- 人格一致性应归 `PersonalityConsistencyReviewer` 或 BookState cognition gate。
- WebNovelExperienceReviewer 应只关心 `ChapterExperiencePlan / BandDelightSchedule / WriterOutput`。

建议拆成：

```text
forwin/reviewer/experience.py
forwin/reviewer/map_movement.py
forwin/reviewer/personality.py
forwin/reviewer/llm_webnovel.py
forwin/reviewer/hub.py
```

`HistoricalReviewHub` 保持聚合器角色：

```text
continuity reviewer
+ governance reviewer
+ experience reviewer
+ map movement reviewer
+ personality reviewer
+ lint collector
```

---

### 2.9 `WritingOrchestrator` 仍然是依赖装配器 + 主流程 + 状态机 +治理事件记录器

当前 `WritingOrchestrator.__init__()` 仍然直接创建：

```text
LLMClient
ArcDirector
BookGenesisService
SubWorldManager
RetrievalBroker
ArtifactStore
ChapterWriter
PacingStrategist
ReplanGovernor
NPCIntentGenerator
WorldSimulator
ArcEnvelopeManager
HistoricalReviewHub
RepairPolicy
RepairVerifier
FinalAcceptanceGate
```

问题：

- Orchestrator 应该只编排流程，不应该自己装配所有依赖。
- 这会阻碍 planner / writer / reviewer / band planner / scheduler 的单独测试和替换。

建议拆：

```text
forwin/runtime/container.py       # 创建依赖
forwin/orchestration/writing.py   # 只保留流程编排
forwin/orchestration/events.py    # stage transition / governance event
forwin/orchestration/chapter_pipeline.py
```

---

### 2.10 `PublisherManager` 仍然混合上传任务、浏览器 session、连接状态、comment sync、audit event

当前 `PublisherManager` 负责：

```text
1. 平台列表与连接状态
2. extension client heartbeat
3. browser session cookie 存储/加解密/选择
4. upload job 创建、批量创建、claim、取消、删除、结果更新
5. comment sync job
6. publisher DecisionEvent 写入
```

问题：

- 对 production scheduler 来说，发布只是一个 stage，不应直接依赖一个大而全 manager。
- 对人类来说，登录/授权/浏览器 session 是手动 workspace；上传 job 执行是后台 worker。

建议拆：

```text
forwin/publisher_runtime/
  connection_state.py
  browser_sessions.py
  upload_jobs.py
  comment_sync_jobs.py
  audit.py
  service.py
```

`PublisherManager` 可先保留为 facade，逐步转发到新 service，避免一次性破坏 API。

---

## 3. 目标三层 Plan 架构

### 3.1 三层定义

```text
Arc Plan
  来源：Genesis book_arc_blueprint + ArcPlanVersion + ArcEnvelope
  职责：arc 边界、目标、章节范围、结构阶段、主线优先级、尺寸弹性

Band Plan
  来源：ArcStructureDraft + ArcExperiencePlan + active chapter range + audience trends
  职责：当前 band 的节奏、爽点密度、问题梯子、任务契约、world contract、subworld activation

Chapter Plan
  来源：ChapterPlan + BandPlan + ChapterExperiencePlan + ChapterWorldDeltaIntent
  职责：单章目标、写作约束、体验兑现点、世界变化意图、实体准入规则
```

### 3.2 新的 plan bundle

```python
class ArcPlanningBundle(BaseModel):
    project_id: str
    arc_id: str
    arc_structure_id: str
    envelope_id: str
    reader_promise: ReaderPromise
    arc_payoff_map: ArcPayoffMap

class BandPlanningBundle(BaseModel):
    project_id: str
    arc_id: str
    band_id: str
    chapter_start: int
    chapter_end: int
    delight_schedule: BandDelightSchedule
    world_contract: BandWorldContract
    task_contract: list[PlanTaskItem]
    active_subworld_ids: list[str]

class ChapterPlanningOverlay(BaseModel):
    project_id: str
    arc_id: str
    band_id: str
    chapter_number: int
    experience_plan: ChapterExperiencePlan
    world_delta_intent: ChapterWorldDeltaIntent
    task_contract: list[PlanTaskItem]
    entity_admission_rule: str
```

### 3.3 新的数据流

```text
BookGenesisPack.book_arc_blueprint
  -> GenesisMaterializer
  -> ArcPlanVersion(planned_target_size, soft_min, soft_max)

ArcPlanVersion + ChapterPlan seed
  -> ArcEnvelopeResolver
  -> ArcEnvelope

ArcEnvelope + ChapterPlan seed + audience trends
  -> ArcStructurePlanner
  -> ArcStructureDraft

ArcStructureDraft + Genesis brief/core_delight + audience trends
  -> ArcExperiencePlanner
  -> ReaderPromise + ArcPayoffMap

ArcPlanningBundle + current band window
  -> BandPlanService
  -> BandExperiencePlan + BandWorldContract + task contracts

BandPlanningBundle + ChapterPlan
  -> ChapterPlanOverlayService
  -> ChapterPlan.experience_plan_json + ChapterWorldDeltaIntent

ContextAssembler
  -> only loads bundles; does not create them
```

---

## 4. 人类输入模块与后台自动化边界

### 4.1 人类输入模块

这些模块应该属于 Human Workspace，不应由后台 scheduler 隐式执行：

```text
1. Genesis Designer
   - brief/world/map/story_engine/book_blueprint/bootstrap 的编辑、生成、重生、锁定
   - refine instruction
   - name suggestion
   - start-writing handoff approval

2. Writing Rhythm Planner
   - 一天计划几章
   - 一天写几章
   - 一天 review 几章
   - 一天发布几章
   - 遇到 pending review 是否停止
   - 是否自动发布
   - 发布平台与绑定配置

3. Review Desk
   - review approve / reject / patch
   - band_plan repair 需要人工确认时的修改入口
   - force accept 决策

4. Publisher Connection Workspace
   - 平台登录
   - 浏览器 session 同步
   - 账号状态确认
   - book binding / upload url / create_if_missing

5. Map / World Merge Workspace
   - Genesis map_atlas 到 BookMap 的冲突处理
   - legacy import conflict report
   - region promotion / merge approval
```

### 4.2 后台自动化模块

这些模块应只消费已批准/已锁定的配置：

```text
1. GenesisStageGenerator
   - 用户触发后运行，不自己推进到写作

2. GenesisHandoffService
   - 用户 start-writing 后执行一次 handoff

3. ProductionScheduler
   - 读取 ProductionPolicy，生成 ProductionPlan
   - 不修改 Genesis 根层
   - 不绕过 Review Desk

4. WritingOrchestrator
   - 消费 ChapterPlan / BandPlan / Context，写 draft/review/canon

5. PublisherRuntime
   - 消费 publish job，执行上传或 comment sync
   - 不决定今天要发布几章；只执行 ProductionPlan 给出的 job
```

### 4.3 边界规则

1. 所有人类输入必须落成 `DecisionEvent(actor_type="manual_ui")` 或等价 human command。
2. 所有自动任务必须落成 `DecisionEvent(actor_type="system" | "scheduler" | "extension")`。
3. Scheduler 不直接改 Genesis pack、不直接 approve review、不直接 force accept。
4. Writer 不创建 band plan；writer 只消费 context。
5. Reviewer 不改 plan；reviewer 只产出 `ReviewVerdict` 和 `RepairInstruction`。
6. Repair executor 才能根据 `RepairInstruction` 修改 draft / chapter_plan / band_plan。
7. Publisher worker 不决定 publish cadence，只执行 upload job。

---

## 5. 分阶段实施计划

### M0：建立重构护栏

目标：在拆之前锁住行为。

动作：

1. 给以下流程加 snapshot / regression test：
   - Genesis create -> lock all stages -> start-writing
   - start-writing 只 materialize active arc chapter plans
   - ensure_active_arc_resolution 产出 envelope / structure / band experience plan
   - context assembler 能读到 reader_promise / band schedule / chapter experience plan
   - review fail 后 repair scope 序列是 draft -> chapter_plan -> band_plan
   - automation pending_review 时不继续写
2. 对 `_persist_experience_overlay()` 当前输出做 golden fixture。
3. 对 `BandExperiencePlan.schedule_json` 和 `ChapterPlan.experience_plan_json` 做 schema assertion。

验收：行为不变，测试先红后绿。

---

### M1：抽 `ExperiencePlanningService`

目标：把爽点规划从 `ArcEnvelopeManager` 中拿出来。

新增：

```text
forwin/experience/planner.py
forwin/experience/band_scheduler.py
forwin/experience/chapter_planner.py
forwin/experience/repository.py
```

迁移：

```text
ArcEnvelopeManager._derive_band_delight_schedule
ArcEnvelopeManager._derive_chapter_experience_plan
ArcEnvelopeManager._build_audience_calibration_profile
```

改造后：

```python
schedule = experience_planning_service.plan_band(...)
chapter_overlay = experience_planning_service.plan_chapter(...)
experience_repository.save_band_schedule(...)
experience_repository.save_chapter_overlay(...)
```

验收：

- `_persist_experience_overlay()` 不再包含具体爽点算法。
- `BandExperiencePlan` 与 `ChapterPlan.experience_plan_json` 输出不变。

---

### M2：抽 `BandPlanService`

目标：让 `Band` 成为真正一等计划单元。

新增：

```text
forwin/planning/band_plan_service.py
forwin/planning/band_window.py
forwin/planning/band_repository.py
```

职责：

```text
1. 计算 band window
2. 加载 active_band chapter plans
3. 生成 BandPlanningBundle
4. 调 experience planner
5. 调 subworld activation planner
6. 调 world contract planner
7. 持久化 BandExperiencePlan / BandWorldContract / Chapter overlays
```

改造后：

```python
band_plan_service.ensure_current_band_plan(
    project_id=project_id,
    arc_id=arc_id,
    activation_chapter=activation_chapter,
    envelope=envelope,
    arc_structure=structure,
)
```

验收：

- `ArcEnvelopeManager._persist_experience_overlay()` 删除或只剩一行 facade。
- band plan 可被 repair executor 独立调用。
- band plan 可被人类 Review Desk 展示和 patch。

---

### M3：抽 `WorldContractService`

目标：把 `_persist_world_contracts()` 从 ArcEnvelopeManager 和 experience overlay 中移出。

新增：

```text
forwin/planning/world_contract_service.py
```

职责：

```text
1. 生成 ArcWorldContract
2. 生成 BandWorldContract
3. 生成 ChapterWorldDeltaIntent
4. 保存到 WorldContractRepository
```

验收：

- `_persist_world_contracts()` 不再被 experience overlay 调用。
- `BandPlanService` 明确调用 `WorldContractService`。
- V4 reviewer gate 仍可读取原有 contract。

---

### M4：瘦身 `ArcEnvelopeManager`

目标：让它只负责 Arc Envelope。

保留职责：

```text
1. arc activation
2. sizing source selection
3. ArcEnvelope / ArcEnvelopeAnalysis persistence
4. current_projected_size / confidence
```

移出职责：

```text
1. arc structure draft generation
2. experience overlay
3. band plan
4. world contracts
5. scenario rehearsal
6. provisional preview
7. subworld registry initialization
```

建议最终文件：

```text
forwin/orchestrator/phase24.py                # thin facade / backward compatibility
forwin/planning/arc_envelope.py               # ArcEnvelopeResolver
forwin/planning/arc_structure_service.py
forwin/planning/scenario_rehearsal_service.py
forwin/planning/provisional_preview_service.py
```

验收：

- `ArcEnvelopeManager.ensure_active_arc_resolution()` 低于 80 行。
- 任何 band/chapter overlay 算法不在该文件内。

---

### M5：拆 Genesis Designer 与 Handoff

目标：让“需要人类输入的 Genesis 工作台”和“后台写作 handoff”边界清楚。

新增：

```text
forwin/genesis/schema.py
forwin/genesis/workspace_service.py
forwin/genesis/stage_generator.py
forwin/genesis/refine_service.py
forwin/genesis/name_service.py
forwin/genesis/materializer.py
forwin/genesis/map_handoff.py
forwin/genesis/handoff_service.py
```

迁移：

```text
BookGenesisService.create_initial_revision -> workspace_service
BookGenesisService.generate_stage -> stage_generator + workspace_service
BookGenesisService.refine_stage -> refine_service
BookGenesisService.generate_name_suggestions -> name_service
BookGenesisService.materialize_book_arcs -> materializer
BookGenesisService.materialize_arc_chapter_plans -> materializer
api_project_ops._ensure_initial_book_map_from_genesis -> map_handoff
api_project_ops.start_project_writing -> handoff_service facade
```

验收：

- Genesis Designer 不创建 generation task。
- Handoff Service 是唯一能从 `genesis_ready` 切到 `writing` 的服务。
- start-writing API handler 不直接 materialize arcs/chapter/map。

---

### M6：实现 Production Scheduler / 写作更新节奏规划器

目标：把“今天做什么”从 API helper 变成独立模块。

新增：

```text
forwin/production/policy.py
forwin/production/backlog.py
forwin/production/planner.py
forwin/production/scheduler.py
forwin/production/executor.py
forwin/production/repository.py
```

UI / human input 对应：

```text
ProductionPolicyEditor
  - daily_start_time
  - plan quota
  - write quota
  - review quota
  - publish quota
  - stop_when_review_pending
  - auto_publish
  - publish bindings
```

后台执行：

```text
ProductionScheduler.run_due_projects()
  -> ProductionPlanner.plan_today()
  -> ProductionPlanExecutor.enqueue_generation_tasks()
  -> ProductionPlanExecutor.enqueue_review_tasks_or_reminders()
  -> ProductionPlanExecutor.enqueue_publish_jobs()
```

验收：

- `api_automation.py` 只调用 `ProductionScheduler`。
- 可以单独测试：pending review 阻塞、publish backlog、daily quota、active task conflict。
- `daily_chapter_quota` 兼容迁移到 `quota.write`。

---

### M7：拆 Context Provider 与 Reviewer

目标：减少上下文拼装和 review 的横向耦合。

Context：

```text
forwin/context/providers/genesis_provider.py
forwin/context/providers/experience_provider.py
forwin/context/providers/map_provider.py
forwin/context/providers/book_state_provider.py
forwin/context/providers/personality_provider.py
forwin/context/gates/personality_integrity_gate.py
```

Reviewer：

```text
forwin/reviewer/experience.py
forwin/reviewer/map_movement.py
forwin/reviewer/personality.py
forwin/reviewer/llm_webnovel.py
```

验收：

- `assemble_context()` 只做 provider orchestration。
- `WebNovelExperienceReviewer` 不再包含 map movement 逻辑。
- `HistoricalReviewHub` 成为明确 aggregator。

---

### M8：拆 Publisher Runtime

目标：让发布成为 Production stage，浏览器登录成为 Human Workspace。

新增：

```text
forwin/publisher_runtime/connection_state.py
forwin/publisher_runtime/browser_sessions.py
forwin/publisher_runtime/upload_jobs.py
forwin/publisher_runtime/comment_sync_jobs.py
forwin/publisher_runtime/audit.py
forwin/publisher_runtime/service.py
```

保留：

```text
forwin/publishers/manager.py  # facade，兼容旧 API
```

验收：

- ProductionPlanExecutor 只依赖 `UploadJobService`，不依赖整个 `PublisherManager`。
- Publisher Connection Workspace 只管登录/session/binding。
- Browser extension 只执行 claim 到的 job，不决定 publish cadence。

---

### M9：引入 Runtime Container / DI

目标：把依赖装配从 `WritingOrchestrator` 移出。

新增：

```text
forwin/runtime/container.py
forwin/runtime/factories.py
forwin/orchestration/writing_orchestrator.py
forwin/orchestration/chapter_pipeline.py
```

验收：

- `WritingOrchestrator.__init__()` 不再 new 大量具体对象。
- planner / writer / reviewer / band planner / scheduler 都可以注入 mock 单测。

---

## 6. 推荐的最终目录结构

```text
forwin/
  runtime/
    container.py
    factories.py

  orchestration/
    writing_orchestrator.py
    chapter_pipeline.py
    events.py

  genesis/
    schema.py
    workspace_service.py
    stage_generator.py
    refine_service.py
    name_service.py
    normalizer.py
    materializer.py
    map_handoff.py
    handoff_service.py
    trace_service.py

  planning/
    arc_envelope.py
    arc_structure_service.py
    band_window.py
    band_plan_service.py
    world_contract_service.py
    scenario_rehearsal_service.py
    provisional_preview_service.py

  experience/
    arc_planner.py
    band_scheduler.py
    chapter_planner.py
    repository.py
    templates.py

  production/
    policy.py
    backlog.py
    planner.py
    scheduler.py
    executor.py
    repository.py

  context/
    assembler.py
    providers/
      genesis_provider.py
      experience_provider.py
      map_provider.py
      book_state_provider.py
      personality_provider.py
    gates/
      personality_integrity_gate.py

  reviewer/
    hub.py
    experience.py
    map_movement.py
    personality.py
    governance.py
    llm_webnovel.py

  publisher_runtime/
    connection_state.py
    browser_sessions.py
    upload_jobs.py
    comment_sync_jobs.py
    audit.py
    service.py
```

---

## 7. 关键接口草案

### 7.1 Band Plan

```python
class BandPlanService:
    def ensure_current_band_plan(
        self,
        *,
        session: Session,
        project_id: str,
        arc_id: str,
        activation_chapter: int,
        envelope: ArcEnvelope,
        structure: ArcStructureDraftData,
    ) -> BandPlanningBundle:
        ...
```

### 7.2 Experience Planning

```python
class ExperiencePlanningService:
    def plan_arc_experience(...) -> ArcExperienceBundle: ...
    def plan_band_experience(...) -> BandDelightSchedule: ...
    def plan_chapter_experience(...) -> ChapterExperiencePlan: ...
```

### 7.3 Genesis Handoff

```python
class GenesisHandoffService:
    def start_writing(self, command: StartWritingCommand) -> StartWritingResult:
        # validate genesis_ready
        # materialize arcs
        # materialize active arc chapter plans
        # ensure book map from genesis
        # mark project writing
        # enqueue writing task
        ...
```

### 7.4 Production Scheduler

```python
class ProductionScheduler:
    def run_due_projects(self, *, now: datetime) -> list[ProductionRunResult]: ...

class ProductionPlanner:
    def plan_today(
        self,
        *,
        project: Project,
        policy: ProductionPolicy,
        backlog: ProductionBacklog,
        now: datetime,
    ) -> ProductionPlan: ...
```

### 7.5 Human Action Boundary

```python
class HumanAction(BaseModel):
    actor_type: Literal["manual_ui", "operator"] = "manual_ui"
    project_id: str
    action_type: str
    payload: dict[str, object] = {}
    reason: str = ""

class AutomationAction(BaseModel):
    actor_type: Literal["system", "scheduler", "extension"]
    project_id: str
    action_type: str
    payload: dict[str, object] = {}
```

---

## 8. 迁移策略

### 8.1 保持兼容 facade

不要一次性删除旧类。先保留：

```text
BookGenesisService
ArcEnvelopeManager
PublisherManager
api_automation.run_automation_scheduler_pass
```

但内部改成转发：

```python
class ArcEnvelopeManager:
    def __init__(self, services: PlanningServices): ...
    def ensure_active_arc_resolution(...):
        return services.arc_resolution_workflow.ensure(...)
```

### 8.2 数据库不用第一阶段大改

现有表先保留：

```text
ArcStructureDraft
BandExperiencePlan
ChapterPlan.experience_plan_json
WorldContractRepository 现有存储
Project.automation_json
PublisherUploadJob
```

新增 schema 可以先用 JSON 兼容，等服务边界稳定后再建新表。

### 8.3 每次只拆一个方向

推荐顺序：

```text
Experience -> BandPlan -> WorldContract -> ArcEnvelope -> Genesis -> Production -> Context/Reviewer -> Publisher -> Runtime DI
```

不要先拆 `WritingOrchestrator`，否则会在大流程里同时改太多依赖。

---

## 9. 验收清单

### 行为不变

- [ ] Genesis 完成前不能启动写作。
- [ ] start-writing 只为 active arc 生成 ChapterPlan。
- [ ] ArcEnvelope sizing 仍优先读取 `ArcPlanVersion.planned_*`。
- [ ] 当前 band 的 `BandExperiencePlan.schedule_json` 与重构前等价。
- [ ] `ChapterPlan.experience_plan_json` 与重构前等价。
- [ ] `ContextAssembler` 仍能读到 reader_promise / arc_payoff_map / band_delight_schedule / chapter_experience_plan。
- [ ] Review fail 的 repair scope 序列仍是 `draft -> chapter_plan -> band_plan`。
- [ ] pending review 时自动调度不继续生成。
- [ ] publisher upload job 生命周期事件仍存在。

### 边界变清楚

- [ ] `ArcEnvelopeManager` 不再直接生成 band/chapter experience overlay。
- [ ] `BandPlanService` 可以被 repair executor 单独调用。
- [ ] `GenesisHandoffService` 是唯一从 `genesis_ready` 到 `writing` 的入口。
- [ ] `ProductionScheduler` 可以独立计算今天计划写/审/发哪些章节。
- [ ] Publisher worker 不决定发布节奏。
- [ ] Human action 和 automation action 的 `actor_type` 不混用。

### 单测建议

```bash
python3 -m pytest \
  tests/test_book_genesis_flow.py \
  tests/test_governance_review_and_checkpoint.py \
  tests/test_scenario_rehearsal_resolution.py \
  tests/test_project_publish_bindings.py \
  tests/test_api_task_routes.py \
  tests/test_api_split_modules.py \
  -q
```

新增测试：

```text
tests/test_experience_planning_service.py
tests/test_band_plan_service.py
tests/test_genesis_handoff_service.py
tests/test_production_scheduler.py
tests/test_context_provider_chain.py
tests/test_publisher_runtime_services.py
```

---

## 10. 优先级排序

| 优先级 | 工作 | 原因 |
|---|---|---|
| P0 | 抽 ExperiencePlanningService + BandPlanService | 直接解决你指出的 band plan 耦合 |
| P0 | 抽 WorldContractService | 当前 world contracts 被 overlay 重复触发，边界不清 |
| P1 | 瘦身 ArcEnvelopeManager | 三层 plan 的主耦合源 |
| P1 | 拆 Genesis Workspace / Handoff | 人类设计器与后台写作边界最关键 |
| P1 | Production Scheduler | 写作更新节奏规划器需要从 API helper 升级为模块 |
| P2 | Context Provider Chain | 降低 context assembler 横向耦合 |
| P2 | Reviewer 拆分 | 避免 experience reviewer 继续吞 map/personality 规则 |
| P2 | Publisher Runtime | 发布作为 production stage 接入 |
| P3 | Runtime Container / DI | 等业务模块边界稳定后再做 |

---

## 11. 最小可落地切入点

第一批 PR 建议只做三件事：

```text
PR-1: 新建 forwin/experience/*，迁移 band/chapter experience 生成算法，保持输出不变。
PR-2: 新建 forwin/planning/band_plan_service.py，让 _persist_experience_overlay() 变成 facade。
PR-3: 新建 forwin/planning/world_contract_service.py，把 _persist_world_contracts() 从 phase24 移出。
```

第一批完成后，`ArcEnvelopeManager` 仍可存在，但它不再是 band plan 的真实主人。之后再拆 Genesis 和 Production Scheduler，风险会低很多。

---

## 12. 源码证据索引

| 证据 | 路径 | 说明 |
|---|---|---|
| 设计基线 | `Design-docs/V2_9_2.md` | 定义对象层级、Genesis 边界、start-writing 交接、Band 一等治理单元、repair scope 语义 |
| 写作状态机 | `Design-docs/writing_flow_state_machine.md` | 明确 Genesis 不属于正式写作任务状态机，start-writing 后进入写作链 |
| 残余设计收束 | `Design-docs/V4.5.1_markstone.md` | 明确后端 contract、Genesis map -> BookMap 合并、audit、movement policy 等残余边界 |
| Band plan 耦合 | `forwin/orchestrator/phase24.py` | `ArcEnvelopeManager._persist_experience_overlay()` 同时处理 band schedule、subworld activation、chapter overlay、world contracts |
| Genesis 耦合 | `forwin/book_genesis.py` | Genesis stage、prompt、fallback、normalization、LLM、命名、materialization 集中在一个服务 |
| start-writing 耦合 | `forwin/api_project_ops.py` | API handler 同时处理 manual request、materialization、map generation、task enqueue |
| 自动调度耦合 | `forwin/api_automation.py` | daily scheduler 与 API callback、DB、task creation 混在一起，只支持 daily_chapter_quota |
| Context 耦合 | `forwin/context/assembler.py` | Genesis、BookState、Map、Experience、Personality context 和 integrity side effect 混在一起 |
| Reviewer 耦合 | `forwin/reviewer/webnovel.py` | Experience reviewer 内含 map movement、personality、LLM/heuristic review |
| Repair scope | `forwin/reviser/policy.py`, `forwin/protocol/review.py` | 固定 repair scope sequence 与 legacy/v4 scope normalization |
| Publisher 耦合 | `forwin/publishers/manager.py` | 上传任务、浏览器 session、连接状态、comment sync、audit event 混在一个 manager |
| Orchestrator 耦合 | `forwin/orchestrator/loop.py` | 依赖装配、legacy planning、writing flow、governance event 全在一个类 |
