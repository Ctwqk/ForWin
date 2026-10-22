# 世界引擎大修 P3：世界引擎 tick、世界与计划的冲突审查 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 每章入 canon 之后运行一次世界 tick：确定性地结算时限和到点意图、传播公共消息，再让行动者按自己的认知提出新意图，经约束检查和世界审查门后提交为 `source_type="world_engine"` 的 GraphDelta；触及叙事边界或影响等级高的变化不直接提交，而是成为冲突，按影响等级交给代码、Codex（`gpt-6-astra`）或用户决定，用户决定时生成在下一章前暂停。

**Architecture:**
- 新包 `forwin/world_engine/`：
  - 契约（`types.py`、`conflict_types.py`）与共享的出行耗时估算（`travel.py`，从审查门抽出）。
  - 确定性步骤（`clocks.py`、`intents.py`、`propagation.py`、`constraints.py`）。
  - 两个 LLM 节点（`adjudicator.py` 的 `world.adjudicate`、`actor_turns.py` 的 `world.actor_turn`），提示词放在 `forwin/writer/prompt_core/world_engine.py`。
  - delta 构建（`delta_builder.py`）。
  - 编排（`tick.py`）。
- 冲突部分：
  - 存储：新表 `world_plan_conflicts`，迁移 `0005_world_plan_conflicts`。
  - 路由：`conflict_routing.py`，确定性。
  - Codex 审稿：`conflict_delegate.py`，复用 Spark 委托门的调用、模型校验与审计写入方式，抽成共享的 `run_codex_structured_call`。
  - 执行选项：`conflict_resolution.py`。
- canon 写入新增 `CanonAdmissionService.commit_world_tick`，与 `commit_world_edit` 同一模式。审查门 v6 对 `world_engine` delta 运行与章节抽取相同的移动、死亡、受限、物品持有检查。历史重写接受 tick delta，重放时对它做 CAS 校验，失败转为冲突。
- post-Canon 步骤改为 `("world", "planning", "arc", "feedback")`。world 步骤先跑 tick，再跑旧的 WorldSimulator（P4 删除）。run control 在每章开始前检查阻塞性冲突。冲突入口包括 HTTP、MCP（`world_conflict_list/get/resolve`）和控制台任务抽屉。
- LLM 路由：Codex 模型改由配置声明（`FORWIN_CODEX_MODELS`），取代子串判断。RuntimePolicy 新增 `world` 与 `models.role_preferences` 两节。
- 顺带完成 P2 推迟项：
  - Genesis 地图边按声明的端点物化；地图种子稳定。
  - 时限到期由 tick 推进；审稿表单只保护未到期的时限并设上限；秘密时限的描述不再带节点 id。

**Tech Stack:** Python 3.13（`.venv`）、SQLAlchemy、Alembic、PostgreSQL（测试库 `127.0.0.1:55432`）、pydantic、pytest、Playwright（浏览器测试）。

**Spec:** `docs/designs/2026-10-20-world-engine-overhaul.md`。重点读 §1.5、§1.6、§3、§6、§8、§9.3、§11 中的 P3。P1、P2 计划中的“与设计的取舍”继续有效。

## 与设计的取舍（实施者照此执行）

1. **post-Canon 步骤在 P3 是 `("world", "planning", "arc", "feedback")`。** world 移到最前，并且先跑 tick、再跑旧 `WorldSimulator`。设计的最终形态 `("world", "plan", "feedback")` 在 P4 随规划器重写落地。
2. **叙事边界只读现有数据。**
   - protection 存为 narrative `review_constraint` 节点，payload 为 `{ref, kind, until_chapter, source, observer_ref?, fact_ref?, field?, value?}`。tick 读取并遵守这些节点。
   - 节拍表与 band 计划这两个生产者在 P4 才有，所以 P3 中 protection 通常为空，测试直接种入。
   - 揭示时间表只约束读者，tick 从不写读者认知（第 9 条），因此 tick 不触发 reveal_schedule 冲突。
3. **critical 等级需要 P4 的卷里程碑与 exit_state。** P3 的影响分级函数接收一个里程碑引用集合；P3 传空集合，所以 critical 只在 P4 之后出现。用户依然可以在 P3 选择 `amend_plan`。
4. **onstage 意图的“规划没有安排”在 P3 的判定**：意图已经是 due，而本章提交时章节抽取的 `intent_interactions` 里没有它，就算顺延一次（`slip_count + 1`，`scheduled_at` 改为当前时间）。超过 `world.max_onstage_slips` 产生 `onstage_slip` 冲突。P4 的 band 规划器取代这一判定。
5. **`world.drift`（世界级变量）推迟。** BookState 里没有 institution / resource_economy 的世界变量节点。
6. **rule 的 `checkable` 谓词推迟。** Genesis 没有定义任何谓词 kind，P3 不求值，也不报错。
7. **资源持有只检查 item / resource 节点的 `state.holder_id`。** 行动者 `state.resources` 的自由文本只作为回合输入。
8. **在途消息存放在 tick delta 的 `metadata["news_in_transit"]`**，元素为 `{fact_ref, observer_ref, arrive_at}`。每次 tick 读取上一次 tick delta 的队列，投递已到达的，写回其余的，不新增节点类型。
9. **“对读者隐藏”靠构造保证。** world_engine delta 不得包含 `reader` 观察者的认知补丁（审查门 error `world_engine_reader_knowledge`）。读者认知只由正文抽取推进。
10. **P3 的冲突来源只有 `world_tick` 与 `historical_rewrite`。**
    - `chapter_extraction`、`plan_feasibility`、`band_planning` 三类来源在 P4 出现，因此 `rewrite_chapter` 选项在 P3 不可用。
    - `violated` 在设计的枚举之外增加两个值：`impact_review` 表示 major 及以上、虽未违反边界但必须审查；`replay_cas` 表示历史重写重放 tick delta 时 CAS 失败。
11. **选项在 P3 的执行方式**：
    - `veto_world`：意图标为 vetoed，约束注入该行动者的下一次回合，在 `world.max_rerolls` 之内让它下一次 tick 重新行动。
    - `accept_with_obligation`：提交变化，并经 `DeferAcceptanceTransaction` 记一条叙事义务。
    - `plot_responds`：提交变化，并给下一个未写章节的 ChapterPlan 追加一个回应目标；P4 的 band 规划器会读到它。
    - `amend_plan`：只能由用户选择。提交变化并解除相关 protection；P3 没有节拍表，改计划由用户手动完成。
12. **阻塞用派生的 `BlockingReasonCode` 表示**，值为 `world_conflict_pending`。任务和章节没有 reason 列：run control 以 `paused_for_review` 结束（任务状态为 needs_review），continue 与 review approve 的 409 守卫读这个 code。
13. **MCP `world_conflict_list` 改指新冲突。** 旧的 CanonQualitySignal 列表改名为 `world_model_signal_list`，HTTP 路由 `/world-model/conflicts` 保留，供 writer 上下文与 UI 使用。
14. **世界角色的路由。**
    - `world.actor_turn` 与 `world.adjudicate` 使用新的 `task_family="world_engine"`。它不在 Codex 家族中，所以默认走普通 profile 链（低成本优先，对应设计“风险”一节）。
    - `review.world_conflict` 用 `task_family="review"`，按 `models.role_preferences["review.world_conflict"]`（默认 `("gpt-6-astra",)`）经 Codex 通道调用。
15. **地图种子改为 `sha256(f"{project_id}|{subworld_id}")`**，并且每条 Genesis 地图边在声明的两个端点之间物化。生产中已建好的地图不会重建（`map/service.py` 跳过已存在的 subworld），只影响新书与测试。
16. **意图引出新的次要行动者（§3.5“经实体准入”）推迟到 P4。** P3 的 tick 只引用已有节点，新建的只有意图、agenda 与事实，不新建行动者。

## Global Constraints

