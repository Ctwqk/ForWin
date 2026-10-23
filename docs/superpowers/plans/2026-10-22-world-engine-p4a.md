# 世界引擎大修 P4a：卷节拍表、band 规划器、可行性审计、POV 世界视图 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 让剧情规划从世界取材：

- 卷激活时生成卷节拍表。
- 每个 band 按世界简报即时规划章节；每章入 canon 后用确定性的可行性审计检查剩余计划，不可行就自动重规划，仍不可行则产生冲突。
- writer 只看 POV 世界视图。
- 删除旧的规划栈、world contracts、WorldSimulator、subworld 角色路径和 ArcDirector。

**Architecture:**
- 规划：
  - 节拍表在 `forwin/planning/beat_sheet/`，新表 `arc_beat_sheets`，LLM 角色 `plan.arc_beats`；它的 protection 经新的 canon 入口 `commit_plan_constraints` 落为 narrative `review_constraint` 节点。
  - band 规划器在 `forwin/planning/band_planner/`，新表 `band_plans`，`chapter_plans` 增加新列，LLM 角色 `plan.band`。
  - 世界简报在 `forwin/planning/world_digest.py`，可行性审计在 `forwin/planning/feasibility.py`，post-Canon `plan` 步骤在 `forwin/planning/plan_step.py`。
- 冲突来源新增 `plan_feasibility` 与 `band_planning`。计划类冲突的 `amend_plan` 由注入的 `PlanAmender` 执行，它负责重规划 band 或生成新版节拍表。
- 写作：
  - `forwin/world_view/` 确定性地构建 `ChapterWorldView`，经 context provider 进入全部 5 个 writer prompt builder。
  - writer 不再渲染 world contracts、world_pressure、world_model 页、arc envelope、旧的地图与时间段。
- 流水线：
  - start-writing 只生成卷 1 节拍表，不再生成全卷章节计划。
  - run control 按卷的章节范围计算要写的章节，每章前确保该章计划存在（缺了就同步规划 band）。
  - post-Canon 步骤改为 `("world", "plan", "feedback")`，卷末由 plan 步骤激活下一卷。
- 删除附录 B 中剩余的规划栈与写作输入，迁移 `0006_planning_v6`。

**Tech Stack:** Python 3.13（`.venv`）、SQLAlchemy、Alembic、PostgreSQL（测试库 `127.0.0.1:55432`）、pydantic、pytest、Playwright。

**Spec:** `docs/designs/2026-10-20-world-engine-overhaul.md`。重点读 §2.4、§3.3、§4、§5.1–5.4、§6.2–6.3、§8、§9、§11 P4、附录 B。P1–P3 计划中的“与设计的取舍”继续有效。研究笔记在 `/private/tmp/claude-501/-Users-magi1-ForWin-source-github/79ab9c24-6cf3-5267-875a-345b2ab61341/scratchpad/p4-research-{planning,writer,deletions,deferrals}.md`，带 file:line，实施者可以按需读取。

## 与设计的取舍（实施者照此执行）

1. **P4 分为两份计划。** 两份都完成后才部署。
   - 本计划 P4a 是设计 §11 的 P4 本体。
   - P4b 另立计划，内容是：
     - 来自正文的冲突（`chapter_extraction` 来源与 `rewrite_chapter` 选项）；
     - `world.drift`、`checkable` 谓词求值、意图引出新行动者；
     - 重写最新章后的增量 tick（P26 遗留）与 retained tick 的引用检查（M7）；
     - `task_contract_json` 的删除、P3 遗留的小项与成本项、控制台冲突卡片。
2. **卷长固定为 Genesis 蓝图给出的章节范围。** 节拍表只把卷内章节切成 band（`band_breakdown`），不改 `chapter_start/chapter_end`，也不改 `target_total_chapters`。原因：卷长一变，后续卷、揭示时间表和总章数都要跟着移动。`planned_soft_min/max` 保留，留给 P5 调整。
3. **节拍表的额外字段。**
   - `protagonist_ref`：必须是种子中的 character 节点。world_seed 没有主角标记，节拍表 LLM 从输入的行动者中选出，fallback 取 story_engine `core_cast` 的第一个。
   - `reader_promise` 与 `arc_payoff_map`：沿用现有的 `ReaderPromise` / `ArcPayoffMap` 结构。它们现在只存在于要删除的 `arc_structure_drafts`，而保留下来的体验层需要它们。
   - 体验层原来读 `structure.key_beats`，改读节拍表的 phase purpose 与 milestone description。
4. **protection 的来源在 P4a 只有节拍表**，`source="beat_sheet"`。
   - 只用 `until_chapter`，取对应里程碑 `target_range` 的结束章；不实现 `until_milestone`。
   - band 计划与人工来源的 protection 暂不生产。
   - 节拍表被新版本取代时，旧版本的 protection 在同一个 delta 中把 `until_chapter` 设为最新 canon 章，即解除。
5. **计划约束的 canon 写入。**
   - 新入口 `CanonAdmissionService.commit_plan_constraints`，`source_type="plan_constraint"`，挂在最新 canon 章（start-writing 时是第 0 章），按节拍表 id 幂等。
   - 审查门 v6 对它只校验 narrative 补丁的 payload。
   - 历史重写把它当作与 `world_engine` 同类的、可以原样重放并做 CAS 校验的 delta。
6. **揭示时间表的来源。**
   - 对 reader 的揭示，以 active 节拍表的 `reveal_targets`（observer=reader）的 `range` 起点为准。
   - 没有覆盖的事实，回退到 Genesis `reveal_candidates`，并改为映射到**当前** `ArcPlanVersion` 的 `chapter_start`（修复 P2 遗留：原来读的是蓝图卷，续写卷不可见）。
   - 本章 `reveal_directives` 允许的事实，豁免 `premature_reveal`。
7. **band 计划逐个即时生成。**
   - `band_plans` 每个 band 一行，有版本，重规划时旧行标 superseded。
   - `chapter_plans` 行在重规划时**原地更新**（行 id 不变），之后对仍有效的 `NarrativePlanPatch` 重新应用一次（应用是幂等的）。
   - accepted / drafted / needs_review 的计划永远不被改写。这同时修复现在 `BandPlanService` 改写已接受计划的问题。
   - `band_id` 字符串保持 `band:{start}:{end}`，因为 MCP band checkpoint、成本与 gate 账本、trope 记录都依赖它。
   - `band_experience_plans` 继续由确定性排期写入，band checkpoint 与 strict progression 照旧读它。
8. **band 规划失败不产出劣质计划。**
   - LLM 调用异常，或者输出校验 / 可行性审计连续两次不通过，都不写任何 ChapterPlan。
   - plan 步骤里规划失败只记录在 `result_json`，由 run control 在该章开始前同步重试。
   - 重试仍失败，就产生 `band_planning` 冲突（human，阻塞该章）。
   - 节拍表 LLM 失败时，用确定性的结构空壳：均分 band，没有 milestone 与 protection，标记 `source="fallback"`。
9. **可行性审计的运行位置。**
   - post-Canon 的 `plan` 步骤：对本 band 剩余未写章节运行，不可行就自动重规划（上限 `planning.max_replans_per_band`），仍失败产生 `plan_feasibility` 冲突。
   - 写前检查（`_audit_current_plan_before_write`）：对当前章运行。
   - order controls 继续保留 `FuturePlanAuditor` 中与题材无关的义务、invariant、signal 绑定；其中的宏观进度部分删除。
10. **计划类冲突的选项。**
    - `plan_feasibility` / `band_planning` 冲突：decider 为 human，选项只有 `("amend_plan",)`。执行方式由 `PlanAmender` 决定：
      - 违反 milestone 或 exit_state 时，生成新版节拍表，再重规划；
      - 其余情况，带着用户 rationale 强制重规划该 band，忽略一次上限。
    - world tick 冲突的 `amend_plan`（human）在 P3 语义之上，再触发一次当前 band 剩余章节的重规划，把这次世界变化作为输入。
11. **`plot_responds` / `accept_with_obligation` 不再因为“没有下一章计划”而升级给用户。**
    - 没有未接受的计划时：
      - `plot_responds` 只提交变化，不写计划补丁，记为待回应；
      - `accept_with_obligation` 只记义务，不写计划补丁。
    - band 规划器读取待回应的冲突（resolved、`option=plot_responds`、`result_refs` 中没有 `plan_patch:`），以及未了义务。
    - 计划保存后，为每条待回应建一个 plot-response 补丁，补丁 id 追加到该冲突的 `result_refs`。P33 的回卷逻辑因此继续有效。
12. **onstage 顺延改由计划判定**，取代 P3 的临时规则：
    - 计划的 `world_bindings` 把该意图绑定到本章（kind=intent），而本章 `intent_interactions` 中没有它 → 顺延一次。
    - band 最后一章入 canon 后，本 band 内到点却从未被任何计划绑定的 onstage 意图 → 顺延一次。
    - 其余情况不顺延。
13. **critical 等级开始生效。**
    - tick 把 active 节拍表中尚未过期的 hard milestone 的 `required_refs`、加上 `exit_state` 涉及的引用，作为 `milestone_refs` 传给 `classify_impact`。
    - 触及这些引用的候选产生 `violated="milestone"` 冲突，路由给用户。
14. **POV 世界视图的取舍。**
    - 预算 `writer.world_view_budget_chars`：standard 6000，pulp 4000。
    - 读者已知的事实只取**已记录**的 reader 认知，不用 `CognitionView.knowledge_state` 的“公开即已知”默认。
    - 禁止定论话题的文本用事实 `related_refs` 中实体的名字拼成，模板放在 prompt_core，不含真相。
    - 视图中的 POV 地图只含 writer 可见的边（`is_writer_visible_map_edge`），不走“无 overlay 即用客观图”的回退。
    - 人物性格段只取本章 onstage 角色；“当前允许命名角色”段改用视图的实体表。
    - `checker/hard_floor.py` 的 `must_not_reveal` 子串检查删除（它在 P0 之后已经恒为空）；揭示保护由审查门的揭示检查承担。
    - scene 的 `<<FORWIN_LOCATION>>` 改为输出视图地点表中的 id 或 `new:<名称>`；各 scene 的取值作为非证据提示 `scene_location_hints` 交给章节抽取。
    - 抽取输入中的实体表、地点表、秘密清单在 P4a 不收窄（`ref_check` 把秘密清单当作 fact 引用全集，收窄会导致误报），留到 P4b。
