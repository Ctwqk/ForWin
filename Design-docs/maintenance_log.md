# ForWin Maintenance Log

本文件是项目总维护日志。只记录关键代码改动、行为变化、重要 bug、验证结果和部署状态；不记录日常过程流水账。

维护规则：

- 新条目追加到最上方。
- 每条保持短小，优先写“影响”和“验证”，不要复制完整实现细节。
- 如果改动尚未部署到 `8899`，必须明确写“未部署原因”和“切换条件”。
- 旧的专项日志可保留，但关键结论需要汇总到这里。

## 2026-04-18

### V2.8.1 全量补齐增量（scene-era contract / WNER 证据 / feedback 校准 / trope 导入 / 稳定性）

按 `V2_8_1.md` 与完成情况文档补齐剩余设计口径，同时保留旧数据兼容。

关键变化：

- `WriterOutput` 扩展 `scene_continuation`、`lore_candidates`、`timeline_hints`、`writer_notes`；scene prompt 增加 continuation 标签，structured extraction 增加第三段续航信息抽取。
- WNER 将 `confirmed_signals` 提升为一等 evidence，保存 `reviewer_mode` 与 `confirmed_signal_refs`；LLM hard fail 不能只依赖 audience signal。
- reader estimate 改为平台指标优先，缺失时回退 `comment_proxy`；aggregate/trend 暴露 `estimation_method` 与 `scale_confidence`。
- 新增 feedback action effectiveness 视图，并接入 governance insights。
- Trope registry 保持 seed 真值，新增完整 JSON 清单校验、summary、过滤和 validate API；未提供 188 清单时继续标记为 starter。
- band checkpoint 增加基于 WNER review meta 的 director imbalance warn 规则，并避免对无体验证据的旧式极简输出误报。
- task-center/API 暴露重启中断状态与恢复建议，新增 active generation task 只读检查接口。

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/protocol/scene.py forwin/protocol/writer.py forwin/protocol/review.py forwin/writer/prompts.py forwin/writer/chapter_writer.py forwin/reviewer/webnovel.py forwin/reviewer/hub.py forwin/orchestrator/feedback_aggregator.py forwin/audience_metrics.py forwin/protocol/trope_library.py forwin/governance_checks.py forwin/orchestrator/loop.py forwin/api_schemas.py forwin/api.py forwin/models/base.py forwin/models/publisher.py tests/test_writer_split_pipeline.py tests/test_audience_feedback_alignment.py tests/test_phase05_regressions.py tests/test_governance_decision_api.py tests/test_governance_review_and_checkpoint.py tests/test_generation_task_persistence.py`
- `PYTHONPATH=. pytest -q tests/test_writer_split_pipeline.py tests/test_audience_feedback_alignment.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_generation_task_persistence.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py -k "wener or trope_templates_and_band_experience_override_api or chapter_review_api_exposes_v27_fields"`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_api_pages_rendering.py tests/test_project_operation_guards.py tests/test_continue_project_orphan_review.py tests/test_api_runtime_progress.py tests/test_bulk_delete_api.py`
- `PYTHONPATH=. pytest -q`

部署状态：未部署到 `8899`。原因：本轮完成代码与本地回归，尚未重建/重启容器。切换条件：先确认 `/api/tasks/active-generation-check` 返回 `safe_to_restart=true`，再执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

## 2026-04-17

### P1/P2B 治理层补完（future constraint 开关 / arc replay / constraint 生命周期）

把原始治理层设计剩余缺口收完，重点是让已有治理对象形成稳定导演闭环，不新增新表。

关键变化：

- `future_constraints_enabled` 成为真实判定开关：关闭时 constraint 仍可保存和展示，但不进入 chapter review、context pack、band checkpoint、`future_constraint_block`、next-band compatibility 或 future preservation 判定。
- band checkpoint 补齐 `intra_band_consistency`：未处理 review、latest review fail、failed provisional gate、同 band 未决 checkpoint 都会形成明确 issue。
- `next_band_compatibility` 扩展为读取 next band task contract、active constraints 与当前 band 末状态；hard constraint 才能 fail，next-band task/soft 冲突只 warn。
- checkpoint evaluator 异常会落 `BandCheckpoint(status="error")`，写 `checkpoint_evaluator_error` 与 `band_checkpoint_hit`，并暂停运行。
- review/checkpoint issue 新增 `issue_group`，区分 `fact_conflict`、`director_imbalance`、`runtime_observation`、`governance_action`；UI 同步显示 issue group 与 future preservation category。
- narrative constraint 新增编辑/停用 API，所有 lifecycle 修改必须带 reason，并写 `constraint_updated` / `constraint_archived`。
- `causal-replay` 支持 `scope=arc` 与可选 `arc_id`；项目 payload 暴露 `active_arc_id`，drawer 默认可看 arc 因果回放。
- `governance-insights` 增加 `issue_group_distribution`，推荐项开始按事实冲突/导演失衡聚合。
- 书本 drawer 补齐 constraint 编辑/停用、future constraints 是否参与判定、arc replay、issue group/category 展示。