- 运行代码只处理结构；故事内容只来自 Genesis、world_seed、BookState 或 LLM 输出；题材相关词只能来自本书的 lexicon。新增运行代码不得引入 CJK 词表（模块级集合、`re` 模式、`in` 成员判断、函数内字面量迭代）。提示词与面向写作的文本模板放在 `forwin/writer/prompt_core/**` 这类已登记为 `prompt_text` 的位置，条目数变化时同步 `tests/overfit_allowlist.toml` 的 `max_literals`。
- 已合入的迁移不得修改。`0005_world_plan_conflicts` 在 P3 合入前允许本期任务追加修改；revision id ≤ 32 字符；新 ORM 模型必须能在 SQLite 上 `create_all`。
- `BookStateCompiler(` 只能出现在 `forwin/canon/admission.py`；所有 canon 写入经 `CanonAdmissionService`。`forwin/world_engine/**` 不得直接写 world / map / cognition / narrative 表。
- 故事时间是自纪元起的整数分钟。地图边的 `travel_time` 单位是小时，换算成分钟时乘以 60。LLM 给出的时长只能用本书日历的单位或 lexicon 的别名，换算统一用 `forwin/book_state/story_clock.duration_to_minutes`。
- 每个结构补丁（不含 create）都必须带 `old_value` 与 `field_path`，否则历史重写无法回卷，CAS 也会被跳过。
- 新枚举值一律用英文 token。LLM 输出中的引用只能是输入里给出的 BookState 节点 id、地图节点 id 或 `fact:<id>`；不认识的引用带着错误重试一次，仍不认识就丢弃该候选。
- 每个 LLM 调用点都必须保存 PromptTrace 行（带 stage_key）和 LLM DecisionEvents，不得只写 outbox 产物。Codex 审稿的审计写入走独立事务（与 Spark 委托门相同）。
- 测试不调用真实 LLM，不连接生产库，不访问网络。Codex 通过伪造的 LLM 客户端模拟，包括 `last_call_result.backend == "codex_bridge"` 和 `actual_model`。
- 分支：从 `claude/world-engine-p2` 的当前 HEAD 新建 `claude/world-engine-p3`。本机没有 git 身份，每次提交都用 `git -c user.name=CASPER -c user.email=magi1@CASPERs-Mac-mini.local commit ...`，提交信息末尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`；提交信息中不得出现模型名。
- 测试命令：`.venv/bin/python -m pytest -q`。基线在建分支时测得并记入 ledger。每个任务结束时全量测试必须全部通过。
- 生产操作只在 Task 20 中执行，每一步经用户确认，并且必须在 P0、P1、P2 都部署之后。

## Review Focus

1. **行动者回合返回无效意图**：例如引用了输入里没有的地点，或者在 `scheduled_at` 之前从当前位置赶不到意图地点。这时应带着错误重试一次；仍无效就丢弃该意图，写 `WORLD_TICK_CANDIDATE_DROPPED` 事件，tick 照常提交其余变化。由 Task 13 的 `test_invalid_actor_intent_retries_then_drops_and_tick_commits_rest` 覆盖。
2. **一次 tick 什么都没有到点、也没有行动者轮到**：不提交空 delta，world 步骤照常成功，`result_json["graph_delta_ids"] == []`。由 Task 13 的 `test_quiet_tick_commits_nothing_and_succeeds` 覆盖。
3. **Codex 不可用**：包括关闭 Codex、超时、模型不符、返回不在选项内的选择。冲突应升级给用户，涉及的变化保持 held、不被提交；只有 `blocks_from_chapter` 不晚于下一章时才阻塞生成。由 Task 11 的 `test_delegate_failures_escalate_without_applying` 和 Task 13 的 `test_escalated_conflict_holds_change_and_blocks_next_chapter` 覆盖。
4. **运营者在 tick 之后重写更早的章节**：tick delta 原样重放；CAS 一致时重写成功；不一致时该 tick delta 不再生效，产生一条 `replay_cas` 冲突并阻塞后续生成，不崩溃，也不破坏其他 delta。由 Task 6 的 `test_rewrite_replays_tick_deltas_and_turns_cas_failure_into_conflict` 覆盖。
5. **同一冲突被决定两次**：例如 UI 与 MCP 同时提交，或 Codex 已决定后用户又提交。第二次应返回 409，变化只执行一次。由 Task 12 的 `test_resolving_twice_raises_and_applies_once` 和 Task 15 的 `test_resolve_route_returns_409_when_not_open` 覆盖。

---

### Task 1: 配置声明的 Codex 模型清单

**Files:**
- Create: `forwin/llm/codex_models.py`
- Modify: `forwin/config.py`（`_InfrastructureFields` 新增 `codex_models`，env `FORWIN_CODEX_MODELS`）、`forwin/llm/router.py:554-581`（`preferred_codex_requested`）、`forwin/llm/factory.py`、`forwin/writer/llm/routing.py:385-402`（`_profile_kind`）以及 `_profile_kind` 的调用点（`routing.py:101,103,205,283,298,412`、`writer/llm/profile_options.py:161`）、`.env.example`、`docs/codex-pro-runtime.md`
- Test: `tests/test_codex_model_registry.py`（新建）、`tests/test_codex_bridge.py`、`tests/test_llm_client_retry.py`

**Interfaces:**
- Produces：
  - `CODEX_MODELS_ENV = "FORWIN_CODEX_MODELS"`。
  - `is_codex_model(model: str, codex_models: Iterable[str]) -> bool`：去空白、不区分大小写的精确匹配，不做子串判断。
  - `InfrastructureConfig.codex_models: tuple[str, ...]`，默认 `(DEFAULT_CODEX_MODEL, "gpt-6-astra")`。env 为逗号分隔，用已有的 `_env_csv` 读取。
  - router 判定：`preferred_provider_kind ∈ {"codex", "codex_bridge", "spark"}`，或 `is_codex_model(preferred_model, codex_models)` 时视为请求 Codex。`codex_models` 由 `maybe_wrap_with_codex_router` 传入 `RoutedModelAdapter`。
  - profile：`profile.model` 在清单内时 `_profile_kind` 返回 `"spark"`。删除 `"codex-spark"` / `"gpt-5.3"` / `"codex"` 子串判断。

- [ ] **Step 1: 写失败测试（`tests/test_codex_model_registry.py`）**
  - `test_declared_model_routes_to_codex_bridge`：`codex_models=("gpt-6-astra",)`，`preferred_model="gpt-6-astra"`，Codex 已启用、`task_family="review"` → 走 bridge，并且 `codex_kwargs["model"] == "gpt-6-astra"`。
  - `test_undeclared_model_is_not_codex_even_with_codex_substring`：`preferred_model="custom-codex-like"` 不在清单内 → 走普通 adapter。
  - `test_profile_kind_spark_comes_from_registry`：profile 的 model 在清单内 → kind 为 `spark`；不在清单内 → 按原有规则。
  - `test_env_csv_parses_codex_models`：`FORWIN_CODEX_MODELS=" gpt-6-astra , gpt-5.3-codex-spark "` → `("gpt-6-astra", "gpt-5.3-codex-spark")`。
- [ ] **Step 2: 运行，确认失败**：`.venv/bin/python -m pytest -q tests/test_codex_model_registry.py` → FAIL
- [ ] **Step 3: 按 Interfaces 实现，迁移受影响的现有测试**（用 `rg -n "gpt-5.3|codex-spark" forwin tests` 复核，保留测试原本断言的行为）
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_codex_model_registry.py tests/test_codex_bridge.py tests/test_llm_client_retry.py tests/test_gate_delegation.py tests/test_config_env_resolution.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(llm): declare Codex models in config instead of matching name substrings`

---

### Task 2: RuntimePolicy 的 `world` 与 `models` 两节

**Files:**
- Modify: `forwin/runtime/policy.py`、`forwin/api_schema/policy.py`、`forwin/application/runtime_policy.py`、`forwin/mcp/http.py`、`forwin/mcp/client.py`、`forwin/mcp/models.py`（`ProjectView.world_conflict_delegate`）、`forwin/ui_assets/home/app_task_control.js`（运行策略表单）、`tests/browser/fixtures.py`（`sample_runtime_policy`）
- Test: `tests/test_runtime_policy.py`、`tests/test_project_policy_api.py`、`tests/test_mcp_server.py`、`tests/browser/test_project_control_and_chapters.py`

**Interfaces:**
- Produces：
  - 新增 `WorldPolicy(FrozenPolicyModel)`，字段与默认值：

    | 字段 | standard | pulp |
    | --- | --- | --- |
    | `enabled: bool` | True | True |
    | `max_actor_turns_per_tick: int` | 6 | 3 |
    | `actor_turn_batch_size: int` | 1 | 1 |
    | `max_new_intents_per_actor: int` | 2 | 2 |
    | `max_major_events_per_tick: int` | 1 | 1 |
    | `max_onstage_slips: int` | 2 | 2 |
    | `max_rerolls: int` | 2 | 2 |
    | `conflict_delegate: Literal["codex", "human"]` | `"codex"` | `"codex"` |

  - 新增 `ModelsPolicy(FrozenPolicyModel)`：`role_preferences: dict[str, tuple[str, ...]]`，默认 `{"review.world_conflict": ("gpt-6-astra",)}`。
  - `RuntimePolicy.world: WorldPolicy` 与 `RuntimePolicy.models: ModelsPolicy` 都用 `default_factory`，所以没有这两节的已存策略 JSON 和任务快照照常加载。`schema_version` 不变。
  - `role_models(policy: RuntimePolicy, role: str) -> tuple[str, ...]`：返回 `policy.models.role_preferences.get(role, ())`。
  - `with_user_settings(..., world_conflict_delegate: Literal["codex", "human"] | None = None)`：None 表示保持原值；修改 quality_profile 后重建时也保留用户设定的 `conflict_delegate`。
  - `RuntimePolicyUpdateRequest.world_conflict_delegate: Literal["codex", "human"] | None = None`。现有的 `project_set_gate_delegate` 不发送它，所以不会把它重置。
  - MCP 写工具 `project_set_world_conflict_delegate(project_id, delegate, reason)`，模式同 `project_set_gate_delegate`。描述中必须含 "Use this when"。
  - 控制台：运行策略卡片新增两个按钮的切换，id 为 `runtime_policy_world_conflict_codex` / `runtime_policy_world_conflict_human`，保存时随运行策略一起提交。

