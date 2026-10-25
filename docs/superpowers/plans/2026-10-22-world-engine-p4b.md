# 世界引擎大修 P4b：正文冲突、世界级变量、规则谓词、新行动者与收尾 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 补齐世界引擎剩下的部分：
- 正文违反叙事边界时，先退回修复；修复后仍违反，就产生 `chapter_extraction` 冲突，在章内暂停。
- 重写最新章后重新运行一次 tick。
- 世界级变量的漂移（`world.drift`）。
- 可由代码检查的规则谓词（`checkable`）。
- 幕后结算可以引出新的次要行动者。
- 删除 `task_contract_json`，清理 P2、P3 遗留的小项与成本项。
- 最后给出 P0–P4 合并执行的部署 runbook。

**Architecture:**
- 正文冲突：
  - 新增 `forwin/book_state/protections.py`，审查门与 tick 共用它检查一个 delta 是否违反 protection 与里程碑。
  - 修复循环本身就是自动的 `rewrite_chapter`，修复后仍残留的违反由 `forwin/world_engine/chapter_conflicts.py` 记为冲突。
  - `project_chapters` 在章内暂停；冲突解决后，从原草稿继续。
- 重写最新章：退役原 tick delta，world 步骤重新完整运行一次 tick。重写更早的章节时，确定性地让过期的 open 冲突失效。历史重写会拒绝“后续 delta 引用了被替换章节才有的 ref”。
- 世界级变量：新节点类型 `world_variable`，档位由 LLM 按题材写成有序列表，代码只比较整数下标；tick 中新增 `world.drift` 节点。
- 规则谓词：`forwin/book_state/rule_predicates.py` 定义 5 种结构谓词，审查门、`check_hard_rules` 和 `estimate_travel` 求值。
- 新行动者：只有幕后结算可以新建 `character` / `group`，受每次 tick 的预算限制；`commit_world_tick` 为新角色执行实体准入。
- 所有 schema 改动都追加到 `0006_planning_v6`。

**Tech Stack:** Python 3.13（`.venv`）、SQLAlchemy、Alembic、PostgreSQL（测试库 `127.0.0.1:55432`）、pydantic、pytest、Playwright。

**Spec:** `docs/designs/2026-10-20-world-engine-overhaul.md`。重点读 §1.5、§3.2 第 6–7 步、§3.3、§3.5、§5.2、§5.4、§6。P4a 计划（`docs/superpowers/plans/2026-10-22-world-engine-p4a.md`）的取舍继续有效，本计划在它全部完成后执行。研究笔记：`/private/tmp/claude-501/-Users-magi1-ForWin-source-github/79ab9c24-6cf3-5267-875a-345b2ab61341/scratchpad/p4b-research.md`（带 file:line，按条目编号）。

## 与设计的取舍（实施者照此执行）

1. **正文冲突只记残留的违反。**
   - 违反 protection 或里程碑时，先作为 `world_consistency` error 进入修复循环，带着约束作为 must_fix。这就是自动的 `rewrite_chapter`，不建冲突行。
   - 修复后仍违反，或者 canon 准备阶段才发现违反，才建冲突。
2. **残留的正文冲突从不走 auto。**
   - critical 交给用户；其余在 `world.conflict_delegate == "codex"` 时交给 Codex，否则交给用户。
   - Codex 选择 `rewrite_chapter` 时，再给一次完整草稿范围的修复；仍残留则交给用户。
   - Codex 选择 `amend_plan` 时，照 P3 规则升级给用户。
3. **正文冲突的影响等级表：**
   - 触及里程碑的引用：critical；
   - `must_be_alive`（角色）、`must_exist`：major；
   - `must_not_know` 针对 hidden / secret 事实：major；
   - `must_not_know` 针对公开事实、`must_hold_state`：moderate。
4. **章内暂停。** 该章标为 needs_review，原因 `world_conflict`，草稿保留。冲突解决后，下一次 continue 从该草稿继续：
   - `rewrite_chapter`：以约束为 must_fix 重跑修复，phase 为 `world_conflict_repair`，有单独的尝试预算。
   - `amend_plan`（只能由用户选择）：
     - 违反 protection：经 `commit_plan_constraints` 解除该 protection，再用原文重新审查，不调用 writer。
     - 违反里程碑：由 P4a 的 `PlanAmender` 修订节拍表。
5. **重写最新章时，退役它的 tick 并重新运行。**
   - 被退役 tick 的 open 冲突在新 tick 运行前标为 `expired`。
   - 不再产生只能 veto 的 `replay_cas` 冲突。
   - 重写更早的章节时，由 `expire_stale_open_conflicts` 确定性地让失效的 open 冲突过期。
6. **历史重写中消失的 ref。**
   - 被替换章节创建、而新正文没有重新创建的 ref，记为消失的 ref。
   - 读取它的 tick delta 被丢弃，产生 replay_cas 冲突，原因写明“来自被替换的第 k 章”。
   - 读取它的非 tick delta 使重写中止（`HistoricalRewriteInvalid`）。
   - 字段值不一致（state drift）只记录事件，不中止。
7. **世界级变量。**
   - 节点类型 `world_variable`，`scale` 是 2–7 个由 LLM 按题材写的有序档位，`level` 是当前下标。
   - 每个 institution / resource_economy profile 至多对应一个变量。
   - 每次漂移最多移动一档；v1 中漂移只产生事件，不产生意图。
   - `world.drift_every_story_minutes`：standard 10080，pulp 0（关闭）；`world.max_drift_variables_per_tick = 3`。
8. **规则谓词的词表只有 5 种：**
   - `actor_cannot_enter`、`requires_travel_mode`、`exclusive_relation`、`irreversible_state`、`blocked_channel`。
   - 参数只能是 BookState id、地图节点 id、lexicon id、schema 枚举 token 或整数。
   - 物品类谓词推迟，因为种子里没有物品节点。
   - hard 规则违反为 error，soft 为 warning。
   - schema 对 `checkable` 只校验形状；审查门遇到不认识的 kind 给 warning `unknown_rule_predicate`。
9. **新行动者。**
   - 只有幕后结算（`AdjudicatedOutcome.effects`）可以新建 `character` 或 `group`，用 `actor:new:<key>` 引用。
   - 预算 `world.max_new_actors_per_tick`：standard 1，pulp 0。
   - 名字不得与已有节点名、别名或 Entity 重复。
   - 带新行动者的候选影响等级至少为 moderate。
   - 被丢弃的 tick 若创建过角色，对应的 Entity 置为 inactive。
10. **`task_contract_json` 删除，不再提供编辑器。**
    - HTTP 路由、UI 编辑器、`planning/contracts.py` 一并删除（顺带删掉一个 CJK allowlist 条目）。
    - 读取方改读 goals、experience plan 与 P4a 的新列。
11. **P2 遗留项的处理：**
    - 删除：`band_task_completion` / `future_resource_preservation` / `next_band_compatibility`、`form_invariant_drift` 及其补丁、`_invariant_constraint_sections` 的死分支。
    - 改为生产：`director_setup_without_delivery`，由 thread beats 驱动。
    - 保留：`form_countdown_inconsistency`，它由 clock 节点驱动。
12. **抽取输入收窄到 POV 视图。**
    - 实体表、地点表、相关秘密清单（禁止话题背后的秘密、POV 知道的未揭示秘密、计划指令点名的事实）来自 `ChapterWorldView`。
    - `ref_check` 对清单之外的 fact_ref，改为对照 runtime 中的事实判断，不再一律报 unknown。