影响文件：

- `forwin/governance.py`
- `forwin/governance_checks.py`
- `forwin/protocol/review.py`
- `forwin/context/assembler.py`
- `forwin/state/repo.py`
- `forwin/state/updater.py`
- `forwin/orchestrator/loop.py`
- `forwin/api_schemas.py`
- `forwin/api_project_payloads.py`
- `forwin/api.py`
- `forwin/api_pages.py`
- `tests/test_governance_decision_api.py`
- `tests/test_governance_review_and_checkpoint.py`
- `tests/test_api_pages_rendering.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/governance.py forwin/protocol/review.py forwin/governance_checks.py forwin/api_schemas.py forwin/api_project_payloads.py forwin/state/repo.py forwin/context/assembler.py forwin/state/updater.py forwin/orchestrator/loop.py forwin/api.py forwin/api_pages.py tests/test_governance_decision_api.py tests/test_governance_review_and_checkpoint.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_api_pages_rendering.py tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py`
- `PYTHONPATH=. pytest -q`
- `node --check /tmp/forwin_home_script.js`
- `docker compose build forwin`
- `docker compose up -d forwin`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/`
- `curl -fsS http://127.0.0.1:8899/api/task-center/items?limit=20`
- `curl -fsS http://127.0.0.1:8899/api/projects?limit=5`
- `GET /api/projects/{project_id}/causal-replay?scope=arc`
- `GET /api/projects/{project_id}/governance-insights`

部署状态：已部署到 `8899`。部署前确认无 `starting/running/terminating` generation task；当前容器状态 `healthy`，本地全量 Python 回归 `178 passed, 5 subtests passed`，`phase05` 回归 `113 passed`。

### P2A 闭环增量（reason contract / replay&insights UI / taxonomy / 风险分类）

围绕现有治理层做一次收口式增量，不新增治理对象，只补齐口径、可见性和统计闭环。

关键变化：

- `DecisionEvent.event_type` 收成中心化 taxonomy，并在 API / orchestrator 落事件时统一校验，不再继续散落自由字符串。
- 治理动作开始强制 `reason`：项目治理修改、manual checkpoint、checkpoint pass/override、review approve 缺 `reason` 时统一返回 `400`。
- `future_constraint_block` 真正贯穿到 `blocking_reason`；chapter review 因 hard future constraint 失败时，continue / approve+continue 会稳定暴露阻断原因和 `decision_event_id`。
- `governance-insights` 补齐 `recommended_adjustments` 与 `recent_examples`，继续只基于 `DecisionEvent` / checkpoint verdict 反推，不新增第二套统计真值。
- runtime observation 补齐 stage / duration / fallback 摘要事件；writer fallback 开始直接消费 `generation_meta["model_fallbacks"]`，落成 `fallback_profile_switched`。
- `future_resource_preservation` 从单一粗粒度 warn 升级为 5 类风险分类：`character_locked_out`、`thread_closed_too_early`、`relationship_closed_too_early`、`secret_over_explained`、`growth_arc_completed_too_early`。
- 书本 drawer 新增“因果回放”“治理洞察”卡片；治理动作改走轻量 reason modal；决策事件深链修正为字符串 ID，不再错误地按数字处理。

影响文件：

