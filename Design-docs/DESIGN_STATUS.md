# ForWin Design Status

更新时间：2026-07-12

状态：active-current。本文档给当前保留的设计文档标注阅读顺序和权威等级。

## 状态枚举

- `active-current`：当前架构入口或当前实现规格。
- `active-maintenance`：仍在维护的专项规则、内容库或操作说明。
- `baseline-with-overrides`：历史主链基线，但必须被当前文档覆盖解释。
- `legacy-compatibility`：只描述兼容、迁移、投影或历史边界。
- `historical-plan`：已执行或被后续设计覆盖的实施计划，不作为目标架构依据。
- `future-product-backlog`：后续产品化方向，不作为当前后端缺口。

## 当前入口

| 文档 | 状态 | 说明 |
|---|---|---|
| `CURRENT_ARCHITECTURE.md` | active-current | 当前唯一架构入口，固定 RuntimePolicy / application boundary / BookState / BookMap / review 口径。 |
| `DESIGN_STATUS.md` | active-current | 本状态清单。 |
| `../forwin_architecture_consolidation_audit.md` | historical-plan | 2026-07-09 架构收敛审计（历史论证记录）。其 Phase A-D 已由 v5 hard-cut 完成并替代，Phase A-F 不再作为待办；source-of-truth 思维与测试/风险框架由后续计划继承。 |
| `../docs/superpowers/specs/2026-07-09-forwin-v5-architecture-convergence-design.md` | active-current | v5 破坏性收敛规格；旧项目和旧设置不迁移。 |
| `../docs/superpowers/plans/2026-07-12-forwin-v5-final-roadmap.md` | active-current | v5 收尾最终路线：测试债清偿 → 必做删除 → 度量仪表 → 发布验证（V1-V6）→ 200 章 no-hotfix → 部署。 |
| `../docs/superpowers/plans/2026-07-12-forwin-measurement-loop.md` | active-current | Measurement Loop（S1-S8）：门禁效力账本、规则出身制度、成本/人时、读者回路、多样性遥测、体验校准。 |
| `V4.5_markstone.md` | active-current | 当前代码与设计差距统一入口，旧 `world_model_v4` 已降级。 |
| `V4.5.1_markstone.md` | active-current | V4.5 后端闭环后的残余 contract / 文档 / 测试收束。 |
| `V4_final_book_state_runtime.md` | active-current | BookState 最终 runtime 规格。 |
| `map_scheme_c.md` | active-current | Scheme C BookMap 最终语义。 |
| `writing_flow_state_machine.md` | active-current | 当前写作任务状态机。 |
| `V4.6_knowledge_system.md` | active-current | BookState DB Canon -> Obsidian -> LLM KB 权威关系。 |
| `trope_library_pulp_v1.md` | active-current | Pulp 爽点库当前 runtime seed；运行时扩展到 50+ template，并携带 genre/audience/platform/payoff metadata。 |

## 维护文档

| 文档 | 状态 | 说明 |
|---|---|---|
| `V4.7_character_personality_skill.md` | active-maintenance | Character Personality Skill Library 设计。 |
| `V4.7_character_personality_maintenance.md` | active-maintenance | personality skill 内容、runtime compression、reviewer 和 World Studio 维护。 |
| `V4.8_character_creation_personality_assignment.md` | active-maintenance | 人物创建与自动 personality assignment 设计。 |
| `V4.8_character_creation_personality_maintenance.md` | active-maintenance | 人物创建 helper、assignment、coverage、metrics 和维护流程。 |
| `maintenance_log.md` | active-maintenance | 项目总维护日志。 |

## 历史基线与兼容说明

| 文档 | 状态 | 说明 |
|---|---|---|
| `V2_9_2.md` | baseline-with-overrides | Genesis / Writer / Review / Governance 主链基线；SubWorld / world model 语义以 V4.5、BookState 和 Scheme C 覆盖。 |
| `V2_9_3_skill_runtime.md` | baseline-with-overrides | ForWin-native instruction-only Skill Runtime；API/UI/script/tool-backed 属 future。 |
| `V3_8.md` | baseline-with-overrides | backend observability / audit / PromptTrace 规格；dashboard/SLO 属 future。 |
| `provisional_mechanism_check.md` | legacy-compatibility | Provisional Band Preview 物理删除记录；当前判断路径是 Scenario Rehearsal、Candidate Draft Review 和 BookState gate。 |
| `review_fix_log_2026-04-15.md` | legacy-compatibility | 历史 review 修复记录。 |