15. **评论分析迁出 WorldSimulator。** `CommentAnalyzer` 是 `comment_signal_candidates` 的唯一写入者。
    - 连同 `_KEYWORD_RULES` 移到 `forwin/audience/comment_analysis.py`，由 feedback 步骤在聚合前调用。
    - allowlist 条目改登记为 `language_lexicon`（读者评论用语，不是故事内容），从 `pending_removal` 移除。
16. **subworld roster 删除后的替代。**
    - 写作上下文的 active_subworlds 改为：本章 `location_refs` 所在地图节点的 `subworld_id`；没有时，用 POV 当前位置所在的 subworld。
    - 实体准入中的 roster 绑定、API 字段 `roster_item_id`、`character_identity_map.roster_item_ids_json` 一并删除。
17. **WorldDigest 与 band 规划器用作者视角。** 可以看到隐藏意图与秘密，但只进入规划 prompt，从不进入 writer prompt。
18. **access rule / 队伍速度 / 边倍率不实现。** 设计用 lexicon `travel_modes` 取代了它们，文档中记为“已由出行方式取代”。

## Global Constraints

- 运行代码只处理结构；故事内容只来自 Genesis、world_seed、BookState 或 LLM 输出；题材相关词只能来自本书的 lexicon。
  - 新增运行代码不得引入 CJK 词表（模块级集合、`re` 模式、`in` 成员判断、函数内字面量迭代）。
  - 提示词与面向写作的文本模板放在 `forwin/writer/prompt_core/**` 这类已登记为 `prompt_text` 的位置，条目数变化时同步 `tests/overfit_allowlist.toml` 的 `max_literals`。
- 迁移：
  - 已合入的迁移（`0001`–`0005`）不得修改。
  - 本期迁移 `0006_planning_v6` 由 Task 2 创建；P4a 与 P4b 的任务都可以往里追加，直到合入 master。
  - revision id ≤ 32 字符。新 ORM 模型必须能在 SQLite 上 `create_all`。
- `BookStateCompiler(` 只能出现在 `forwin/canon/admission.py`；所有 canon 写入经 `CanonAdmissionService`。`forwin/planning/**` 与 `forwin/world_view/**` 不得直接写 world / map / cognition / narrative 表。
- 故事时间是自纪元起的整数分钟；时长换算统一用 `forwin/book_state/story_clock.duration_to_minutes`；地图边 `travel_time` 是小时，乘 60 换成分钟；出行耗时统一用 `forwin/world_engine/travel.estimate_travel`。
- 每个结构补丁（不含 create）都必须带 `old_value` 与 `field_path`。
- 新枚举值一律用英文 token。LLM 输出中的引用只能是输入里给出的 BookState 节点 id、地图节点 id、`fact:<id>` 或输入列出的 milestone / directive id；不认识的引用带着错误重试一次，仍不认识就判为失败。
- 每个 LLM 调用点都必须保存 PromptTrace 行（带 stage_key）和 LLM DecisionEvents，不得只写 outbox 产物（`plan.arc_beats`、`plan.band` 都要）。
- 锁顺序沿用 P3：冲突行在前，项目行 `FOR NO KEY UPDATE` 在后。规划器写 canon 的路径（`commit_plan_constraints`）只经 `CanonAdmissionService`。
- writer prompt 永远不得出现：POV 不知道、读者按时间表也不该知道、本章也没有揭示指令的秘密的真相文本。
- 测试不调用真实 LLM，不连接生产库，不访问网络。
- 分支与提交：
  - 分支 `claude/world-engine-p4` 已从 P3 头 `7fff996` 建好。
  - 本机没有 git 身份。每次提交都用 `git -c user.name=CASPER -c user.email=magi1@CASPERs-Mac-mini.local commit ...`，提交信息末尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`；提交信息中不得出现模型名。
- 测试命令：`.venv/bin/python -m pytest -q`。每个任务结束时全量测试必须全部通过。
- 不做任何生产操作。部署 runbook 放在 P4b 的最后一个任务里，P0–P4 合并执行，每一步经用户确认。

## Review Focus

1. **band 规划两次都不可行**（例如 onstage 角色已死，或地点在 `expected_span` 内到不了）：
   - 不写任何 ChapterPlan，产生 `band_planning` 冲突，run 在该章前以 needs_review 停下。
   - 用户 `amend_plan` 之后可以继续。
   - 由 Task 6 的 `test_infeasible_band_twice_creates_conflict_and_writes_no_plans` 和 Task 9 的 `test_run_pauses_before_unplannable_chapter` 覆盖。
2. **卷末章节入 canon 之后**：
   - plan 步骤激活下一卷：节拍表、protection、第一个 band 的计划。
   - 下一次 continue 直接写下一卷首章，不需要人工物化。
   - plan 步骤里 LLM 不可用时，由 run control 同步补齐。
   - 由 Task 10 的 `test_arc_end_activates_next_arc_and_plans_first_band` 和 Task 9 的 `test_run_control_activates_next_arc_when_plan_step_could_not` 覆盖。
3. **秘密进入 writer prompt**：对 POV 不知道、读者未到揭示时间、本章也没有揭示指令的秘密，scene、single、preview 三类 prompt 都只给话题，不给真相；有揭示指令的事实给真相。由 Task 12 的 `test_no_builder_leaks_unpermitted_secret_truth` 参数化覆盖全部 5 个 builder。
4. **世界变化引发 band 重规划**：
   - accepted、drafted 的章节不被改动。
   - 未写章节上的 plot-response 补丁与义务补丁在重规划后仍然存在。
   - 由 Task 6 的 `test_replan_keeps_accepted_rows_and_reapplies_active_patches` 覆盖。
5. **P3 留下的数据在 0006 之后仍可加载**：
   - 存储中带 `planning.use_llm_simulation` 的 policy JSON；
   - 名为 `planning` / `arc` 的 post-Canon run 行。
   - 由 Task 2 的 `test_0006_upgrade_rewrites_legacy_step_rows_and_policies` 和 Task 1 的 `test_stored_policy_with_removed_keys_loads` 覆盖。

---

### Task 1: 规划、写作的成本旋钮与已删除键的兼容加载

**Files:**
- Modify: `forwin/runtime/policy.py`（`PlanningPolicy` 新字段，新增 `WriterPolicy` 与 `RuntimePolicy.writer`，before-validator 剥离已删除的键）、`forwin/api_schema/policy.py`（只读展示新字段，如已有 world 段的模式）
- Test: `tests/test_runtime_policy.py`

**Interfaces:**
- Produces：
  - `PlanningPolicy` 新增三个字段：
    - `band_size: int`（4–12）
    - `max_replans_per_band: int`（0–5）
    - `digest_budget_chars: int`（2000–20000）
  - 新类 `WriterPolicy(world_view_budget_chars: int)`（1000–20000），`RuntimePolicy.writer: WriterPolicy`。
  - 默认值：
    - standard：`band_size=6`、`max_replans_per_band=2`、`digest_budget_chars=8000`、`world_view_budget_chars=6000`
    - pulp：`8`、`1`、`6000`、`4000`
  - 加载：
    - 缺失的新字段与 `writer` 段，按 `quality_profile` 用 profile 默认值补齐（与 P3 的 `_load_world_defaults` 同一模式）。
    - 存储中出现的 `planning.use_llm_simulation`、`planning.context_recency_window` 在 before-validator 中丢弃；字段本身由 Task 14、Task 18 删除。

- [ ] **Step 1: 写失败测试**
  - `test_profile_defaults_for_planning_and_writer_knobs`：断言上面的两组默认值。
  - `test_band_size_bounds_enforced`：3 和 13 都 ValidationError。
  - `test_stored_policy_with_removed_keys_loads`：一个缺 `writer` 段、带 `use_llm_simulation` 和 `context_recency_window` 的 P3 policy dict 能加载，并得到 profile 默认值。
- [ ] **Step 2: 运行，确认失败**：`.venv/bin/python -m pytest -q tests/test_runtime_policy.py` → FAIL
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_runtime_policy.py tests/test_project_policy_api.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(policy): add band, digest and world-view budget knobs`

---

### Task 2: 迁移 0006 与新模型

**Files:**
- Create: `forwin/migrations/versions/0006_planning_v6.py`、`forwin/models/planning_v6.py`（`ArcBeatSheetRow`、`BandPlanRow`）
- Modify: `forwin/models/project.py`（`ChapterPlan` 新列）、`forwin/models/__init__.py`、`tests/test_v5_recovery_schema.py`（迁移头断言）
- Test: `tests/test_planning_v6_schema.py`（新建）

**Interfaces:**
- Produces：
  - 表 `arc_beat_sheets`：
    - 列：`id`、`project_id`（FK projects）、`arc_plan_id`（FK arc_plan_versions）、`version` int、`status`（`active` / `superseded`）、`source`（`llm` / `fallback`）、`protagonist_ref`、`phases_json`、`milestones_json`、`protections_json`、`reveal_targets_json`、`exit_state_json`、`band_breakdown_json`、`reader_promise_json`、`arc_payoff_map_json`、`revision_reason`、`created_at`。
    - 唯一约束 `(arc_plan_id, version)`，索引 `(project_id, status)`。
  - 表 `band_plans`：
    - 列：`id`、`project_id`、`arc_plan_id`、`beat_sheet_id`（FK arc_beat_sheets）、`band_index` int、`chapter_start`、`chapter_end`、`status`（`active` / `superseded`）、`version` int、`replan_count` int、`plan_json`、`created_at`。
    - 索引 `(project_id, arc_plan_id, band_index, status)`。
  - `chapter_plans` 新列（都可空或带默认值）：
    - `band_plan_id`（FK band_plans，可空）
    - `pov_ref` str 默认 ""
    - `onstage_refs_json`、`location_refs_json`、`world_bindings_json`、`reveal_directives_json`、`hint_directives_json`、`intended_changes_json`、`milestone_ids_json`：Text，默认 "[]"
    - `expected_span_minutes` int 可空
    - `hook` Text 默认 ""
  - 唯一索引 `uq_chapter_plans_project_chapter (project_id, chapter_number)`，取代原非唯一索引。
  - 数据步骤：
    - `post_canon_maintenance_runs.step_name`：`'planning'` 改为 `'plan'`，删除 `'arc'` 行。
    - 项目存储的 `runtime_policy_json` 中，`planning.use_llm_simulation` 与 `planning.context_recency_window` 删除；做法同 0003 的 `_drop_book_state_layers_from_policies`。
  - downgrade：撤销表和列。数据步骤不可逆，写明注释。
  - 删除旧表的语句由 Task 14–18 追加到本迁移。

