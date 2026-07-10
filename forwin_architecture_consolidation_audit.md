# ForWin 架构收敛审计（修订版 v2）

**仓库：** `Ctwqk/ForWin`（本地 `master` @ `1272252`，2026-07-09）
**审计方式：** 实地核验代码（forwin/ 606 个 Python 文件、tests/ 289 个测试文件）、配置、设计文档与调用链；初版报告（GPT Pro）作为对照底稿。
**结论先行：** 应该做架构收敛，且初版报告的大方向正确；但初版基于稍早的 master，遗漏了 2026-07-09 当天落地的 `reckless review` 模式、已经部分落地的 EntityRegistrar，以及最核心的结构性负债——`WritingOrchestrator` 猴子补丁拼装。本修订版据此调整了合并决策和实施顺序。

## 2026-07-09 实施校正

本报告的 Phase A 已按更激进的 v5 方案完成，不再采用报告初稿中的兼容迁移设计：

- `InfrastructureConfig` 只保留进程基础设施和环境模型目录；旧 `Config` 名称、模式字段和 API Key 持久化表面已删除。
- 每个项目只有一份带版本号的不可变 `RuntimePolicy`；可编辑项仅为 `standard/pulp`、模型 profile、章节长度、暂停策略和 `human/spark` gate 委托。
- 每个 durable generation task 保存完整且不可变的 policy snapshot；API、worker、scheduler、CLI、Genesis handoff 和自动续跑均经过 `GenerationApplicationService`。
- `RuntimeSettingsStore`、`governance_json` 运行时设置、request-level model/mode override、checkpoint/copilot/reckless 兼容映射全部删除。旧项目和旧设置文件不迁移。
- 控制台只读环境模型目录，项目抽屉是唯一 RuntimePolicy 写入口；MCP 用 `project_set_gate_delegate`，不再提供 reckless-mode 开关。

Slice 1 实施提交依次为 `a7f53bb`、`f7790c5`、`61392af`、`6c2eb0f`、`131e697`、`bc85d91`、`3e0c10f`、`a6a75fb`、`589e58a`、`e87e67b`，并由 completion commit 收口。本轮同时删除失效的旧架构测试大套件和无实现支撑的 review-engine cutover 脚本，常量化 chapter review form 主路径，并增加 production/UI/application/container boundary guard；后续 Phase B-F 不因此标记完成。

Phase B 已完成：零引用 `forwin/orchestration` ports 和 `_compile_world_model_after_acceptance` 空壳已删除；BookState canon 主路径已改名 `_commit_book_state_canon`；`HistoricalReviewHub` 已替换为 `DraftReviewService`；`FinalAcceptanceGate` 已合并为 `FinalResidualPolicy`；原 `reviewer`、`review_engine`、`reviser` 三个平级包已物理合并为 `forwin.review/{draft_service,decision,repair}`；唯一 candidate -> canon 决策体已迁入 `forwin.canon.CanonAdmissionService`；1056 行 live repair loop 已迁入 `forwin.review.repair.RepairService`，pipeline 只调用两个显式入口；canon/repair 专属 helper 注入已删除，`WritingOrchestrator._*` 拼装由 102 条降至 89 条；draft review 与 canon gate 通过 `QualityAnalysisRunRow` 共享有效 primary 分析。下文保留旧名称的段落是审计时基线证据，不代表当前代码仍保留旧入口。

Phase C 实现已完成：Genesis handoff 后 revision 永久冻结；`EntityRegistrar` 改为 `EntityAdmissionPlan` 规划/验证器，草稿阶段不再写 `Entity`/`EntityAlias`，`EntityAdmissionCommitter` 只在 Canon 成功路径落实无冲突计划；旧 SubWorld admission policy/patch/repair、checker 判决、nonblocking 例外和 summary 名字桥已删除。Planning 服务群由 `PlanningService` / `PlanningQuery` 归口，future audit、patch validation、scenario rehearsal 统一为 `PlanHealth`；`future_plan_auditor.py` 与 `phase24.PlanningServices` 转发门面已删除，orchestrator 拼装降至 87 条。200 章 no-hotfix 运行 gate 尚未执行，因此这里只标记实现完成。

Phase D 与 schema 收口已按批准的 hard-cut 方案完成：`BookStateQuery` / `ReviewQuery` 接管 accepted-state 读侧，WriterOutput 结构化合约与 `EntityAdmissionPlan` 统一转为 GraphDelta；旧 StateUpdater accepted-state 双写、StateRepository 通用读族和八张 legacy accepted-state ORM/表全部删除。`forwin.world_model` facade 被物理删除，live page/proposal/Obsidian/retrieval helper 迁入 `knowledge_system` / `obsidian`；保留的 `/world-model/*` 只是 BookState-backed HTTP adapter。历史 Alembic revision 链和 `models/base.py` 手写 `schema_migrations` / `_upgrade_*` 系统也已删除，生产只接受单一 `0001_v5_baseline`，旧库不迁移。下文关于双写、旧表或 deprecated world-model package 的内容只保留为审计时证据。

Phase E/F 的结构收口也已完成：`WritingOrchestrator`、`forwin.orchestrator`、`orchestrator_loop_core` 和 87 条残余类属性拼装全部删除，唯一运行管线为显式构造的 `forwin.generation.pipeline.ChapterPipeline`。Genesis 收口为单一 `forwin.genesis` 包；API 动态 module proxy、`api_core.exports`、`globals().update()` handler 注入以及根层 project/publisher/schema/payload 转发壳已删除。项目与 publisher 路由分别绑定 `ProjectApplicationService` / `PublisherApplicationService`，生成任务继续统一经过 `GenerationApplicationService`。同时清除拆分遗留的 2,336 个未使用 import，并把 `pipeline_core.common` / `genesis.constants` 的借道 re-export 改为真实 owner 直连。验证仅执行编译、静态未定义名、全仓测试收集和聚焦架构守卫；未宣称全量测试、部署或 30/60/100/200 章长跑完成。下文对旧入口和 Phase E 待办的描述同样只保留为审计时基线。

---

## 0. 对初版报告的核验结论

### 0.1 已确认的论断（可直接沿用）