## 2026-07 V5 Final Roadmap Execution Status

状态：R0-R2 complete；旧长跑已安全退出，current-HEAD 测试债已清零，Provisional Band Preview 家族已物理删除，下一阶段为 R3 低风险归位。

- 旧项目 `a06cf00db3ba4cbe8b9862e20e9d6248` 的生成任务已于 2026-07-12 安全暂停，权威 active task count 为 0。
- 该运行固定归类为 `pre-roadmap-soak`，`release_evidence=false`；路线图变更从快照之后开始，因此它不得作为最终 V6 发布证明。
- 不恢复、不清洗该项目；保留为只读回归样本。
- 完整身份、事件、Canon、trace、投影和发布证据见 `../docs/operations/v5-pre-roadmap-soak-snapshot.md`。
- R1 首次可信全量基线为 `97 failed, 1540 passed, 1 skipped`；97 项均已按当前 v5 公共契约修复或删除，不以放宽生产约束换取通过。
- 测试 PostgreSQL harness 改为每个测试及时回收 transient database，browser session database 仅在进程退出时回收；全量运行后遗留测试库为 0。
- R1 最终证据：`1602 tests collected`；全量 `1601 passed, 1 skipped, 112 warnings, 33 subtests passed in 175.31s`；`ruff check forwin tests`、`compileall -q forwin` 和 R1 聚焦门均通过。
- R2 删除 policy 开关、第二 writer、preview service、execution/fallback/repair callback、两张表、事件、HTTP/read-model/task/UI/probe 全链，生产禁止符号零命中；保留 Arc sizing window、promotion/projection 与普通 writer preview fallback。
- R2 最终证据：`1601 tests collected`；全量 `1599 passed, 1 skipped, 110 warnings, 33 subtests passed in 169.70s`，新增 band repair 无 preview callback 回归通过；Ruff、compileall、rendered page 测试、源码扫描和 `git diff --check` 通过。
- 最终 200 章 gate 必须等待 A2/A4 数据决策落地与 R9 Release Candidate 冻结后，使用全新项目从第 1 章开始。

## 兼容 / 弃用矩阵

本矩阵是代码迁移的当前权威入口。新增调用方不得再引入 `deprecated` 模块；保留调用方必须通过对应的 current/compat 模块收束。