- [ ] **Step 1: 写失败测试（`tests/test_planning_v6_schema.py`，PostgreSQL）**
  - `test_0006_creates_beat_sheet_and_band_plan_tables`
  - `test_chapter_plans_gain_planning_columns_and_unique_chapter`：同一项目同一章插第二行会违反唯一约束。
  - `test_0006_upgrade_rewrites_legacy_step_rows_and_policies`：先在 0005 下插入 `planning`、`arc` 两行 run，以及带两个旧键的 policy；upgrade 之后只剩 `plan` 行，policy 不再含这两个键。
  - `test_models_create_all_on_sqlite`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_planning_v6_schema.py tests/test_v5_recovery_schema.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(planning): add beat sheet and band plan tables`

---

### Task 3: 卷节拍表（`plan.arc_beats`）

**Files:**
- Create: `forwin/planning/beat_sheet/{__init__,types,repository,service,fallback}.py`、`forwin/writer/prompt_core/planning.py`（`build_arc_beats_messages`）
- Modify: `forwin/writer/llm/routing.py`（注册 `plan.arc_beats`，task_family `planning`）、`tests/overfit_allowlist.toml`（登记 `prompt_core/planning.py` 为 prompt_text）
- Test: `tests/test_arc_beat_sheet.py`（新建）

**Interfaces:**
- Consumes：
  - `ArcPlanVersion`：title / goal / stakes / payoff_direction / arc_synopsis / chapter_start / chapter_end。
  - world_seed 的 `reveal_candidates`。
  - Task 5 的 `build_world_digest`：本任务先依赖它的签名；若 Task 5 未完成，测试中桩住。
  - `NarrativeObligationRepository.list_active_for_context`。
  - `build_arc_activation_review_pack`，提供上一卷的收尾状态。
  - Task 1 的 `policy.planning.band_size`。
- Produces：
  - `forwin/planning/beat_sheet/types.py` 中的类型：
    - `BeatPhase{name, chapter_range: tuple[int, int], purpose}`
    - `Milestone{id, description, target_range: tuple[int, int], required_refs: list[str], hard: bool}`
    - `ProtectionSpec{ref, kind, until_chapter, observer_ref, fact_ref, field, value}`：`kind` ∈ `must_exist` / `must_be_alive` / `must_hold_state` / `must_not_know`
    - `RevealTarget{fact_ref, observer, target_state, range: tuple[int, int]}`
    - `ExitStateItem{ref, field, value, note}`
    - `BandSpan{band_index, chapter_start, chapter_end}`
    - `ArcBeatSheetData`：上述各项，加上 `protagonist_ref`、`reader_promise: ReaderPromise`、`arc_payoff_map: ArcPayoffMap`。
  - `ArcBeatSheetRepository(session)`：
    - `.active_for_arc(arc_plan_id) -> ArcBeatSheetRow | None`
    - `.active_for_project(project_id) -> list[ArcBeatSheetRow]`
    - `.create(project_id, arc_plan_id, data, *, source, reason) -> ArcBeatSheetRow`：版本号加 1，旧版本标 superseded。
    - `.load(row) -> ArcBeatSheetData`
  - `ArcBeatSheetService(llm_client)`：
    - `.ensure_for_arc(session, *, project_id, arc_plan, policy, reason="activation") -> ArcBeatSheetRow`：已有 active 行就直接返回。
    - `.revise(session, *, project_id, arc_plan, policy, reason, conflict_summary) -> ArcBeatSheetRow`：生成新版本。
  - 校验规则（确定性，不通过就带错误重试一次，仍不通过走 fallback）：
    - `phases` 与 `band_breakdown` 恰好覆盖 `[chapter_start, chapter_end]`，不重叠；每个 band 的长度在 `[4, 12]` 之内。卷长小于 4 时允许一个短 band。
    - milestone 的 `target_range` 在卷内；`required_refs`、protection 与 exit_state 的 `ref`、`protagonist_ref` 都是 BookState 中存在的节点；`fact_ref` 是已有事实。
    - protection 的 `until_chapter` 在卷内。
  - fallback：
    - band 按 `policy.planning.band_size` 均分，phase 只有一个，`purpose = arc_plan.goal`；没有 milestone、protection、reveal target。
    - `protagonist_ref` 取 core_cast 第一个已种入的角色；`reader_promise` / `arc_payoff_map` 为空结构；`source="fallback"`。
  - 记录：每次调用写 PromptTrace（`stage_key="plan.arc_beats"`）与 LLM DecisionEvents，走与 genesis 调用相同的保存路径。
  - 本任务不写 protection 节点，那是 Task 4 的事。

- [ ] **Step 1: 写失败测试**
  - `test_beat_sheet_from_stub_llm_validates_and_persists`：版本 1、active，band 覆盖整卷，并写了 PromptTrace。
  - `test_invalid_ref_retries_once_then_falls_back`：两次都引用不存在的节点 → `source="fallback"`，band 均分。
  - `test_band_breakdown_must_cover_arc_without_overlap`：参数化覆盖漏章、重叠、band 过长三种情况。
  - `test_revise_creates_new_version_and_supersedes_old`
  - `test_ensure_is_idempotent`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_arc_beat_sheet.py tests/test_no_story_content_in_runtime.py tests/test_llm_router.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(planning): generate a beat sheet when an arc activates`

---

### Task 4: 计划约束入 canon、揭示时间表改读节拍表

**Files:**
- Modify:
  - `forwin/canon/admission.py`：新增 `commit_plan_constraints`
  - `forwin/book_state/narrative.py`：常量 `PLAN_CONSTRAINT_SOURCE_TYPE = "plan_constraint"`
  - `forwin/book_state/reviewer.py`：`_V6_SOURCE_TYPES` 加入新来源；`review_constraint` payload 校验
  - `forwin/canon/historical_rewrite.py`：接受并 CAS 重放 `plan_constraint` delta
  - `forwin/book_state/reveal_schedule.py`：先读节拍表，再回退到当前卷
  - `forwin/planning/beat_sheet/service.py`：建表之后提交 protection
- Test: `tests/test_plan_constraints_canon.py`（新建）、`tests/test_world_gate_v6.py`、`tests/test_historical_rewrite_world_ticks.py`

**Interfaces:**
- Consumes：Task 3 的 `ArcBeatSheetRow` 与 `ArcBeatSheetData.protections`、`reveal_targets`。
- Produces：
  - `CanonAdmissionService.commit_plan_constraints(*, session, project_id, beat_sheet_id: str, creates: list[ProtectionSpec], releases: list[str]) -> CanonPlanConstraintOutcome(graph_delta_ids: tuple[str, ...], idempotent: bool)`：
    - 锁项目行 `FOR NO KEY UPDATE`。
    - 挂在最新 canon 章；没有 canon 章时为 0，排在种子 delta 之后。
    - 按 `metadata.beat_sheet_id` 幂等。
    - `creates` 落为新的 narrative `review_constraint` 节点，payload 为 `{ref, kind, until_chapter, source: "beat_sheet", observer_ref?, fact_ref?, field?, value?}`。
    - `releases` 是旧节拍表的 protection 节点 id，它们的 `until_chapter` 设为最新 canon 章，带 `old_value`。
  - 审查门对 `plan_constraint` delta 的规则：
    - 只允许 narrative 补丁。
    - `kind` 必须属于四个合法值；`must_not_know` 必须带 `observer_ref` 与 `fact_ref`；`must_hold_state` 必须带 `field`；`until_chapter` 是 ≥0 的 int。
    - 违反时为 error，code 为 `invalid_protection`。
  - 节拍表建立或修订之后，`ArcBeatSheetService` 调用 `commit_plan_constraints`：修订时 `releases` 为上一版本的全部 protection 节点。
  - `earliest_reveal_chapters(session, project_id) -> dict[str, int]` 的取值规则：
    - 优先：active 节拍表中 `observer == "reader"` 的 reveal target，取其 `range[0]`。
    - 其余事实：world_seed candidate 的 `arc_number` 对应当前 `ArcPlanVersion` 的 `chapter_start`（含续写卷）。
    - 函数签名不变。
  - `milestone_refs_for_chapter(session, project_id, chapter_number) -> frozenset[str]`，放在 `forwin/planning/beat_sheet/service.py`，供 Task 8 使用。结果是两部分的并集：
    - active 节拍表中 `hard=True` 且 `target_range[1] >= chapter_number` 的 milestone 的 `required_refs`；
    - 当前卷 `exit_state` 的 `ref`。

- [ ] **Step 1: 写失败测试**
  - `test_commit_plan_constraints_creates_protection_nodes_idempotently`：调两次只有一个 delta。check_narrative_boundaries 能读到这些节点，因为它读的是 runtime。
  - `test_revised_beat_sheet_releases_old_protections_in_same_delta`
  - `test_gate_rejects_malformed_protection`：参数化覆盖 4 种缺字段的情况。
  - `test_historical_rewrite_replays_plan_constraint_delta`
  - `test_reveal_schedule_prefers_beat_sheet_reader_targets`
  - `test_reveal_schedule_falls_back_to_live_arc_start_including_extended_arc`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_plan_constraints_canon.py tests/test_world_gate_v6.py tests/test_historical_rewrite_world_ticks.py tests/test_arc_beat_sheet.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(canon): commit beat-sheet protections and read reveal targets from the beat sheet`

---

### Task 5: 世界简报（`WorldDigest`）

**Files:**
- Create: `forwin/planning/world_digest.py`
- Test: `tests/test_world_digest.py`（新建）

**Interfaces:**
- Consumes：
  - `BookStateProjection.load_runtime_as_of`
  - `propagation.latest_news_in_transit`
  - `estimate_travel`
  - 叙事义务仓储
  - `book_state/thread_sampling.py` 的停滞剧情线采样
  - 节拍表的 `protagonist_ref`
- Produces：
  - `build_world_digest(session, *, project_id, as_of_chapter: int, protagonist_ref: str, window_end_minute: int, budget_chars: int, runtime=None) -> WorldDigest(sections: list[DigestSection(key, title, lines: list[str])], text: str, trimmed_keys: list[str])`。
  - 8 个 section，key 依次为：`protagonist`、`actors`、`news_in_transit`、`offscreen_events`、`secrets_progress`、`tensions`、`clocks`、`stalled_threads_obligations`。
  - 各 section 的内容：
    - **actors**：按三项排序，取前 `DIGEST_TOP_ACTORS = 8`，附带可见与隐藏的意图及其预计时间。
      1. 与主角的关系距离（边的跳数）；
      2. 地图耗时；
      3. 意图是否指向主角或其关联对象。
    - **news_in_transit**：只列 `arrive_at <= window_end_minute` 且到达主角所在地的消息。
    - **offscreen_events**：最近 3 章的 `world_engine` 事件。
    - **secrets_progress**：reader 已记录的认知状态，对照 reveal target。
  - 文本构造与裁剪：
    - 行文本只由节点名字、字段值和英文结构标签拼成；中文标题模板放在 `forwin/writer/prompt_core/planning.py`。
    - 超出预算时，从最后一个 section 开始整段删除，直到不超；`trimmed_keys` 记录被删的段。
  - 简报是作者视角（隐藏意图可见），只用于规划 prompt。

- [ ] **Step 1: 写失败测试**
  - `test_digest_orders_sections_and_ranks_actors_by_relevance`
  - `test_digest_trims_from_the_end_under_budget`
  - `test_digest_lists_only_news_arriving_within_window`
  - `test_digest_reader_progress_uses_recorded_reader_states_only`：一个没有记录的公开事实不算读者已知。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_digest.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(planning): build a deterministic world digest for planners`

