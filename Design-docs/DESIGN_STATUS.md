# ForWin Design Status

更新时间：2026-07-09

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
| `../forwin_architecture_consolidation_audit.md` | active-current | v5 架构收敛决策、删除清单和 Phase A-F 路线。 |
| `../docs/superpowers/specs/2026-07-09-forwin-v5-architecture-convergence-design.md` | active-current | v5 破坏性收敛规格；旧项目和旧设置不迁移。 |
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
| `provisional_mechanism_check.md` | legacy-compatibility | legacy provisional 边界说明；当前判断路径是 Scenario Rehearsal、Candidate Draft Review 和 BookState gate。 |
| `review_fix_log_2026-04-15.md` | legacy-compatibility | 历史 review 修复记录。 |

## 兼容 / 弃用矩阵

本矩阵是代码迁移的当前权威入口。新增调用方不得再引入 `deprecated` 模块；保留调用方必须通过对应的 current/compat 模块收束。

| 模块 | 状态 | 当前替代 | 删除 / 复核目标 | 说明 |
|---|---|---|---|---|
| `forwin.world_model` | deprecated | `forwin.book_state` | v5.0 删除直接业务依赖 | 仅保留 legacy projection / wiki / export 兼容入口，不是 canon；不得重新进入 accepted-chapter runtime。 |
| `forwin.world_model_v4` | removed | `forwin.book_state` | 已删除 | 旧 compatibility projection/debug bridge 已从生产模块删除。 |
| `forwin.world_v4_compat` | removed | `forwin.book_state` | 已删除 | 旧 compatibility projection writer 已从生产模块删除。 |
| `forwin.reviewer_v4` | deprecated | `forwin.world_v4_review_gate` | v5.0 删除 alias 包 | 仅作为旧导入路径 alias；新代码必须导入 `world_v4_review_gate`。 |
| `forwin.world_v4_review_gate` | legacy-compatibility | `forwin.review` 主域 | v5.0 复核是否仍需 extraction gate | 兼容 gate，不是主 chapter reviewer。 |
| `forwin.planning.scenario_rehearsal` | deprecated | `forwin.planning.scenario_rehearsal_service` | v5.0 删除直接业务依赖 | 旧 monolith 仅保留历史 API 兼容；新增 orchestration 必须走 service。 |
| `forwin.planning.scenario_rehearsal_service` | active-current | 无 | 无 | 当前 Scenario Rehearsal service 入口。 |
| `forwin.runtime_settings` | removed | `forwin.runtime.policy` | 已删除 | 不再有进程内可变生成设置文件。 |
| `Project.governance_json` settings | removed | `Project.runtime_policy_json` + version | 已删除 | manual checkpoint / decision event 等治理账本仍保留；项目运行设置已迁出 governance 命名。 |
| `forwin.orchestration` | removed | owner-local typed services | 已删除 | `ChapterPipelinePorts` / `OrchestrationEvent` 为零调用 `Any` ports，未作为 v5 边界采用。 |
| `forwin.reviewer` | removed | `forwin.review` | 已删除 | 草稿评审、decision rules 与 repair 归入一个 bounded package，不留旧导入 alias。 |
| `forwin.review_engine` | removed | `forwin.review.decision` | 已删除 | 决策规则不再作为与 review 平级的第二套域。 |
| `forwin.reviser` | removed | `forwin.review.repair` | 已删除 | rewrite executor 与 verifier 归 repair owner。 |
| `HistoricalReviewHub` | removed | `forwin.review.draft_service.DraftReviewService` | 已删除 | 草稿评审只产出 evidence/verdict，不决定 canon。 |
| `FinalAcceptanceGate` | removed | `forwin.review.decision.rules.final_residual.FinalResidualPolicy` | 已删除 | repair 耗尽后的残留策略；字段为 `final_residual_decision`，仍必须经过 BookState canon commit。 |
| `_apply_world_v4_gate` | removed | `_commit_book_state_canon` | 已删除 | 当前路径是 BookState extraction/review/compile，不再使用 legacy v4 命名。 |
| `_compile_world_model_after_acceptance` | removed | 无 | 已删除 | 恒返回 `True` 的空壳及两处调用均删除。 |
| `WritingOrchestrator._apply_canon_candidate` | removed | `forwin.canon.CanonAdmissionService.commit` | 已删除 | generation 与人工接受共享同一强类型 canon admission；不接受字符串/None compatibility outcome。 |
| `orchestrator_loop_core.quality_gate_types` | removed | `forwin.canon.types` | 已删除 | canon outcome 类型归 canon owner；`CanonApplyOutcome` 改名 `CanonAdmissionOutcome`。 |
| `orchestrator_loop_core.repair_loop` | removed | `forwin.review.repair.RepairService` | 已删除 | 1056 行 live repair 算法迁入 owner；pipeline 只调用 `review_candidate` / `repair_canon_block`。 |
| `orchestrator_loop_core.__init__` re-export | removed | explicit submodule imports | 已删除 | 包初始化不再反向加载 `common.*` 和完整 `WritingOrchestrator`。 |