| # | 初版论断 | 代码证据 |
|---|---|---|
| 1 | `operation_mode` 三分支（blackbox/checkpoint/copilot）仍在主循环活跃；只有 blackbox 自动接受 warn、失败后走 canon repair | `forwin/orchestrator_loop_core/project_chapters.py:475-503`、`:615` |
| 2 | `review_engine_live_cutover_*` 已被运行时忽略，仅发 DeprecationWarning | `forwin/config.py:30-33,203-213` |
| 3 | `decide_repair_v2` 无条件调用（repair_v2 已全量 live）；`review_engine_repair_v2_enabled`（默认 False）在 config 之外**零读取**，是死旗标 | `forwin/orchestrator_loop_core/repair_loop.py:340`；全仓 grep 仅 config.py 命中 |
| 4 | `review_engine_auto_approve_enabled` 同样是死旗标（无生产调用方） | 全仓 grep 仅 config.py 命中 |
| 5 | `FinalAcceptanceGate` 仅在 blackbox 下评估 force-accept，且只处理软性残留 issue | `forwin/reviser/final_acceptance.py:40-56` |
| 6 | BookState = CANON、world_model = deprecated projection（import 即 warning）、map = CANON Scheme C runtime | 三个包的 `__init__.py` 包级 docstring 与 DeprecationWarning |
| 7 | `book_genesis_core/workflow.py` 的 early-return 后不可达实现 | **已删除**：588 行文件整体移除，9 个 workspace 委托改为 `BookGenesisService` 真方法；名称 profile resolver 归 `GenesisNameSuggestionService` |
| 8 | 架构边界测试已在守护删除项（world_v4 模块、review-engine safety-net、repair 死代码） | `tests/test_architecture_boundaries.py:227,233,254` |
| 9 | 生成任务已是 durable worker 模式：API 入队，worker `claim + lease + resume` | `forwin/generation/worker.py:46-167`、`forwin/generation/task_lease.py` |
| 10 | `Design-docs/DESIGN_STATUS.md` 是真实维护中的弃用矩阵（含 v5.0 删除目标），不是摆设 | `Design-docs/DESIGN_STATUS.md` 兼容/弃用矩阵段 |

### 0.2 需要修正 / 已过时的论断

| # | 初版论断 | 修正 |
|---|---|---|
| 1 | 模式清单只到 `operation_mode`/`writer_mode`/五个 `*_mode` | **漏了第 9 条模式轴**：`review_delegation_mode: human \| reckless`（2026-07-09 当天 8 个 commit 落地）。reckless 模式用 `gpt-5.3-codex-spark`（经 Codex bridge）代替人工审批 7 类暂停门；见 §3.1 |
| 2 | D14"新建 EntityAdmissionService/注册器" | **已完成割接**：`EntityRegistrar` 只构建 `EntityAdmissionPlan`；分类失败/遗漏/歧义/唯一性冲突 fail-closed；Canon `EntityAdmissionCommitter` 是唯一实体写方。旧 admission 五面已物理删除，不保留兼容旁路 |
| 3 | Phase E"把路由逻辑抽到 service 层"即可统一入口 | **低估了前置条件**：`WritingOrchestrator` 是由 13 个模块、约 115 个函数在 `service.py:111-242` 用属性赋值拼出来的上帝对象，且各模块被回注类引用（`_module.WritingOrchestrator = WritingOrchestrator`）、`__module__` 伪装成 `forwin.orchestrator.loop`。不先给这个类立缝（seam），service 抽取无从谈起 |
| 4 | "audit old StateUpdater writes：keep as projections or rewrite" | **实测是活的双写**：`_apply_canon_candidate` 在 BookState direct-commit 通过后，仍继续 `updater.apply_state_changes/apply_events/apply_thread_beats/apply_time_advance` 写 legacy 状态（`quality_gates.py:992-1020`）；且 canon 门自身的 `RetrievalBroker.build_world_model_pack` 和 context assembler 还在读 legacy `StateRepository`。删双写前必须先做**读方清单迁移** |
| 5 | post-100 修复计划 = "backlog，未落地" | 已过时：`a803cf9 Implement post-100 generation repair reset` 及后续 nonblocking-subworld 系列 commit（07-07/07-08）已落地主体；剩余项是 200 章 no-hotfix 验证与旧旁路清理 |
| 6 | 三层配置（env / runtime store / API schema） | 实际是**四层**：还有 `Project.governance_json`（`default_operation_mode`、`review_delegation_mode`、progression 等，`forwin/governance.py:406-407,505-560`）。四层字段集互不相同：如 `review_delegation_mode` 只能走 governance_json / MCP `project_set_reckless_mode`，不在 RuntimeSettingsStore；`quality_profile`/`writer_mode` 只在 env |
| 7 | 旗标清单 | 明显不全，漏：`chapter_review_form_mode`、`final_completion_gate`、`style_telemetry_mode`、`hard_floor_gate_enabled`、`canon_quality_review_in_hub_enabled`、`map_movement/personality/experience/lint_review_enabled`、12 个 `form_blocking_*`、`book_state_layers`、`context_recency_window_chapters`、`repair_model_sequence`、`codex_*`，以及 `long_run_policy.py` 的 `LongRunMode(daily_serial/factory_batch/soak_test)` 枚举。Config 总字段约 150 个 |
| 8 | 入口清单（API/worker/MCP/CLI/UI） | 漏了 **API 进程内的 automation scheduler 守护线程**（`forwin/api_core/automation.py:258-270`，30 秒轮询 `ProductionScheduler`）。它入队 durable 任务（方向正确），但属于第 6 个入口面，需并入统一 service |

### 0.3 初版遗漏的新发现

1. **`WritingOrchestrator` 猴子补丁拼装**（见 0.2#3）——这是整个系统最大的结构性负债，也是 2026-05-18 "giant file decomposition" 计划的副产物：文件被拆了，但边界没有建立。
2. **`forwin/orchestration` 包是死代码**：`ChapterPipelinePorts` / `OrchestrationEvent` 定义了理想的管线接口（context_assembler / writer / review_hub / repair_policy / final_acceptance_gate），但全仓无 import。要么把它变成拆解 orchestrator 的目标接口，要么删除。
3. **`_compile_world_model_after_acceptance` 是空壳**：直接 `return True`（`finalization.py:48-56`），纯接口残留，可删。
4. **`_apply_world_v4_gate` 名不副实**：它实际执行的是 BookState 抽取 → BookStateReviewGate → `BookStateDirectCommitService.compile_approved` → `KnowledgeProjectionRefresher`（`world_projection.py:105-330`），是当下的 canon 提交主路径，却顶着 legacy 名字。重命名是低成本高收益项。
5. **`quality_profile="premium"` 是空 override**（`config.py:663 PREMIUM_OVERRIDES = {}`）——要么定义要么删。
6. **reckless 模式可以批准 `chapter_blackbox_failure` 门**：即修复耗尽后 verdict=fail 的章节可被 LLM 放行（`project_chapters.py:488-493,521-581`）。实现质量不错（model-mismatch/parse 失败即 fail-closed、全量 PromptTrace、`RECKLESS_GATE_OVERRIDDEN` 事件），但"LLM 放行修复失败章节"应在收敛设计里被显式限定范围。
7. **governance 命名过载**：`forwin/governance.py`（决策事件+设置）、`governance_checks.py`、`governance_keywords.py`、`codex_governance.py`、`api_governance_support/ops/routes.py`、`orchestrator_loop_core/governance.py`、`reviewer/governance.py` 七处 "governance" 含义各不相同（事件账本 / 项目设置 / 关键词审查 / Codex 权限 / 审计动作）。
8. **两套 UI 栈**：服务端渲染的 home/publishers（`api_pages_*` + `ui_assets/`）与独立的 `frontend/world-studio` SPA；MCP 已有 31 个工具（含新的 `project_set_reckless_mode`）。

---

## 1. Executive Summary