13. **soft protection 不实现。** protection 没有强弱字段，设计 §6.3 的“soft 约束 → auto”没有数据支撑。
14. **部署在本计划最后一个任务中执行。** 迁移 `0001 → … → 0006` 一次完成；每一步经用户确认；P2、P3 ledger 中的 runbook 裁定都并入这一步。

## Global Constraints

- 与 P4a 相同：
  - 运行代码不得含 CJK 词表，提示词放在已登记为 `prompt_text` 的 prompt_core 文件，`max_literals` 保持精确。
  - 已合入的迁移不得修改，`0006_planning_v6` 可以追加。
  - `BookStateCompiler(` 只在 admission.py 中出现，所有 canon 写入经 `CanonAdmissionService`。
  - 故事时间是整数分钟，换算用 `duration_to_minutes`；出行耗时只用 `estimate_travel`。
  - 每个非 create 补丁都带 `old_value`。
  - LLM 调用点都保存 PromptTrace 与 LLM DecisionEvents。
  - 锁顺序：冲突行在前，项目行 `FOR NO KEY UPDATE` 在后，章节计划行最后。
  - 测试不调用真实 LLM，不连生产库，不访问网络。
- 正文冲突的修复文本、冲突摘要、UI 文案都不得出现事实的 proposition，只用观察者与话题实体的名字。
- 分支：`claude/world-engine-p4`，P4a 完成之后继续。提交身份与 trailer 同 P4a。
- 生产操作只在 Task 14 中进行，每一步经用户确认。

## Review Focus

1. **正文让受保护的角色死亡：**
   - 修复循环先尝试改写；改好了不留冲突行。
   - 改不好时，只建一条 open 冲突（同一章同一约束不重复），该章在原草稿上暂停为 needs_review。
   - 用户选 `rewrite_chapter` 后，continue 从原草稿重跑修复。
   - 由 Task 5 的 `test_residual_violation_creates_one_conflict_and_pauses_mid_chapter` 与 Task 6 的 `test_rewrite_chapter_resumes_repair_from_saved_draft` 覆盖。
2. **用户在冲突未解决时点“接受 review”**：返回 409，提示先处理冲突；不得悄悄回到 needs_review。由 Task 6 的 `test_accept_review_refused_while_chapter_conflict_open` 覆盖。
3. **重写最新章：**
   - 原 tick 被退役，它的 open 冲突变为 expired。
   - 新 tick 只运行一次，不出现 `replay_cas` 冲突。
   - expired 冲突不阻塞生成。
   - 由 Task 4 的 `test_rewriting_latest_chapter_retires_its_tick_and_reticks` 覆盖。
4. **LLM 写出的 `requires_travel_mode` 让主角家乡不可达**：world_seed 校验给出 warning，Genesis 审阅时能看到解析后的谓词；不会悄悄让所有移动报错。由 Task 8 的 `test_seed_predicate_isolating_major_actor_warns` 覆盖。
5. **新行动者重名**：构建报错 `new_actor_name_taken`，该候选带着反馈重试一次，仍重名则丢弃，tick 照常提交其余变化。由 Task 10 的 `test_new_actor_name_collision_retries_then_drops` 覆盖。

---

### Task 0: 冲突记账的前置字段

**Files:**
- Modify:
  - `forwin/migrations/versions/0006_planning_v6.py`：追加 `world_plan_conflicts.origin_key`（String，NOT NULL，默认 ''）与索引 `ix_world_plan_conflicts_project_origin (project_id, origin_key)`
  - `forwin/models/world_conflict.py`
  - `forwin/world_engine/{conflict_types,conflict_repository,tick_conflicts,intents}.py`
  - `forwin/audit/events.py`：`WORLD_CONFLICT_EXPIRED`
  - `forwin/http/adapters/api_world_conflict_routes.py`：status 过滤接受 `expired`
  - `forwin/mcp/{http,client}.py`
  - `forwin/ui_assets/home/app_task_control.js`：过滤
  - `forwin/api_schema/world_conflicts.py`：`origin_key`
- Test: `tests/test_world_conflict_repository.py`、`tests/test_world_conflict_api.py`

**Interfaces:**
- Produces：
  - `ConflictDraft.origin_key: str = ""`。
  - `WorldConflictRepository` 新增：
    - `.expire(row, *, reason: str) -> None`：只对 open 行生效，`decision_json` 记录 reason。
    - `.open_by_origin(project_id, origin_keys: Sequence[str]) -> list[Row]`
    - `.open_for_origin(project_id, origin_key) -> Row | None`
    - `.pending_chapter_resumes(project_id, *, chapter_number) -> list[Row]`：已 resolved 的 `chapter_extraction` 行，`blocks_from_chapter == N`，`decision.option ∈ {rewrite_chapter, amend_plan}`，`result_refs` 中没有 `candidate:`。
  - tick 冲突与 onstage 顺延冲突的 `origin_key = tick_id`。
  - expired 行不出现在 `open_blocking`，也不计入阻塞原因。