| 模块 | 状态 | 当前替代 | 删除 / 复核目标 | 说明 |
|---|---|---|---|---|
| `forwin.world_model` | removed | `forwin.knowledge_system` + `forwin.book_state` | 已删除 | live page/proposal/Obsidian/retrieval helper 已迁入 owner；`/world-model/*` 只保留 HTTP 传输契约名。 |
| `forwin.world_model_v4` | removed | `forwin.book_state` | 已删除 | 旧 compatibility projection/debug bridge 已从生产模块删除。 |
| `forwin.world_v4_compat` | removed | `forwin.book_state` | 已删除 | 旧 compatibility projection writer 已从生产模块删除。 |
| `forwin.reviewer_v4` | removed | `forwin.book_state.extraction` | 已删除 | 旧导入 alias 包已物理删除。 |
| `forwin.world_v4_review_gate` + `forwin.extractor` | removed | `forwin.book_state.extraction` | 已删除 | extraction contract、evidence rules、deterministic gate 与 GraphDelta converter 归同一 owner；`V4Review*` / `WorldDeltaExtractor` 旧类型无 alias。 |
| `forwin.book_state.extraction` | active-internal | 无 | 无 | 当前 BookState candidate extraction owner，不是主 chapter reviewer。 |
| `forwin.planning.scenario_rehearsal` | removed | `forwin.planning.scenario_rehearsal_service` | 已删除 | runner/repository 实现迁入 `scenario_rehearsal_engine`；生产编排只经 service。 |
| `forwin.planning.scenario_rehearsal_service` | active-current | 无 | 无 | 当前 Scenario Rehearsal service 入口。 |
| `forwin.planning.scenario_rehearsal_engine` | active-internal | service / resolution owner | 无 | 确定性 runner 与 repository，不作为应用入口。 |
| `forwin.runtime_settings` | removed | `forwin.runtime.policy` | 已删除 | 不再有进程内可变生成设置文件。 |
| `Project.governance_json` settings | removed | `Project.runtime_policy_json` + version | 已删除 | 项目运行设置只由版本化 RuntimePolicy 持有。 |
| `forwin.governance*` / `models.governance` / `review.governance` | removed | `forwin.audit` + `forwin.planning` + `forwin.review` | 已删除 | 决策事件、计划控制和草稿规则分别归真实 owner，不保留兼容 facade。 |
| `api_governance_*` / `api_schema.governance` | removed | `api_project_control_*` + `api_schema.project_control` | 已删除 | HTTP transport 使用 project-control 与 audit 术语。 |
| `codex_governance` | removed | `forwin.codex_bridge.governed_actions` | 已删除 | Codex 白名单动作留在 Codex bridge 边界。 |
| `app_task_governance.js` | removed | `app_task_control.js` | 已删除 | 项目抽屉只呈现项目控制与审计状态。 |
| `forwin.orchestration` | removed | owner-local typed services | 已删除 | `ChapterPipelinePorts` / `OrchestrationEvent` 为零调用 `Any` ports，未作为 v5 边界采用。 |
| `WritingOrchestrator` + `forwin.orchestrator*` | removed | `forwin.generation.pipeline.ChapterPipeline` | 已删除 | pipeline 静态组合 typed stage owner；模块回注、身份伪装和类体函数赋值均不存在。 |
| `book_genesis.py` / `book_genesis_core` / `genesis_workspace` / `genesis_handoff` | removed | `forwin.genesis` | 已删除 | Genesis service、workspace 与 handoff 归入一个包；不再经延迟 facade 查询 helper。 |
| `api.py` 动态代理 + `api_core` | removed | `forwin.http.create_app` + instance `HttpRuntime` | 已删除 | `forwin.api` 只公开 `app/create_app/lifespan`；每个 App 独立持有 session、pipeline、task cache、scheduler 与 publisher 状态。 |
| root `api_route_registry.py` + `api_*_routes.py` | removed | `forwin.http.routes` + `forwin.http.adapters` | 已删除 | HTTP adapters 归同一 transport owner，不保留旧导入 alias。 |
| `/api/generate` + `GenerateRequest` | removed | Genesis handoff + project continuation | 已删除 | 不允许绕过 Genesis 或 durable generation task；CLI/MCP/UI 使用同一 HTTP workflow。 |
| `api_runtime.py` generic project creation runner | removed | `application.generation_execution.execute_continuation` | 已删除 | worker 只执行 project-backed claimed task；无项目 `pipeline.run()` 分支已删除。 |
| `api_project_control_ops/support` | removed | `forwin.application.project_control` | 已删除 | project-control HTTP adapter 只绑定 `ProjectControlApplicationService`。 |
| `api_project_ops` / `api_project_policy` / root `project_ops` | removed | `forwin.application.projects.ProjectApplicationService` | 已删除 | 项目、Genesis、章节和 review 路由只绑定 application service。 |
| `api_publisher_ops` | removed | `forwin.application.publisher.PublisherApplicationService` | 已删除 | extension auth、publisher jobs、cover 与 comment sync 共享一个应用边界。 |
| `forwin.reviewer` | removed | `forwin.review` | 已删除 | 草稿评审、decision rules 与 repair 归入一个 bounded package，不留旧导入 alias。 |
| `forwin.review_engine` | removed | `forwin.review.decision` | 已删除 | 决策规则不再作为与 review 平级的第二套域。 |
| `forwin.reviser` | removed | `forwin.review.repair` | 已删除 | rewrite executor 与 verifier 归 repair owner。 |
| `HistoricalReviewHub` | removed | `forwin.review.draft_service.DraftReviewService` | 已删除 | 草稿评审只产出 evidence/verdict，不决定 canon。 |
| `FinalAcceptanceGate` | removed | `forwin.review.decision.rules.final_residual.FinalResidualPolicy` | 已删除 | repair 耗尽后的残留策略；字段为 `final_residual_decision`，仍必须经过 BookState canon commit。 |
| `_apply_world_v4_gate` | removed | `CanonPreparationService` + `CanonAdmissionService.commit_plan` | 已删除 | extraction/review 在事务外准备，compile 只在原子 Canon 事务内执行。 |
| `_compile_world_model_after_acceptance` | removed | 无 | 已删除 | 恒返回 `True` 的空壳及两处调用均删除。 |
| `WritingOrchestrator._apply_canon_candidate` | removed | `forwin.canon.CanonAdmissionService.commit_plan` | 已删除 | generation 与人工接受共享持久化 `CanonCommitPlan`；旧 `commit()` 不存在。 |
| `orchestrator_loop_core.quality_gate_types` | removed | `forwin.canon.types` | 已删除 | canon outcome 类型归 canon owner；`CanonApplyOutcome` 改名 `CanonAdmissionOutcome`。 |
| `orchestrator_loop_core.repair_loop` | removed | `forwin.review.repair.RepairService` | 已删除 | live repair 算法迁入 owner；`ChapterPipeline` 只调用 `review_candidate` / `repair_canon_block`。 |
| `orchestrator_loop_core.__init__` re-export | removed | explicit owner imports | 已删除 | `pipeline_core` 初始化为空边界；子模块不再借 `common.py` 转发外域类型。 |
| pipeline runtime helper injection | removed | owner-local typed stage owners | 已删除 | `ChapterPipeline` 构造器无 `Any` collaborator；repair/Canon preparation 只接收冻结的窄 execution context。 |
| duplicate canon quality analysis | removed | `QualityAnalysisRunRow` | 已删除 | draft review/canon gate 共享有效 primary 结果；content/plan/analyzer 指纹变化自动失效，replay/dry-run/失败结果不复用。 |
| `book_genesis_core.workflow` unreachable implementation | removed | typed `BookGenesisService` -> `GenesisWorkspaceService` methods | 已删除 | 588 行文件整段删除；handoff 锁 active revision，四个 workspace mutation 入口 fail-closed。 |
| SubWorld entity admission policy/patch/repair | removed | `EntityRegistrar` -> `EntityAdmissionPlan` -> Canon `EntityAdmissionCommitter` | 已删除 | 草稿期不写 Entity/EntityAlias；旧 canon checker、repair scope、nonblocking 例外和 summary 名字桥全部删除。 |
| `planning.future_plan_auditor` re-export + `phase24.PlanningServices` bag | removed | `PlanningService` / `PlanningQuery` / `PlanHealthService` | 已删除 | planning 包不再动态转发旧符号；future audit、patch validation、scenario rehearsal 共享 typed health contract。 |
| Provisional Band Preview runtime | removed | `ScenarioRehearsalService` + Candidate Draft Review + writer fallback | 已删除 | 不存在 policy 开关、第二 writer、preview service、execution/ledger 表、事件、HTTP/UI 或 repair callback；同名 planning window、promotion/projection 语义不属于该功能。 |