1. **应该收敛，且现在是最佳窗口。** 100 章生产验证已完成、post-100 修复已落地主体、DESIGN_STATUS 弃用矩阵和架构边界测试已就位——系统已经知道"哪条路是真的"。再往后拖，每个新功能（如今天的 reckless 模式）都会继续按"加一条模式轴"的方式生长。
2. **最大问题不是重复代码，而是重复的决策权与拼装式结构。** 9 条模式轴、~150 个 Config 字段、4 层配置存储、5 套 subworld admission 面、canon 双写、115 个函数拼出来的 orchestrator——每一处都是"多个历史阶段同时在线"。
3. **优先级第 1：模式/配置收敛。** blackbox 成为唯一用户级生成模式；checkpoint/copilot 降级为 pause policy；reckless 定位为"人工门的委托策略"而非新模式；`*_mode` 五件套和死旗标进入 profile 内部或删除。
4. **优先级第 2：给 WritingOrchestrator 立缝。** 不是大爆炸重写，而是按功能族把猴子补丁函数群逐批换成显式协作对象（DraftReviewService / RepairService / CanonAdmissionService / PlanningService），`forwin/orchestration.ChapterPipelinePorts` 就是现成的目标接口。
5. **优先级第 3：subworld admission 完成割接。** EntityRegistrar 已 live，现在要删旧路（canon 门内的名字白名单校验、admission repair scope、summary 桥接），做到"一个名字只在一处被判决"。
6. **优先级第 4：BookState 单一真相。** 先迁读方（context providers、RetrievalBroker、ContinuityChecker），再删 `_apply_canon_candidate` 里的 legacy 双写；`_apply_world_v4_gate` 重命名为 BookState commit。
7. **优先级第 5：入口收敛到 application service。** API 路由（12+ 域文件、~200 handler）、worker、MCP（31 工具）、automation scheduler、CLI 全部调用同一层 service；durable worker 模式已经证明了正确形态。
8. 文档侧不需要新建体系：**DESIGN_STATUS.md 已是权威弃用矩阵**，收敛工作只需把本报告的决策合并进去并归档 2026-05 的 cutover 计划。
9. 部署形态（Docker Swarm：API / generation worker / MCP / publisher worker+browser / Postgres / Qdrant / MinIO）**不动**；不做微服务化，不做多租户。
10. 全程用架构边界测试锁定每一次删除——这个仓库已有这个肌肉（`test_review_engine_safety_net_runtime_paths_are_removed` 等），照方抓药即可。

---

## 2. Current Source-of-Truth Map

| 核心事实 | 当前真实 source | 旁路副本 / projection | 风险与动作 |
|---|---|---|---|
| 项目创建状态 | `projects.creation_status` + `active_genesis_revision_id` | 项目摘要、task-center、UI workspace | 低风险；保持 |
| Genesis / 书本设置 | 活跃 `BookGenesisRevision.pack_json`（brief→world→map→story_engine→book_blueprint→bootstrap） | stage traces、name suggestions、UI detail | `start-writing` 后必须冻结 revision；handoff 已幂等（tests/test_genesis_handoff_service.py 覆盖） |
| Book-level plan | handoff 前：Genesis `book_arc_blueprint`；handoff 后：`ArcPlanVersion` 物化行 | Genesis pack 留作蓝图存档 | 明示"handoff 后 Genesis 只是历史文档" |
| Arc / band / chapter plan | `ArcPlanVersion`、`ChapterPlan` + `PlanningService` / `PlanningQuery` | provisional preview、future plan audit、patch payloads | 写侧服务群已有统一 facade；audit/validation/rehearsal 统一投影为 `PlanHealth` |
| Chapter draft / accepted canon | `ChapterDraft`/`ChapterReview`/`CandidateDraftRecord`；接受 = `_apply_canon_candidate` 成功 | frozen candidates、artifacts、memory index | **注意：接受路径当前是双写**（BookState + legacy StateUpdater 行），见下 |
| BookState / world state | `GraphDeltaRow` + `BookStateCompiler`（经 `BookStateDirectCommitService.compile_approved`，藏在 `_apply_world_v4_gate` 里） | world/map/cognition/narrative snapshots、knowledge projection、**legacy `world_nodes`/`fact_nodes`/events 行（仍在被写）** | 双写是当前最大的状态源风险；Phase D 处理 |
| Map / subworld / entity admission | `forwin.map`（Scheme C 行）+ `EntityRegistrar` 产生 `EntityAdmissionPlan` + Canon `EntityAdmissionCommitter` | `reference_classifier` 仅作为 registrar 的确定性输入 | 一个未知名只在 registrar 判决；实体/别名只在 Canon 事务写入 |
| Review verdict | `HistoricalReviewHub` 聚合（continuity/lint/personality/canon_quality/webnovel/governance/map/publisher 信号）→ `ChapterReview` | review form、dashboard、candidate 元数据 | hub 与 canon 门各跑一次 canon_quality 分析（hub 侧受 `canon_quality_review_in_hub_enabled` 控制）；需缓存共享 |
| Repair decision | `decide_repair_v2`（无条件 live）+ 6 个仍在读取的 `review_engine_*` 布尔门（arc/book patcher、local rewrite、obligation verifier 等，默认全 False） | rewrite attempts、design patch snapshots | 死旗标 2 个删除；活旗标 6 个并入 profile 策略 |
| Final acceptance | `FinalAcceptanceGate`（blackbox-only、软性残留才 force-accept；由 repair 耗尽路径触发） | verdict.final_gate_decision、forced-accept 事件 | 定位为 `FinalResidualPolicy`；不越过 canon 门 |
| 人工门 / 委托决定 | `review_delegation_mode`（governance_json，per-project）→ `RecklessReviewAgent`，7 类 gate_kind | `RECKLESS_REVIEW_REQUESTED/DECIDED/FAILED`、`RECKLESS_GATE_OVERRIDDEN` 事件 + PromptTrace | 新增行。需限定 reckless 可批准的 gate 集合（尤其 `chapter_blackbox_failure`） |
| Publisher state | publisher worker/browser 独立角色；extension API + session 加密强制校验（config 构造器硬校验） | publisher UI、outbox、upload jobs | 保持隔离；不并入生成域 |
| Runtime settings | **四层**：env/Config(~150 字段) → RuntimeSettingsStore JSON(~15 字段) → `Project.governance_json`(~6 字段) → API/task payload | 各层字段集互不相同、各自 normalize | Phase A 建 RuntimePolicy 单模型 + 分层职责（env=bootstrap、store=可变、governance=per-project、payload=快照） |
| Observability / audit | `DecisionEvent`（220+ 事件类型）+ `PromptTrace` + artifact store + performance spans | dashboard chips、task logs | 健康；补 payload 契约与 operation_id 贯通即可 |

---

## 3. Functional Overlap Inventory

### 3.1 模式 / blackbox / reckless / runtime settings（最高优先）