---

### Task 6: band 规划器（`plan.band`）

**Files:**
- Create: `forwin/planning/band_planner/{__init__,types,inputs,validation,service,persistence}.py`
- Modify:
  - `forwin/writer/prompt_core/planning.py`：`build_band_plan_messages`
  - `forwin/writer/llm/routing.py`：注册 `plan.band`
  - `forwin/experience/band_scheduler.py`、`forwin/experience/chapter_planner.py`：输入改为 `BeatSheetExperienceInput`，`ArcStructureDraftData` 保留到 Task 17a 删除
  - `forwin/state/updater.py`：原地更新计划行的方法
  - `forwin/world_engine/conflict_repository.py`：`pending_plot_responses`、`attach_result_refs`
  - `tests/overfit_allowlist.toml`
- Test: `tests/test_band_planner.py`（新建）、`tests/test_trope_selector.py`、`tests/test_experience_planning_service.py`

**Interfaces:**
- Consumes：
  - Task 3：`ArcBeatSheetData` 与 `BandSpan`
  - Task 5：`build_world_digest`
  - 保留下来的 `BandExperienceScheduler`、`TropeSelector`、`ChapterExperiencePlanner`、trope 冷却
  - `NarrativeObligationRepository`
  - 到点的 onstage 意图：`requires_onstage` 且 `status=due`
  - Task 7 的 `FeasibilityAuditor.audit_plans`：本任务先依赖它的签名，Task 7 之前桩住
- Produces：
  - `forwin/planning/band_planner/types.py` 中的类型：
    - `WorldBinding{kind, ref, how}`：kind ∈ `intent` / `event` / `news` / `clock` / `relation` / `secret`
    - `RevealDirective{id, fact_ref, observer, target_state}`
    - `HintDirective{id, fact_ref, hint_text}`
    - `ChapterPlanDraft{chapter_number, title, one_line, goals, pov_ref, onstage_refs, location_refs, expected_span{amount, unit}, world_bindings, reveal_directives, hint_directives, intended_changes: list[{ref, field, value}], milestone_ids, reward_slot, trope_ids, hook}`
    - `BandPlanDraft{band: BandSpan, chapters: list[ChapterPlanDraft]}`
  - `BeatSheetExperienceInput`：从节拍表构造体验层原来读 `ArcStructureDraftData` 的部分，包括 key_beats（phase purpose 与 milestone description）、`reader_promise` 与 `arc_payoff_map`。
  - `BandPlanner(llm_client, *, feasibility)`，三个方法：
    - `.plan_band(session, *, project_id, arc_plan, beat_sheet_row, band: BandSpan, policy, extra_notes: list[str] = ()) -> BandPlanOutcome(band_plan_id: str | None, chapter_plan_ids: list[str], issues: list[PlanIssue], conflict_id: str | None)`
    - `.ensure_plan_for_chapter(session, *, project_id, chapter_number, policy) -> ChapterPlan | None`：已有 planned / failed 行就返回它；否则找到该章所在的卷与 band，规划整个 band。失败时返回 None，此时冲突已经建好。
    - `.replan_remaining(session, *, project_id, band_plan_id, from_chapter, policy, notes: list[str]) -> BandPlanOutcome`：只重写 `chapter_number >= from_chapter` 且 status ∈ {planned, failed} 的行。
  - 输入：
    - 节拍表中与本 band 相交的 phase，以及窗口内和邻近的 milestone。
    - 确定性体验排期（reward slot、trope 及冷却）。
    - 世界简报：`policy.planning.digest_budget_chars`，`window_end_minute` = 上一章 `story_time_end` + 本 band 章数 × 上一 band 实际平均章时长；没有上一 band 时，用 1 天对应的分钟数。
    - 上一个 band 的实际结果：已接受章节的摘要。
    - 停滞剧情线、未了义务、到点的 onstage 意图。
    - 待回应的世界变化：`WorldConflictRepository.pending_plot_responses(project_id)`。
  - 输出校验（确定性）：
    - 章号恰好是该 band 的章节。
    - `pov_ref ∈ onstage_refs`；所有 ref 都存在于 runtime；地点是地图节点。
    - `fact_ref` 是已有事实；`milestone_ids` 属于节拍表。
    - `hint_text` 规范化空白后不得包含该事实的 proposition。
    - `expected_span` 的单位属于本书日历。
    - 每条待回应必须被某一章的 goal 或 binding 承担。
  - 失败处理：
    - 校验或 `FeasibilityAuditor.audit_plans` 有 error → 带着错误重试一次。
    - 仍有 error，或 LLM 抛异常 → 不写计划。
    - 由 `ensure_plan_for_chapter` 调用时，建一个冲突：`source="band_planning"`、`violated="feasibility"`、decider human、`options=["amend_plan"]`，`proposed_change={"kind": "plan_issues", "band": [start, end], "issues": [...]}`，`blocks_from_chapter` = band 起始章。
    - 由 `plan_band` 直接调用时，只返回 issues。
  - 持久化，在一个事务内完成：
    - `band_plans` 新行（旧的 active 行标 superseded，`replan_count + 1`）。
    - 每章的 ChapterPlan：已存在就原地更新，否则 `create_chapter_plan`，写入全部新列，`expected_span_minutes` 用日历换算。
    - `ChapterExperiencePlan`：由排期与 draft 确定性派生，`chapter_entry_targets = onstage_refs`。
    - `band_experience_plans`：排期，band_id 为 `band:{start}:{end}`。
    - 写完之后：
      - 对目标计划上仍有效的 `NarrativePlanPatch`（没有被 revert，来源冲突仍为 resolved）重新应用一次。
      - 为每条待回应建 plot-response 补丁，并 `attach_result_refs(conflict, [f"plan_patch:{id}"])`。
  - 记录：PromptTrace（`stage_key="plan.band"`）与 LLM DecisionEvents。
  - `WorldConflictRepository.pending_plot_responses(project_id) -> list[WorldPlanConflictRow]`：status resolved、`decision.option == "plot_responds"`、`result_refs` 中没有 `plan_patch:` 前缀的行。
  - `WorldConflictRepository.attach_result_refs(row, refs) -> None`：追加并去重。