- [ ] **Step 1: 写失败测试**
  - `test_expire_only_open_rows_and_hides_from_blocking`
  - `test_tick_and_slip_drafts_carry_tick_origin`
  - `test_list_route_accepts_expired`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_conflict_repository.py tests/test_world_conflict_api.py tests/test_world_conflict_run_control.py tests/test_planning_v6_schema.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): track conflict origins and let conflicts expire`

---

### Task 1: 世界引擎的小修与成本（第一批）

**Files:** `forwin/world_engine/{tick,tick_nodes,conflict_resolution,conflict_repository,actor_turns,superseded_resolutions,adjudicator}.py`、`forwin/writer/prompt_core/world_engine.py`；Test: `tests/test_world_tick_service.py`、`tests/test_world_conflict_resolution.py`、`tests/test_world_adjudicator.py`

**Interfaces:**
- Produces（研究笔记中的编号）：
  - **W8 + C8**：`unprotected_news(runtime, news) -> PropagatedNews`。
    - protection 禁止送达的条目改为重新入队，`arrive_at` 改为下一次 tick 的 now，不再丢弃。
    - tick 与直接解决路径共用这个函数。
  - **W10**：veto 不为已死的行动者安排下一回合。
  - **W11**：
    - 被 supersede 的解决结果，在 `decision_json["superseded_by_delta"]` 中记录 delta id。
    - `reroll_count` 与 `recent_vetoes` 跳过这些行。
  - **W13**：裁决 prompt 中，事件事实的上限改为按意图分配：每个意图 `MAX_ADJUDICATION_EVENT_FACTS_PER_INTENT = 5`，总量仍不超过 20。
  - **W15 + C10**：
    - 每个 LLM 阶段（裁决、回合、Codex）开始前，先做一次快照检查；快照已变时，提前以 `CanonStaleVersion` 结束。
    - 只产生冲突、不写补丁的 tick 也做 P27 检查（不加锁地读快照 id）。
  - **K1**：一批意图超出提示上限时，先贪心地装满上限内的部分；单个就超限的意图，`scheduled_at` 推迟到下一次 tick，记 DROPPED，reason 为 `prompt_too_large_deferred`。
  - **K5**：第 1 次尝试得到的部分结果，在第 2 次尝试的尺寸检查失败时保留下来。

- [ ] **Step 1: 写失败测试**：每个编号至少一个测试，例如 `test_forbidden_delivery_is_requeued_not_dropped`、`test_superseded_veto_does_not_cap_rerolls`、`test_oversized_intent_is_deferred_not_repeated_every_tick`、`test_stale_snapshot_stops_before_llm_phase`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_tick_service.py tests/test_world_conflict_resolution.py tests/test_world_adjudicator.py tests/test_world_actor_turns.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`fix(world_engine): requeue protected news, defer oversized intents and stop stale ticks early`

---

### Task 2: 成本与 canon 的小修（第二批）

**Files:** `forwin/world_engine/{delta_builder,running_state,tick,conflict_resolution}.py`、`forwin/book_state/{reviewer,repository}.py`、`forwin/book_state/extraction/{world_review,translator,chapter_input}.py`、`forwin/models/book_state.py`、`0006_planning_v6.py`、`forwin/narrative_obligations/repository.py`、`forwin/book_state/extraction/chapter_input.py`；Test: 对应测试文件

**Interfaces:**
- Produces（研究笔记中的编号）：
  - **W7、K2、K4/M5**：
    - `WorldTickDeltaBuilder.build`、`BookStateReviewGate.review`、`ChapterDeltaTranslator.translate`、`build_chapter_extraction_input` 都新增可选参数 `runtime=`。
    - `RunningStateCheck` 复用自己的 `base`。
    - `review_chapter_extraction` 只加载一次 as-of N−1 的 runtime。
    - 测试用加载计数器验证：20 个结果的 tick 中，加载次数比原来减少一半以上。
  - **W16**：
    - 快照排序不再依赖事务开始时间。`world_snapshots` 增加 `seq` 列（数据库序列），排序改为 `as_of_chapter desc, seq desc`。
    - 原来在 `historical_rewrite.py` 中的规避逻辑删除。
  - **B1（P4a 审计；P4a R24 M11 原定在此）**：`plan.band` 的 payload 加字符上限 `planning.band_payload_budget_chars`（默认 60000）。地点只取本 band 的 subworld 加主角邻近；事实只取 offered 行动者相关的；意图只取 offered 行动者的。`OfferedRefs` 同步收窄，保证校验器不会拒绝 prompt 没有提供的 ref。测试：大地图夹具的 payload 不超过上限，校验与 prompt 一致。
  - **C11**：
    - 义务仓储的 `mark_obligation_planned`、`mark_obligation_resolved`、`expire_obligation` 遇到已关闭的状态时拒绝。
    - `chapter_input` 只提供状态为 active 的 `obligations_to_resolve`。
  - **K6**：
    - `_reachable_places` 改为每个行动者一次单源最短路。
    - 传播改为每个事件一次单源最短路。

- [ ] **Step 1: 写失败测试**：`test_tick_reuses_runtime_for_builds_and_gate`（计数器）、`test_snapshot_order_is_monotonic_within_one_transaction`、`test_closed_obligation_cannot_be_reopened`、`test_single_source_reachability_matches_pairwise`
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_world_tick_service.py tests/test_world_gate_v6.py tests/test_chapter_delta_translator.py tests/test_narrative_obligation_ledger.py tests/test_historical_rewrite_world_ticks.py tests/test_world_actor_turns.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`perf(book_state): reuse one runtime per tick and review pass`

---

### Task 3: 正文违反的检测（C1a）

**Files:**
- Create: `forwin/book_state/protections.py`
- Modify:
  - `forwin/book_state/reviewer.py`：`BookStateReviewIssue.detail`、`milestone_refs` 参数、`_protection_issues`
  - `forwin/world_engine/constraints.py`：`check_narrative_boundaries` 改读 `active_protections`
  - `forwin/book_state/extraction/world_review.py`
  - `forwin/review/world_consistency.py`
  - `forwin/canon/preparation.py`
  - `forwin/writer/prompt_core/world_repair.py`：protection 修复文本
  - `forwin/planning/feasibility.py`：P4a-R10 的 `protection_conflict` 若尚未覆盖 `intended_changes`，在此补齐
  - `tests/overfit_allowlist.toml`
- Test: `tests/test_world_gate_v6.py`、`tests/test_world_consistency_review.py`、`tests/test_book_state_protections.py`（新建）

**Interfaces:**
- Produces：
  - `ActiveProtection{node_id, kind, ref, until_chapter, source, observer_ref, fact_ref, field, value}` 与 `active_protections(runtime, *, chapter) -> list[ActiveProtection]`。
  - `DeltaBoundaryViolation{protection_node_id, milestone_ref, kind, ref, observer_ref, fact_ref, field, value, until_chapter, source, impact_level, evidence}`。
  - `delta_boundary_violations(runtime, delta, *, chapter, milestone_refs=frozenset()) -> list[DeltaBoundaryViolation]`，判定规则：
    - 状态类：把 delta 的非 create 补丁叠加到当前 state 上，与叠加前比较。
    - `must_not_know`：看 delta 的 `metadata.knowledge_tags`，状态为 knows 或 believes，闪回也算。
    - 里程碑：对 `milestone_refs` 中的引用做终结性改变（角色死亡、`status` 进入 `ENDED_STATUSES`）时，为 critical。
    - 影响等级按取舍第 3 条。
  - 审查门对 `chapter_extraction` delta 产生 error：
    - code 为 `protection_violated` 或 `milestone_ref_ended`；
    - `target_ref` 为 `review_constraint:<id>` 或 `node:<ref>`；
    - `detail` 为该 violation。
  - world_engine delta 不受影响。
  - world_consistency 把 `detail` 写入 `original_result["boundary"]`，修复文本来自 `world_repair.protection_repair_fix(kind, names)`，不含 proposition。
  - `review_chapter_extraction`、`WorldConsistencyReviewer`、`BookStateCanonPreparer` 用 P4a 的 `milestone_refs_for_chapter` 计算 `milestone_refs` 并传给审查门。

- [ ] **Step 1: 写失败测试**
  - `test_delta_violations_cover_four_kinds_and_milestone`
  - `test_gate_reports_protection_violation_on_chapter_delta_only`
  - `test_repair_fix_text_has_no_fact_proposition`
  - `test_tick_boundary_check_reads_shared_protections`
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_book_state_protections.py tests/test_world_gate_v6.py tests/test_world_consistency_review.py tests/test_world_constraints.py tests/test_plan_feasibility.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(book_state): detect protection and milestone violations in chapter text`

---

### Task 4: 重写后的 tick 退役与消失的 ref（C9 + W6）

**Files:** `forwin/canon/historical_rewrite.py`、`forwin/canon/admission.py`（`result_payload["historical_rewrite"]["retired_tick_ids"]`）、`forwin/world_engine/{tick,conflict_revalidation,superseded_resolutions}.py`（`conflict_revalidation.py` 新建）、`forwin/audit/events.py`（`HISTORICAL_REWRITE_STATE_DRIFT`）；Test: `tests/test_historical_rewrite_world_ticks.py`、`tests/test_world_tick_service.py`