- `forwin/governance.py`
- `forwin/api_schemas.py`
- `forwin/api_project_payloads.py`
- `forwin/governance_checks.py`
- `forwin/orchestrator/loop.py`
- `forwin/writer/llm_client.py`
- `forwin/api.py`
- `forwin/api_pages.py`
- `tests/test_generation_control_payload.py`
- `tests/test_governance_review_and_checkpoint.py`
- `tests/test_governance_decision_api.py`
- `tests/test_api_pages_rendering.py`
- `tests/test_project_operation_guards.py`
- `tests/test_phase05_regressions.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/governance.py forwin/api_schemas.py forwin/api_project_payloads.py forwin/governance_checks.py forwin/orchestrator/loop.py forwin/writer/llm_client.py forwin/api.py forwin/api_pages.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py tests/test_phase05_regressions.py tests/test_generation_control_payload.py tests/test_governance_decision_api.py tests/test_governance_review_and_checkpoint.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q`
- `docker compose build forwin`
- `docker compose up -d forwin`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/`
- `curl -fsS http://127.0.0.1:8899/api/task-center/items?limit=20`
- `curl -fsS http://127.0.0.1:8899/api/projects?limit=5`
- `docker compose ps forwin`
- `docker compose logs --tail=120 forwin`

部署状态：已部署到 `8899`。部署前已确认 `task-center` 无 `starting/running/terminating` 生成任务；当前容器状态 `healthy`，线上 `health` / 首页 / `task-center` / `projects` smoke 正常，本地全量 Python 回归 `176 passed, 5 subtests passed`。

### P1 收尾、全量回归与 `8899` 部署

把治理层 P1 最后一段闭环收完，并完成 `8899` 切换与线上 smoke。

关键变化：

- 书本 drawer / continue modal 补齐治理深链：支持从 `blocking_reason`、chapter review、band checkpoint 直接跳到对应决策链，决策时间线支持范围筛选。
- 修复治理上下文装配回归：`context/assembler` 改为按关键字参数读取 active constraints，避免 chapter review / governance pack 组装时报错。
- 为 Python 3.13 下失效的 Starlette `TestClient` 增加测试侧兼容调用层，绕开 AnyIO threadpool 死锁；`phase05` 回归恢复可跑。
- 对齐三条旧回归用例到当前治理语义：chapter task contract 会参与 review，因此相关 fixture 改为显式满足任务合同，而不是继续假设旧版宽松 verdict。
- `forwin` 容器已重建并切换到 `8899`；线上首页、health 与 task-center smoke 正常。

影响文件：

- `forwin/api_pages.py`
- `forwin/context/assembler.py`
- `tests/test_api_pages_rendering.py`
- `tests/test_phase05_regressions.py`
- `Design-docs/maintenance_log.md`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/api_pages.py forwin/context/assembler.py tests/test_api_pages_rendering.py tests/test_phase05_regressions.py`
- `PYTHONPATH=. pytest -q tests/test_api_pages_rendering.py tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py`
- `PYTHONPATH=. pytest -q`
- `docker compose build forwin`
- `docker compose up -d forwin`
- `curl -fsS http://127.0.0.1:8899/health`
- `curl -fsS http://127.0.0.1:8899/`
- `curl -fsS http://127.0.0.1:8899/api/task-center/items?limit=20`
- `docker compose ps forwin`
- `docker compose logs --tail=120 forwin`

部署状态：已部署到 `8899`。当前容器状态 `healthy`，本地全量 Python 回归 `173 passed`，`phase05` 回归 `113 passed`。

### 治理因果链 / replay / insights 收口

把 P1 的 `DecisionEvent` 从“事件列表”收成可追溯因果链，并启动 P2 的 replay / insight 接口。

关键变化：

- `DecisionEvent` 新增 `parent_event_id` / `causal_root_id`，形成稳定的 run / repair / approve / override 因果链。
- `pause requested`、`terminate requested`、`continue requested`、`pause reached`、`terminate reached`、`repair started/failed/succeeded`、`forced_accept_applied`、`band_checkpoint_hit` 等关键路径开始稳定落事件。
- `ChapterReviewDetail.decision_refs`、`BandCheckpointDetail.decision_refs` 开始真实填充；`blocking_reason` 会尽量回链到对应决策事件。
- 新增 `GET /api/projects/{project_id}/causal-replay` 与 `GET /api/projects/{project_id}/governance-insights`，支持按 root 链回放和基于现有事件/ checkpoint 的 override / blocking 聚合。
- runtime observation 继续细化：补入 `provisional_gate_evaluated`、`llm_request_started/succeeded/failed`、`retry_attempt`、`memory_index_upsert_*` 摘要事件。

影响文件：