| 功能族 | 当前相关文件 | 重合/冲突点 | 保留 | 合并/删除/降级 | 风险 |
|---|---|---|---|---|---|
| 运行模式 | `config.py`、`runtime_settings.py`、`governance.py:406-407`、`api_schema/llm.py`、`project_chapters.py`、`reckless_review.py`、`long_run_policy.py` | 9 条模式轴并存：`operation_mode`(3值)、`review_delegation_mode`(2值)、`quality_profile`(3值,premium空)、`writer_mode`、`progression_mode`(normalizer 已强制 band_guard)、5 个 `*_mode` hybrid 件套、`canon_quality_gate`、`chapter_review_form_mode`/`final_completion_gate`/`style_telemetry_mode`、`LongRunMode`。四层存储字段集互异 | `blackbox` 唯一用户级模式；`quality_profile` 唯一用户级质量选择；pause policy（review_interval / manual checkpoint / band checkpoint / generation audit）；`review_delegation_mode` 作为 pause policy 的**委托维度**（human=停下等人，reckless=LLM 代批） | `checkpoint` ≡ blackbox + 每章 pause；`copilot` ≡ blackbox + 非 pass 即 pause——两者改为兼容映射；`writer_mode`/`reviewer_quality_mode`/`phase4_use_llm`/5 件套 → profile 内部解析值；`progression_mode` 只留 normalizer；死旗标删除 | 旧 runtime_settings.json 与历史 task payload 兼容；需迁移测试 |

**reckless 当前覆盖的 7 类 gate**（`project_chapters.py`）：`manual_checkpoint_chapter_start`(:203)、章节 review 门四种（`chapter_operation_checkpoint`/`chapter_copilot_verdict`/`chapter_blackbox_failure`/`chapter_review_interval`，:521）、`generation_audit_pause`(:879)、`band_checkpoint_pause`(:971)、`manual_checkpoint_chapter_accepted`(:994)、`manual_checkpoint_band_end`(:1005)。收敛后 checkpoint/copilot 消失，reckless 的语义自然简化为"pause policy 触发的每一个人工门，由谁来批"。

### 3.2 Genesis vs book/arc/band/chapter planning

| 功能族 | 当前相关文件 | 重合/冲突点 | 保留 | 合并/删除/降级 | 风险 |
|---|---|---|---|---|---|
| 写前蓝图 vs 运行时计划 | `project_ops/`、`book_genesis_core/`(~3.9k 行)、`genesis_workspace/`、`genesis_handoff/`、`planning/`(30 文件) | Genesis pack 与物化行两套"计划"易混；`book_genesis_core/workflow.py` 死代码；planning/ 内 arc/band/patch/audit/rehearsal/provisional 服务群入口分散 | Genesis=写前蓝图+一次性 handoff；`ArcPlanVersion`/`ChapterPlan`=运行时唯一计划源 | 删 workflow.py 死块；handoff 后锁 Genesis revision；planning/ 服务群统一挂到 PlanningService 门面 | Genesis UI 写后编辑与运行计划冲突 |

### 3.3 review hub vs review_engine vs canon_quality vs form

| 功能族 | 当前相关文件 | 重合/冲突点 | 保留 | 合并/删除/降级 | 风险 |
|---|---|---|---|---|---|
| 草稿评审 | `reviewer/hub.py`(718行)、`review_engine/`、`canon_quality/`、`quality_gates.py:369-500`、`chapter_review_form_*` | hub 聚合 8 类信号且可含 canon_quality（`canon_quality_review_in_hub_enabled`）；canon 门再跑一次 `analyze_writer_output_quality`；review_engine 同时管 repair 路由、final acceptance、audit、dashboard、parity | `DraftReviewService`（=hub 重命名定位）；`canon_quality` 作为共享分析器；`review_engine` 只留决策规则引擎 | 按 draft(body hash+plan version) 缓存 canon_quality 分析，hub/门共享；`review_engine/parity.py` 与 shadow 测试转 test-only 或删；`chapter_review_form_mode` 常量化（现值恒 "primary"） | 缓存 key 错误会掩盖 repair 后的新正文 |

### 3.4 repair_v2 vs final acceptance vs reckless override

| 功能族 | 当前相关文件 | 重合/冲突点 | 保留 | 合并/删除/降级 | 风险 |
|---|---|---|---|---|---|
| 修复与残留放行 | `repair_loop.py`(1067行)、`review_engine/rules/repair_v2.py`、`rules/final_acceptance.py`、`reviser/final_acceptance.py`、`reckless_review.py` | repair_v2 已 live 但死旗标仍在；final acceptance 在 repair 耗尽时触发；**reckless 又提供了第三条放行路径**（可批 `chapter_blackbox_failure`）——三层"放行"语义无统一表述 | RepairService 拥有 scope 选择→patch→rewrite→verify→耗尽；`FinalResidualPolicy`（软性残留、blackbox、修复已验证）；reckless 只代批"本来会停给人看的门" | 删 `review_engine_repair_v2_enabled`/`auto_approve_enabled` 字段；明确 reckless **不得**放行 hard canon blocker（现状：canon 门在 reckless 之后仍会跑，是安全的，但应写成显式不变量+测试） | reckless 批准 fail verdict 章节后，canon 门成为唯一防线 |

### 3.5 subworld admission vs entity registration（已收口）

| 功能族 | 当前相关文件 | 重合/冲突点 | 保留 | 合并/删除/降级 | 风险 |
|---|---|---|---|---|---|
| 实体准入 | `naming/entity_registrar.py`、`naming/types.py`、`canon/entity_admission.py` | 无平行判决面；`reference_classifier` 只做确定性输入 | `EntityRegistrar` 四分类并附着 fingerprinted `EntityAdmissionPlan`；Canon 提交器落实 entity/alias | 旧 policy/patch/repair/checker/summary bridge 及专用测试删除 | 聚焦测试已覆盖四结果、fail-closed、候选变更失效、Canon 前零实体写；200 章 gate 待跑 |

### 3.6 future plan audit vs plan patch validator vs band checkpoint

已产出统一 `PlanHealth`（severity/scope/evidence/reasons/blocking），并由 `PlanHealthService` 适配 future audit、patch validation 和 scenario rehearsal；`PlanningService` / `PlanningQuery` 成为运行计划门面。`future_plan_auditor.py` re-export shim 与 `phase24.PlanningServices` service bag 已删除。

### 3.7 BookState / world_model / map / knowledge projection

| 功能族 | 当前相关文件 | 重合/冲突点 | 保留 | 合并/删除/降级 | 风险 |
|---|---|---|---|---|---|
| 世界状态 | `book_state/`、`world_model/`(deprecated)、`map/`、`knowledge_system/`、`llm_kb/`、`obsidian/`、`world_projection.py`、`state/updater.py`、`state/repo.py` | ① canon 提交双写（BookState compile + legacy apply_*）；② 读方（context assembler、RetrievalBroker、ContinuityChecker）大量走 legacy `StateRepository`；③ `_apply_world_v4_gate` 名字误导；④ `_compile_world_model_after_acceptance` 空壳 | `GraphDeltaRow`+`BookStateCompiler` 唯一 accepted-chapter 写方；BookMap 保留运行时语义（bootstrap/pathfinding/visibility）；knowledge/llm_kb/obsidian 全部 projection | 先迁读方 → 再删双写；重命名 gate；删空壳函数；`world_model` 维持 deprecated 直到 v5.0 | 双写删早了 context/review 全瞎；必须以"读方清单清零"为前置 |

### 3.8 入口面：API / worker / MCP / automation / CLI / UI