**Interfaces:**
- Consumes：Task 0 的 `expire`、`open_by_origin`、`origin_key`。
- Produces：
  - `HistoricalCanonReplacement.vanished_refs: dict[str, VanishedRef(ref, origin: "retired_contribution" | "dropped_tick", delta_id)]`，取代 `dropped_tick_refs`。
    - 在 `rebuild_successor_projections` 开始时写入：被替换的章节 delta 创建、而新贡献没有重新创建的 ref。
    - `_vanished_dependency(delta) -> VanishedRef | None` 取代 `_dropped_tick_dependency`，处理规则按取舍第 6 条。
  - 重写最新章时，`tick-*` delta 不再重放，改为 `_retire_for_retick`：
    - retire 并 supersede 它们，把它们创建的 ref 记入 `vanished_refs`；
    - 不建 `replay_cas` 冲突；
    - 把 tick id 写进 `retired_tick_ids`。
  - `WorldTickService.run` 在构建 tick 之前读取 commit 的 `retired_tick_ids`：
    - 把这些 tick 的 open 冲突逐个加锁并 expire，写 `WORLD_CONFLICT_EXPIRED`；
    - 然后正常完整运行 tick。原来跳过的理由 `chapter_already_ticked` 不再适用。
  - `expire_stale_open_conflicts(session, *, project_id, from_chapter, policy, reason) -> list[str]`：
    - 在 world 步骤因 `chapter_not_latest` 跳过之前调用。
    - 对 `detected_after_chapter >= k` 的 open `world_tick` 冲突，依次做三项检查：重建所持变化、做 P22 与 P30 的检查、确认 `involved_refs` 仍存在。任何一项失败就 expire。
    - 不调用 LLM。

- [ ] **Step 1: 写失败测试**
  - `test_rewriting_latest_chapter_retires_its_tick_and_reticks`
  - `test_retained_tick_reading_vanished_event_fact_is_dropped`
  - `test_later_chapter_using_vanished_entity_aborts_rewrite`
  - `test_recreated_stable_id_does_not_vanish`
  - `test_state_drift_records_event_without_abort`
  - `test_older_rewrite_expires_stale_open_conflicts_only`
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_historical_rewrite_world_ticks.py tests/test_world_tick_service.py tests/test_world_conflict_run_control.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(canon): re-tick a rewritten latest chapter and refuse vanished references`

---

### Task 5: 正文冲突的记录、路由与章内暂停（C1b）

**Files:**
- Create: `forwin/world_engine/chapter_conflicts.py`
- Modify:
  - `forwin/world_engine/conflict_routing.py`：`auto_option(draft)` 与 chapter_extraction 分支
  - `forwin/writer/prompt_core/world_engine.py`：`CHAPTER_CONFLICT_REVIEW_INSTRUCTIONS`
  - `forwin/world_engine/conflict_delegate.py`：按来源选择指令
  - `forwin/generation/pipeline_core/project_chapters.py`：review 之后的记录与暂停；canon 准备被阻断时的记录
  - `forwin/canon/types.py` 与 `forwin/canon/preparation.py`：`CanonPreparationOutcome.book_state_review`
  - `tests/overfit_allowlist.toml`
- Test: `tests/test_chapter_extraction_conflicts.py`（新建）、`tests/test_world_conflict_routing.py`、`tests/test_world_conflict_delegate.py`

**Interfaces:**
- Consumes：Task 0 与 Task 3。
- Produces：
  - 常量：`CHAPTER_CONFLICT_OPTIONS = ("rewrite_chapter", "amend_plan")`、`WORLD_CONFLICT_REPAIR_PHASE = "world_conflict_repair"`。
  - 转换函数：
    - `violations_from_verdict(verdict) -> list[DeltaBoundaryViolation]`
    - `violations_from_gate(gate_verdict) -> list[DeltaBoundaryViolation]`
    - `chapter_conflict_draft(v, *, project_id, chapter_number, story_time) -> ConflictDraft`：
      - `source="chapter_extraction"`，`violated` 为 `protection` 或 `milestone`，`blocks_from_chapter=N`，`detected_after_chapter=N-1`；
      - `origin_key` 为 `chapter_extraction:{N}:{protection_node_id}`，里程碑时为 `chapter_extraction:{N}:milestone:{ref}`；
      - `proposed_change={"kind": "chapter_boundary", "chapter": N, "boundary": ..., "candidate_id": ...}`；
      - summary 只用名字。
  - `ChapterConflictRecorder(session, *, policy, delegate, audit_writer_factory).record(*, project_id, chapter_number, candidate_id, violations) -> ChapterConflictOutcome(conflict_ids, blocking_conflict_id, codex_rewrite_ids)`。逻辑：
    - 同一 `origin_key` 已有 open 行就复用它，否则新建，并写 DETECTED。
    - 按取舍第 2 条路由。同一个 origin_key 只让 Codex 决定一次。
    - 本章已经不再违反的 open 行，按 `{option: rewrite_chapter, decided_by: auto}` 解决，`result_refs` 为 `["candidate:<id>"]`。
  - 路由矩阵：
    - `auto_option(draft)`：`world_tick` 返回 `veto_world`，`chapter_extraction` 返回 `rewrite_chapter`，其余返回 None。
    - `available_options` 与 `route_conflict` 增加 chapter_extraction 分支，残留的正文冲突从不走 auto。
  - `project_chapters` 在 `review_candidate` 之后、hard floor 之前调用 `record`：
    - `codex_rewrite_ids` 不为空：调用 `repair_world_conflict` 一次（来自 Task 6；本任务先桩住），然后再记录一次。
    - `blocking_conflict_id` 不为空：该章标为 needs_review，提交，`pause_reason="world_conflict"`，`paused_for_review` 的 payload 带上 reason 与 conflict_id，然后跳出循环。草稿保留。
  - canon 准备被审查门阻断时，用 `violations_from_gate` 同样记录；产生阻塞冲突时，暂停原因为 world_conflict。

- [ ] **Step 1: 写失败测试**
  - `test_violation_fixed_in_repair_loop_stores_no_row`
  - `test_residual_violation_creates_one_conflict_and_pauses_mid_chapter`：跑两次，仍然只有一行；草稿还在；`pause_reason == "world_conflict"`。
  - `test_routing_matrix_for_chapter_conflicts`：参数化 impact × delegate。
  - `test_codex_rewrite_gets_one_more_pass_then_human`
  - `test_canon_prep_violation_creates_conflict`
  - `test_chapter_conflict_summary_has_no_proposition`
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_chapter_extraction_conflicts.py tests/test_world_conflict_routing.py tests/test_world_conflict_delegate.py tests/test_world_conflict_run_control.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): turn residual chapter boundary violations into conflicts`

---

### Task 6: 正文冲突的解决与续写（C1c，含控制台卡片）

**Files:**
- Modify:
  - `forwin/world_engine/conflict_resolution.py`：`_resolve_chapter_conflict`；删除 `_check_decision` 中对 `rewrite_chapter` 的无条件拒绝
  - `forwin/review/repair/service.py`：`_review_from_world_conflicts`、`repair_world_conflict`
  - `forwin/generation/pipeline_core/project_chapters.py`：写作前的续写分支
  - `forwin/generation/pipeline_core/acceptance.py`：冲突未解决时拒绝接受
  - `forwin/http/adapters/api_world_conflict_routes.py`：`ChapterNotAwaitingConflict` 映射为 409；有活跃生成任务时拒绝
  - `forwin/planning/checkpoints.py` 与 api_schema：`BlockingReasonInfo` 增加 `conflict_id`、`conflict_source`
  - `forwin/mcp/{http,client}.py`：docstring；补上 resolve 缺少的 dict 守卫
  - `forwin/ui_assets/home/{app_task_progress,app_task_control}.js`
  - `tests/browser/fixtures.py`
  - `docs/codex-forwin-mcp.md` 与 `plugins/forwin-operator/skills/forwin-operator/SKILL.md`：新来源与新选项
- Test: `tests/test_chapter_extraction_conflicts.py`、`tests/test_world_conflict_api.py`、`tests/test_world_conflict_resolution.py`、`tests/browser/test_world_conflicts_drawer.py`、`tests/browser/test_task_center_drawer.py`