## 2026-07 V5 Slice 1 Status

状态：implementation-complete，已部署；旧 200 章运行已退出并降级为 pre-roadmap soak，等待新 RC gate。

- `InfrastructureConfig` 仅负责基础设施、凭据和环境模型目录；不存在 `Config` 兼容名。
- 项目运行行为只来自版本化 `RuntimePolicy`，质量 profile 仅有 `standard/pulp`，gate delegate 仅有 `human/spark`。
- generation task 保存不可变 policy snapshot；所有任务生产者和 worker 执行均通过 `GenerationApplicationService`。
- GET `/api/settings/llm` 是 secret-free 只读目录；控制台不再保存 API Key、模型 profile 或全局生成偏好。
- 项目抽屉是唯一 RuntimePolicy UI 写入口；MCP 对应工具为 `project_set_gate_delegate`。
- 已删除旧 mode/reckless/request override、RuntimeSettingsStore、旧治理设置 DTO，以及绑定这些接口的失效测试套件。

实现提交：`a7f53bb`、`f7790c5`、`61392af`、`6c2eb0f`、`131e697`、`bc85d91`、`3e0c10f`、`a6a75fb`、`589e58a`、`e87e67b`、`307b0dd`。

验证口径：前序聚焦 policy/store/API/snapshot/application/worker/MCP/browser 测试已通过；completion gate 的 architecture/config 为 24 passed，策略分支补充为 3 passed，`compileall` 成功，全仓 1598 tests collect 成功且无收集错误。旧长跑已退出；最终 200 章 no-hotfix 门禁必须等待新 RC 冻结后从全新项目开始。