| 功能族 | 当前相关文件 | 重合/冲突点 | 保留 | 合并/删除/降级 | 风险 |
|---|---|---|---|---|---|
| 六个入口面 | `api_route_registry.py`(12+ 域路由文件, ~200 handler)、`api.py`(动态 export 兼容壳)、`api_runtime.py`、`generation/worker.py`、`mcp/http.py`(31 工具)、`api_core/automation.py`(进程内 30s 调度线程)、`cli.py`、两套 UI 栈 | 各入口面直接调用 orchestrator/api_runtime 帮助函数；`api.py` 用 `__getattribute__` 魔法代理 exports；automation scheduler 是隐藏入口 | durable task 队列为唯一执行边界；scheduler 只入队（现状已基本如此）；MCP/CLI/UI 调 service | 业务逻辑从 route ops 移入 application service；`api.py` 兼容壳在调用方清零后删除 | service 抽取受 §0.3#1 orchestrator 拼装制约，须与 Phase B 协同 |

### 3.9 UI 面板与设置

两套 UI 栈（服务端 home/publishers + world-studio SPA）职责基本清晰，问题集中在**设置暴露面**：`LLMPreferencesRequest` 暴露 `operation_mode` 等 11 字段，与 governance per-project 设置、env 专属字段三方并行。收敛后 UI 只暴露：quality profile、pause policy（含 reckless 开关）、模型 profile、章节长度。其余转 operator/debug（API/MCP 可达，UI 隐藏）。

### 3.10 observability / decision events / governance 命名

`DecisionEvent`（220+ 类型）+ `PromptTrace` 体系健康且被 reckless 模式充分使用（fail-closed + 全痕迹是好范例）。问题是 §0.3#7 的 governance 命名过载——建议仅做**命名与模块归位**（事件账本 → `forwin/audit/` 或保持 governance.py 但拆出 settings；关键词审查并入 reviewer），不动数据契约。

---

## 4. Recommended Target Architecture

### 4.1 用户级模式：只剩两个旋钮 + 一组 pause 策略

```text
quality_profile: pulp | standard          # premium 删除或定义后再上
generation:      blackbox                 # 唯一模式，operation_mode 仅作兼容解析
pause_policy:
  review_interval_chapters: int
  manual_checkpoints_enabled: bool
  auto_band_checkpoint: bool + band_warn_action
  generation_audit_interval + pause_enabled
  delegation: human | reckless            # 每个被触发的人工门由谁批
```

`RuntimePolicyResolver` 输入四层配置（env → runtime store → governance_json → task payload），输出一个不可变 `GenerationPolicy`（含 writer_pipeline、reviewer_llm_policy、canon_admission_profile、planning_policy、repair_policy）。五个 `*_mode` hybrid 件套、`phase4_use_llm`、`writer_mode`、`canon_quality_gate` 全部变成 resolver 的内部产物。

### 4.2 评审/修复/放行四层（含 reckless 的位置）

```text
1. DraftReviewService        # hub 重命名：草稿信号聚合，产出 ReviewVerdict（信号，非判决）
2. RepairService             # repair_v2 路由：local→writer→chapter/band patch→structural→registrar→耗尽
3. FinalResidualPolicy       # 仅耗尽+软性残留+修复已验证 → force-accept 候选
4. CanonAdmissionService     # canon_quality 门 → 义务/延期验证 → BookState extract/review/compile → projection
                             # 唯一 canon 判决者；任何 force-accept / reckless 批准都不越过它
[横切] HumanGateDelegation   # pause policy 触发的门 → human 停下 / reckless LLM 代批
                             # 显式不变量：reckless 只批"门"，不批"canon"
```

### 4.3 状态权威

维持初版结论并加严：`GraphDeltaRow + BookStateCompiler` 是唯一 accepted-chapter 写方；**读方迁移完成前不删双写**；BookMap 保留运行时地位（不是缓存）；`world_model`/`llm_kb`/`obsidian` 均为可重建 projection；`_apply_world_v4_gate` → `_commit_book_state_canon`。

### 4.4 Orchestrator 拆解路线（新增，替代初版 Phase E 的前半）

不重写、不搬家，按"每个 Phase 收敛一族方法"的节奏把 `service.py` 里的函数群转成显式协作对象：

```text
WritingOrchestrator (变薄的编排壳)
 ├─ ChapterPipeline(ports=forwin.orchestration.ChapterPipelinePorts)   # 采用现有死代码接口
 ├─ DraftReviewService      ← review_autofix.py + reviewer/hub
 ├─ RepairService           ← repair_loop.py + repair_patches.py + structural_patches.py
 ├─ CanonAdmissionService   ← quality_gates.py(_apply_canon_*) + world_projection.py
 ├─ PlanningService         ← governance.py(audit 部分) + planning/*
 └─ HumanGateDelegation     ← reckless_review.py + checkpoint 判断
```

验收信号很简单：`service.py` 的属性赋值行数单调下降，直到只剩 `run/continue_project/accept_review` 等公共入口。

### 4.5 入口统一

六个入口面（API 路由、worker、MCP、automation scheduler、CLI、UI）全部落到 application service；durable 任务队列是唯一执行边界；`api.py` 动态代理壳在外部调用方清零后删除。

### 4.6 文档权威

以 `Design-docs/DESIGN_STATUS.md` 为唯一弃用矩阵入口，把本报告决策合并进去；2026-05 review-engine cutover / legacy-removal 系列全部标 `historical-plan` 归档；`2026-07-09-forwin-reckless-review-mode-design.md` 与 post-100 计划标注实际落地状态。

---

## 5. Concrete Merge Decisions

标注：✅=沿用初版；✎=修订初版；★=本次新增。