- [ ] **Step 1: 写失败测试（桩 LLM，PostgreSQL）**
  - `test_plan_band_writes_plans_with_new_columns_and_experience`
  - `test_invalid_output_retries_once_with_errors`：第一次 pov 不在 onstage 里，第二次正确，最终成功。
  - `test_infeasible_band_twice_creates_conflict_and_writes_no_plans`：Task 7 的审计桩对两次都返回 `onstage_unavailable`。
  - `test_hint_text_containing_truth_is_rejected`
  - `test_replan_keeps_accepted_rows_and_reapplies_active_patches`：
    - 第 1 章 accepted，第 2 章 drafted，第 3、4 章 planned，第 3 章带一个 plot-response 补丁。
    - 从第 3 章重规划后：第 1、2 章不变；第 3 章行 id 不变、内容更新，补丁的目标仍在 goals 中。
  - `test_pending_plot_response_is_bound_and_attached`
  - `test_llm_outage_returns_no_plans_without_raising`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_band_planner.py tests/test_trope_selector.py tests/test_experience_planning_service.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(planning): plan each band from the beat sheet and the world digest`

---

### Task 7: 可行性审计、自动重规划与计划类冲突

**Files:**
- Create: `forwin/planning/feasibility.py`、`forwin/planning/amend.py`（`PlanAmender`）
- Modify:
  - `forwin/world_engine/conflict_routing.py`：两个计划来源 → human，选项只有 `amend_plan`
  - `forwin/world_engine/conflict_resolution.py`：注入 `plan_amender`；计划类冲突不走 `TickCandidate` 路径；world tick 冲突的 `amend_plan` 在提交后调用 amender
  - `forwin/generation/pipeline_core/audit_control.py`：`_audit_current_plan_before_write` 增加可行性审计
  - `forwin/runtime/container.py`：装配
  - `forwin/http/adapters/api_world_conflict_routes.py`：把 amender 传给服务
- Test: `tests/test_plan_feasibility.py`（新建）、`tests/test_world_conflict_resolution.py`、`tests/test_world_conflict_routing.py`

**Interfaces:**
- Consumes：Task 3、Task 4、Task 6 的接口，以及 `estimate_travel`、`earliest_reveal_chapters`。
- Produces：
  - `PlanIssue{code, severity: "error" | "warning", chapter_number, ref, message}`。
  - `FeasibilityAuditor()`：`.audit_plans(session, *, project_id, plans: Sequence[ChapterPlanDraft | ChapterPlan], beat_sheet: ArcBeatSheetData | None, policy, runtime=None) -> list[PlanIssue]`。检查项：
    - `onstage_unavailable`（error）：onstage ref 不存在、已死、或处于受限状态。受限的例外：计划的 `intended_changes` 含有该 ref 的 `restraint` 变为 free。
    - `location_unknown`（error）：地点不是地图节点。
    - `location_unreachable`（error）：POV 从当前位置（前一章结束时）到第一个 `location_ref`，用 `estimate_travel` 算出的耗时大于 `expected_span_minutes × (1 + review.travel_tolerance)`。
    - `reveal_too_early`（error）：reader 的揭示指令早于 `earliest_reveal_chapters`，或者违反 `must_not_know(reader, fact)` protection。
    - `milestone_uncovered`（error）：hard milestone 的 `target_range[1]` ≤ 已规划的最后一章，但没有任何 accepted 或 planned 计划带有该 milestone id。
    - `exit_state_unaddressed`（warning）：卷最后一个 band 中，没有章节的 `intended_changes` 触及某个 exit_state ref。
    - `clock_unanswered`（warning）：band 窗口内到期的 running 公开 clock，没有被任何 `world_bindings` 引用。
    - `due_onstage_unbound`（warning）：到点的 onstage 意图，没有被本 band 绑定。
  - `PlanAmender(band_planner, beat_sheet_service)`：`.amend(session, *, conflict: WorldPlanConflictRow, decision: ConflictDecision, policy) -> AmendResult(band_plan_id, beat_sheet_id)`。规则：
    - 计划类冲突：
      - violated 是 `milestone` 或 issues 含 `exit_state_unaddressed`：`beat_sheet_service.revise`，再 `replan_remaining`；
      - 否则：`replan_remaining(notes=[decision.rationale])`，忽略一次 `max_replans_per_band`。
    - world tick 冲突：在 P3 的提交与解除 protection 之后，`replan_remaining(from_chapter=最新 canon 章+1, notes=[冲突 summary])`。
  - 自动重规划由 Task 10 的 plan 步骤调用：
    - 审计有 error 且 `band_plans.replan_count < max_replans_per_band` → `replan_remaining`。
    - 否则建冲突：`source="plan_feasibility"`、`violated` 取第一个 error 对应的值（`reveal_too_early` → `reveal_schedule`，`milestone_uncovered` → `milestone`，其余 → `feasibility`）、decider human、`blocks_from_chapter` = 第一个出错的章。
  - 写前检查：当前章有 error 时，以 `future_plan_audit_blocked:feasibility` 失败，与现有 FPA 阻塞同一路径；warning 只记录。

- [ ] **Step 1: 写失败测试**
  - `test_feasibility_checks`：参数化覆盖 8 个 code。
  - `test_feasibility_allows_restrained_onstage_when_plan_releases_them`
  - `test_plan_conflict_offers_only_amend_and_routes_human`
  - `test_amend_plan_on_plan_conflict_replans_band_with_rationale`
  - `test_amend_plan_on_milestone_conflict_revises_beat_sheet_first`
  - `test_world_amend_plan_triggers_replan_of_unwritten_band_chapters`
  - `test_prewrite_feasibility_error_blocks_chapter`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_plan_feasibility.py tests/test_world_conflict_resolution.py tests/test_world_conflict_routing.py tests/test_world_conflict_api.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(planning): audit planned chapters against the world and route infeasible plans`

---

### Task 8: 世界引擎与计划的衔接

**Files:**
- Modify:
  - `forwin/world_engine/tick.py`：传 `milestone_refs`；band 末顺延
  - `forwin/world_engine/tick_conflicts.py`：`violated="milestone"`；删除 `NO_UNACCEPTED_PLAN` 升级
  - `forwin/world_engine/intents.py`：顺延改读计划绑定
  - `forwin/world_engine/conflict_resolution.py`：没有下一章计划时记为待回应
  - `forwin/world_engine/constraints.py`：保持 `classify_impact(..., milestone_refs=)` 签名
- Test: `tests/test_world_tick_service.py`、`tests/test_world_tick_steps.py`、`tests/test_world_conflict_resolution.py`

**Interfaces:**
- Consumes：
  - Task 4：`milestone_refs_for_chapter`
  - Task 6：`band_plans`、ChapterPlan 的 `world_bindings`
  - Task 6：`pending_plot_responses` 的约定
- Produces：
  - tick 在分类影响时传入 `milestone_refs_for_chapter(project_id, N+1)`。
    - 结果为 critical 且触及 milestone 引用的候选，产生 `violated="milestone"`、decider human 的冲突。
    - P21 的防御：`option_candidates` 对 `violated ∈ {milestone, reveal_schedule}` 的冲突，只接受 `veto_world` 与 `amend_plan`。
  - onstage 顺延（取舍第 12 条）：`onstage_bookkeeping(..., bound_intent_refs: frozenset[str], band_ended: bool)`。
    - `bound_intent_refs` 是本章计划中 kind=intent 的绑定。
    - `band_ended` 表示本章是其 band 的最后一章。
  - 待回应：
    - `plot_responds` 与 `accept_with_obligation` 在没有未接受计划时，不再抛 `ConflictOptionUnavailable`，也不再升级。
    - 前者只提交，`decision_json.pending_plan_response = true`；后者记义务，不写计划补丁。
    - `tick_conflicts` 中的 `NO_UNACCEPTED_PLAN` 分支删除。

- [ ] **Step 1: 写失败测试**
  - `test_change_touching_hard_milestone_is_critical_and_goes_to_human`
  - `test_bound_but_undelivered_onstage_intent_slips`
  - `test_unbound_due_onstage_intent_slips_only_at_band_end`
  - `test_plot_responds_without_next_plan_is_pending_not_escalated`：in-tick 与 HTTP 两条路径都测。
  - `test_accept_with_obligation_without_next_plan_records_obligation_only`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现，迁移 P3 中相应的测试**（例如 `NO_UNACCEPTED_PLAN` 的期望），保留原测试要证明的行为
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_tick_service.py tests/test_world_tick_steps.py tests/test_world_conflict_resolution.py tests/test_historical_rewrite_world_ticks.py tests/test_world_tick_multi_genre.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): read milestones and plan bindings from the planner`

---

### Task 9: start-writing、run control 与工作集

**Files:**
- Modify:
  - `forwin/genesis/handoff/service.py`：第 5 步改为节拍表，不再物化章节计划
  - `forwin/application/projects/genesis.py`：`requested_chapters` = 卷 1 章数
  - `forwin/generation/pipeline_core/run_control.py`：`continue_project` 不再要求有计划；删除 `_materialize_next_genesis_arc_if_needed`
  - `forwin/generation/pipeline_core/project_chapters.py`：章节列表按卷范围计算；每章前 `ensure_plan_for_chapter`
  - `forwin/generation/continue_workset.py`：按卷范围计算；删除 `future_arc_materialization_required`
  - `forwin/generation/auto_continue.py`
  - `forwin/application/projects/{generation,reviews}.py`
  - `forwin/application/read_models/generation.py`：`plan_state` 与可恢复判定
  - `forwin/production/repository.py`
  - `forwin/application/projects/generation.py::extend_project_generation`：只加卷骨架，激活时再生成节拍表
- Create: `forwin/planning/arc_progression.py`
- Test: `tests/test_continue_generation_workset.py`、`tests/test_genesis_handoff_service.py`、`tests/test_generation_auto_continue.py`、`tests/test_generation_control_next_arc.py`、`tests/test_project_operation_guards.py`、`tests/test_run_control_band_planning.py`（新建）

**Interfaces:**
- Consumes：Task 3、Task 4、Task 6。
- Produces：
  - `ArcProgressionService(beat_sheets: ArcBeatSheetService)`：`.activate_next_arc_if_needed(session, *, project_id, after_chapter: int, policy) -> ArcPlanVersion | None`。
    - 当 `after_chapter == active.chapter_end` 时：当前卷标 completed，`chapter_start` 最小的 planned 卷标 active。
    - 然后 `ensure_for_arc` 生成节拍表并提交 protection。
    - 下一卷第一个 band 的计划不在这里规划，交给 plan 步骤或 run control 的 `ensure_plan_for_chapter`。
  - start-writing 第 5–6 步：
    - 只调用 `ensure_for_arc(arc 1)`，含 protection 提交。
    - 不创建 ChapterPlan，`active_chapter_plan_count` 改名为 `active_arc_chapter_count`，值为卷 1 章数。
  - `build_continue_generation_workset(session, project_id, *, max_chapters)` 返回 `ContinueWorkset(chapter_numbers, reason)`：
    - 从第一个未 accepted 的章号开始，连续取到 `min(target_total_chapters, 现有卷的最大 chapter_end)`，长度不超过 `max_chapters`。
    - 计划行是否存在不影响结果。
    - 原有的 blocker（needs_review、drafted）保持不变。
    - reason 取值：`project_completed`、`pending_review_blocker`、`pending_acceptance_blocker`、`chapters_pending`。
  - run control 每章前依次执行：
    1. 冲突阻塞检查（P3）。
    2. 若该章不在 active 卷内：`activate_next_arc_if_needed(after_chapter=该章-1)`。
    3. `ensure_plan_for_chapter`。返回 None 时，以 `paused_for_review` 结束，阻塞原因来自 Task 6 建的冲突，即 P3 的 `world_conflict_pending` 派生。
    4. 原有的写前检查。
  - read model：没有计划但仍有未写章节时，`plan_state` 为 `"pending_band_plan"`，`can_resume=True`。

- [ ] **Step 1: 写失败测试**
  - `test_start_writing_creates_beat_sheet_and_no_chapter_plans`
  - `test_workset_counts_unwritten_chapters_without_plan_rows`
  - `test_run_control_plans_band_before_first_chapter`
  - `test_run_pauses_before_unplannable_chapter`
  - `test_run_control_activates_next_arc_when_plan_step_could_not`
  - `test_extend_generation_adds_arc_and_beat_sheet_on_activation`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现，并改写 `future_arc_materialization_required` 与 `launch_arc` 相关测试的期望**，保留各测试原本证明的阻塞与恢复行为
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_run_control_band_planning.py tests/test_continue_generation_workset.py tests/test_genesis_handoff_service.py tests/test_generation_auto_continue.py tests/test_generation_control_next_arc.py tests/test_project_operation_guards.py tests/test_handoff_atomicity.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(generation): plan bands just in time and activate arcs from run control`

---

### Task 10: post-Canon `plan` 步骤与步骤顺序 `("world", "plan", "feedback")`