- [ ] **Step 1: 写失败测试**
  - `test_world_and_models_defaults_per_profile`：断言两个 profile 的上表数值，以及 `role_models(policy, "review.world_conflict") == ("gpt-6-astra",)`。
  - `test_stored_policy_without_world_section_loads_defaults`：用当前 HEAD 的 `model_dump_json()` 删掉 `world` 与 `models` 两键后加载。
  - `test_update_world_conflict_delegate_round_trip`（API）以及 `test_gate_delegate_update_keeps_world_conflict_delegate`。
  - `test_mcp_set_world_conflict_delegate`。
  - 浏览器：`test_runtime_policy_world_conflict_switch_saves`，断言捕获的 payload 中 `world_conflict_delegate == "human"`。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_runtime_policy.py tests/test_project_policy_api.py tests/test_project_policy_store.py tests/test_mcp_server.py tests/test_generation_task_payload.py tests/browser/test_project_control_and_chapters.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(policy): add world engine knobs, a conflict delegate setting and role model preferences`

---

### Task 3: 冲突存储：模型、迁移、仓储与决策事件

**Files:**
- Create: `forwin/world_engine/__init__.py`、`forwin/world_engine/conflict_types.py`、`forwin/world_engine/conflict_repository.py`、`forwin/models/world_conflict.py`、`forwin/migrations/versions/0005_world_plan_conflicts.py`
- Modify: `forwin/models/__init__.py`、`forwin/audit/events.py`、`tests/test_v5_live_migration.py`（`EXPECTED_REVISION`）、迁移文件列表测试
- Test: `tests/test_world_conflict_repository.py`（新建）、`tests/test_v5_recovery_schema.py`、`tests/review_decision/test_audit.py`

**Interfaces:**
- Produces：
  - `conflict_types.py`：
    - 字面量类型：`ConflictSource = Literal["world_tick", "historical_rewrite", "chapter_extraction", "plan_feasibility", "band_planning"]`；`ConflictViolation = Literal["protection", "milestone", "reveal_schedule", "feasibility", "onstage_slip", "impact_review", "replay_cas"]`；`ConflictOption = Literal["veto_world", "rewrite_chapter", "amend_plan", "plot_responds", "accept_with_obligation"]`；`ConflictDecider = Literal["auto", "codex", "human"]`。
    - `ConflictDraft(BaseModel)`：`source`、`detected_after_chapter: int`、`story_time: int`、`involved_refs: list[str]`、`violated`、`impact_level`（`IMPACT_LEVELS` 中的值）、`summary: str`、`proposed_change: dict`（tick 候选的 JSON）、`intent_ref: str = ""`、`actor_ref: str = ""`、`blocks_from_chapter: int`。
    - `ConflictDecision(BaseModel)`：`option`、`decided_by: Literal["auto", "codex", "human"]`、`actor_id: str = ""`、`rationale: str`、`decided_at: datetime`。
  - ORM `WorldPlanConflictRow`，表 `world_plan_conflicts`。列为 §6.1 全部字段，JSON 列以 `_json` 结尾，另加 `summary`、`intent_ref`、`actor_ref`、`created_at`、`updated_at`。索引为 `(project_id, status)` 与 `(project_id, blocks_from_chapter)`。
  - 迁移：`revision = "0005_world_plan_conflicts"`，`down_revision = "0004_drop_parallel_ledgers"`；`downgrade()` 删表。
  - `WorldConflictRepository(session)`，方法如下：
    - `create(draft: ConflictDraft, *, required_decider: ConflictDecider, options: Sequence[ConflictOption]) -> WorldPlanConflictRow`，status 为 open。
    - `get(project_id, conflict_id, *, for_update: bool = False) -> WorldPlanConflictRow | None`。
    - `list(project_id, *, status: str | None = None) -> list[WorldPlanConflictRow]`，按 created_at 倒序。
    - `open_blocking(project_id, *, next_chapter: int) -> WorldPlanConflictRow | None`：只取 status 为 open、`required_decider == "human"`、`blocks_from_chapter <= next_chapter` 的，按 `blocks_from_chapter`、`created_at` 排序取第一条。
    - `mark_resolved(row, *, decision: ConflictDecision, result_refs: Sequence[str]) -> None`：不是 open 时抛 `ConflictNotOpen`。
    - `escalate(row) -> None`：把 required_decider 改为 human。
    - `reroll_count(project_id, *, actor_ref: str) -> int`：该行动者已被 `veto_world` 决定过的冲突数。
  - `DecisionEventType` 新增：`WORLD_TICK_COMMITTED`、`WORLD_TICK_CANDIDATE_DROPPED`、`WORLD_CONFLICT_DETECTED`、`WORLD_CONFLICT_ESCALATED`、`WORLD_CONFLICT_RESOLVED`，同时登记到 `KNOWN_DECISION_EVENT_TYPES`。

- [ ] **Step 1: 写失败测试**
  - `test_create_list_and_open_blocking`
  - `test_open_blocking_ignores_auto_codex_and_later_chapters`
  - `test_mark_resolved_twice_raises_conflict_not_open`
  - `test_reroll_count_counts_veto_decisions_per_actor`
  - `test_world_conflict_table_matches_migration_0005`：放入 `test_v5_recovery_schema.py`，用 0001 / 0004 相同的方式对照 ORM 与迁移。
  - `test_world_event_types_registered`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_conflict_repository.py tests/test_v5_recovery_schema.py tests/test_v5_live_migration.py tests/review_decision/test_audit.py tests/test_architecture_boundaries.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): store world-plan conflicts and register world tick events`

---

### Task 4: 世界引擎契约与共享出行耗时

**Files:**
- Create: `forwin/world_engine/types.py`、`forwin/world_engine/travel.py`
- Modify: `forwin/book_state/reviewer.py:389-426`（`_route`、`_shortest_minutes` 改为调用 `estimate_travel`，行为不变）
- Test: `tests/test_world_engine_contracts.py`（新建）、`tests/test_world_gate_v6.py`

**Interfaces:**
- Produces（全部 pydantic，`extra="forbid"`）：
  - 常量 `WORLD_ENGINE_SOURCE_TYPE = "world_engine"`。
  - `WorldEffects` 的各个列表：
    - `entity_changes: list[EffectEntityChange{ref, field, new_value}]`
    - `relation_changes: list[EffectRelationChange{source_ref, target_ref, edge_type, change: Literal["create", "end", "set"], field: str = "", new_value: Any = None}]`
    - `movements: list[EffectMovement{entity_ref, to_location_id, travel_mode_id: str = "", depart_at: int, arrive_at: int}]`
    - `item_transfers: list[EffectItemTransfer{item_ref, from_ref, to_ref}]`
    - `events: list[EffectEvent{summary, participant_refs, location_id, visibility: Literal["public", "secret"], at: int}]`
    - `new_facts: list[EffectFact{key, proposition, truth_value: bool, sensitivity: Literal["public", "hidden", "secret"], known_by: list[str]}]`
    - `knowledge: list[EffectKnowledge{observer_ref, fact_ref, state, channel, source_ref: str = ""}]`

    `state` 与 `channel` 取自 `KNOWLEDGE_STATES` / `KNOWLEDGE_CHANNELS`。`fact_ref` 是 `fact:<id>`，或同一 effects 中 `new_facts` 的 `fact:new:<key>`。
  - LLM 输出契约：
    - `AdjudicatedOutcome{intent_id, result: Literal["resolved", "disrupted"], summary, effects: WorldEffects}`，`AdjudicationResult{outcomes: list[AdjudicatedOutcome]}`。
    - `ProposedIntent{agenda_ref, action_type（INTENT_ACTION_TYPES 中的值）, summary, target_refs, location_id, scheduled_in: Duration, requires_onstage: bool, impact_level（IMPACT_LEVELS 中的值）, expected_effects: WorldEffects}`；`Duration` 复用 `chapter_delta_schema.Duration`。
    - `AgendaChange{agenda_ref, status: str = "", priority: int | None = None, goal: str = "", motivation: str = ""}`：`agenda_ref="new"` 表示新建，此时 goal 必填。
    - `ActorTurnResult{actor_id, new_intents: list[ProposedIntent], agenda_changes: list[AgendaChange], next_turn_in: Duration}`。
  - tick 内部类型：
    - `TickContext{project_id, chapter_number, now: int, window_start: int, seed: int}`。
    - `TickCandidate{candidate_id, origin: Literal["clock", "intent_interaction", "adjudication", "onstage", "propagation", "actor_turn", "conflict_resolution"], actor_id: str = "", intent_id: str = "", summary, effects: WorldEffects, new_intents: list[ProposedIntent], agenda_changes: list[AgendaChange], intent_status_changes: dict[str, str], slip_updates: dict[str, int], next_turn_at: dict[str, int], impact_level: str = "minor"}`。
    - `NewsItem{fact_ref, observer_ref, arrive_at: int}`。
  - `tick_seed(project_id: str, chapter_number: int) -> int`：`int(sha256(f"{project_id}|{chapter_number}").hexdigest()[:12], 16) % 2_000_000_000`。
  - `travel.py`：
    - `TravelEstimate{reachable: bool, minutes: float | None, route_known: bool, blocked_reason: str}`。
    - `estimate_travel(runtime, lexicon, *, mover_id: str, from_location_id: str, to_location_id: str) -> TravelEstimate`。语义与审查门现有的 `_route` + `_shortest_minutes` 完全一致：客观路线必须可达；移动者有认知覆盖层时，只用它已知的路线；最短耗时取各出行方式的最小值，含 R16 的按方式受限最短路。