| # | 保留 | 合并进 | 删除/弃用 | 迁移方式 | 需要测试 | 备注 |
|---|---|---|---|---|---|---|
| D01 ✅ 已完成 | strict pipeline | `RuntimePolicy.pause` | checkpoint/copilot 与用户 mode 轴 | 直接删除，不做旧值映射 | strict pass/warn/fail + pause policy | pause 能力保留，模式等价物删除 |
| D02 ✎ 已完成 | 可选暂停门委托 | `PausePolicy.gate_delegate` | `review_delegation_mode` 与 reckless 叙事 | 值严格为 `human/spark` | human/spark gate 分支 | Spark 是委托目标，不是运行模式 |
| D03 ★ 已完成 | Spark fail-closed + 完整痕迹 | `GateDelegationService` | fail/hard residual 的委托能力 | fail 或 hard residual 永不进入委托 | model mismatch/parse/trace/approval 不变量 | 不存在可配置的危险门 allowlist |
| D04 ✅ 已完成 | `quality_profile` | `RuntimePolicy` | writer/reviewer/planning 等独立模式轴 | 只允许 `standard/pulp`，不解析旧 env mode | profile 与显式注入测试 | 不再需要四层 resolver |
| D05 ★ 已完成 | pulp/standard | — | `premium` 空 profile | 直接删除 | policy literal 校验 | 无空 override |
| D06 ✅ 已完成 | 环境基础设施 + 项目策略 + 任务快照 | `InfrastructureConfig` / `RuntimePolicy` / policy snapshot | `RuntimeSettingsStore`、`governance_json` 设置与 request override | 直接删除，无 patch alias、无 backcompat | store/version/snapshot/catalog | 三段职责，不是四层归一 |
| D07 ✎ 已完成 | `HistoricalReviewHub` 行为 | `forwin.review.DraftReviewService` | 旧类名、`review_hub` runtime 字段、`reviewer` 旧包 | 破坏性改名并合入单一 review bounded package，不留 alias | review 聚焦测试 + 架构扫描 | 草稿评审只产 evidence/verdict |
| D08 ✅ 已完成 | `canon_quality` 分析器 | `QualityAnalysisRunRow` 缓存 | hub/门重复 LLM 分析 | 按 title/body/summary content hash + chapter plan fingerprint + prior-canon context + mode/version/model fingerprint 缓存；replay/dry-run/失败不复用 | hub/门同 payload parity；正文、计划或分析上下文变化失效 | 比初版 body hash 更严格，避免摘要、模型或前序状态变化误命中 |
| D09 ✅ | `decide_repair_v2` | `RepairService` | `review_engine_repair_v2_enabled`、`review_engine_auto_approve_enabled` **字段本体** | 直接删字段（config 之外零读取）；shadow/parity 测试转 test-only 或删 | repair 路由测试改为 live 断言 | 两个死旗标，零生产风险 |
| D10 ✎ | arc/book patcher、obligation verifier、local rewrite、commit_with_obligation 等 6 个活旗标 | profile 内部策略位 | 独立 env 暴露 | 默认值随 profile；env 覆盖仅 test | 各 gate 开/关行为测试 | 初版笼统归为"hide"；实测它们是活的门 |
| D11 ✎ 已完成 | `FinalAcceptanceGate` | `forwin.review.decision.FinalResidualPolicy` | `reviser/final_acceptance.py`、旧 rule/class/字段 | 实现并入 final residual rule；协议/API 使用 `final_residual_decision`，不留 alias | 软过硬拦 7 tests | 仍不越过 canon commit |
| D12 ✎ 已完成 | `_apply_canon_candidate` 行为 | `forwin.canon.CanonAdmissionService` | orchestrator 方法、compatibility outcome coercer 与 canon helper 注入 | 唯一决策体迁入 canon owner；generation/人工接受直接调用强类型 `commit`；quality/BookState/projection helper 改为直接协作 | canon block/commit/失败冻结 | |
| D13 ✎ 已完成 | BookState direct-commit 路径 | `_commit_book_state_canon` | `_apply_world_v4_gate` 误导名 | 函数、变量、artifact key 和 block kind 同步改为 BookState 语义 | BookState canon 测试 | 不留旧名 |
| D14 ✎ 已完成 | — | — | `_compile_world_model_after_acceptance` 空壳 | 函数、两处调用和恒真 pause 分支已删除 | compile/collect + acceptance 回归 | `return True` 存根已消失 |
| D15 ✎ | `BookStateCompiler`/GraphDelta 唯一写方 | `BookStateRuntime` | canon 提交内的 legacy `apply_state_changes/apply_events/apply_thread_beats/apply_time_advance` 双写 | **先**迁读方（context providers/RetrievalBroker/ContinuityChecker → BookState projection），读方清零后删写方 | 读方清单测试 + 30 章对拍（legacy vs projection 上下文一致） | 初版低估了读方迁移量 |
| D16 ✅ 实现完成 | `EntityRegistrar` + `EntityAdmissionPlan` | 唯一实体判决点；Canon 唯一实体写方 | canon checker、`subworld_admission_patch` scope、nonblocking 胶水、summary 名字桥 | 全部物理删除；候选 fingerprint 防止旧 plan 复用 | 12 个聚焦 entity/architecture tests；200 章 gate 待跑 | 比初版更严格：Canon 前零 entity/alias 写 |
| D17 ✅ 实现完成 | `FuturePlanAuditor`/patch validator/scenario rehearsal | `PlanningService` / `PlanningQuery` / `PlanHealthService` | 动态 package forwarding、`PlanningServices` bag、future auditor shim | 全部删除；typed health 接入 pre/post audit event | planning facade/health 聚焦测试 | band checkpoint 后续可继续直接消费同一 health DTO |
| D18 ✅ 已完成 | Genesis 六阶段 + handoff | 冻结语义 | `book_genesis_core/workflow.py` early-return 死块与 handoff 后可继续编辑 | 删除 588 行 workflow；workspace 委托成为 typed service 方法；handoff 设置 revision `locked`，mutation 统一拒绝 | Genesis freeze + handoff 流程 | 不保留直接编辑旁路 |
| D19 ✎ 已完成 | owner-local typed services | owner-local typed services | `forwin.orchestration.ChapterPipelinePorts` / `OrchestrationEvent` | 未采用 `Any` ports，整包删除；新协议随 owner 定义 | 文件不存在边界测试 | 零引用死代码已清除 |
| D20 ★ | `WritingOrchestrator` 公共入口 | 显式协作对象（§4.4） | `service.py` 猴子补丁拼装 + 模块回注 | 每 Phase 收敛一族方法；当前 87 条 | 每族替换后全量回归 | 收敛的结构性主线 |
| D21 ✅ | route registry 域分组 | application services | route ops 内嵌业务逻辑 | 按域抽 service，响应契约不变 | API 回归 + 分组边界测试（已有） | |
| D22 ✎ 已完成 | durable worker + automation scheduler 入队 | `GenerationApplicationService` | API/CLI/Genesis/auto-continue 的直接任务构造旁路 | 所有生产者统一调用 application service | 任务 lease/resume/cancel/幂等 | worker 只保留 claim/lease/heartbeat/observability |
| D23 ✅ | publisher worker/browser 隔离 + 强制加密校验 | `PublisherApplicationService` | 生成流程内的 publisher 调用 | 保持后 canon 工作流 | publish=false 冒烟 + 风险门 | 不弱化 browser 风险门 |
| D24 ✅ | `DecisionEvent`+`PromptTrace` | 统一 payload 契约（operation_id/policy_id/artifact refs） | 平行 ledger 命名 | 契约化，不动存量数据 | 审计查询/dashboard | reckless 的痕迹实现可作模板 |
| D25 ★ | `governance.py` 事件账本 | 拆分：settings 归 RuntimePolicy、关键词审查归 reviewer | 七处 "governance" 命名混用 | 仅移动+重导出，不改数据 | import 边界测试 | 纯可读性收敛，可最后做 |
| D26 ✅ | DESIGN_STATUS.md 弃用矩阵 | 本报告决策合并入矩阵 | 2026-05 cutover/legacy-removal 计划的"现行设计"地位 | 全部标 historical-plan；reckless/post-100 标实际状态 | 文档状态测试（已有） | |

---

## 6. Implementation Roadmap

> 顺序调整理由：A（模式）几乎零行为风险先行；B 同时启动 orchestrator 立缝（D20）因为它是 C/D/E 的公共前置；C（admission 割接）紧随其后因为它是当前生产 hotfix 压力的来源；D（状态双写）风险最高放中后；E/F 收尾。

### Phase A — 模式与配置收敛（D01-D06,D10）✅ Slice 1 已完成