**Files:**
- Create: `forwin/planning/plan_step.py`
- Modify:
  - `forwin/maintenance/events.py`：新元组
  - `forwin/maintenance/post_canon.py`：注册 plan 步骤；删除 `_run_planning_step` 与 `_run_arc_step` 的调用（模块由 Task 16、17a 删除）
  - `forwin/maintenance/trace_upload.py`：`Literal`
  - `forwin/http/adapters/api_maintenance_routes.py`
  - `forwin/runtime/container.py`
  - 固定了步骤名的测试：`tests/test_post_canon_maintenance.py`、`tests/test_post_canon_maintenance_api.py`、`tests/test_post_canon_trace_upload.py`、`tests/test_world_tick_service.py`、`tests/test_canon_atomic_transaction.py`、`tests/test_historical_rewrite_world_ticks.py`
- Test: `tests/test_plan_step.py`（新建）

**Interfaces:**
- Consumes：Task 6、Task 7、Task 9。
- Produces：
  - `PlanStepService(band_planner, feasibility, arc_progression)`：`.run(session, commit: CanonCommitRecord, *, policy) -> dict`。设第 N 章入 canon，依次：
    1. 若 N 是卷末：`activate_next_arc_if_needed`。
    2. 对 N 所在 band 的剩余未写章节做可行性审计，按 Task 7 的规则自动重规划或建冲突。
    3. 若 N 是 band 末：规划下一个 band（本卷下一个；卷末时为新卷第一个）。规划失败不抛异常，记入 `band_planning_deferred`。
  - result_json 为 `{feasibility_issue_count, replanned_band_ids, conflict_ids, planned_band_id, activated_arc_id, band_planning_deferred}`。
  - `POST_CANON_STEP_NAMES = ("world", "plan", "feedback")`。world 步骤内容不变：tick 加 WorldSimulator，WorldSimulator 由 Task 14 删除。

- [ ] **Step 1: 写失败测试**
  - `test_steps_are_world_plan_feedback`
  - `test_plan_step_replans_infeasible_remaining_chapters`
  - `test_plan_step_creates_conflict_after_replan_cap`
  - `test_band_end_plans_next_band`
  - `test_arc_end_activates_next_arc_and_plans_first_band`
  - `test_plan_step_llm_outage_defers_without_failing_step`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现，迁移固定步骤名的测试**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_plan_step.py tests/test_post_canon_maintenance.py tests/test_post_canon_maintenance_api.py tests/test_post_canon_trace_upload.py tests/test_world_tick_service.py tests/test_canon_atomic_transaction.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(maintenance): run the plan step after the world tick`

---

### Task 11: POV 世界视图（`ChapterWorldView`）

**Files:**
- Create: `forwin/world_view/{__init__,types,builder}.py`
- Test: `tests/test_chapter_world_view.py`（新建）

**Interfaces:**
- Consumes：
  - Task 2 的 ChapterPlan 新列。
  - `CognitionView`。
  - `estimate_travel`；`MapGraph`，按 `is_writer_visible_map_edge` 预先过滤。
  - `earliest_reveal_chapters`。
  - `StoryCalendarRepository` 与 `format_story_time`。
- Produces：
  - `ChapterWorldView`（pydantic），字段：
    - `pov_ref`、`pov_name`
    - `onstage: list[OnstageEntity{ref, name, location_name, alive_status, restraint, health, held_items, public_identity}]`
    - `pov_knowledge: list[KnownFact{fact_ref, proposition, state, pov_believes_false: bool, pov_only_secret: bool}]`
    - `reader_known: list[str]`：fact_ref 与 proposition
    - `allowed_reveals: list[{directive_id, fact_ref, proposition, target_state}]`
    - `hints: list[{directive_id, hint_text}]`
    - `forbidden_topics: list[{fact_ref, topic}]`
    - `locations: list[{id, name, minutes_from_pov}]`
    - `pov_location`
    - `now_label`、`expected_span_label`
    - `pov_clocks: list[{label, remaining_label}]`
    - `trimmed: list[str]`
  - `build_chapter_world_view(session, *, project_id, chapter_plan: ChapterPlan, budget_chars: int, runtime=None) -> ChapterWorldView`，加载 as-of `chapter_plan.chapter_number - 1`。各部分的规则：
    - **pov_knowledge**：POV 已记录的认知，外加 `truth_value=false` 的误信（标记 `pov_believes_false`）。若 POV 知道某个秘密，而读者按揭示时间表还未到，标记 `pov_only_secret`。
    - **reader_known**：只取已记录的 reader 认知，与本章 onstage、location ref 相关的事实。
    - **forbidden_topics**：同时满足三个条件的秘密：读者没有记录为 knows/believes、揭示时间未到或没有排期、POV 也不知道。
      - 话题文本由 `forwin/writer/prompt_core/world_view.py` 中的模板加上 `related_refs` 实体名生成，没有可用名字时用 fact id。
      - 不含 proposition。
    - **locations**：本章 `location_refs`、POV 当前位置，以及从当前位置出发最近的 `VIEW_NEARBY_LOCATIONS = 8` 个可达节点，耗时按 POV 的出行方式计算。
    - **pov_clocks**：公开 clock，以及 POV 在知情者名单（`owner_ref` 的 agenda `secrecy_refs`）中的秘密 clock。
  - 裁剪：
    - 不可裁剪的部分：`allowed_reveals`、`hints`、`forbidden_topics`、`pov_location`。
    - 其余按 `reader_known` → `locations`（只保留本章地点）→ `pov_knowledge`（保留秘密与误信）→ `onstage` 的 `held_items` 的顺序裁剪，直到渲染后的字符数不超过预算；`trimmed` 记录裁剪了什么。

- [ ] **Step 1: 写失败测试**
  - `test_view_lists_onstage_perceptible_state`
  - `test_view_marks_false_belief_and_pov_only_secret`
  - `test_forbidden_topic_has_no_truth_text`
  - `test_reveal_directive_gives_truth_for_that_fact_only`
  - `test_reader_known_uses_recorded_states_only`
  - `test_view_map_excludes_hidden_edges_for_pov_without_overlay`
  - `test_view_trims_to_budget_but_keeps_untrimmable_parts`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**（渲染函数在 Task 12，本任务的预算估算可以用一个临时的长度函数，Task 12 替换为真实渲染）
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_chapter_world_view.py tests/test_no_story_content_in_runtime.py tests/test_architecture_boundaries.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_view): build the POV world view for a planned chapter`

---

### Task 12: writer 上下文改用 POV 世界视图

**Files:**
- Create: `forwin/context/providers/world_view_provider.py`、`forwin/writer/prompt_core/world_view.py`（`_world_view_section`、话题模板）
- Modify:
  - `forwin/context/assembler_core/assembler.py`：provider 链在 book_state 之后加入 world_view；`ChapterContextPack.world_view`
  - `forwin/protocol/context.py`
  - `forwin/writer/prompt_core/builders.py`：5 个 builder
  - `forwin/writer/prompt_core/sections.py`
  - `forwin/writer/chapter_writer.py`：`<<FORWIN_LOCATION>>`
  - `forwin/protocol/scene.py`
  - `forwin/context/providers/personality_provider.py`：只取 onstage
  - `forwin/checker/hard_floor.py`：删除 `must_not_reveal` 检查
  - `tests/fixtures/prompt_regression/**`、`tests/test_prompt_revision.py`：快照
  - `tests/overfit_allowlist.toml`
- Test: `tests/test_writer_world_view_prompt.py`（新建）、`tests/test_writer_prompt_contract.py`、`tests/test_writer_split_pipeline.py`、`tests/test_context_provider_chain.py`、`tests/test_hard_floor.py`

**Interfaces:**
- Consumes：Task 11 的 `build_chapter_world_view` 与 `ChapterWorldView`；预算 `policy.writer.world_view_budget_chars`。
- Produces：
  - 5 个 builder 都渲染 `_world_view_section`：scene breakdown / generation / stitch、single、preview。
  - 5 个 builder 都不再渲染这些段：`_world_intent_section`、`_world_pressure_section`、`_world_model_section`、`_arc_envelope_section`、`_map_runtime_section`、`_timeline_section`。
  - 删除时机：
    - `_world_model_section`、`_map_runtime_section`、`_timeline_section` 在本任务删除；review 仍在使用的 `KnowledgeContextQuery` 保留。
    - `_world_pressure_section` 由 Task 14 删除，`_world_intent_section` 由 Task 15 删除，`_arc_envelope_section` 由 Task 17a 删除。
  - “当前允许命名角色”改用视图中的 onstage 与 POV 已知实体，不再打印隐藏实体的 description。
  - “人物准入规则”的出场名单改为本章 `onstage_refs`。
  - 本章计划段增加 `hook` 与当前 phase 的 purpose（来自节拍表）。
  - single prompt 删除第二份 canon 质量段（`builders.py:159`），保留第一份。
  - scene 输出：
    - `<<FORWIN_LOCATION>>` 的说明改为“填地点表中的 id，新地点写 `new:<名称>`”。
    - `SceneOutput.scene_location_id` 校验为视图地点 id 或 `new:` 前缀，不合法时保留原文，并记一个 writer 警告。
    - 汇总后的 `scene_location_hints: list[str]` 放在 WriterOutput 上，供 Task 13 使用。
  - context 体积：`world_view` 不计入 broker 的 `context_budget_chars` 裁剪，它有自己的预算。