- [ ] **Step 1: 写失败测试**
  - `test_world_effects_reject_unknown_action_type_and_knowledge_state`
  - `test_agenda_change_new_requires_goal`
  - `test_tick_seed_is_stable_and_project_scoped`
  - `test_estimate_travel_matches_gate_cases`：复用 `test_world_gate_v6.py` 中的地图夹具，覆盖已知路线、未知路线、按出行方式受限、不可达。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现，并让审查门改用 `estimate_travel`**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_engine_contracts.py tests/test_world_gate_v6.py tests/test_world_consistency_review.py tests/test_chapter_delta_multi_genre.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): define tick contracts and share travel estimates with the world gate`

---

### Task 5: tick delta 构建、审查门对 world_engine delta 的检查与 `commit_world_tick`

**Files:**
- Create: `forwin/world_engine/delta_builder.py`
- Modify: `forwin/book_state/reviewer.py`（`_ChapterExtractionChecks` 同样作用于 `world_engine` delta；新增读者认知检查）、`forwin/canon/admission.py`
- Test: `tests/test_world_tick_delta.py`（新建）

**Interfaces:**
- Consumes：Task 4 的契约与 `WORLD_ENGINE_SOURCE_TYPE`。
- Produces：
  - `WorldTickDeltaBuilder(session).build(*, context: TickContext, candidates: Sequence[TickCandidate], news_in_transit: Sequence[NewsItem], tick_id: str) -> TickDeltaBuild(delta: GraphDelta | None, issues: list[TickIssue])`。
  - `TickIssue{severity: Literal["error", "warning"], code, message, candidate_id}`：只要有 error，`delta` 就是 None。
  - delta 字段：`source_type="world_engine"`，`chapter_number=context.chapter_number`，`story_time_start=context.window_start`，`story_time_end=context.now`。
  - delta `metadata` 包含 `tick_id`、`seed`、`source_chapter`、`movements`、`item_transfers`、`knowledge_tags`、`news_in_transit`、`candidate_ids`。其中 `movements`、`item_transfers`、`knowledge_tags` 的形状与 `translator.py` 写入的完全相同，审查门 v6 可直接读取。
  - 补丁：
    - 意图：create（`ProposedIntent` → intent 节点，`scheduled_at = now + duration_to_minutes(scheduled_in)`）、`state.status`、`state.slip_count`、`state.scheduled_at`。
    - agenda：create / set。
    - 时限：`state.status`。
    - 行动者：`state.next_turn_at`。
    - effects：与翻译器同一套 patch 形式与 `old_value` 规则，包括 R8 的首次设置合并。
    - 新事实：fact 节点加知情者的 cognition 补丁。
  - 所有引用必须是现有节点 id 或同一 delta 中的新建项；否则记 error `unresolved_ref`，`candidate_id` 指向出错的候选。
  - 审查门：
    - `_ChapterExtractionChecks` 同时用于 `world_engine` delta。时间窗为 delta 的 `story_time_start`–`story_time_end`；R13 的“死亡按 N−1 计”对 tick 改为按 as-of N 计，即 tick 基于已提交的本章状态。
    - 新增 error `world_engine_reader_knowledge`：world_engine delta 中出现 `reader` 观察者的认知补丁时报出。
    - 不可变规则定义检查同样适用于 world_engine delta。
  - `CanonAdmissionService.commit_world_tick(*, session, project_id: str, chapter_number: int, approved_changes: ApprovedGraphDeltaSet, tick_id: str) -> CanonWorldTickOutcome(graph_delta_ids: tuple[str, ...], idempotent: bool)`。调用方持有事务，步骤：
    1. 以 `FOR NO KEY UPDATE` 锁项目。
    2. 要求 `chapter_number` 等于最新 canon 章节，并且每个 delta 的 `source_type == "world_engine"`，否则抛 `CanonWriteFailure`。
    3. 若已有 `metadata["tick_id"] == tick_id` 的 delta，直接返回这些 delta，`idempotent=True`。
    4. `BookStateCompiler.compile(..., compiler_run_id=f"canon-world-tick-{tick_id}")`；CAS 失败抛 `CanonStaleVersion`。
    5. 写 `WORLD_TICK_COMMITTED`，并入队 outbox `knowledge.projection.refresh_requested`，event_id 为 `canon-world-tick:{tick_id}:{delta_id}:projection`。

- [ ] **Step 1: 写失败测试（`tests/test_world_tick_delta.py`）**
  - `test_builder_writes_gate_compatible_movement_metadata`
  - `test_builder_creates_intents_with_absolute_schedule_from_calendar_units`
  - `test_builder_reports_unresolved_ref_per_candidate`
  - `test_gate_flags_dead_actor_and_restrained_mover_in_world_engine_delta`
  - `test_gate_rejects_reader_knowledge_in_world_engine_delta`
  - `test_commit_world_tick_is_idempotent_by_tick_id`
  - `test_commit_world_tick_rejects_stale_chapter_and_foreign_source_type`
  - `test_secret_tick_event_leaves_reader_overlay_unchanged`：提交后 as-of N 的读者 `knowledge_by_ref` 与提交前相同。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_tick_delta.py tests/test_world_gate_v6.py tests/test_canon_commit_plan.py tests/test_architecture_boundaries.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): build tick deltas, gate them and commit them through canon admission`

---

### Task 6: 历史重写接纳 tick delta，重放 CAS 失败转为冲突

**Files:**
- Modify: `forwin/canon/historical_rewrite.py:104-153`（`validate_contribution`）、`:287-463`（重放）
- Test: `tests/test_historical_rewrite_world_ticks.py`（新建）、`tests/test_historical_canon_rewrite*.py`

**Interfaces:**
- Consumes：Task 3 的仓储与 `ConflictDraft`；Task 5 的 `world_engine` delta 与 `metadata["tick_id"]`。
- Produces：
  - `validate_contribution`：章节里 `source_type == "world_engine"` 的保留 delta，只有满足以下任一条件才合法，否则仍抛 `HistoricalRewriteInvalid`：
    - 它的 id 列在该章 canon commit 对应的、已成功的 `world` 步骤 `PostCanonMaintenanceRun.result_json["graph_delta_ids"]` 中；
    - 它列在某条已决定冲突的 `result_refs_json` 中。
  - 重放：
    - world_engine delta 按原记录重放；先对每个非 create 补丁比对 `old_value` 与重放中的运行时，比对方式与 `BookStateCompiler` 的 CAS 相同。
    - 任一不一致时，该 delta 整体不再生效：不重新应用，已物化的行按回卷规则撤销。
    - 同时创建冲突 `ConflictDraft(source="historical_rewrite", violated="replay_cas", impact_level=<delta metadata 中候选的最高等级或 "moderate">, proposed_change={"graph_delta_id": ..., "tick_id": ...}, blocks_from_chapter=<最新 canon 章节 + 1>)`，`required_decider="human"`，`options=("veto_world",)`，并写 `WORLD_CONFLICT_DETECTED`。
    - 其他 delta 照常重放。

- [ ] **Step 1: 写失败测试**
  - `test_rewrite_keeps_listed_tick_deltas`：第 2、3 章各有 tick delta，重写第 2 章后两条 tick delta 仍然生效。
  - `test_rewrite_replays_tick_deltas_and_turns_cas_failure_into_conflict`：新的第 2 章改变了第 2 章 tick delta 所依赖的值 → 该 tick delta 不生效，产生一条 open 的 human 冲突，`open_blocking(next_chapter=4)` 能找到它，其余 delta 生效。
  - `test_unlisted_world_engine_delta_still_invalidates_rewrite`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_historical_rewrite_world_ticks.py tests/test_canon_atomic_transaction.py tests/test_book_state_repository_projection_compiler.py` 加上 `rg -l historical tests` 列出的历史重写测试 → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(canon): replay world tick deltas in historical rewrites and turn CAS failures into conflicts`

---

### Task 7: tick 的确定性步骤：时限、意图簿记、onstage 顺延与消息传播

**Files:**
- Create: `forwin/world_engine/clocks.py`、`forwin/world_engine/intents.py`、`forwin/world_engine/propagation.py`
- Test: `tests/test_world_tick_steps.py`（新建）

**Interfaces:**
- Consumes：Task 4 的 `TickCandidate`、`NewsItem`、`EffectKnowledge`、`estimate_travel`；Task 3 的 `ConflictDraft`。
- Produces：
  - `expire_clocks(runtime, *, now: int) -> list[TickCandidate]`：
    - 只处理 status 为 running 且 `due_at <= now` 的时限，把 `state.status` 改为 expired。
    - `profile.on_expire` 等于某个 intent 节点 id 时，把该意图的 `scheduled_at` 改为 now。
    - 否则生成一个事件：summary 为 label，加上 on_expire 文本（两者都来自 BookState）；scope 为 public 时 visibility 为 public，scope 为 secret 时为 secret；参与者是 owner_ref。
  - `apply_intent_interactions(runtime, *, interactions: Sequence[Mapping]) -> list[TickCandidate]`：读取本章 chapter_extraction delta 的 `metadata["intent_interactions"]`。disrupted 映射为 status disrupted，fulfilled 映射为 resolved，advanced 不改状态。
  - `select_due_intents(runtime, *, now: int) -> DueIntents(offstage: list[WorldNode], onstage: list[WorldNode])`：status 为 planned 且 `scheduled_at <= now`。按 `scheduled_at`、所属行动者 tier、id 排序。
  - `onstage_bookkeeping(runtime, *, onstage_due, already_due, interactions, now, max_slips, chapter_number) -> OnstageResult(candidates: list[TickCandidate], conflicts: list[ConflictDraft])`：
    - 新到点的 onstage 意图改为 due。
    - 已是 due、而本章 interactions 中没有它的意图：`slip_count + 1`，`scheduled_at = now`。
    - `slip_count > max_slips` 时产生 `ConflictDraft(source="world_tick", violated="onstage_slip", impact_level=<意图的 impact_level>, blocks_from_chapter=chapter_number + 1)`。
  - `latest_news_in_transit(session, project_id: str) -> list[NewsItem]`：读取最近一条 world_engine delta 的 `metadata["news_in_transit"]`。
  - `propagate_news(runtime, lexicon, *, events: Sequence[EffectEvent], in_transit: Sequence[NewsItem], now: int, fact_ref_for_event: Mapping[int, str]) -> PropagationResult(knowledge: list[EffectKnowledge], in_transit: list[NewsItem])`：
    - 公共事件：每个有位置的行动者在 `at + 客观最短路线分钟 ÷ lexicon.news_speed_multiplier` 时获得 `heard` / `rumor`。到达时间不晚于 now 的立即投递，其余入队；不可达的行动者收不到。
    - 秘密事件：只给参与者，以及事件发生时与事件同地点的行动者，状态为 `knows` / `witnessed`。
    - 已经知道该事实（knows / believes）的行动者不重复投递。
    - 队列中到达时间不晚于 now 的条目被投递。
    - 事件来源有两个：本章 chapter_extraction delta 中的事件，以及本次 tick 产生的事件。
    - `fact_ref_for_event` 把事件在 `events` 中的位置映射到它的事实引用：本章事件用已有的 `fact:<id>`，tick 事件用 `fact:new:event-<n>`，由 delta 构建解析为新建事实。