**Interfaces:**
- Consumes：Task 5。
- Produces：
  - 解决 `chapter_extraction` 冲突时，加锁顺序为：冲突行、项目行、第 N 章计划行。只有该计划处于 needs_review、并且没有活跃的生成任务时才继续，否则返回 409。
    - `rewrite_chapter`：不写 canon；计划状态 needs_review → planned；`mark_resolved(result_refs=[])`；写 RESOLVED。
    - `amend_plan`（只能由用户选择）：
      - 违反 protection：`commit_plan_constraints(..., beat_sheet_id=f"conflict:{row.id}", creates=[], releases=[node_id])`。把这个参数改名为 `constraint_set_id`，并保留旧名作为关键字别名。
      - 违反里程碑：调用 `PlanAmender`。
      - 然后计划状态改为 planned，写 RESOLVED。
  - 续写：章节写作前，若有 `pending_chapter_resumes`，就读取最新草稿：
    - 有 `rewrite_chapter`：调用 `repair_world_conflict`，结果是一份合成的不通过 review，约束为 must_fix，phase 为 `world_conflict_repair`。
    - 否则：对同一份正文重新运行 `review_candidate`。
    - 之后 `attach_result_refs(row, [f"candidate:{id}"])`，接着走正常的 review / canon 流程。
    - P4a 的 `replan_remaining` 跳过有待续写的章节。
  - `AcceptanceStage.accept_review` 在有阻塞第 N 章的 open 正文冲突时，抛 `ValueError`，HTTP 返回 409。
  - UI：
    - 存在正文冲突时，不再清空 review 章节列表，文案区分来源。
    - 冲突卡片显示来源、保护类型、引用名、`until_chapter`、证据引文，以及所持变化（U1，按公开 / 秘密分开渲染）。
    - 选项标签：`rewrite_chapter` 为“退回修复正文”，`amend_plan` 为“接受正文并解除保护”。

- [ ] **Step 1: 写失败测试**
  - `test_rewrite_chapter_resumes_repair_from_saved_draft`：约束出现在 must_fix 中，并且没有新的 writer 调用。
  - `test_amend_plan_releases_protection_and_rereviews_same_text`：一个 `plan_constraint` delta（幂等），之后该章通过。
  - `test_accept_review_refused_while_chapter_conflict_open`
  - `test_resolve_refused_while_generation_active`
  - `test_replan_skips_chapter_with_pending_resume`
  - 浏览器测试 `test_drawer_shows_chapter_conflict_with_review_chapters`
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_chapter_extraction_conflicts.py tests/test_world_conflict_api.py tests/test_world_conflict_resolution.py tests/browser/test_world_conflicts_drawer.py tests/browser/test_task_center_drawer.py tests/test_mcp_server.py tests/test_forwin_operator_plugin.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(generation): resolve chapter conflicts by rewriting or amending the plan`

---

### Task 7: 删除 `task_contract_json`

**Files:** 研究笔记第 7 节的整张表（约 22 个运行文件、13 个测试、UI、迁移），要点：
- Delete：`forwin/planning/contracts.py`；`tests/overfit_allowlist.toml` 中它的条目
- 删除 HTTP 的 chapter / band task-contract 路由、`PLAN_TASK_CONTRACT_UPDATED` 事件、UI 编辑器（`app_task_control.js:454-525` 及按钮）
- `create_chapter_plan` 与 `save_band_experience_plan` 去掉 task_contract 参数和派生
- `band_plan_patcher` 只维护 `schedule.band_obligation_contract`
- 修复补丁与修复快照改用 P4a 新列
- FPA 的“计划是否提到 X”收敛为一个 `fpa/helpers.plan_text(plan)`
- 抽取的 `obligations_to_resolve` 只读 `experience_plan_json`
- context pack 中从未渲染的 contract 字段删除
- `0006_planning_v6.py` 追加删除 `chapter_plans.task_contract_json` 与 `band_experience_plans.task_contract_json`
- 边界测试：`planning/contracts.py` 必须不存在

**Interfaces:**
- Produces：`rg -n "task_contract|PlanTaskItem|derive_chapter_task_contract|derive_band_task_contract" forwin tests` 只剩迁移中的命中。

- [ ] **Step 1: 写失败测试**：边界测试 `test_task_contract_storage_and_routes_are_gone`
- [ ] **Step 2–4: 运行，确认失败；实现并迁移研究笔记中列出的测试；运行通过**：`.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_band_plan_obligation_patch.py tests/test_future_plan_auditor.py tests/test_chapter_delta_contract.py tests/test_plan_fulfillment_review.py tests/browser/test_task_center_drawer.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`refactor: delete chapter and band task contracts`

---

### Task 8: 规则谓词（W4）与组织、地点的状态枚举（W12）

**Files:**
- Create: `forwin/book_state/rule_predicates.py`、`forwin/writer/prompt_core/rule_predicates.py`（谓词的中文说明表，prompt_text）
- Modify:
  - `forwin/book_state/reviewer.py`：`_rule_issues`
  - `forwin/world_engine/travel.py`：`requires_travel_mode`、`TravelEstimate.blocked_by_rule`
  - `forwin/world_engine/constraints.py`：新意图地点的 `actor_cannot_enter`
  - `forwin/genesis/{world_seed,messages,constants}.py`：kind 限制与校验
  - `forwin/book_state/schema.py`：faction / organization / group / site_state 的 status 枚举，包含 `ENDED_STATUSES`
  - `forwin/writer/prompt_core/world_repair.py`
  - `tests/overfit_allowlist.toml`：如需要，在 `story_id_literal.allowed_values` 加入 `"fact_ref"`
  - 三份 world_seed 夹具：每份加一条 checkable 规则
- Test: `tests/test_rule_predicates.py`（新建）、`tests/test_world_gate_v6.py`、`tests/test_genesis_world_seed.py`、`tests/test_world_constraints.py`

**Interfaces:**
- Produces：
  - 取舍第 8 条中的 5 种 kind 及参数。
  - `RULE_PREDICATE_KINDS`、`RulePredicate{rule_id, hard, kind, params}`、`RuleRefScope{locations, actors, facts, modes}`。
  - `parse_checkable(payload, *, scope) -> (RulePredicate | None, issues)`、`active_rule_predicates(runtime)`。
  - `RuleSubjectChanges`、`changes_from_delta(runtime, delta)`、`changes_from_new_intents(runtime, candidate)`。
  - `RuleViolation`、`evaluate_rules(runtime, predicates, changes)`、`blocked_edge_types(runtime, predicates, *, mover_id)`。
  - 审查门：`rule_violated`，hard 为 error、soft 为 warning；不认识的 kind 为 warning `unknown_rule_predicate`。
  - `estimate_travel`：被规则阻断时，返回 `reachable=False` 与 `blocked_by_rule`；审查门据此报 `movement_rule_blocked`。
  - world_seed 校验：
    - 用 `parse_checkable` 解析，出错为 `invalid_checkable`。
    - 谓词使某个 major 行动者的位置与主角互不可达时，给 warning。
  - Genesis world_seed 审阅界面（`app_genesis.js`）显示解析后的谓词。