## 2026-07 V5 Slice 2 Status

状态：implementation-complete，已部署；旧 200 章运行已退出并降级为 pre-roadmap soak，等待新 RC gate。

- 已删除零调用 `forwin.orchestration` ports 和恒成功 `_compile_world_model_after_acceptance` 空壳。
- BookState canon 旧路径已被 `CanonPreparationService` + `CanonAdmissionService.commit_plan` 取代，旧 direct commit 服务与端口均删除。
- `reviewer`、`review_engine`、`reviser` 已物理合并为 `forwin.review/{draft_service,decision,repair}`；不存在旧 package alias。
- `HistoricalReviewHub` 已破坏性改名为 `DraftReviewService`；runtime 字段为 `draft_review`。
- `FinalAcceptanceGate` 已合入 `FinalResidualPolicy`；协议/API 字段为 `final_residual_decision`，不存在旧 alias。
- `CanonAdmissionService` 已拥有唯一 candidate -> canon 原子写事务；旧 `_apply_canon_candidate`、`commit()` 和 outcome coercer 已删除。
- live repair loop 已迁入 `forwin.review.repair.service`；`ChapterPipeline` 只调用明确的 repair/canon service。
- D08 quality analysis 共享缓存已落地；旧 `WritingOrchestrator` 与 90 条跨模块方法赋值均已删除。Pipeline stage owner、`RepairExecution` 与 `CanonPreparationContext` 完成 Phase B 的行为和结构收口。
- Phase C 实现完成：Genesis handoff 永久冻结；EntityRegistrar 只产出候选 `EntityAdmissionPlan`，Canon 是唯一实体写方；旧 SubWorld admission 全链删除；planning 服务群归口到 `PlanningService` / `PlanningQuery` / `PlanHealthService`。200 章 no-hotfix 运行 gate 尚未执行。

## 已知限制

- `canon_quality` 仍以 countdown/state-kind 为主要组织形态，短期内通过 project-level rule profile 降低故事硬编码；完整 schema 化 state projection 是后续大改。
- 中文否定识别目前使用轻量前缀窗口和规则 registry，能挡住明显的“不要/避免/禁止 X”误判，但不是完整句法 scope 分析。
- 质量闭环已开始从 review-time 反应式扫描前移到 plan-time patch；遗留 reviewer signal 仍会保留兜底提示，但必须避免重复注入同一约束。
- Prompt 回归测试固定 deterministic fixture 和 revision hash，不替代真实 LLM A/B 评估。

## 2026-07 V5 Slice 4 Status

状态：implementation-complete，已部署；旧 200 章运行已退出并降级为 pre-roadmap soak，等待新 RC gate。

- WriterOutput 的实体准入、状态变化、事件、剧情线 beat 与时间推进统一翻译为 GraphDelta；未知角色/事件引用与 stale old-value 均 fail-closed。
- `BookStateQuery` 已接管 context、review、checker、autofix、finalization 和 EntityRegistrar 的 accepted-state 读取；`ReviewQuery` 接管已接受摘要与 review notes。
- Canon 已删除 legacy `apply_state_changes/apply_events/apply_thread_beats/apply_time_advance` 双写；`StateRepository` 对应 accepted-state 查询族及 Genesis legacy entity 预灌已物理删除。
- `entities/entity_aliases` 只保留作 Canon 后身份唯一性索引；八张 legacy accepted-state 表及其 ORM 已物理删除。
- live page/proposal/Obsidian/retrieval helper 已迁入 `knowledge_system` / `obsidian`，`forwin.world_model` facade 与旧 world-model ORM 已物理删除。
- 项目详情、CLI、phase3/phase4、thread sampling、personality relation enrichment 与兼容 HTTP adapter 均读取 BookState/Knowledge Projection，不再回退旧 accepted-state 表。
- GraphDelta patch 持久化现在记录并按 sequence 重放，避免 create/append 顺序在数据库 round-trip 后漂移。
- Candidate 版本现在 append-only，持久化 body/plan/policy 指纹、review/repair/entity/eligibility/Canon plan 与父版本链。
- `CanonCommitPlan` 在事务外冻结；Canon 事务锁项目/章节/候选并原子提交 BookState、实体、义务、accepted 状态、审计、outbox 与 `CanonCommitRecord`。
- 五阶段故障注入、stale revalidation、幂等 replay 和 projection retry 已有聚焦测试；world edit proposal 也已归入 Canon 单写者。