- **落地形态**：strict pipeline 是唯一生成路径；`RuntimePolicy` 是唯一项目行为 DTO；任务保存精确 policy snapshot。
- **职责边界**：环境只给 `InfrastructureConfig` 和只读模型目录；项目策略按 version 乐观锁保存；请求只给运行边界，不覆盖策略。
- **直接删除**：旧 `Config` 名称、`RuntimeSettingsStore`、`governance_json` 设置、mode/reckless/feature-flag DTO、浏览器 API Key 保存表单和兼容测试。
- **入口统一**：`GenerationApplicationService` 已覆盖 API、worker、scheduler、CLI、Genesis、continue 和 auto-continue。
- **验证**：RuntimePolicy/store/API/snapshot/application/MCP/console 有聚焦测试；completion gate 为 architecture/config 24 passed、策略分支 3 passed、1598 tests collect 无错误；完整长跑与部署 gate 另行执行。

### Phase B — review/repair/final/canon 分层 + orchestrator 立缝（D07-D13,D19,D20 首批）

- **当前进度**：已完成。D07-D14、D19 与本阶段 D20 切片均落地；canon/repair 专属 helper 注入已清除，`WritingOrchestrator` 拼装降至 89 条；D08 使用 content/plan/analyzer 三重指纹共享分析且自动失效。
- **目标**：四层判决语义落地为四个显式 service；`service.py` 猴子补丁开始收敛。
- **涉及**：`review/{draft_service,decision,repair}`、`orchestrator_loop_core/{review_autofix,repair_loop,quality_gates,world_projection,service}.py`。
- **不变量**：hard blocker 必拦；force-accept/reckless 不越 canon 门（新增显式不变量测试）；`ChapterReview` 持久化与 UI 队列可读。
- **具体**：① 建 `DraftReviewService`/`RepairService`/`FinalResidualPolicy`/`CanonAdmissionService` 包装现函数；② `QualityAnalysisRun` 缓存；③ `_apply_world_v4_gate` 重命名；④ 删空壳 `_compile_world_model_after_acceptance`；⑤ reckless per-gate allowlist（D03）。
- **测试**：verdict parity（包装前后）；repair 路由 live 断言；软/硬残留；reckless 批准后 canon 门执行不变量；同一 accept 尝试内 canon_quality LLM 只调一次。
- **回滚**：service 为包装层，import 换回即回。
- **验收**：`service.py` 属性赋值减少 ≥40 行；四个 service 有独立单测；shadow/cutover 术语从生产代码消失。

### Phase C — Genesis/planning/subworld admission 割接收口（D16-D18）✅ 实现完成

- **当前进度**：实现完成。Genesis 冻结、EntityAdmissionPlan 单判决/Canon 单写方、旧 admission 删除、Planning facade/query/health 与 forwarding shim 删除均已落地；200 章 no-hotfix 运行 gate 待独立执行。
- **目标**：EntityRegistrar 成为唯一实体判决点；Genesis 冻结；planning 服务群归口。
- **具体**：① canon 门 `_validate_subworld_admission` 改为"验证 registrar 结论"；② 删 `subworld_admission_patch` scope、nonblocking 胶水（93278ab/c12e003/47c193b 引入的特例）、summary 名字桥；③ `reference_classifier` 清单转为 registrar 输入；④ 删 workflow.py 死块；⑤ handoff 后锁 Genesis revision；⑥ `PlanningService` 门面。
- **不变量**：`start-writing` 仍要求 `manual_ui`+`genesis_ready`；handoff 幂等与回滚（现测试已覆盖）；registrar 失败 fail-closed。
- **测试**：100 章冻结 fixture 回放零新增 regex；alias 唯一约束；registrar 失败→needs_review；Genesis 冻结后编辑走 proposal。
- **验收**：代码边界已保证一个未知名字只在 registrar 一处被判决，且 Canon 前零实体写；200 章 no-hotfix 仍是部署前运行验收，不在本次“尽量跳过测试”的主进程中伪造完成。

### Phase D — BookState 单一写方（D14,D15）

- **当前进度**：核心割接完成，清理继续。WriterOutput 完整结构合约与 EntityAdmissionPlan 已统一转 GraphDelta；context/review/checker/autofix/finalization/registrar 已改读 `BookStateQuery`/`ReviewQuery`；legacy accepted-state reader 名称在生产代码清零；Canon 双写、Genesis legacy entity 预灌与对应 `StateRepository`/`StateUpdater` 方法已删除。legacy 表和 deprecated world-model projection facade 尚待物理清除；30/60/100 章运行 gate 未执行。
- **目标**：删 canon 提交双写，前置是读方清单清零。
- **步骤**：① 盘点 legacy `StateRepository` 读方（context providers、RetrievalBroker.build_world_model_pack、ContinuityChecker、phase3/phase4、api world routes）；② 逐个改读 BookState projection，30 章对拍上下文一致；③ 读方清零后删 `_apply_canon_candidate` 内 `apply_state_changes/apply_events/apply_thread_beats/apply_time_advance`；④ legacy 行转只读归档或重建脚本。
- **不变量**：canon 提交幂等；old-value mismatch 拦截；BookMap pathfinding 不降级；LLM KB 刷新不变。
- **回滚**：双写删除放在独立 commit，revert 即回；本 Phase 无破坏性数据迁移。
- **验收**：架构测试断言 accepted-chapter 路径无 legacy 写；30/60 章运行上下文对拍一致。

### Phase E — 入口 service 化（D21-D23）

- **目标**：六入口面全部经 application service；`api.py` 动态代理壳退役。
- **具体**：`forwin/application/` 建 generation/genesis/planning/review_repair/canon_admission/projection/publisher/runtime_policy 服务；routes/MCP/scheduler/CLI 改调；worker 执行 `GenerationApplicationService.execute_task`。
- **不变量**：HTTP 契约不变；MCP 31 工具行为不变；publisher 角色隔离不变。
- **测试**：API 全流程回归；worker lease/resume/cancel；MCP 与 API 状态一致性；publish=false 冒烟。
- **验收**：route handler 只剩校验+适配；"开始写作/继续生成"只有一条实现路径。

### Phase F — 文档归档与测试补全（D24-D26）

- 合并本报告决策入 `DESIGN_STATUS.md`；cutover/legacy-removal 系列标 historical-plan；reckless/post-100 文档标实际落地状态；补架构测试：禁止 UI 暴露废弃模式字段、禁止新增 `*_mode` 配置字段（allowlist 机制）、`service.py` 赋值行数上限递减断言。

---

## 7. Deletion / Deprecation List

### Safe to delete now（零生产风险，删后跑全量测试）

| 项 | 依据 | 验证 |
|---|---|---|
| `Config.review_engine_repair_v2_enabled`、`review_engine_auto_approve_enabled` 字段 | config 之外零读取；repair_v2 无条件 live | 全量测试 + grep 零命中 |
| `book_genesis_core/workflow.py` early-return 死块 | 不可达；行为已委托 workspace | Genesis 流程测试 |
| `_compile_world_model_after_acceptance` | `return True` 空壳 | 全量回归 |
| `forwin/orchestration/` 包（若不采纳为 ports） | 零 import | import 测试 |
| `planning/future_plan_auditor.py` shim | 6 行 re-export | 改调用方 import |
| `PREMIUM_OVERRIDES` 空 profile（或补定义） | 空 dict 占位 | Config 枚举测试 |
| review_engine shadow/parity 测试对生产旗标的断言（`test_repair_v2_shadow.py`、`test_shadow_mode.py`、`test_auto_approve.py` 中依赖被删字段的部分） | 断言的是已死的迁移语义 | 换成 live 路由断言 |