- [ ] **Step 1: 写失败测试**
  - `test_parse_each_kind_valid_and_invalid`
  - `test_gate_flags_rule_violation_hard_and_soft`
  - `test_requires_travel_mode_blocks_or_reroutes`
  - `test_new_intent_into_forbidden_location_retries`
  - `test_seed_predicate_isolating_major_actor_warns`
  - `test_group_status_enum_rejects_invented_status`
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_rule_predicates.py tests/test_world_gate_v6.py tests/test_genesis_world_seed.py tests/test_world_constraints.py tests/test_world_tick_multi_genre.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(book_state): evaluate structural rule predicates`

---

### Task 9: 世界级变量与 `world.drift`（W3）

**Files:**
- Create: `forwin/world_engine/drift.py`
- Modify:
  - `forwin/book_state/schema.py`：`world_variable` 节点类型及其枚举
  - `forwin/genesis/{world_seed,messages,constants,helpers}.py`
  - `forwin/genesis/handoff/world_seed_committer.py`
  - `forwin/runtime/policy.py`：两个 world 旋钮；`world` 段的 profile 默认值按缺失字段合并
  - `forwin/world_engine/{tick,tick_nodes,types,constraints}.py`
  - `forwin/writer/prompt_core/world_engine.py`：`build_drift_messages`
  - `forwin/writer/llm/routing.py`：`world.drift`
  - `forwin/planning/world_digest.py` 与 `forwin/writer/prompt_core/planning.py`：第 9 个 section `world_conditions`
  - `forwin/runtime/container.py`
  - 三份 world_seed 夹具
  - `tests/overfit_allowlist.toml`
- Test: `tests/test_world_drift.py`（新建）、`tests/test_genesis_world_seed.py`、`tests/test_runtime_policy.py`、`tests/test_world_digest.py`、`tests/test_world_tick_service.py`

**Interfaces:**
- Produces：
  - `world_variable` 节点：
    - profile 字段：`label`、`source_kind`、`source_profile_id`、`scale`、`scope_refs`、`holder_refs`、`visibility`、`drift_tendency`、`summary`
    - state 字段：`level`、`trend`、`last_drift_at`、`status`
  - 规则：`0 <= level < len(scale)`，`scale` 有 2–7 个不重复的档位。
  - `WorldPolicy.drift_every_story_minutes`（standard 10080，pulp 0）与 `max_drift_variables_per_tick = 3`。
  - `due_world_variables(runtime, *, now, every, limit)`。
  - `WorldDriftRunner(llm_client).drift(...) -> DriftCall`：`stage_key="world.drift"`，PromptTrace 与 DecisionEvents 照常保存。
  - `drift_candidate(variable, change, *, now) -> TickCandidate`，`origin="drift"`。
  - 硬规则：
    - 每次最多移动一档（`drift_step_too_large`）；
    - 档位越界报 `drift_level_out_of_range`；
    - 事件参与者必须在 holder 或 scope 之内（`drift_participant_out_of_scope`）。
  - 影响等级：档位变化为 moderate，holder 易主为 major。
  - tick 的 `_collect` 在行动者回合之后加入漂移候选。LLM 不可用时，`last_drift_at` 不变，下次再试。

- [ ] **Step 1: 写失败测试**
  - `test_seed_world_variables_validate_and_commit`
  - `test_due_selection_and_disabled_at_zero`
  - `test_drift_step_too_large_retries_then_drops`
  - `test_llm_outage_keeps_last_drift_at`
  - `test_drift_candidate_commits_in_tick`
  - `test_digest_lists_world_conditions`
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_world_drift.py tests/test_genesis_world_seed.py tests/test_runtime_policy.py tests/test_world_digest.py tests/test_world_tick_service.py tests/test_world_tick_multi_genre.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): drift world variables on a story-time cadence`

---

### Task 10: 幕后结算引出新行动者（W5）

**Files:** `forwin/world_engine/{types,effect_validation,delta_builder,constraints,tick}.py`、`forwin/writer/prompt_core/world_engine.py`、`forwin/canon/{admission,entity_admission,historical_rewrite}.py`、`forwin/runtime/policy.py`、`tests/overfit_allowlist.toml`；Test: `tests/test_world_tick_delta.py`、`tests/test_world_tick_service.py`、`tests/test_historical_rewrite_world_ticks.py`、`tests/test_world_engine_contracts.py`

**Interfaces:**
- Produces：
  - `NEW_ACTOR_REF_PREFIX = "actor:new:"`。
  - `EffectNewActor{key, node_type: "character" | "group", name, role_hint, location_id, affiliation_refs, summary}`，`WorldEffects.new_actors`。
  - `actor:new:<key>` 引用必须能在同一个 effects 内解析。
  - 只有 `AdjudicatedOutcome.effects` 可以带 `new_actors`；行动者回合的意图带了就被拒绝。
  - builder：
    - 新节点 id 由 `_tick_local_id` 生成，重放时稳定。
    - 新建的 character 为 `actor_tier="minor"`、`alive_status="alive"`、`next_turn_at=now`。
    - 为 `affiliation_refs` 建 `member_of` 边。
    - 名字重复时报错 `new_actor_name_taken`。
  - 预算 `WorldPolicy.max_new_actors_per_tick`（standard 1，pulp 0），在 `_admitted` 中累计；裁决 prompt 中写明剩余预算。
  - `commit_world_tick` 为新建的 character 执行 `EntityAdmissionCommitter.apply`，计划的 `candidate_fingerprint=f"world_tick:{tick_id}"`。人格由 compiler 自动分配。
  - 被丢弃或被退役的 tick（Task 4）所创建的角色，由 `EntityAdmissionCommitter.retire(project_id, entity_ids)` 置为 inactive。

- [ ] **Step 1: 写失败测试**
  - `test_adjudicated_outcome_creates_minor_actor_with_entity_and_personality`
  - `test_actor_turn_cannot_create_actor`
  - `test_new_actor_budget_drops_second`
  - `test_new_actor_name_collision_retries_then_drops`
  - `test_dropped_tick_retires_created_entity`
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_world_tick_delta.py tests/test_world_tick_service.py tests/test_historical_rewrite_world_ticks.py tests/test_world_engine_contracts.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): let offstage outcomes introduce minor actors`

---

### Task 11: P2 遗留项与抽取输入收窄

**Files:** 研究笔记第 8 节的表。要点：
- 删除失效规则名、`form_invariant_drift` 及 ledger drift 补丁、`_invariant_constraint_sections` 的死分支；issue groups 加入 P4a 可行性审计的 code。
- translator 记录 `metadata["thread_beats"]`；band checkpoint 读取它，生产 `director_setup_without_delivery`。
- `book_state/extraction/chapter_input.py` 按取舍第 12 条从 `ChapterWorldView` 构建输入；`ref_check.py` 对清单之外的 fact_ref 对照 runtime 判断。
- Test: 研究笔记中列出的测试，外加 `tests/test_chapter_delta_contract.py`、`tests/test_chapter_delta_ref_check.py`

**Interfaces:**
- Produces：
  - 抽取输入的实体表与地点表来自本章视图。
  - 秘密清单为三类事实的并集：禁止话题背后的秘密、POV 知道的未揭示秘密、计划指令点名的事实。
  - 不在清单中、但在 runtime 中存在的 fact_ref 不报 `unknown_ref`。

- [ ] **Step 1: 写失败测试**
  - `test_extraction_input_uses_world_view_tables`
  - `test_ref_check_accepts_runtime_fact_outside_offered_list`
  - `test_setup_beats_without_delivery_flag_band`
  - 边界测试：删除的规则名与模块不存在
- [ ] **Step 2–4: 运行，确认失败；实现；运行通过**：`.venv/bin/python -m pytest -q tests/test_chapter_delta_contract.py tests/test_chapter_delta_ref_check.py tests/test_chapter_delta_extractor.py tests/test_architecture_boundaries.py tests/test_gate_outcome_producers.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`refactor: retire dead review signals and narrow extraction to the chapter view`