- [ ] **Step 1: 写失败测试（`tests/test_world_tick_steps.py`）**
  - `test_running_clock_past_due_expires_and_schedules_named_intent`
  - `test_expired_secret_clock_event_is_secret`
  - `test_intent_interactions_disrupt_and_resolve`
  - `test_due_selection_orders_by_time_tier_and_id`
  - `test_onstage_intent_slips_until_conflict`：`max_slips=2`，第三次顺延时产生冲突。
  - `test_public_news_reaches_far_actor_later_and_queues`：两个行动者离事件 1 小时与 10 小时，`news_speed_multiplier=2.0`，tick 窗口 4 小时 → 近者立即 heard，远者入队且 `arrive_at` 等于事件时间加 300 分钟。
  - `test_secret_event_only_reaches_participants_and_witnesses`
  - `test_queued_news_is_delivered_by_a_later_tick`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_tick_steps.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): expire clocks, keep intent books and propagate news deterministically`

---

### Task 8: 约束检查与影响分级

**Files:**
- Create: `forwin/world_engine/constraints.py`
- Test: `tests/test_world_constraints.py`（新建）

**Interfaces:**
- Consumes：Task 4 的 `TickCandidate`、`estimate_travel`。
- Produces：
  - `classify_impact(runtime, candidate: TickCandidate, *, milestone_refs: frozenset[str] = frozenset()) -> str`：取下表计算结果与 `candidate.impact_level`（LLM 声明）两者中较高的等级。
    - critical：任何补丁或 effects 引用了 `milestone_refs` 中的 id（P3 恒为空）。
    - major：
      - `actor_tier` 为 major / minor 的角色 `alive_status` 改为 dead。
      - faction / organization / group 节点 `is_active` 变为 false，或 `state.controlled_by_id` 改变。
      - 地点节点（site_state）`is_active` 变为 false。
      - sensitivity 为 hidden / secret 的事实改为 public，或者一个 public 事件引用了这样的事实。
    - moderate：
      - 关系 create / end / set。
      - item / resource 的 `holder_id` 改变。
      - `restraint` 改变。
      - 角色 `alive_status` 改为 unknown。
      - 角色除位置、`next_turn_at` 之外的其他 state 改变。
    - minor：其余。
  - `check_hard_rules(runtime, lexicon, candidate, *, now: int) -> list[ConstraintIssue]`，codes 如下：
    - `dead_owner_acts`：意图所有者或事件参与者已死亡。
    - `unreachable_before_scheduled`：新意图在 `scheduled_at - now` 内从所有者当前位置到达不了意图地点（用 `estimate_travel`）。
    - `resource_not_held`：物品转移的 `from_ref` 不是当前持有者。
    - `restrained_moves_without_release`：受限者移动，而同一候选中没有解除它的 restraint。
    - `time_not_monotonic`：`scheduled_at < now`，或者 effects 中的时间落在 `[window_start, now]` 之外。
  - `check_narrative_boundaries(runtime, candidate, *, next_chapter: int) -> list[BoundaryViolation]`：
    - 读取 `until_chapter >= next_chapter`（或 `until_chapter == 0`，表示开放）的 `review_constraint` narrative 节点。
    - kind 为 must_exist：引用节点 `is_active` 变为 false。
    - kind 为 must_be_alive：引用节点 `alive_status` 改为 dead。
    - kind 为 must_hold_state：`field` 被改离 `value`。
    - kind 为 must_not_know：`observer_ref` 对 `fact_ref` 进入 knows / believes。
    - `BoundaryViolation{constraint_node_id, kind, ref}`。
  - `apply_budgets(candidates: Sequence[TickCandidate], *, impacts: Mapping[str, str], max_major_events: int) -> BudgetResult(kept: list[TickCandidate], postponed: list[TickCandidate])`：
    - major 及以上的候选超过上限时，排序靠后的被顺延：其意图保持 planned，`scheduled_at = now`。
    - 排序依次按所属行动者 tier、agenda priority、candidate_id。
  - `ConstraintIssue{code, message, ref}`。

- [ ] **Step 1: 写失败测试**：每个分级行与每个 code 各一个参数化用例；另加 `test_declared_impact_cannot_lower_computed_level`、`test_inactive_or_expired_protection_is_ignored`、`test_budget_postpones_extra_major_candidates`。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_constraints.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): check hard rules and narrative boundaries and classify impact`

---

### Task 9: `world.adjudicate`：结算到点意图

**Files:**
- Create: `forwin/world_engine/adjudicator.py`、`forwin/writer/prompt_core/world_engine.py`
- Modify: `forwin/writer/llm/routing.py`（`task_family="world_engine"` 映射到 `planning_json_general`）、`tests/overfit_allowlist.toml`（`prompt_core/world_engine.py` 登记为 `prompt_text`）
- Test: `tests/test_world_adjudicator.py`（新建）、`tests/test_llm_client_retry.py`

**Interfaces:**
- Consumes：Task 4 的 `AdjudicationResult`、`TickContext`；Task 8 的 `ConstraintIssue`。
- Produces：
  - `WorldAdjudicator(llm_client, *, max_attempts: int = 2, max_tokens: int = 6000)`。
  - `.adjudicate(*, context: TickContext, intents: Sequence[WorldNode], runtime, lexicon, calendar, feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None) -> AdjudicationCall(result: AdjudicationResult | None, prompt_trace: dict, issues: list[str])`。
  - 调用参数：`stage_key="world.adjudicate"`、`task_family="world_engine"`、`response_format={"type": "json_object"}`，以及 output_schema。
  - 输入内容：
    - 每个意图的 profile / state。
    - 所有者与目标的 state、资源和位置。
    - rule 节点的 hardness 与 statement。
    - 本书日历单位，以及 `seed`。
    - 不包含读者认知。结算者代表世界规则，可以看到全部事实，包括隐藏事实。
  - 校验：结果中不在输入意图里的 `intent_id` 被丢弃；解析或校验失败时带错误重试，最多 `max_attempts` 次。
  - prompt_trace：与 `ChapterDeltaExtractor` 相同的 attempts 结构，`trace_scope="world_engine"`。`feedback` 非空时把每个意图的约束错误写进反馈段。
  - `build_adjudication_messages(...) -> list[dict]` 位于 `prompt_core/world_engine.py`。

- [ ] **Step 1: 写失败测试**
  - `test_adjudicate_returns_outcomes_for_input_intents_only`
  - `test_invalid_json_retries_then_returns_none_with_trace`
  - `test_feedback_issues_are_rendered_per_intent`
  - `test_prompt_carries_seed_rules_and_calendar_units_but_no_reader_knowledge`
  - `test_world_engine_family_routes_to_planning_json`（放在 `tests/test_llm_client_retry.py`）
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_adjudicator.py tests/test_llm_client_retry.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): adjudicate due intents with a structured LLM call`

---

### Task 10: `world.actor_turn`：行动者回合

**Files:**
- Create: `forwin/world_engine/actor_turns.py`
- Modify: `forwin/writer/prompt_core/world_engine.py`（新增 `build_actor_turn_messages`）
- Test: `tests/test_world_actor_turns.py`（新建）

**Interfaces:**
- Consumes：Task 4 的 `ActorTurnResult`、`estimate_travel`；Task 3 的 `WorldConflictRepository`（读取否决记录）；Task 2 的 `WorldPolicy`。
- Produces：
  - `rank_actors(runtime, *, now: int, limit: int) -> list[WorldNode]`：
    - 只取 `state.next_turn_at <= now` 的行动者节点。
    - 排序依次为：tier（major > minor > background）、相关度（与 major 角色相连的 world 边数，加上与 major 角色同地点的数量，均为降序）、id。
  - `build_actor_view(runtime, lexicon, *, actor_id: str, now: int, vetoes: Sequence[WorldPlanConflictRow], boundary_refs: Sequence[str]) -> ActorView`，内容包括：
    - 该行动者认知中 heard / suspects / believes / knows 的事实。误信的事实按它相信的内容呈现，不给出真值。
    - 它的 agendas、资源和位置。
    - 可达地点与耗时：按 `estimate_travel` 取最近的 12 个。
    - rule statements。
    - 它知道的时限：public 的全部，secret 的只取 `owner_ref` 是它、或它在 agenda `secrecy_refs` 中的。
    - `boundary_refs`：只列引用，不解释原因。
    - 近期对它的否决：conflict summary 与被否决的意图 summary。
  - `ActorTurnRunner(llm_client, *, max_attempts: int = 2)`：
    - `.run(*, context: TickContext, views: Sequence[ActorView], policy: WorldPolicy, calendar, lexicon, feedback: Mapping[str, Sequence[ConstraintIssue]] | None = None) -> list[ActorTurnCall(actor_id, result: ActorTurnResult | None, prompt_trace: dict, issues: list[str])]`。
    - 按 `actor_turn_batch_size` 分批，每批一次调用，`stage_key="world.actor_turn"`、`task_family="world_engine"`。
    - 校验：`new_intents` 数量不超过 `max_new_intents_per_actor`；单位换算失败或引用不在视图中时，带错误重试。
  - `turn_to_candidate(call: ActorTurnCall, *, now: int, calendar, lexicon) -> TickCandidate`：
    - `origin="actor_turn"`。
    - `next_turn_at = now + duration_to_minutes(next_turn_in)`，至少 now + 1。
    - 每个 `ProposedIntent` 的 `scheduled_at` 由 delta 构建换算。

- [ ] **Step 1: 写失败测试**
  - `test_rank_actors_by_due_tier_relevance_and_id`
  - `test_actor_view_hides_facts_the_actor_does_not_know`：另一行动者独知的 secret 事实不出现在视图里。
  - `test_actor_view_presents_false_belief_as_believed`
  - `test_actor_view_lists_only_known_clocks_and_vetoes`
  - `test_runner_batches_and_caps_new_intents`
  - `test_unknown_unit_or_ref_retries_with_feedback`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_actor_turns.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): run actor turns from each actor's own cognition`