- [ ] **Step 1: 写失败测试**
  - `test_no_builder_leaks_unpermitted_secret_truth`：参数化覆盖 5 个 builder。一个禁止话题秘密的 proposition 不出现，话题文本出现；揭示指令对应的事实出现。
  - `test_removed_world_sections_absent_from_all_builders`
  - `test_single_prompt_renders_canon_quality_once`
  - `test_scene_location_marker_lists_view_location_ids`
  - `test_personality_section_only_onstage`
  - 改写 `tests/test_hard_floor.py`：不再有 `must_not_reveal` 失败。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现，更新快照**（只有确实发生的变化才更新快照哈希，在报告中逐个说明原因）
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_writer_world_view_prompt.py tests/test_writer_prompt_contract.py tests/test_writer_split_pipeline.py tests/test_prompt_revision.py tests/test_context_provider_chain.py tests/test_hard_floor.py tests/test_large_module_boundaries.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(writer): give the writer the POV world view instead of world contracts`

---

### Task 13: 抽取与审查读取新的计划字段

**Files:**
- Modify:
  - `forwin/book_state/extraction/chapter_input.py`：fulfillment target 增加 `bind:<n>`、`reveal:<id>`、`hint:<id>`；`scene_location_hints`
  - `forwin/writer/prompt_core/chapter_delta.py`
  - `forwin/book_state/extraction/chapter_delta_extractor.py`：`required_targets` 包含 `bind:`；缓存键包含计划指纹
  - `forwin/review/plan_reviewer.py`：检查 goal 与 bind
  - `forwin/book_state/extraction/world_review.py` 与 `forwin/book_state/reviewer.py`：读本章 `reveal_directives`，豁免 `premature_reveal`
  - `forwin/candidate_drafts.py`、`forwin/canon_quality/cache.py`：计划指纹加入新列
  - `tests/overfit_allowlist.toml`
- Test: `tests/test_chapter_delta_contract.py`、`tests/test_chapter_delta_extractor.py`、`tests/test_world_gate_v6.py`、`tests/test_plan_reviewer_bindings.py`（新建）

**Interfaces:**
- Consumes：Task 2 的列；Task 12 的 `WriterOutput.scene_location_hints`。
- Produces：
  - `plan_fingerprint(plan: ChapterPlan) -> str`，放在 `forwin/planning/band_planner/persistence.py`。哈希的内容是 goals、`experience_plan_json` 与 Task 2 的全部新列。`candidate_plan_revision`、canon_quality 缓存键与抽取缓存键都改用它。
  - fulfillment target：
    - `bind:<index>`：必需。
    - `reveal:<directive_id>` 与 `hint:<directive_id>`：可选，只做记录。
  - 抽取 prompt 中列出 `scene_location_hints`，标注为“写作者声明的场景地点，只辅助定位，不是证据”。
  - 审查门：本章计划有 `reveal_directives{fact_ref, observer="reader"}` 时，该事实的 `premature_reveal` 不报 error；`contradicts_hidden_fact` 不受影响。
  - `PlanContractReviewer`：`bind:<n>` 为 missing 时，产生 blocking issue `plan_binding_missing`。

- [ ] **Step 1: 写失败测试**
  - `test_extraction_targets_include_bindings_and_directives`
  - `test_missing_binding_blocks_review`
  - `test_reveal_directive_exempts_premature_reveal`
  - `test_plan_fingerprint_changes_with_new_columns_and_invalidates_extraction_cache`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_chapter_delta_contract.py tests/test_chapter_delta_extractor.py tests/test_chapter_delta_ref_check.py tests/test_world_gate_v6.py tests/test_plan_reviewer_bindings.py tests/test_world_consistency_review.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(extraction): verify plan bindings and honour reveal directives`

---

### Task 14: 删除 WorldSimulator 与 world_pressure，迁出评论分析

**Files:**
- Create: `forwin/audience/comment_analysis.py`（从 `simulation/world.py` 迁入 `CommentAnalyzer`、`_KEYWORD_RULES`、`_keyword_fallback`、`_load_highlight_comments`、`load_recent_signals`、`aggregate_and_level_signals`，以及 `comment_analysis` stage key 与可选 LLM 超时）
- Delete: `forwin/simulation/`、`forwin/models/phase4.py`（`world_simulation_turns`）
- Modify:
  - `forwin/maintenance/post_canon.py`：world 步骤只运行 tick；feedback 步骤在聚合前调用评论分析
  - `forwin/runtime/{container,policy}.py`：删除 `planning.use_llm_simulation`
  - world_pressure 的全部消费者（见研究笔记 writer §2.2）：
    - context providers：state、personality
    - `protocol/context.py`
    - writer `_world_pressure_section` 以及 `builders.py:317` 的提示
    - review
    - read models 与 api_schema
    - routing 中的 `world_pressure` stage key
    - `llm_eval/validators.py`
    - `scripts/minimax_longrun_prompt_probe.py`
  - `tests/overfit_allowlist.toml`：`_KEYWORD_RULES` 改为在新路径下登记的 `language_lexicon`，删除 pending_removal 条目
  - `forwin/migrations/versions/0006_planning_v6.py`：drop `world_simulation_turns`
  - `.env.example` 与文档中 `FORWIN_OPTIONAL_PHASE4_LLM_TIMEOUT_SECONDS` 的说明
- Test: `tests/test_audience_feedback_alignment.py`、`tests/test_phase4_optional_timeout.py`（改为测 `audience/comment_analysis.py`）、相关测试

**Interfaces:**
- Produces：
  - world 步骤的 result_json 恰好是 tick 的 `step_result()`。
  - `run_feedback_aggregation_pass` 之前，先对新评论做分析，与原来 `WorldSimulator` 中 `analyze_missing=True` 的行为相同。
  - 全仓 `rg -n "WorldSimulator|world_pressure|WorldPressureView|world_simulation_turns|use_llm_simulation" forwin tests` 只剩迁移 0001 与 0006 中的命中。

- [ ] **Step 1: 写失败测试**
  - `test_feedback_step_analyzes_new_comments_before_aggregation`
  - `test_world_step_result_is_tick_result_only`
  - 在 `tests/test_architecture_boundaries.py` 中加入 `forwin.simulation` 禁止导入的检查。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现并迁移测试**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_audience_feedback_alignment.py tests/test_post_canon_maintenance.py tests/test_world_tick_service.py tests/test_architecture_boundaries.py tests/test_runtime_policy.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`refactor: delete the world simulator and keep comment analysis in the feedback step`

---

### Task 15: 删除 world contracts 与 broker 的世界模型包

**Files:**
- Delete: `forwin/planning/world_contracts.py`、`forwin/planning/world_contract_service.py`、`forwin/models/world_contract.py`
- Modify:
  - `forwin/planning/{service,arc_envelope,band_plan_service}.py` 中的调用
  - `forwin/context/providers/experience_provider.py`
  - `forwin/context/assembler_core/assembler.py`
  - `forwin/protocol/context.py`：world 字段、`WorldModelRetrievalPack` 及其子类、`knowledge_system_context`
  - `forwin/retrieval/broker_core/broker.py`：`build_world_model_pack`、`_load_reveal_ladder`、`_filter_writer_safe_world_context`、`_merge_writer_world_model_pack`、`_augment_v46_context`
  - `forwin/http/routes.py` 与 `forwin/http/adapters/api_llm_kb_routes.py`：删除 `GET /context-pack/{role}`
  - `forwin/llm_kb/compiler.py`：删除按角色打包
  - `forwin/writer/prompt_core/sections.py`：删除 `_world_intent_section`
  - `forwin/models/__init__.py`
  - `0006_planning_v6.py`：drop 3 张 contract 表
- Test: 删除 `tests/test_world_contract_service.py`、`tests/test_planning_world_contracts.py`；改写 `tests/test_knowledge_system_v46.py` 中的打包测试、`tests/test_retrieval_typed_budget.py`、`tests/test_architecture_boundaries.py`

**Interfaces:**
- Produces：
  - broker 只负责检索记忆、裁剪和挑选。
  - 人物性格段只用 provider 的版本（Task 12 已经限为 onstage）。
  - `rg -n "world_contract|WorldContract|build_world_model_pack|WorldModelRetrievalPack|knowledge_system_context|context-pack" forwin tests` 只剩迁移中的命中。
  - 边界测试把这 3 个模块与 3 张表加入禁止列表。

- [ ] **Step 1: 写失败测试**：边界测试新增 `test_world_contract_modules_and_tables_are_gone`、`test_broker_has_no_world_model_pack`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现并迁移测试**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_knowledge_system_v46.py tests/test_retrieval_typed_budget.py tests/test_context_provider_chain.py tests/test_lazy_external_indexes.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`refactor: delete world contracts and the broker world-model pack`

---

### Task 16: 删除重规划器与宏观进度

**Files:**
- Delete: `forwin/planning/stage_analysis.py`、`forwin/planning/macro_progression.py`、`forwin/planning/future_plan_audit/macro_progression.py`、`forwin/book_state/macro_status.py`
- Modify:
  - `forwin/planning/future_plan_audit/apply.py`：删除 macro 钩子
  - `forwin/maintenance/post_canon.py`：删除 stage_analyzer 等构造参数
  - `forwin/runtime/container.py`
  - `forwin/models/phase.py`：删除 `ProjectStageAnalysis`、`ProjectReplanEvent`
  - read models：`state/query_helpers.py`、`application/read_models/{runtime_maps,project_detail,project_summary,generation}.py`
  - `forwin/api_schema/{project,tasks}.py`
  - `forwin/ui_assets/home/{app_task_control,app_state,app_task_progress}.js`：replan / pacing 字段
  - `tests/browser/fixtures.py`
  - `0006_planning_v6.py`：drop `project_stage_analyses`、`project_replan_events` 和 `arc_plan_versions.macro_progression_json`
- Test: 删除 `tests/test_arc_macro_progression.py`、`tests/test_book_state_macro_status.py`、`tests/test_future_plan_macro_progression_audit.py`；改写 `tests/test_generation_control_payload.py`、`tests/test_generation_control_next_arc.py` 和相关浏览器测试

**Interfaces:**
- Produces：
  - `latest_stage` 只取任务阶段。
  - `rg -n "StageAnalyzer|PacingStrategist|ReplanGovernor|ProjectStageAnalysis|ProjectReplanEvent|macro_progression|ProtagonistMacroStatus|chapters_until_replan" forwin tests` 只剩迁移中的命中。

- [ ] **Step 1: 写失败测试**：边界测试 `test_replanner_and_macro_progression_are_gone`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现并迁移测试**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_generation_control_payload.py tests/test_generation_control_next_arc.py tests/test_future_plan_auditor.py tests/browser/test_task_center_drawer.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`refactor: delete the stage replanner and macro progression`

---

### Task 17a: 删除 ArcDirector、envelope、结构草案、`launch_arc_N` 与 legacy 种子路径

**Files:**
- Delete:
  - `forwin/director/`
  - `forwin/planning/{arc_envelope,arc_envelope_resolver,arc_structure_service,band_window}.py`
  - `forwin/planning/band_plan_service.py`：被 Task 6 取代
  - `forwin/genesis/planning.py` 中的 `_plan_arc_chapters` 以及 `chapter_materializer` 的章节生成部分；`build_arc_activation_review_pack` 保留，供节拍表使用
  - `forwin/genesis/materialize.py::promote_next_arc_if_needed`
  - `RunControlStage.run` 与 `finalization._seed_state`