---

### Task 12: 多题材端到端（P4b 部分）

**Files:** `tests/fixtures/world_seed/{xianxia,interstellar,urban_mystery}.json`、`tests/test_world_engine_p4b_multi_genre.py`（新建）

- [ ] **Step 1: 写测试（参数化三个夹具）**
  - `test_chapter_conflict_rewrite_resume_in_three_genres`
  - `test_checkable_rule_blocks_move_in_three_genres`
  - `test_drift_step_in_three_genres`
  - `test_new_actor_in_three_genres`
  - `test_three_genres_share_p4b_paths`：用 spy 比较调用序列。
- [ ] **Step 2: 运行，确认通过。** 只增加测试与夹具；若失败，回到对应任务修实现。
- [ ] **Step 3: 全量测试并提交**：`test: run P4b world-engine paths in three genres`

---

### Task 13: 文档收尾

**Files:**
- 五份设计文档、`forwin/book_state/README.md`、`docs/codex-forwin-mcp.md`、operator SKILL
- `tests/test_design_status_docs.py`：增加标记 `chapter_extraction`、`rewrite_chapter`、`world_variable`、`world.drift`、`checkable`、`expired`

- [ ] **Step 1: 更新文档**，并删除“推迟到 P4”的段落中已经完成的项目。
- [ ] **Step 2: 运行文档与边界测试，然后全量测试并提交**：`docs: close world engine overhaul P4`

---

- （P4a 审计 B14）`Design-docs/CURRENT_DESIGN.md`、`CURRENT_ARCHITECTURE.md`、`DESIGN_STATUS.md` 中重复的“世界审查”段只保留一处，其余改为链接，并同步 `tests/test_design_status_docs.py` 的标记。

### Task 14: 生产部署 runbook（P0–P4 合并；仅控制者执行，每一步经用户确认）

顺序依据最终整支审查 I-2：`scripts/purge_all_projects.py` 调用 `require_v5_schema`，只能在库已升到 `0006_planning_v6` 之后运行；线上库目前在 `0001_v5_baseline`，部署副本（`/Users/magi1/ForWin-swarm`）里没有清库脚本。所以是“先迁移、再用新代码清库”。每条命令都注明在哪个 checkout、对哪个库版本执行。

1. **停写**（线上，旧代码）：`task_active_generation_check` 确认没有活跃生成任务；暂停全部任务，优先 `task_pause`；关闭自动调度；暂停 post-Canon worker。随后停止使用线上库的 ForWin 服务（infra-150 上的 API `:8899` 与 MCP `:8896`、magi1 上的 runtime），直到第 9 步以新代码启动，避免旧代码在迁移期间访问 0006 库。
2. **备份**（线上库，0001）：`pg_dump`；外部存储：MinIO 中的 artifacts、Qdrant 的 `chapter_memories` 与 `llm_kb_vectors`、`data/llm_kb`、`data/world_vaults`、`data/publisher_covers`。
3. **迁移前检查**（线上库，0001；只读 SQL，用 psql 或部署副本的连接）：
   - `SELECT project_id, chapter_number, count(*) FROM chapter_plans GROUP BY 1,2 HAVING count(*) > 1`。有结果时，0006 的 `uq_chapter_plans_project_chapter` 会失败，需先在用户确认下删除重复行（这些项目随后都会被清掉）。
   - 列出存储中名字或 base_url 含 codex-spark、但 model 不在 `FORWIN_CODEX_MODELS` 中的 model profile（P3 裁定 P3）。
   - 运行 `docs/operations/world-engine-p4-deploy-preflight.md` 中的退化卷范围 SQL（P4a R34）。
   - 只读查看部署同步脚本 `deploy-github-sync.sh` 是否会自动执行 `alembic upgrade head`；无论结果如何，第 5 步都先显式迁移，同步脚本的迁移届时是空操作。
4. **合入 master 并推送**（源码 checkout `/Users/magi1/ForWin-source-github`，在 magi1 上）：把 `claude/world-engine-p4` 合入 master，推送到 GitHub `Ctwqk/ForWin`。此时服务仍处于停写状态，部署副本未变。
5. **迁移**（源码 checkout `/Users/magi1/ForWin-source-github`，合入后的 master；`DATABASE_URL` 指向线上库；库版本 0001 → 0006）：显式执行 `alembic upgrade head`，一次完成 `0001 → … → 0006_planning_v6`（0002–0006 的回填按有数据的库编写，`tests/test_planning_v6_schema.py` 覆盖了带数据的 0005 升级）。无论同步脚本是否会自动迁移，都在这里先迁移，之后同步脚本的迁移只是已在 head 的空操作。
6. **核对版本**（同一 checkout，线上库）：`alembic current` 必须显示 `0006_planning_v6`，否则停止。
7. **清库**（同一 checkout，线上库 0006）：`scripts/purge_all_projects.py` 先 dry-run，经用户确认后再执行。脚本自带的 `require_v5_schema` 检查保持不变。
8. **清库后复查**（同一 checkout，线上库 0006）：重跑第 3 步的 SQL，应全部为空；确认没有 `origin_key` 为空的 world 冲突行（Task 4 裁定）；用新代码加载存储的 runtime policy，确认可加载（`future_constraints` 已由兼容垫片丢弃）。任何一项不通过就停止，不进入部署。
9. **部署**（前提：线上库为 0006，第 8 步通过）：`ssh infra-150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`；部署后重启各服务（Qdrant 就绪缓存）。服务在这一步才以新代码启动。
10. **配置 Codex**：设置 `FORWIN_CODEX_MODELS`，用一次只读调用确认 Codex bridge 以 `gpt-6-astra` 应答，且 `actual_model` 一致。
11. **冒烟**：
    1. 用测试书走完 Genesis 到 start-writing，确认生成了节拍表（含 setting_brief）与 protection、world_variable 与 checkable 规则。
    2. 写第 1 章：确认 band 计划、POV 视图、抽取、tick、plan 步骤都正常。
    3. 用 `world-conflicts` 列表和 MCP `world_conflict_list` 读到同一份数据。
    4. 重写第 1 章：确认 tick 被退役并重新运行；第 2 章草稿若存在，确认其正文冲突过期、需要重新审查（最终审查建议）。
12. **收尾**：由用户决定何时重新打开自动调度，并进入 P5 验收（运行成本需用户批准）。部署稳定后，按 `Design-docs/DESIGN_STATUS.md` 的“部署后清理”清单提交收尾改动。

---

## P4a 审计追加（2026-10-24）

来源：`.superpowers/sdd/2026-10-24-world-engine-audit/comparison.md`（opus 与 fable 两份独立审计，控制者已逐条核实）。A 类的泄密、W9 与 `plot_responds` 已在 P4a follow-up 中修复（`docs/superpowers/plans/2026-10-24-world-engine-p4a-followup.md`）。下面是 B 类中没有归属的部分，B1 已并入 Task 2，B12 已并入 Task 14。

### Task 15: PromptTrace 独立落盘与 world 步骤拆分事务（B2、B3）

**Files:** `forwin/maintenance/post_canon.py`、`forwin/world_engine/tick.py`、`forwin/planning/band_planner/service.py`、`forwin/naming/entity_registrar.py`、`forwin/generation/pipeline_core/review_autofix.py`；Test: 对应测试文件