---

### Task 11: 冲突路由与 Codex 审稿代理

**Files:**
- Create: `forwin/world_engine/conflict_routing.py`、`forwin/world_engine/conflict_delegate.py`
- Modify: `forwin/generation/gate_delegation.py`（抽出 `run_codex_structured_call`，`SparkGateDelegate` 改用它，行为不变）、`forwin/writer/prompt_core/world_engine.py`（新增 `build_conflict_review_messages`）
- Test: `tests/test_world_conflict_routing.py`（新建）、`tests/test_world_conflict_delegate.py`（新建）、`tests/test_gate_delegation.py`

**Interfaces:**
- Consumes：Task 1 的 Codex 清单；Task 2 的 `role_models`、`WorldPolicy.conflict_delegate`；Task 3 的冲突类型与仓储。
- Produces：
  - `available_options(draft: ConflictDraft) -> tuple[ConflictOption, ...]`：
    - source 为 historical_rewrite 时返回 `("veto_world",)`。
    - 其余情况为 `("veto_world", "plot_responds", "amend_plan")`；impact 为 minor / moderate 时再加 `"accept_with_obligation"`。
    - P3 不提供 `rewrite_chapter`。
  - `route_conflict(draft: ConflictDraft, *, policy: RuntimePolicy) -> ConflictDecider`：
    - source 为 historical_rewrite，或 impact 为 critical → human。
    - impact 为 minor → auto，默认执行 `veto_world`。
    - moderate / major：`policy.world.conflict_delegate == "codex"` 时为 codex，否则为 human。
  - `run_codex_structured_call(llm_client, *, audit_writer: GateAuditWriter, gate_kind: str, stage_key: str, requested_model: str, messages: list[dict], output_model: type[BaseModel], output_schema: dict, event_types: CodexCallEventTypes, request_payload: dict) -> CodexCallOutcome(parsed: BaseModel | None, failure_reason: str, trace_id: str, decision_event_id: str)`：
    - 把 `SparkGateDelegate` 现有的步骤原样搬进来：请求事件、调用前后排空 attempts、`preferred_provider_kind="codex"`、`permission_profile="prompt_only_readonly"`、模型校验（只信任 `backend == "codex_bridge"` 的 `actual_model`）、PromptTrace 与审计事件。
    - `failure_reason` 取值为 `llm_call_failed` / `model_mismatch` / `parse_or_schema`。
  - `WorldConflictDelegate(llm_client)`：
    - `.decide(*, conflict: WorldPlanConflictRow, context_snapshot: dict, policy: RuntimePolicy, audit_writer: GateAuditWriter) -> WorldConflictDelegation(option: ConflictOption | None, rationale: str, failure_reason: str, trace_id: str, escalate: bool)`。
    - 模型为 `role_models(policy, "review.world_conflict")[0]`，为空时用 `"gpt-6-astra"`；`stage_key="review.world_conflict"`，`task_family="review"`。
    - 输出契约 `{option, rationale, risk_level, evidence}`：option 不在 `conflict.options_json` 中视为 `parse_or_schema`；选择 `amend_plan` 时 `escalate=True`。任何 failure 也都是 `escalate=True`。
    - 审计事件：`WORLD_CONFLICT_ESCALATED`（升级时），以及 `PROMPT_TRACE_RECORDED`。
  - `context_snapshot`：冲突记录、涉及引用在 as-of N 的 BookState 片段、当前卷的 `ArcPlanVersion`（goal / stakes / payoff_direction / synopsis）、接下来 3 个未写章节计划的 title 与 goals。

- [ ] **Step 1: 写失败测试**
  - `test_route_table`：参数化覆盖 source × impact × delegate 设置。
  - `test_available_options_by_source_and_impact`
  - `test_delegate_returns_option_from_codex`：伪造 LLM 返回 backend 为 codex_bridge、actual_model 为 gpt-6-astra。
  - `test_delegate_failures_escalate_without_applying`：参数化覆盖 llm_call_failed、model_mismatch、选项不在列表中，断言都升级，且仓储中冲突仍为 open。
  - `test_amend_plan_choice_escalates`
  - `test_world_conflict_delegate_module_has_no_canon_write_surface`：沿用 `test_gate_delegation.py` 中的禁用字符串清单。
  - `test_spark_gate_delegate_behaviour_unchanged`：`tests/test_gate_delegation.py` 中的现有测试全部通过即可。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_conflict_routing.py tests/test_world_conflict_delegate.py tests/test_gate_delegation.py tests/test_gate_delegation_chapter.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): route world-plan conflicts and let Codex choose an option`

---

### Task 12: 冲突选项的执行

**Files:**
- Create: `forwin/world_engine/conflict_resolution.py`
- Modify: `forwin/writer/prompt_core/world_engine.py`（`plot_responds` 的目标文本模板）
- Test: `tests/test_world_conflict_resolution.py`（新建）

**Interfaces:**
- Consumes：
  - Task 3 的仓储与 `ConflictDecision`；
  - Task 5 的 `WorldTickDeltaBuilder` 与 `commit_world_tick`；
  - `narrative_obligations.DeferAcceptanceTransaction`；
  - 现有的章节计划补丁（`planning/future_plan_audit/patches.py` 的 chapter goal 补丁方式）。
- Produces：
  - `ConflictResolutionService(session, *, admission: CanonAdmissionService, policy: RuntimePolicy)`。
  - `.resolve(*, project_id: str, conflict_id: str, decision: ConflictDecision) -> ConflictResolutionResult(graph_delta_ids: tuple[str, ...], obligation_id: str, plan_patch_id: str)`。
  - 通用前置检查：
    - 以 `for_update` 取冲突，不是 open 时抛 `ConflictNotOpen`。
    - option 不在 options 中时抛 `ValueError`。
    - `amend_plan` 且 `decided_by != "human"` 时抛 `ValueError`。
  - 各选项：
    - `veto_world`：
      - 意图已存在于 BookState 时，补丁 `state.status → vetoed`。
      - `reroll_count(actor_ref) < world.max_rerolls` 时，补丁该行动者 `state.next_turn_at → 最新 canon 章节的 story_time_end`，让它下一次 tick 重新行动。
      - 冲突行本身就是注入下一回合的否决记录。
    - `accept_with_obligation`：
      - 先用 `proposed_change` 重建并提交变化。
      - 再经 `DeferAcceptanceTransaction` 记义务：`obligation_type="world_change_response"`，`summary` 与 `payoff_test` 都取冲突的 summary，`deadline_chapter = 最新 canon 章节 + 6`，计划补丁指向下一个未接受的 ChapterPlan。
      - 预算拒绝时抛 `ConflictOptionUnavailable`，冲突保持 open，已构建的变化不提交。
    - `plot_responds`：提交变化，并给下一个未接受的 ChapterPlan 追加一个回应目标；文本取 `prompt_core` 模板加冲突 summary。
    - `amend_plan`：提交变化，并在同一 delta 中把相关 `review_constraint` 节点的 `until_chapter` 设为最新 canon 章节，即解除它们。
    - `rewrite_chapter`：抛 `ConflictOptionUnavailable`。
  - 提交：
    - 使用 `commit_world_tick(tick_id=f"conflict-{conflict_id}", chapter_number=<最新 canon 章节>)`。
    - `proposed_change` 的 `old_value` 过期时（`CanonStaleVersion`），冲突保持 open，并抛 `ConflictStale`。
  - 收尾：`mark_resolved(result_refs=[delta ids, obligation id, plan patch id])`，写 `WORLD_CONFLICT_RESOLVED`。
  - 供 Task 13 复用的两个函数，`resolve` 由它们组合而成：
    - `option_candidates(conflict: WorldPlanConflictRow, decision: ConflictDecision, *, runtime, policy: RuntimePolicy, reroll_count: int, now: int) -> list[TickCandidate]`：纯函数，产出实现该选项的世界变化候选，包括否决补丁、被接受的变化和 protection 解除。
    - `record_option_side_effects(session, conflict: WorldPlanConflictRow, decision: ConflictDecision, *, latest_chapter: int) -> OptionSideEffects(obligation_id: str, plan_patch_id: str)`：义务与计划补丁，在提交成功之后调用。

- [ ] **Step 1: 写失败测试**
  - 每个选项一个用例，例如 `test_veto_marks_intent_vetoed_and_reschedules_actor_turn_within_reroll_cap`、`test_accept_with_obligation_commits_change_and_records_obligation`、`test_plot_responds_adds_goal_to_next_unwritten_plan`、`test_amend_plan_releases_protections_and_requires_human`。
  - `test_resolving_twice_raises_and_applies_once`
  - `test_stale_held_change_keeps_conflict_open`
  - `test_obligation_budget_rejection_keeps_conflict_open`
  - `test_rewrite_chapter_unavailable_in_p3`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_conflict_resolution.py tests/test_narrative_obligation_ledger.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): execute conflict options through canon admission`

---

### Task 13: tick 编排与 post-Canon world 步骤

**Files:**
- Create: `forwin/world_engine/tick.py`
- Modify: `forwin/maintenance/events.py:13`（`POST_CANON_STEP_NAMES = ("world", "planning", "arc", "feedback")`）、`forwin/maintenance/post_canon.py:175-180, 926-944`（world 步骤先跑 tick，再跑旧 WorldSimulator）、`forwin/maintenance/state.py`、`forwin/runtime/container.py`（装配）、固定了步骤名的测试（`tests/test_post_canon_maintenance.py`、`tests/test_post_canon_maintenance_api.py`、`tests/test_canon_atomic_transaction.py`）
- Test: `tests/test_world_tick_service.py`（新建）