- `forwin/models/governance.py`
- `forwin/models/base.py`
- `forwin/governance.py`
- `forwin/config.py`
- `forwin/state/repo.py`
- `forwin/state/updater.py`
- `forwin/api_schemas.py`
- `forwin/api_project_payloads.py`
- `forwin/api.py`
- `forwin/orchestrator/loop.py`
- `tests/test_generation_control_payload.py`
- `tests/test_governance_decision_api.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/models/governance.py forwin/governance.py forwin/config.py forwin/models/base.py forwin/state/repo.py forwin/state/updater.py forwin/api_schemas.py forwin/api_project_payloads.py forwin/api.py forwin/orchestrator/loop.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py`
- `PYTHONPATH=. pytest -q tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py tests/test_api_pages_rendering.py`

部署状态：未部署到 `8899`。原因：本轮仍是本地代码与回归验证，尚未重建/重启容器；切换前仍需确认没有运行中的 generation task。切换条件：确认 `8899` 无活跃生成任务后，执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

### 治理层前端入口补齐

把此前只在后端生效的治理能力补进现有 drawer / modal，不新建独立页面。

关键变化：

- 书本详情 drawer 新增治理设置卡片，展示 `governance`、`blocking_reason`、`latest_band_checkpoint`、约束摘要。
- drawer 新增项目级治理修改入口，可直接调整默认运行模式、推进策略、review 间隔、auto band checkpoint、manual checkpoint、future constraints。
- drawer 新增轻量治理操作：插入 manual checkpoint、创建 narrative constraint、对 band checkpoint 执行 override。
- drawer 新增决策时间线展示，直接读取 `decision_timeline`，让阻断点和治理动作可解释。
- 继续生成不再直接裸调 API，而是复用现有生成 modal，允许在 continue 时做运行级治理覆盖。
- 新建生成任务 modal 也新增治理覆盖项：`progression_mode`、`auto_band_checkpoint`、`manual_checkpoints_enabled`、`future_constraints_enabled`。

影响文件：

- `forwin/api_pages.py`
- `tests/test_api_pages_rendering.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/api_pages.py tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_api_pages_rendering.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_project_operation_guards.py tests/test_governance_review_and_checkpoint.py`
- `node --check /tmp/forwin_home_script.js`

部署状态：未部署到 `8899`。原因：本轮只完成页面层接线与本地回归，尚未重建并切换容器；切换仍需避开运行中的 generation task。切换条件：确认 `8899` 无 `starting/running/terminating` 生成任务后，执行 `docker compose build forwin` 与 `docker compose up -d forwin`。

### 治理层增量实现（strict gate / band checkpoint / decision log）

围绕现有 `WritingOrchestrator -> chapter review -> canon -> phase3/phase4` 主链，新增治理层骨架并接入运行时。

关键变化：

- 新增项目治理设置 `projects.governance_json`，新项目默认进入严格治理：`serial_canon_band_guard + auto_band_checkpoint + blackbox`。
- 新增独立治理域：`BandCheckpoint`、`NarrativeConstraint`、`DecisionEvent`，不再把治理真值混在 chapter review 或 `stage_history` 里。
- `generate` / `continue-generation` / `approve review + continue` 开始套用项目治理和运行级覆盖；项目/任务 payload 暴露 `governance`、`latest_band_checkpoint`、`blocking_reason`、`next_gate`。
- 主循环新增 strict progression gate：前序章未 `accepted` 时阻断后章；跨 band 时要求上一 band checkpoint 已 `pass/overridden`。
- 新增 manual checkpoint / governance / constraints / decision-events API；manual checkpoint v1 限制在章边界与 band 边界。
- 自动 band checkpoint 已落地为一等对象，`warn/fail` 会触发暂停；chapter 级与 band 级的关键决策开始写入 `DecisionEvent`。

影响文件：

- `forwin/governance.py`
- `forwin/governance_checks.py`
- `forwin/models/governance.py`
- `forwin/models/base.py`
- `forwin/models/project.py`
- `forwin/models/phase.py`
- `forwin/state/repo.py`
- `forwin/state/updater.py`
- `forwin/context/assembler.py`
- `forwin/reviewer/hub.py`
- `forwin/orchestrator/loop.py`
- `forwin/api.py`
- `forwin/api_project_payloads.py`
- `forwin/api_runtime.py`
- `forwin/api_schemas.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/governance.py forwin/models/governance.py forwin/context/assembler.py forwin/state/repo.py forwin/state/updater.py forwin/api_schemas.py forwin/api_project_payloads.py forwin/api_runtime.py forwin/config.py forwin/api.py forwin/orchestrator/loop.py`
- `PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_project_operation_guards.py tests/test_continue_project_orphan_review.py`
- `PYTHONPATH=. pytest -q tests/test_phase05_regressions.py -k "chapter_review_api_exposes_v27_fields or trope_templates_and_band_experience_override_api"`