## 2026-07 V5 Slice 1 Status

状态：implementation-complete，尚未部署，30 章生产 gate 未执行。

- `InfrastructureConfig` 仅负责基础设施、凭据和环境模型目录；不存在 `Config` 兼容名。
- 项目运行行为只来自版本化 `RuntimePolicy`，质量 profile 仅有 `standard/pulp`，gate delegate 仅有 `human/spark`。
- generation task 保存不可变 policy snapshot；所有任务生产者和 worker 执行均通过 `GenerationApplicationService`。
- GET `/api/settings/llm` 是 secret-free 只读目录；控制台不再保存 API Key、模型 profile 或全局生成偏好。
- 项目抽屉是唯一 RuntimePolicy UI 写入口；MCP 对应工具为 `project_set_gate_delegate`。
- 已删除旧 mode/reckless/request override、RuntimeSettingsStore、旧治理设置 DTO，以及绑定这些接口的失效测试套件。

实现提交：`a7f53bb`、`f7790c5`、`61392af`、`6c2eb0f`、`131e697`、`bc85d91`、`3e0c10f`、`a6a75fb`、`589e58a`、`e87e67b`、`307b0dd`。

验证口径：前序聚焦 policy/store/API/snapshot/application/worker/MCP/browser 测试已通过；completion gate 的 architecture/config 为 24 passed，策略分支补充为 3 passed，`compileall` 成功，全仓 1598 tests collect 成功且无收集错误。全量执行按用户要求由独立测试任务承担，本收敛任务不重复启动；部署和 30 章 no-hotfix gate 仍是后续显式步骤。

## 2026-07 V5 Slice 2 Status

状态：implementation-in-progress，未部署。

- 已删除零调用 `forwin.orchestration` ports 和恒成功 `_compile_world_model_after_acceptance` 空壳。
- BookState canon 主路径已从 `_apply_world_v4_gate` 改名为 `_commit_book_state_canon`，block kind 改为 `book_state`。
- `reviewer`、`review_engine`、`reviser` 已物理合并为 `forwin.review/{draft_service,decision,repair}`；不存在旧 package alias。
- `HistoricalReviewHub` 已破坏性改名为 `DraftReviewService`；runtime 字段为 `draft_review`。
- `FinalAcceptanceGate` 已合入 `FinalResidualPolicy`；协议/API 字段为 `final_residual_decision`，不存在旧 alias。
- `CanonAdmissionService` 已拥有唯一 candidate -> canon 决策体；`WritingOrchestrator._apply_canon_candidate` 和 outcome coercer 已删除。
- live repair loop 已迁入 `forwin.review.repair.service`；`WritingOrchestrator` 不再暴露 `_review_and_maybe_rewrite` / `_run_canon_repair_for_block`。
- 下一步是把 canon/repair 函数族从 `WritingOrchestrator` 属性拼装迁入显式协作对象；Phase C-F 尚未开始。

## 已知限制

- `canon_quality` 仍以 countdown/state-kind 为主要组织形态，短期内通过 project-level rule profile 降低故事硬编码；完整 schema 化 state projection 是后续大改。
- 中文否定识别目前使用轻量前缀窗口和规则 registry，能挡住明显的“不要/避免/禁止 X”误判，但不是完整句法 scope 分析。
- 质量闭环已开始从 review-time 反应式扫描前移到 plan-time patch；遗留 reviewer signal 仍会保留兜底提示，但必须避免重复注入同一约束。
- Prompt 回归测试固定 deterministic fixture 和 revision hash，不替代真实 LLM A/B 评估。

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