### Deprecate with compatibility shim（一个版本周期）

`OPERATION_MODE`/`operation_mode`（四层全部：解析→blackbox+pause）；`WRITER_MODE`；`PROGRESSION_MODE=serial_canon`（normalizer 已在做）；`review_engine_live_cutover_*`；五个 `*_mode` hybrid 件套；`chapter_review_form_mode`；`forwin.world_model` 业务依赖（沿用 DESIGN_STATUS 的 v5.0 期限）；`forwin.reviewer_v4` alias 包（同上）；`api.py` 动态代理壳。

### Keep but hide from UI

`review_engine_local_rewrite_enabled`、`review_engine_arc/book_patcher_enabled`、`review_engine_obligation_verifier_enabled`、`review_engine_commit_with_obligation_enabled`、`review_engine_arc_book_budget_enabled`（6 个活门 → profile 策略位）；`phase4_use_llm`；`provisional_preview_enabled`（有真实调用链，实验特性）；`generation_audit_pause_enabled`；`freeze_failed_candidates`；`form_blocking_*` 12 字段（operator 级）；`repair_model_sequence`；`hard_floor_gate_enabled`。

### Keep as test-only

deterministic/mock reviewer 与 LLM 模式；`review_engine/parity.py`（若保留）；100 章冻结 fixture；hash embedding fallback。

### Keep as production-critical

Swarm 角色分工；Postgres/Qdrant/MinIO/artifact store；durable task lease 机制；canon_quality 门；BookState extract/review/compile；BookMap 生成/寻路/可见性；EntityRegistrar + EntityAlias；band checkpoint 与 future constraints；`DecisionEvent`/`PromptTrace`（含 reckless 全痕迹）；publisher session 加密强制校验（`config.py:880-907`）；`RECKLESS_REVIEW_*` 事件族。

---

## 8. Tests and Verification

| 类别 | 必须证明什么 |
|---|---|
| 单元：RuntimePolicy | standard/pulp 完整不可变策略、长度约束、用户可编辑字段与无 secret snapshot |
| 单元：repair 路由 | repair_v2 live 语义下 scope 选择、预算、耗尽升级正确；不再依赖已删旗标 |
| 单元：gate delegation 不变量 | fail/hard residual 不委托；Spark model-mismatch/parse 失败 → reject + `GATE_DELEGATION_FAILED`；批准后仍执行 canon 门 |
| 单元：canon_quality 缓存 | hub 与 canon 门共享同一 `QualityAnalysisRun`；body 变更使缓存失效 |
| 单元：EntityRegistrar | 未知名 → registered/alias/background/plan-conflict 四分类；分类器异常 fail-closed；alias 项目内唯一 |
| 单元：BookState compiler | 幂等、old-value mismatch 拦截、snapshot 持久化（现有测试保持绿） |
| 集成：draft→review→repair→canon | fail 草稿进 repair、attempt 记录、验证后才进 CanonAdmission；块住时冻结候选+事件+needs_review 且不动 canon |
| 集成：读方对拍（Phase D 专用） | 同一项目 30 章，legacy StateRepository 上下文 vs BookState projection 上下文逐章 diff 为空，然后才允许删双写 |
| 集成：admission 回放 | 100 章冻结 writer 输出回放，registrar 路径零新增 regex、零 needs_review 误报 |
| API/worker/MCP | RuntimePolicy version conflict；lease/resume/cancel/pause；重试执行不产生重复章节/GraphDelta；MCP 与 API 状态一致（含 `project_set_gate_delegate`） |
| UI | settings 只读环境模型目录；项目抽屉只显示 v5 policy 字段；生成 modal 只提交 max/run-until/auto-continue |
| 真实生成 | 30 章（Phase A 后，配置迁移冒烟）→ 60 章（Phase B/C 后，四层判决+registrar）→ 100 章（Phase D 后，单写方）→ **200 章 no-hotfix**（总验收，沿用 post-100 计划标准：全程零代码热修） |
| Publisher 不回归 | publish=false 不触 browser 状态；canon 接受后可入 publisher 队列；CAPTCHA/MFA 停机；publisher 失败不回滚章节接受态 |
| 新项目基线 | 不迁移旧 runtime JSON/governance 数据；只验证新项目 Genesis → policy → task snapshot 主链 |

---

## 9. Risks / Non-goals

### 最高风险（按序）

1. **双写删除（Phase D）**：读方迁移不彻底会让 context/review 静默变瞎。对策：读方清单 + 30 章对拍作硬性前置，双写删除独立 commit。
2. **Spark 委托边界**：只允许可选 pause gate 进入 Spark；fail/hard residual 在调用委托服务前即阻断。持续观察 `GATE_DELEGATION_*` 事件，但不提供扩大危险门范围的配置入口。
3. **orchestrator 立缝的中间态**：包装层与猴子补丁并存期间，方法解析顺序错误可能引入隐蔽 bug。对策：每族替换独立 commit + 全量回归；`service.py` 赋值行数递减断言防止回潮。
4. **admission 割接期**：新旧五面并存时删错顺序会复现 100 章 hotfix 循环。对策：先让 canon 门只"验证"不"判决"，回放绿了再删旧面。
5. **canon admission 抽取**：事件顺序、冻结候选捕获、事务边界易碎。对策：包装先行、事件契约冻结。

### Non-goals（与初版一致，补两条）

不做微服务化、多租户、RBAC；不迁移/清洗旧生产数据；不自动 retcon 已接受章节；不因 BookState 收敛降级 BookMap 运行时能力；不动 publisher worker/browser 角色与风险门；不为"保险"保留等价入口。**新增**：不在本轮收敛中扩展 reckless 到 Genesis/publisher 域（先在生成域验证 200 章）；不重构 DecisionEvent 存量数据契约（只加不改）。

---

## 附：与初版报告的处置总结

- **沿用**：Source-of-truth 框架、四层判决分层、BookState 权威结论、文档分级方法、测试分层策略、Phase A-F 骨架。
- **修订**：D14（registrar 已落地→改割接）、Phase E 前置（orchestrator 立缝）、配置三层→四层、post-100 状态、`review_engine_*` 旗标死活二分（2 死 6 活）。
- **新增**：reckless/`review_delegation_mode` 全套分析与 D02/D03、D13/D14/D19/D20/D25 五项结构决策、双写实证与读方对拍方法、`premium` 空 profile、automation scheduler 第六入口、governance 命名收敛。
- **删减**：初版中按推测写的 "cutover 仍需一个兼容周期" 类表述（代码里 cutover 早已 deprecated+ignored，直接进入删除轨道）；初版对 `production/` 包的忽略（它是活的，不删）。