部署状态：未部署到 `8899`。原因：本轮只完成代码与本地回归，尚未重建/重启容器；同时切换前仍需确认没有运行中的 generation task。切换条件：补完剩余治理 UI/API 回归后，执行 `docker compose build forwin`，确认任务空闲，再 `docker compose up -d forwin`。

### Chapter review / band checkpoint 的 P1 治理检查

继续补上此前未完成的两项：

- `HistoricalReviewHub` 在 chapter review 中新增“规划任务履约”和“hard future constraint”检查，约束冲突可直接给出 `fail`。
- auto band checkpoint 从纯 accepted 判定扩展为 band task completion、future constraint / next-band compatibility、future resource preservation 风险检查。
- 新增治理专项测试，覆盖 chapter review 的 task/constraint 检查和 auto band checkpoint 的 warn 行为。

影响文件：

- `forwin/reviewer/hub.py`
- `forwin/governance_checks.py`
- `forwin/orchestrator/loop.py`
- `tests/test_governance_review_and_checkpoint.py`

验证：

- `PYTHONPATH=. python3 -m py_compile forwin/reviewer/hub.py forwin/governance_checks.py forwin/orchestrator/loop.py tests/test_governance_review_and_checkpoint.py`
- `PYTHONPATH=. pytest -q tests/test_governance_review_and_checkpoint.py`

部署状态：同上，未部署到 `8899`。

### 总规划与当前完成度文档

新增 `Design-docs/project_master_plan.md`，把 `v2.3 Writer 主链`、`v2.6 评论反馈层`、`v2.7 体验层` 合并成统一规划，并按当前代码实现给出完成度判断。

关键结论：

- 项目当前最准确的定义是“长篇连载自动化写作系统”，而不是单一 Writer 原型。
- `v2.3` 主链已经可运行，但 scene-era contract 还没补齐，尤其缺 `scene continuation`、`lore_candidates`、`timeline_hints`、`writer_notes`。
- `v2.6` 已不只是 phased rollout 早期阶段，Phase A/B/C 主骨架基本都已接入。
- `v2.7` 已进入可运行阶段，但 reviewer 闭环和体验校准闭环仍需加强。
- 总规划文档同时收束了项目功能、特性、当前真实状态机，以及建议的后续优先级。

影响文件：

- `Design-docs/project_master_plan.md`
- `Design-docs/maintenance_log.md`

## 2026-04-16

### 书本详情任务管理前端改进

书本详情抽屉新增“状态机驾驶舱”，把任务管理从零散指标改为按写作状态机展示。

关键变化：

- 详情第一屏显示当前状态、下一步、安全操作和运行风险。
- `needs_review`、`paused`、`failed/partial_failed`、`running`、`completed` 分别给出不同的人类可读说明和主操作。
- `needs_review` 会展示阻塞队列，可直接查看 Review、接受、接受并继续。
- `failed` 会展示失败章节队列，可查看失败原因；可继续时提供“重试剩余章节”。
- 运行中任务明确提示安全暂停只在 checkpoint 停住，强制终止作为次级高风险操作。
- 书本列表主按钮按状态切换为“处理 Review / 继续生成剩余章节 / 查看进度 / 写作完成 / 生成首批章节”，避免出现不可解释的禁用按钮。
- 章节流水线在缺少当前任务 stage history 时，会根据章节状态推断 accepted/drafted 的已完成步骤，避免书本入口把已完成章节显示成“未到达”。

影响文件：`forwin/api_pages.py`。

验证：

- `.venv/bin/python -m py_compile forwin/api_pages.py`
- `.venv/bin/python -m unittest tests.test_api_pages_rendering`
- `node --check /tmp/forwin_home_script.js`

部署状态：新镜像已通过 `docker compose build forwin` 构建，尚未重启 `8899`。原因是 `8899` 仍有运行中的生成任务 `78b13db89f74`；部署前仍需确认没有运行中的生成任务，或先安全暂停。

### 书本详情抽屉自动重开 bug