Schema 同期完成破坏性收口：历史 Alembic 链与 `models/base.py` 手写升级器已删除，唯一 revision 为 `0001_v5_baseline`；生产启动拒绝非 v5 schema，不迁移旧库。验证口径将在本 Slice completion commit 重新记录；30/60/100/200 章 gate 均未宣称完成。

## 2026-07 V5 Slice 5 Status

状态：implementation-complete，local verification complete，已部署；旧 200-chapter run 仅保留为 pre-roadmap soak，等待新 RC gate。

- `WritingOrchestrator`、`forwin.orchestrator` 与 `orchestrator_loop_core` 已删除；`ChapterPipeline` 静态组合 12 个 typed stage owner，显式接收具体类型协作者，类体跨模块函数赋值为零。
- Genesis 已收口为 `forwin.genesis/{workspace,handoff}`；旧四个包/门面和 workspace 对 `book_genesis` 的延迟查询全部删除。
- `forwin.http.create_app()` 与 instance-owned `HttpRuntime` 已接管 FastAPI 生命周期；`api_core`、根 route registry、根 route modules、模块级 API mutable state 全部删除。
- `forwin.api` 只公开 `app/create_app/lifespan`；动态 module proxy、route handler `globals()` 注入与私有 patch point 已删除。
- `ProjectApplicationService` 接管项目、Genesis、章节和 review；`TaskApplicationService` 接管任务；`ProjectControlApplicationService` 接管 project-control；`PublisherApplicationService` 接管 publisher/extension；`GenerationApplicationService` 是唯一 durable generation task 入口。
- `/api/generate`、`GenerateRequest` 与远程 LLM-eval 调用已删除；CLI 的 generate/read/status 全部通过 `ForWinAPIClient`，worker 只调用 `execute_claimed`。
- Chapter Review API 与操作台按 `draft_review -> repair -> residual_eligibility -> gate_delegation -> canon` 五层展示；Canon 只认 candidate commit 身份，Review 详情不再使用 alert 或 `review_engine_decision`。
- 根层 `api_project_ops`、`api_project_policy`、`api_publisher_ops`、`project_ops`、`api_schemas`、`api_project_payloads` 及 context/retrieval/writer 转发壳已物理删除。
- D25 命名与所有权切换完成：audit event、planning control、review rule、Codex action、HTTP project-control 和 UI project-control 各有唯一 owner；生产 Python/JS/HTML 对 `governance` 零命中。
- 全仓生产代码不再使用星号导入、类/模块身份篡改或 `common/constants` 借道 re-export；机械删除 2,336 个未使用 import，并修复因此暴露的 8 个隐性依赖和 `llm_eval` 未定义配置。

当前验收证据：`ruff check forwin` 与 `compileall -q forwin` 通过；factory/architecture 39 项、durable task/application/worker 39 项、project operation 28 项、五层 Review 与页面渲染 14 项、Canon repair 32 项、MCP 代表路径 6 项通过；rendered inline JavaScript 通过 Node syntax check，Review 模态窗 Playwright 交互 1 项通过。内置 Browser 不可用，按批准的 fallback 使用仓库 Playwright。全仓 1565 tests collect 成功且无收集错误。代码已部署并通过生产探针；旧长跑已归档为 pre-roadmap soak，Phase E/F 仍须由冻结 RC 上的全新 200 章 no-hotfix gate 证明。

## 2026-07 V5 Post-Convergence Cleanup Status

状态：implementation-complete，聚焦验证完成；随 `master` 同步部署。