**Interfaces:**
- Consumes：Task 3–12 的全部接口。
- Produces：
  - `WorldTickService(*, llm_client, admission: CanonAdmissionService, adjudicator: WorldAdjudicator, actor_runner: ActorTurnRunner, conflict_delegate: WorldConflictDelegate, audit_writer_factory: Callable[[], GateAuditWriter], save_prompt_trace: Callable[[Session, dict], str])`。
  - `.run(session, commit: CanonCommitRecord, *, policy: RuntimePolicy) -> WorldTickResult(tick_id: str, graph_delta_ids: list[str], conflict_ids: list[str], dropped_candidate_ids: list[str], postponed_candidate_ids: list[str])`。
  - 流程：
    1. 准备上下文：`tick_id = f"tick-{commit.id}"`；`now` 为本章 `story_time_end`，`window_start` 为本章 `story_time_start`，`seed = tick_seed(...)`。`policy.world.enabled` 为 False 时直接返回空结果。
    2. 依次收集候选：`apply_intent_interactions`、`expire_clocks`、`select_due_intents`，用 `adjudicator` 结算 offstage 意图（每个结局一个候选），`onstage_bookkeeping`，`propagate_news`（本章事件、tick 事件、队列），`rank_actors` + `build_actor_view` + `ActorTurnRunner`。
    3. 逐个候选检查：`check_hard_rules`，单候选 delta 的审查门，`classify_impact`，`check_narrative_boundaries`。
       - 硬规则或审查门出错的候选，带错误让对应的 adjudicator / actor_runner 重跑一次。
       - 仍然出错就丢弃，写 `WORLD_TICK_CANDIDATE_DROPPED`。
    4. 处理需要审查的候选：违反叙事边界，或影响等级为 major 及以上（`violated="impact_review"`）的候选生成 `ConflictDraft`，不放入本次 delta，并持久化（写 `WORLD_CONFLICT_DETECTED`），再按 `route_conflict` 分流：
       - auto：在本次 tick 内执行 `veto_world`，并入 tick delta。
       - codex：调用 `WorldConflictDelegate`。得到选项且不升级时，在本次 tick 内执行（同 Task 12 的语义，但并入同一个 tick delta 与事务）；升级则 `escalate`，变化保持 held。
       - human：保持 open、held。
    5. `apply_budgets` 之后用 `WorldTickDeltaBuilder.build` 构建整体 delta；有 error 时从 issues 定位到候选，丢弃后重建一次。
    6. 有补丁时 `commit_world_tick(tick_id=...)`。
    7. 保存所有 PromptTrace。返回结果；world 步骤的 `result_json` 写入 `{tick_id, graph_delta_ids, conflict_ids, dropped_candidate_ids, postponed_candidate_ids}`。
  - tick 内执行选项时，使用 Task 12 的 `option_candidates` 与 `record_option_side_effects`，不另写一份实现。

- [ ] **Step 1: 写失败测试（`tests/test_world_tick_service.py`，使用桩 LLM 和真实测试库）**
  - `test_quiet_tick_commits_nothing_and_succeeds`
  - `test_tick_resolves_due_intent_propagates_news_and_commits_one_delta`
  - `test_invalid_actor_intent_retries_then_drops_and_tick_commits_rest`
  - `test_major_impact_goes_to_codex_and_veto_is_applied_in_tick`
  - `test_escalated_conflict_holds_change_and_blocks_next_chapter`：用 `open_blocking(next_chapter=N+1)` 判断。
  - `test_minor_boundary_violation_is_auto_vetoed`
  - `test_tick_saves_prompt_traces_with_stage_keys`：world.adjudicate、world.actor_turn、review.world_conflict 都有 PromptTrace 行。
  - `test_world_step_runs_before_planning_and_records_tick_result`
  - `test_disabled_world_policy_skips_tick`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现，并迁移固定了步骤名或步骤数的测试**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_tick_service.py tests/test_post_canon_maintenance.py tests/test_post_canon_maintenance_api.py tests/test_canon_atomic_transaction.py tests/test_chapter_delta_loop.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): run a world tick as the first post-canon step`

---

### Task 14: run control 中的冲突阻塞

**Files:**
- Modify: `forwin/generation/pipeline_core/project_chapters.py`（在预检恢复之后、`get_chapter_plan` 之前）、`forwin/planning/checkpoints.py:23-30, 103-116`（`BlockingReasonCode` 与 `chapter_blocking_message`）、`forwin/application/read_models/generation.py:30-208`（`_derive_blocking_reason`）及其调用方（`project_detail.py:257`、`project_summary.py:150`、`application/task_center.py:253`）、`forwin/ui_assets/home/app_task_progress.js:940-948`（needs_review 引导文案区分世界冲突）、`tests/test_gate_delegation_chapter.py:296-342`（桩）
- Test: `tests/test_world_conflict_run_control.py`（新建）

**Interfaces:**
- Consumes：Task 3 的 `open_blocking`。
- Produces：
  - 每章开始前（预检恢复之后）检查 `open_blocking(project_id, next_chapter=n)`。命中时依次：
    1. `_emit_progress(stage="paused_for_review", payload={"reason": "world_conflict", "conflict_id": ...})`；
    2. `paused_chapters.append(n)`；
    3. 跳出循环，走最终的 `RunResult`，任务状态为 needs_review。

    ChapterPlan 状态不变。
  - `BlockingReasonCode` 新增 `"world_conflict_pending"`，`chapter_blocking_message` 给出对应文案（沿用该文件现有的文案方式）。
  - `_derive_blocking_reason(plans, latest_band_checkpoint, decision_events, open_world_conflicts: Sequence[WorldConflictBlockInfo] = ())`：存在 human 冲突阻塞下一章时，返回 code `world_conflict_pending`，detail 中带 `conflict_id`。三个调用方传入 `WorldConflictRepository.list(status="open")` 中 human 的冲突。continue 与 review approve 的 409 守卫因此自动生效。
  - `WorldConflictBlockInfo{conflict_id, blocks_from_chapter, summary}`。

- [ ] **Step 1: 写失败测试**
  - `test_open_human_conflict_stops_run_before_blocked_chapter`：阻塞第 3 章 → 写完第 2 章后停下，任务状态 needs_review，进度事件 reason 为 world_conflict。
  - `test_conflict_blocking_later_chapter_does_not_stop_earlier_ones`
  - `test_project_detail_reports_world_conflict_pending`
  - `test_continue_route_returns_409_until_conflict_resolved`
  - `test_resolved_conflict_lets_continue_proceed`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_conflict_run_control.py tests/test_gate_delegation_chapter.py tests/test_project_operation_guards.py tests/test_task_center*.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(generation): pause before a chapter that an open world conflict blocks`

---

### Task 15: 冲突入口：HTTP、MCP 与控制台

**Files:**
- Create: `forwin/http/adapters/api_world_conflict_routes.py`、`forwin/api_schema/world_conflicts.py`
- Modify: `forwin/http/routes.py`、`forwin/api_schema/__init__.py`、`forwin/mcp/http.py`、`forwin/mcp/client.py`、`forwin/mcp/models.py`、`forwin/ui_assets/home/app_task_control.js`（任务抽屉的“世界冲突”卡片与选项弹窗）、`tests/browser/fixtures.py`、`docs/codex-forwin-mcp.md`、`tests/test_architecture_boundaries.py`（路由归属）
- Test: `tests/test_world_conflict_api.py`（新建）、`tests/test_mcp_server.py`、`tests/browser/test_world_conflicts_drawer.py`（新建）

**Interfaces:**
- Consumes：Task 3 的仓储；Task 12 的 `ConflictResolutionService`。
- Produces：
  - HTTP 路由：
    - `GET /api/projects/{project_id}/world-conflicts?status=` 返回 `list[WorldConflictInfo]`。
    - `GET /api/projects/{project_id}/world-conflicts/{conflict_id}` 返回 `WorldConflictInfo`，找不到时 404。
    - `POST /api/projects/{project_id}/world-conflicts/{conflict_id}/resolve`：请求体 `WorldConflictResolveRequest{option, rationale, reason}`，reason 必填。
      - `decided_by="human"`，`actor_type="manual_ui"`。
      - 冲突不是 open 时返回 409（`ConflictNotOpen`、`ConflictStale`）；选项不可用时返回 400（`ValueError`、`ConflictOptionUnavailable`）。
      - 成功时返回 `WorldConflictInfo`。
  - `WorldConflictInfo`：§6.1 的全部字段，加上 `summary`、`options` 和 `decision`。
  - MCP：
    - `world_conflict_list(project_id, status?)`：改指新路由。
    - `world_conflict_get(project_id, conflict_id)`：只读工具。
    - `world_conflict_resolve(project_id, conflict_id, option, rationale, reason)`：写工具。
    - `world_model_signal_list(project_id)`：旧的 CanonQualitySignal 列表，原 `/world-model/conflicts` 路由。
    - 每个工具描述含 "Use this when"，并更新 `test_mcp_server.py` 中的工具名清单。
  - 控制台：
    - 任务抽屉在项目控制卡片之后新增“世界冲突”卡片，只列 open 的冲突，显示 summary、impact、violated 和 blocks_from_chapter。
    - 每行一个“决定”按钮，打开 `openProjectControlActionModal`：一个列出该冲突 options 的 select 字段，加上 reason 与 rationale。
    - 提交后 POST resolve，再刷新抽屉。