修复了书本详情打开后，用户关闭抽屉又被轮询自动打开的问题。

根因：`openTaskDrawer()` / `refreshCurrentDrawerIfChanged()` 的异步请求在用户关闭抽屉后仍可能返回，旧请求继续调用 `renderDrawerSnapshot()`，导致 overlay 被重新打开。

修复：为 drawer 增加 `drawerRequestToken`。每次打开刷新携带 token，关闭抽屉时递增 token 并清空当前任务；旧请求返回后发现 token 失效会直接丢弃。

影响文件：`forwin/api_pages.py`。

验证：

- `.venv/bin/python -m py_compile forwin/api_pages.py`
- `.venv/bin/python -m unittest tests.test_api_pages_rendering`
- `node --check /tmp/forwin_home_script.js`

部署状态：新镜像已通过 `docker compose build forwin` 构建，但未重启 `8899`。原因是 `8899` 当时存在运行中的生成任务 `78b13db89f74`，第 2 章处于 `writing_chapter`；直接重启会中断写作任务。切换条件：任务结束，或先安全暂停后再 `docker compose up -d forwin`。

### 失败原因可点开查看

书本详情的章节时间线中，失败、暂停、`needs_review` 等异常步骤变为可点击节点。

点击后展示：

- 当前步骤、章节、章节状态、任务状态和到达时间。
- `task.message`、`task.error`、失败章节、暂停章节、冻结产物。
- 如果章节有 review，额外拉取 review 详情，展示 verdict、issues、recommended_action、summary。

影响文件：`forwin/api_pages.py`。

维护备注：这是 UI 层的诊断入口，不改变任务状态机。

### 写作流程状态机文档

新增/更新 `Design-docs/writing_flow_state_machine.md`，记录当前真实写作流程状态机。

覆盖范围：

- API 任务状态：`starting/running/paused/needs_review/cancelled/failed/partial_failed/completed`。
- Orchestrator 主流程：规划、project 创建、arc envelope、provisional gate、chapter loop。
- 单章流水线：context、writer、review、canon、post-acceptance。
- Pause/continue 语义。
- LLM 单模型 retry 与跨 profile fallback。

同时记录了 2026-04-16 的测试失败根因：测试 fake chapter body 短于默认 `min_chapter_chars=2500`，触发 `char_count_low` warning，导致期望状态从 `partial_failed/pass` 偏移到 `needs_review/warn`。修复方式是扩展测试正文，保持测试意图不变。

验证：`timeout 180s .venv/bin/python -m unittest tests.test_phase05_regressions`，结果 113 tests passed。

### 安全暂停 / 继续生成

实现生成任务的安全暂停与继续能力。

行为变化：

- 新增 `pause_requested` / `paused`，保留 `terminating/cancelled` 作为强制终止语义。
- 暂停不打断正在进行的 LLM HTTP request，只在安全 checkpoint 停住。
- 继续生成只处理 `planned` / `failed` 章节，不重写 `accepted` 章节。
- 存在真实 `needs_review` 章节时，continue 拒绝执行，要求先处理 review。
- 任务/书本详情返回 `generation_control`，用于展示计划状态、写作状态、review 状态、当前章、下一章、已完成/失败/待 review 章节、可暂停/可继续等。

维护风险：重启容器仍会中断当前进程里的运行中任务；安全暂停是应用级协议，不等于 Docker 级热切换。

### LLM retry 后自动换模型

实现两层 LLM 请求策略。

行为变化：

- 内层保留当前 `LLMClient.chat()` 的单 profile retry。
- 当前 profile retry 用尽后，如果错误是 transient 类型，切换到下一个已保存且有 API Key 的 profile，重发同一个 request。
- transient 类型包括 `429`、`5xx`、`529`、timeout、network disconnect、connection reset。
- `400`、鉴权失败、prompt/schema/JSON 解析失败、内容质量失败、review fail 不触发跨模型 fallback。
- fallback 顺序固定为本次请求指定 profile 或默认 profile 优先，其后按保存配置顺序尝试。
- 每个任务启动时冻结 fallback profile 列表，任务运行中改模型配置不影响已启动任务。

维护备注：fallback 是单个 LLM request 粒度，不是整章失败后再换模型。

### 模型配置与中文站预设

配置页模型设置改为尽量下拉选择，减少手输。

新增/调整：