- 删除零调用根模块 `api_artifacts.py`、`api_task_history.py`，不保留 facade。
- 删除 audience 的 `analysis` 兼容 shim 与根级 `audience_metrics` 副本；信号分级、趋势、关键词情绪统一归 `forwin.audience.feedback`。
- `StateRepository` 删除 6 个零调用查询：prompt trace、repair phase、review note、全量 narrative constraint、decision event、audience trend；领域查询继续由各自 owner 提供。
- planning 删除已由 experience service 接管的 audience 查询副本和重复 calibration DTO；Writer、Review、quality gate 共享唯一 skill-layer trace 序列化函数。
- 本 Slice 生产与测试净减 734 行；`ruff check forwin`、`compileall -q forwin`、architecture boundary 27 项通过。按用户要求未重复运行全量测试。
- 第二轮残留清理删除零调用的 context/runtime/generation ports、personality validation、review interval/repair handler 与 map pathfinding facade；Genesis handoff 已接管的项目地图初始化副本及其级联 helper 同步物理删除。
- Proposal HTTP 只保留通用 `/proposals` owner，Obsidian 只保留 import/export；world-model/obsidian proposal 兼容入口和 world-model Obsidian 转发入口均删除，World Studio 与 MCP 已切换到 canonical route。
- BookState path patch、quality signal 去重与数据库 retry 分类各收敛为一个实现；HTTP adapter 的 project 404 判定统一由 request support 持有。
- 第二轮生产、前端与测试净减 1,211 行；`ruff`、`compileall`、World Studio build 通过，architecture/map/proposal/DB retry/readability-continuity/HTTP factory 聚焦验证 50+ 项通过。另有 3 个 `canon_quality_service` 旧 fixture 在 `159baf8` 加入 unasked-answer fail-closed 后仍未同步，失败位于表单 schema 校验而非本轮去重路径。

## 2026-07 Integrated Roadmap Status

`docs/superpowers/specs/2026-07-06-forwin-integrated-roadmap-full-design.md` and `docs/superpowers/plans/2026-07-06-forwin-integrated-roadmap-full.md` track the integrated runtime work now reflected in code:

- Retrieval: LAN embedding gateway defaults plus accepted-chapter memory re-embedding script.
- Pulp runtime: lightweight BookState fallback deltas for degraded world extraction.
- Arc planning: `ArcActivationReviewPack` and decision-event audit before continuation materialization.
- Canon / repair: `pulp_fatal` gate mode, expanded fatal signals, P0 hard obligation blocking, hard blocker detection for auto-review retry.
- Experience layer: runtime-expanded 50+ pulp trope registry, template repetition cooldown, and prompt-visible execution constraints.
- Operator UI: generation queue health summaries, review / repair queues, retry and soft-accept controls, and proposal-backed repair actions.

## 历史实施计划

| 文档 | 状态 | 说明 |
|---|---|---|
| `docs/superpowers/plans/2026-04-24-forwin-v4-world-model.md` | historical-plan | 解释 `world_model_v4` / `reviewer_v4` side-by-side 来源；已被 BookState final 覆盖。 |
| `docs/superpowers/plans/2026-04-24-forwin-v4-1-runtime-hardening.md` | historical-plan | V4.1 hardening 计划；“V4 source semantics” 口径已被 BookState final 覆盖。 |
| `docs/superpowers/specs/2026-07-09-forwin-reckless-review-mode-design.md` | historical-plan | 已被 RuntimePolicy `gate_delegate=human|spark` 与 fail-closed gate delegation 覆盖。 |
| `docs/superpowers/plans/2026-07-09-forwin-reckless-review-mode.md` | historical-plan | 独立 reckless mode 已删除，不再是当前产品或运行时概念。 |
| `forwin_design_cleanup_update_plan.md` | historical-plan | 旧冗余清单，已被 v5 审计和实施规格覆盖。 |
| `forwin_decoupling_plan.md` | historical-plan | 旧解耦建议；其中 `forwin.orchestration` 目标已被 v5 owner-local service 设计取代。 |
| `pulp_profile_upgrade_plan.md` | historical-plan | 旧实现计划；有效运行语义已固化进 `RuntimePolicy.for_profile("pulp")` 与当前架构入口。 |

## Future Product Backlog

以下内容不作为当前 V4.5.x 后端缺口：完整 World Studio 图谱/地图/认知 UI、metrics dashboard / SLO 看板、Skill API / UI / script-backed / tool-backed execution、native GraphDelta extractor、完整 world/map/cognition rule pack、复杂交通工具体系、可视化地图编辑器、Neo4j 主存储、tile renderer、Genesis 深层 workflow editor、自动 retcon accepted canon、完整 LLM editorial reviewer 产品化。