- Modify:
  - `forwin/planning/service.py`：facade 只保留查询与新组件
  - `forwin/runtime/{container,services}.py`
  - `forwin/generation/pipeline.py`
  - `forwin/experience/*`：删除 `ArcStructureDraftData`，改用 `BeatSheetExperienceInput`；`ArcExperiencePlanningService` 的 CJK fallback 一并删除
  - `forwin/application/project_control/operations.py:948` 与 `forwin/generation/pipeline_core/repair_patches.py:219`：改为从节拍表派生
  - `forwin/state/repo.py`：`get_reader_promise` 与 `get_arc_payoff_map` 改读节拍表；删除 envelope 与结构草案的 getter
  - `forwin/http/runtime.py`：删除 envelope backfill
  - `forwin/writer/llm/routing.py`：删除 `launch_arc*`、`arc_plan`、`arc_envelope_analysis`、`subworld_delta`、`world_scaffold`、`chapter_plan` 这些 stage
  - `forwin/models/phase.py`：删除 envelope、结构、promotion 模型
  - `forwin/llm_eval/runner.py`：改用 Genesis fixture 加 `continue_project`
  - writer `_arc_envelope_section`
  - `0006_planning_v6.py`：drop `arc_envelopes`、`arc_envelope_analyses`、`arc_structure_drafts`、`provisional_promotion_records`
- Create: `tests/pipeline_harness.py`，用 Genesis 种子与 `continue_project` 驱动一章，取代 `pipeline.run(...)`
- Test: 迁移 `tests/test_canon_repair_stage.py`、`tests/test_arc_execution_scoping.py`、`tests/test_writer_attention_fallback.py`、`tests/test_http_app_factory.py` 到新 harness；删除 `tests/test_arc_envelope_resolver.py`、`tests/test_arc_structure_service.py`、`tests/test_runtime_story_free_defaults.py` 中 ArcDirector 的部分

**Interfaces:**
- Produces：
  - `rg -n "ArcDirector|ArcEnvelope|ArcStructureDraft|analyze_arc_envelope|draft_arc_structure|launch_arc|BandWindowResolver|_seed_state|plan_arc\b|RunControlStage.run\b" forwin tests` 只剩迁移中的命中。
  - 每个仍然存在的 LLM 调用点都保存 PromptTrace（边界测试扫描 `stage_key=` 字面量，对照登记表）。

- [ ] **Step 1: 写失败测试**：边界测试 `test_legacy_planner_modules_and_tables_are_gone`；`tests/pipeline_harness.py` 的冒烟测试 `test_harness_runs_one_chapter_from_genesis_seed`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现并迁移测试**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_canon_repair_stage.py tests/test_arc_execution_scoping.py tests/test_writer_attention_fallback.py tests/test_http_app_factory.py tests/test_llm_eval_core.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`refactor: delete the arc director, envelope and full-arc chapter generation`

---

### Task 17b: 删除 subworld 角色路径与 roster

**Files:**
- Delete: `forwin/subworld_manager.py`；保留 `SubWorld` 地图容器与 `project_scoped_subworld_id`
- Modify:
  - `forwin/canon/entity_admission.py`：删除 roster 绑定
  - `forwin/state/{updater,repo}.py`：删除 roster 读写；`get_active_subworld_summary` 与 `get_active_subworld_region_drafts` 按取舍第 16 条改为从计划地点派生
  - `forwin/application/read_models/project_detail.py`
  - `forwin/protocol/subworld.py`
  - `forwin/api_schema/world.py`：删除 `roster_item_id`
  - `forwin/characters/{models,creation,identity,registry}.py`
  - `forwin/models/{subworld,book_state}.py`：删除 roster 模型与 `roster_item_ids_json`
  - `forwin/review/repair/local_rewrite_executor.py`
  - `forwin/canon_quality/readability.py`
  - `tests/overfit_allowlist.toml`：删除 `_window_overlaps` 条目
  - `0006_planning_v6.py`：drop `sub_world_roster_items` 与 `character_identity_map.roster_item_ids_json`
- Test: 删除 `tests/test_subworld_control.py` 中依赖 roster 的部分，保留并改写其中仍然成立的上下文断言；改写 `tests/test_canon_atomic_transaction.py`（roster 夹具）和 `tests/test_character_creation_helper.py`

**Interfaces:**
- Produces：
  - `rg -n "SubWorldManager|SubWorldRosterItem|sub_world_roster|roster_item|plan_band_activation|ensure_initial_registry|plan_subworld_delta|apply_arc_delta" forwin tests` 只剩迁移中的命中。
  - 写作上下文的 active_subworlds 由本章地点派生（`test_active_subworlds_follow_plan_locations`）。

- [ ] **Step 1: 写失败测试**：`test_active_subworlds_follow_plan_locations`；边界测试 `test_subworld_role_paths_are_gone`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现并迁移测试**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_canon_atomic_transaction.py tests/test_character_creation_helper.py tests/test_context_provider_chain.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`refactor: delete the subworld roster and character slot paths`

---

### Task 18: 删除其它死代码，补齐边界测试

**Files:**
- Delete:
  - `forwin/context/gates/recency_truncate.py`，以及 `PlanningPolicy.context_recency_window`（Task 1 已经让加载时丢弃这个键）
  - `CodexGovernedActionProcessor`，包括其模块与测试
- Modify:
  - `DraftReviewService` 中未使用的 reviewer 字段
  - `ChapterContextPack` 中从未赋值的字段：`reader_feedback`、`character_cognition_states`、`recent_*`、`promise_debts`。`active_narrative_obligations` 与 `future_plan_audit_summary` 被 ReviewContextPack 回退读取，先确认，再只删除确实无人读的
  - `tests/test_architecture_boundaries.py`：把 P4a 删除的全部模块和表登记到禁止列表
- Test: `tests/test_architecture_boundaries.py`、`tests/test_context_provider_chain.py`、`tests/test_draft_review_service*.py`

**Interfaces:**
- Produces：`rg -n "RecencyTruncateGate|context_recency_window|CodexGovernedActionProcessor" forwin tests` 无命中。

- [ ] **Step 1: 写失败测试**：边界测试 `test_p4a_deleted_modules_and_tables_stay_gone`，列出 Task 14–18 的全部模块和表
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_context_provider_chain.py tests/test_runtime_policy.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`refactor: delete unwired gates and context fields`

---

### Task 19: 多题材端到端（设计 §7.4）

**Files:**
- Modify: `tests/fixtures/world_seed/{xianxia,interstellar,urban_mystery}.json`：增加 `beat_sheet` 与 `band_plan` 段，内容是桩 LLM 输出；必要时修改 `world_tick` 段
- Create: `tests/test_planning_multi_genre.py`

**Interfaces:**
- 每本书都走同一条路径：
  1. world_seed，种子提交；
  2. 节拍表与 protection；
  3. band 规划，可行性通过；
  4. 写第 1 章：桩 writer，然后抽取、翻译、审查门；
  5. tick；
  6. plan 步骤：剩余章节的审计；band 末规划下一个 band。

- [ ] **Step 1: 写测试（参数化三个夹具，复用 `tests/test_world_tick_multi_genre.py` 与 Task 17a 的 harness）**
  - `test_start_writing_to_first_band_plan_in_three_genres`：节拍表、protection 节点、band 计划的新列都写入了。
  - `test_writer_prompt_uses_world_view_and_hides_unpermitted_secrets`
  - `test_world_change_triggers_feasibility_replan`：tick 把 onstage 角色移到很远的地方 → plan 步骤重规划。
  - `test_three_genres_share_planning_pipeline`：用 spy 包住 `ArcBeatSheetService.ensure_for_arc`、`BandPlanner.plan_band`、`FeasibilityAuditor.audit_plans`、`build_chapter_world_view`，三本书的调用序列相同。
- [ ] **Step 2: 运行，确认通过。** 只增加测试与夹具；若失败，回到对应任务修实现，不修改夹具去迁就。`.venv/bin/python -m pytest -q tests/test_planning_multi_genre.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 3: 全量测试并提交**：`test: plan, write and tick the first band in three genres`

---

### Task 20: P4a 收尾文档

**Files:**
- Modify:
  - `tests/overfit_allowlist.toml`：`meta.completed_phase = "P4"`，`pending_removal` 区段必须为空
  - `Design-docs/CURRENT_ARCHITECTURE.md`、`CURRENT_DESIGN.md`、`DESIGN_STATUS.md`、`writing_flow_state_machine.md`
  - `forwin/book_state/README.md`、`docs/codex-forwin-mcp.md`
  - `plugins/forwin-operator/skills/forwin-operator/SKILL.md`
  - `tests/test_design_status_docs.py`：P3 的步骤元组断言改为新元组，新增 P4 标记断言
- Test: `tests/test_design_status_docs.py`、`tests/test_no_story_content_in_runtime.py`、`tests/test_architecture_boundaries.py`

- [ ] **Step 1: 设置 `completed_phase = "P4"`，运行护栏测试。** `pending_removal` 不为空就回到对应任务处理。
- [ ] **Step 2: 更新文档**，写明以下内容，并保留并新增文档测试要求的标记（`arc_beat_sheets`、`band_plans`、`plan.arc_beats`、`plan.band`、`("world", "plan", "feedback")`、`ChapterWorldView`、`0006_planning_v6`）：
  - 节拍表与 protection；
  - band 即时规划与重规划规则；
  - 可行性审计的检查项与冲突；
  - POV 世界视图；
  - 新的步骤顺序；
  - 删除清单；
  - 待回应的世界变化；
  - 取舍第 2、3、14、18 条；
  - P4b 的范围。
- [ ] **Step 3: 运行文档与边界测试，然后全量测试并提交**：`docs: close world engine overhaul P4a`

---

## 执行分工（沿用 P3 模式）

- Codex `gpt-6-astra`：Task 2、5、10、13、14、15、16（约三分之一）
- Codex `gpt-6.1-sol`：Task 1、18、20
- Claude opus：Task 3、4、6、7、8、9、11、12、17a、17b、19
- 依赖顺序：
  - 1 → 2 → 3 → 4、5 → 6 → 7 → 8、9 → 10。
  - 11 在 2 之后可以与 6–10 并行；12 在 11 之后；13 在 12 之后。
  - 14 在 1 之后、10 之前或之后都可以，因为它只改 world 步骤与 feedback 步骤。
  - 15 在 12 之后；16 在 10 之后；17a 在 9、10 之后；17b 在 6、12 之后；18 在 14–17b 之后。
  - 19 在 1–18 之后；20 最后。