- MiniMax 中文站预设，base URL 默认 `https://api.minimaxi.com/v1`。
- Kimi / Moonshot.cn 中文站预设，支持保存 API Key 和模型 profile。
- 模型字段保留自定义输入，同时提供推荐模型下拉。
- 新建生成任务的模型选择使用已保存 profile。

维护备注：用户明确 MiniMax 和 Kimi 都买的是中文站，默认配置必须优先中文站，不应切到国际站域名。

### 生成设置

新增生成设置：

- `min_chapter_chars`，默认 `2500`。
- `review_interval_chapters`，`0` 表示不启用周期性人工检查，`N>0` 表示每 N 章进入人工 review checkpoint。

影响：最低字数会影响 ContinuityChecker verdict；测试夹具和短正文模拟必须显式满足或覆盖该设置，否则容易误触发 `char_count_low`。

### Provisional 机制核对

根据 `Design-docs/provisional_mechanism_check.md` 核对当前 provisional 预演设计，并补充状态机文档。

关键结论：

- `provisional` 是正式 canonical 提交前的预写/预审层，不是整本书预演，也不是每章双写。
- 运行位置应在 Writer/Review Hub 之后，canonical State Updater 之前。
- 失败的 provisional execution 会阻断后续推进并标记相关章节失败。
- Promote 到 canonical 仍只能通过 canon/state updater 路径完成。

维护风险：如果未来继续强化 band 级 provisional，需要避免把 provisional 候选直接写入 canonical 历史。

### 任务中心 / 书本页 UI

首页从任务中心单视角扩展为书本、任务、配置三个视角。

关键变化：

- 书本页展示每本书的章节规划、已生成章节、上传入口和自动化配置。
- 任务中心保留生成与上传任务混排。
- 详情统一通过右侧抽屉展示。
- 新增批量删除入口，但 active generation/upload 仍应被 API guard 拒绝删除。

维护备注：书本详情抽屉会被轮询刷新，因此新增 UI 功能时要避免刷新破坏用户已展开正文、滚动位置或关闭状态。

### 8899 部署注意事项

当前 `docker-compose.yml` 中 `forwin` 服务没有挂载源码，`uvicorn` 也不是 reload 模式。修改 `forwin/api_pages.py` 这类服务端渲染 HTML/JS 后，必须重建并重启容器才会在 `8899` 生效。

注意：

- `docker compose build forwin` 不会中断运行任务。
- `docker compose up -d forwin` 会重建/重启 `forwin` 容器，会中断进程内运行任务。
- 部署前必须查 `/api/task-center/items`，确认无 `starting/running/terminating` 任务，或先让任务安全暂停。

### 容器内 curl 缺失

`forwin` 容器内没有安装 `curl`，因此不能用 `docker exec forwin curl ...` 调试外部 LLM API。

替代方案：

- 在宿主机 `.venv` 或 shell 中发请求。
- 用容器内 Python 标准库/httpx 脚本发请求。
- 如确实需要容器内 curl，需要修改 Dockerfile 安装，但这会增大镜像并要求重建。

## 2026-04-15

### Review / API guard 修复汇总

从 `Design-docs/review_fix_log_2026-04-15.md` 汇总关键结论。

修复：

- `/api/generate` 省略 `base_url` 或 `model` 时，runtime model overrides 正确使用已保存 runtime/profile 值。
- 已有项目存在 active generation 时，禁止再次启动 generation/continue。
- 项目删除和批量删除会拒绝 active generation/upload 项目，避免删除运行中任务的数据。
- Project automation updates 支持 `publish_bindings`，双平台绑定可在项目创建后继续编辑。
- FastAPI lifespan 在测试中复用预配置 runtime objects，避免 `TestClient` 回归测试重新初始化注入状态。

影响文件：

- `forwin/api.py`
- `forwin/api_schemas.py`
- `tests/test_phase05_regressions.py`
- `tests/test_project_publish_bindings.py`
- `tests/test_project_operation_guards.py`

验证：

- `python3 -m pytest -q tests/test_project_operation_guards.py`
- `python3 -m pytest -q tests/test_project_publish_bindings.py`
- `python3 -m pytest -q tests/test_phase05_regressions.py`
- `python3 -m pytest -q`
- `npm test` in `browser_extension/forwin-publisher`

结果：

- `tests/test_phase05_regressions.py`: 110 passed
- Python test suite: 144 passed
- Browser extension tests: 26 passed