- [ ] **Step 1: 写失败测试**
  - `test_list_and_get_world_conflicts`
  - `test_resolve_route_applies_option_and_records_human_decision`
  - `test_resolve_route_returns_409_when_not_open`
  - `test_resolve_route_returns_400_for_unavailable_option`
  - `test_mcp_world_conflict_tools_and_signal_list`
  - 浏览器：`test_drawer_lists_open_conflicts_and_submits_resolution`，断言捕获的 payload 中 option 与 reason 正确。
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_world_conflict_api.py tests/test_mcp_server.py tests/test_canon_quality_world_conflicts.py tests/test_architecture_boundaries.py tests/browser/test_world_conflicts_drawer.py tests/browser/test_task_center_drawer.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(world_engine): list and resolve world conflicts over HTTP, MCP and the console`

---

### Task 16: Genesis 地图边按声明端点物化，地图种子稳定（P2 推迟项）

**Files:**
- Modify: `forwin/map/genesis_adapter.py:115, 262-295, 325-328`、`forwin/map/protocol.py:58-73`（`SubWorldMapSpec.required_connections`）、`forwin/map/generator.py:36-50, 213-231`
- Test: `tests/test_map_genesis_adapter.py`、`tests/test_map_generation.py`、`tests/test_genesis_map_structure.py`、`tests/test_chapter_delta_multi_genre.py`、`tests/test_chapter_delta_loop.py`

**Interfaces:**
- Produces：
  - `_stable_seed(project_id, subworld_id) -> int` 改为 `int(sha256(f"{project_id}|{subworld_id}").hexdigest()[:12], 16) % 2_000_000_000`。调用点仍在 `:115`，函数名不变，测试可以继续打桩。
  - `SubWorldMapSpec.required_connections: list[RequiredConnection]`，默认 `[]`。`RequiredConnection{from_source_node_id, to_source_node_id, edge_type, bidirectional: bool, hidden: bool, source_edge_id: str}`。adapter 在 `_connection_roles` 的同一循环中填充它，`required_connection_roles` 的输出保留。
  - 生成器：
    - 每条 `required_connection` 在 `source_node_id` 对应的两个锚点之间生成一个候选边，带 `metadata.required_connection=True`，并保留 `edge_type` / `bidirectional` / `hidden`。
    - 没有声明边的 subworld 仍用相邻锚点链。
    - 声明边在预算填充之前预选；其中非 hidden 的预先并入 MST，并计入目标边数。
    - `kind` 为 `hidden_route` 的边状态仍是 open，不从 kind 推出 hidden。
    - 声明为 hidden 的边物化为 `status="hidden"`、`discovered_by_default=False`。

- [ ] **Step 1: 写失败测试**
  - `test_declared_atlas_edges_connect_their_endpoints`：三个节点、声明 A–C 一条 river，生成的地图中 A 与 C 之间直接有 river 边。
  - `test_declared_hidden_edge_is_hidden_and_undiscovered`
  - `test_declared_one_way_edge_is_not_bidirectional`
  - `test_seed_ignores_revision_and_atlas_changes`
  - `test_subworld_without_declared_edges_keeps_anchor_chain`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现。** 调整受影响的现有断言，例如 `test_map_genesis_adapter.py:97` 的“相邻锚点”断言改为“声明端点”。三本多题材夹具的行程断言若因地图变化需要调整，必须保持原测试意图：commit 测试仍然不报 world_consistency error，too-fast 测试仍然报 error。
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_map_genesis_adapter.py tests/test_map_generation.py tests/test_genesis_map_structure.py tests/test_map_world_integration.py tests/test_world_seed_multi_genre.py tests/test_chapter_delta_multi_genre.py tests/test_chapter_delta_loop.py tests/test_book_genesis_flow.py tests/test_genesis_handoff_service.py tests/test_handoff_atomicity.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`feat(map): build declared Genesis map edges between their endpoints with a stable seed`

---

### Task 17: 审稿表单的时限保护与秘密时限描述（P2 推迟项）

**Files:**
- Modify: `forwin/canon_quality/chapter_review_form/form_builder.py:120-135`、`forwin/canon_quality/chapter_review_form/pruning.py:23, 62-72, 187-189`、`forwin/canon_quality/chapter_review_form/canon_projector.py:43-45`、`forwin/writer/prompt_core/constraints.py:228-242`（如需）
- Test: `tests/test_chapter_review_form_from_book_state.py`、`tests/test_canon_projector.py`、`tests/test_form_builder.py`

**Interfaces:**
- Produces：
  - 只有 remaining > 0 的 running 时限受剪枝保护。remaining ≤ 0 的 running 时限按 `closed` 处理：不受保护，只在正文提到时才问。
  - 受保护的时限最多问 `MAX_PROTECTED_COUNTDOWNS = 6` 个，按 remaining 升序。
  - 时限信号的 description 不再含节点 id：
    - public 时限：description 用时限的 `label`，`subject_key` 仍是节点 id，用于去重。
    - secret 时限：description 用一句不含 label 与 id 的通用文本。通用文本放在 `prompt_core`，或沿用 `canon_projector.py` 现有的英文描述方式。
  - 回答格式不变。

- [ ] **Step 1: 写失败测试**
  - `test_overdue_running_clock_is_not_protected`
  - `test_protected_countdowns_are_capped_by_remaining`
  - `test_public_clock_signal_uses_label_not_node_id`
  - `test_secret_clock_signal_reveals_neither_label_nor_id`
- [ ] **Step 2: 运行，确认失败**
- [ ] **Step 3: 实现**
- [ ] **Step 4: 运行通过**：`.venv/bin/python -m pytest -q tests/test_chapter_review_form_from_book_state.py tests/test_canon_projector.py tests/test_form_builder.py tests/test_chapter_review_form_e2e.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 5: 全量测试并提交**：`fix(canon_quality): protect only running clocks, cap them and keep secret clocks out of writer text`

---

### Task 18: 多题材世界 tick 端到端

**Files:**
- Modify: `tests/fixtures/world_seed/{xianxia,interstellar,urban_mystery}.json`：每本书新增 `world_tick` 段，内容为 adjudicate / actor_turn / conflict review 的桩输出；必要时给 world_seed 的意图加短的 `scheduled_in`，让第 1 章之后就到点。
- Create: `tests/test_world_tick_multi_genre.py`

**Interfaces:**
- 每本书第 1 章入 canon 后的 tick 至少包含：
  - 一个到点的 offstage 意图被结算，产生一个 public 事件；
  - 该事件按地图耗时传播，一个行动者立即 heard，一个入队；
  - 一个行动者回合提出一个新意图，地点可达；
  - 一个 major 变化，例如命名角色死亡，进入冲突，由桩 Codex 选择 `veto_world`。

- [ ] **Step 1: 写测试（参数化三个夹具，复用 `tests/test_chapter_delta_multi_genre.py` 的 `_start_book` 与管线夹具）**
  - `test_tick_after_chapter_one_commits_world_delta_and_keeps_reader_unchanged`
  - `test_major_change_becomes_conflict_and_codex_veto_is_applied`
  - `test_news_queue_is_delivered_after_chapter_two`
  - `test_three_genres_share_tick_pipeline`：用 spy 包住 `select_due_intents`、`WorldAdjudicator.adjudicate`、`ActorTurnRunner.run`、`route_conflict`、`commit_world_tick`，三本书的调用序列相同。
- [ ] **Step 2: 运行，确认通过。** 只增加测试与夹具；若失败，回到对应任务修实现，不修改夹具去迁就。`.venv/bin/python -m pytest -q tests/test_world_tick_multi_genre.py tests/test_no_story_content_in_runtime.py` → PASS
- [ ] **Step 3: 全量测试并提交**：`test: run a world tick after chapter one in three genres`

---

### Task 19: P3 收尾

**Files:**
- Modify: `tests/overfit_allowlist.toml`（`meta.completed_phase = "P3"`）、`Design-docs/CURRENT_ARCHITECTURE.md`、`Design-docs/CURRENT_DESIGN.md`、`Design-docs/DESIGN_STATUS.md`、`Design-docs/writing_flow_state_machine.md`（post-Canon 步骤顺序与冲突暂停）、`forwin/book_state/README.md`、`docs/codex-forwin-mcp.md`
- Test: `tests/test_no_story_content_in_runtime.py`、`tests/test_design_status_docs.py`、`tests/test_architecture_boundaries.py`

- [ ] **Step 1: 设置 `completed_phase = "P3"`，运行护栏测试。** `test_pending_removal_entries_name_a_future_phase` 若失败，说明还有 `remove_in = "P3"` 的条目，回到对应任务处理。
- [ ] **Step 2: 更新文档，写明以下内容并保留文档测试要求的全部标记**
  - tick 流程、`commit_world_tick`、历史重写对 tick delta 的处理；
  - 冲突模型、路由、Codex 审稿与选项语义；
  - 阻塞与恢复；
  - 迁移头 `0005_world_plan_conflicts`；
  - 与设计的取舍：推迟到 P4 的项目，包括 protection 生产者、critical / 里程碑、`rewrite_chapter`、`world.drift`、`checkable`。
- [ ] **Step 3: 运行文档与边界测试，然后全量测试并提交**：`docs: close world engine overhaul P3`

---

### Task 20: 生产 runbook（仅控制者执行，每一步经用户确认）

前提：P0、P1、P2 都已部署（P0 包含清库）。

1. 确认线上没有活跃的生成任务（`task_active_generation_check`），并且 P0 清库已经完成。
2. 备份数据库，`pg_dump`。
3. 合入 master，执行 `alembic upgrade head` 到 `0005_world_plan_conflicts`。
4. 配置 `FORWIN_CODEX_MODELS`，确认 Codex bridge 能以 `gpt-6-astra` 完成一次只读调用，`actual_model` 一致。
5. 通过 150 sync 部署：`ssh infra-150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`。
6. 冒烟：
   1. 用测试书写一章；
   2. 查看 post-Canon world 步骤的结果和 `world-conflicts` 列表；
   3. 用 MCP `world_conflict_list` 读取同一份列表。