- world 步骤、band 规划器 `audit_error` 路径、实体准入分类器的 PromptTrace 与 LLM DecisionEvent 在独立事务里提交（照 Codex delegate 的 `own_transaction_audit_writer`）；步骤回滚或 tick 过期重试时，已花费的调用仍有 trace。实体准入分类器的 attempts 不再被下一个 trace 吸走。
- world 步骤改为多个已提交单元（照 plan 步骤）：LLM 调用阶段不在任何打开的事务里；只在提交 tick delta 时上锁。trace 与事件插入不再长时间持有项目行的 key-share 锁。
- 测试：stale 重试后 trace 数等于真实调用数；分类器调用有自己的 trace；world 步骤的 LLM 调用期间，另一连接能拿到项目行 `FOR UPDATE`。

### Task 16: canon 不变式与类型化查询（B5、B6、B13、band_plans 唯一性）

**Files:** `forwin/book_state/reviewer.py`、`forwin/book_state/compiler.py`、`forwin/canon/admission.py`、`forwin/world_engine/{actor_turns,superseded_resolutions,conflict_repository}.py`、`forwin/models/{world_conflict,planning_v6,book_state}.py`、`0006_planning_v6.py`；Test: 对应测试文件

- 审查门新增 error `missing_old_value`：对已存在目标的非 create 补丁缺少 `old_value` 时拒绝（保留 P2 R8 的合并例外与 protection 状态特例）。
- 用类型化列取代 JSON 文本匹配：`world_plan_conflicts.decision_option`（索引）、tick id 列（索引）、结果引用的关联表或数组列；删除 `actor_turns` 的 LIKE、`superseded_resolutions` 的 contains、`admission` 的 metadata contains。CAS 失败按异常类型判断，不再匹配消息文本。
- `band_plans` 增加唯一约束 `(arc_plan_id, band_index, version)`。
- 去掉 `+1µs` 时间戳规避（与 Task 2 的 W16 `seq` 列一起）；排查四条 flake（`test_accepting_a_change_to_a_settled_intent_is_stale`、`test_tick_after_chapter_one…[urban_mystery]`、`test_an_intent_too_large_alone_gives_its_slot_to_the_next_due_intent`、`test_releasing_an_open_protection_rewinds_to_open`）在并发负载下的原因并修复。

### Task 17: 分层与去重复（B4、B7）

**Files:** `forwin/book_state/**`、`forwin/world_engine/**`、`forwin/planning/**`、`forwin/map/**`、`forwin/observability/llm_trace.py`、`tests/test_bare_imports.py`；Test: 对应测试文件

- `book_state` 不再向上依赖：`READER_OBSERVER`、`chapter_plan_fingerprint`、节拍表揭示目标读取、`estimate_travel` 的类型移到叶子模块；完成 P4a R30 搁置的地图 ORM 行迁入 `forwin/models`，删除 `map/__init__` 的导入顺序修补。
- 翻译器的私有名字提升为公共的共享模块（关系规划器合并为一份，按来源参数化）；五份 LLM attempt/trace 管道抽成 `forwin/observability/llm_trace.py` 的一个帮助函数；“事实是否为秘密”的判定合并为一个函数；重复常量（`_MINUTE_EPSILON`、reader 记号、事件事实类型、overlay key 等）合并。
- 修复 5 个冷导入循环（`maintenance.events`、`maintenance.post_canon`、`maintenance.state`、`pipeline_core.world_projection`、`protocol.trope_md_loader`），`tests/test_bare_imports.py` 改为扫描全部 `forwin` 模块。
- 把以过程命名的测试文件（`test_task9_fix1_unit.py`、`test_final_fix_run_a.py`）并入主题测试；overfit 扫描器补上函数内 CJK 列表、`.startswith((…))`、拼接字面量等盲区（P4a 审计 D-14）。

### Task 18: 删除死代码与第二套 protection（B8、B9）

**Files:** `forwin/planning/band_plan/`、`forwin/planning/service.py`、`forwin/protocol/book_state.py`、`forwin/state/query_helpers.py`、`forwin/experience/service.py`、`forwin/writer/llm/routing.py`、`forwin/map/models.py`、`forwin/models/{book_state,planning_control}.py`、`forwin/application/project_control/operations.py`、read models、`forwin/api_schema/project.py`、`forwin/ui_assets/home/app_task_control.js`、`0006_planning_v6.py`；Test: 对应测试文件

- 删除：`planning/band_plan/` 包及其测试、`PlanningService`、`CognitionState`、`_active_arc_rows_for_projects`、`load_long_window_audience_trends`、`SqlBookStateQueryInterface`、`world_line` 残留（`graph_deltas.world_line_id`、`active_world_line_ids_json`、narrative `world_line` 类型）、`routing.py` 中无调用者的 family/stage、`MapEdge.access_rule_id`。部署后的兼容垫片（`policy.py` 的 map_movement 映射与 recency 键 pop、`trace_upload` 旧步骤名、`MAP_MOVEMENT_REVIEW_ISSUE`）在 Task 14 部署之后的收尾提交中删除。
- `narrative_constraints`：六种类型中五种自 P2 起已不生效。改为由操作员创建 source=human 的 `review_constraint` protection（经 `commit_plan_constraints`），迁移现有行后删除旧表、API 字段与 UI 编辑器；或者经用户决定直接删除。实施前向用户确认。

### Task 19: 成本与自治的度量（B10、B11、B14 余项）

**Files:** `forwin/context/**`、`forwin/knowledge_system/**`、`forwin/retrieval/broker_core/broker.py`、`forwin/observability/cost_ledger.py`、`forwin/planning/world_digest.py`、`forwin/planning/feasibility.py`；Test: 对应测试文件

- 写作上下文组装共享一次 BookState runtime（目前一次组装约 15 次加载）；`WorldContextPack` 只在 LLM 审稿需要时惰性组装。加载计数测试：一章的组装不超过 3 次加载。
- 每章报告：各角色 LLM 调用次数与 token、世界冲突数与人工冲突数（P5 验收的度量，对应设计“验证”第 5 条与审计的自治风险）。
- 世界简报改为逐行降级裁剪（避免整段裁掉只剩主角）；`exit_state` 的值与 BookState 比较（不再只看是否被触及）。

### Task 20:（已移出）写作者可见的设定简介

已在 P4a follow-up 中实现（`docs/superpowers/plans/2026-10-24-world-engine-p4a-followup.md` Task F6，裁定 FU-R51），P4b 不再执行。

---

## 执行分工（沿用 P3 / P4a 模式）

- 按用户要求（2026-10-24）：Codex 覆盖约三分之一的子任务。
  - Codex `gpt-6-astra`：Task 0、2、7、11、16、18
  - Codex `gpt-6.1-sol`：Task 13
  - Claude opus：Task 1、3、4、5、6、8、9、10、12、15、17、19
  - 任务审查与最终整支审查用 Claude；Codex 实现的任务由控制者运行 PostgreSQL 测试与全量测试。
- Task 14：仅控制者执行
- 依赖顺序：
  - 0 → 1、2 并行 → 3 → 4、5 并行 → 6。
  - 8 → 9 → 10 依次进行，因为它们共用世界引擎文件。
  - 7、11 从 3 开始可以并行。
  - 15 在 1 之后；16 与 2 共用 W16，放在 2 之后；17 放在 3、10 之后（分层改动面大，避免与功能任务冲突）；18 在 17 之后；19 在 15、17 之后。
  - 12 → 13 → 14，13 的文档覆盖 15–19。
