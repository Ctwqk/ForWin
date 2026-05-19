# Review Engine Upgrade Spec v2 — Cutover & Gap Closure

## Goal

闭合 `codex/review-engine-upgrade` 分支与原始设计(`docs/designs/review-engine-plan.md`)之间的实际差距,并把第一版被划为 out-of-scope 但现在已经成为瓶颈的几项提级。最终目标:engine 真正驱动决策,manual review 占比由"兜底"变成"显式分类",且每个决策都有审计追踪。

## Implementation Status

- Audit event persistence: implemented behind non-blocking event recording.
- Dashboard three-state chip: implemented from real `REVIEW_ENGINE_DECISION` payloads.
- Repair v2 orchestrator wiring: implemented with legacy-live shadow mode by default.
- Local rewrite executor: implemented behind `review_engine_local_rewrite_enabled`.
- Commit with obligation: implemented behind `review_engine_commit_with_obligation_enabled`.
- Arc/book budget: implemented behind `review_engine_arc_book_budget_enabled`; run `scripts/audit_obligation_distribution.py` before enabling.
- Live cutover: implemented behind `review_engine_live_cutover_enabled` and `review_engine_live_cutover_project_allowlist`; production phase advancement still requires elapsed observation windows.
- Legacy removal: not started; requires global cutover stability and separate PRs.

## Current audit settings (2026-05-19)

本节记录当前实现和容器部署如何回答后续审计关注的四类问题。这里的"代码默认值"和"pilot 容器 env"必须分开看:默认值用于安全回滚,pilot env 用于本次 30 章项目灰度验证。

### 1. Local rewrite 不允许写死任何单本书 canon 名

- `LocalRewriteExecutor` 不再提供故事特定默认值,也不再把 `{{地点}}` / `{{角色}}` 替换为固定中文名。
- placeholder 修复只允许从 `context_pack` 读锚点:
  - 角色锚点优先级:`active_entities[kind=character].name` → `allowed_entities[]` → `chapter_entry_targets[].entity_name` → `active_personality_contexts[].character_name`。
  - 地点锚点优先级:`map_context.active_locations[].location_name/name/location_id` → `active_entities[kind=location].name` → `map_context.visible_anchor_nodes[].name/node_id`。
- 若正文包含 `{{角色}}` 或 `{{地点}}` 但对应锚点缺失,executor 返回 `needs_writer`,mode=`missing_canon_placeholder_anchor`,不做本地替换。这样宁可升级到 writer repair,也不伪造 canon。
- repair loop 传入的是当前章节 `current_context`,不是空 `{}`。
- 防回归测试:
  - `tests/review_engine/test_local_rewrite_executor.py`
  - `tests/test_no_story_specific_hardcoding.py::test_local_rewrite_executor_has_no_case_specific_placeholder_defaults`

### 2. Flag 默认关闭,Phase 1 pilot 通过容器 env 显式开启

代码默认值仍全部为 `False`;这是设计要求,不能改成全局默认开启。当前 pilot 容器通过 `FORWIN_ENV_FILE=/tmp/forwin-review-cutover.env` 显式开启:

```text
FORWIN_REVIEW_ENGINE_REPAIR_V2_ENABLED=true
FORWIN_REVIEW_ENGINE_ARC_PATCHER_ENABLED=true
FORWIN_REVIEW_ENGINE_BOOK_PATCHER_ENABLED=true
FORWIN_REVIEW_ENGINE_OBLIGATION_VERIFIER_ENABLED=true
FORWIN_REVIEW_ENGINE_AUTO_APPROVE_ENABLED=true
FORWIN_REVIEW_ENGINE_LOCAL_REWRITE_ENABLED=true
FORWIN_REVIEW_ENGINE_COMMIT_WITH_OBLIGATION_ENABLED=true
FORWIN_REVIEW_ENGINE_ARC_BOOK_BUDGET_ENABLED=true
FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_ENABLED=true
FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_PROJECT_ALLOWLIST=e70538bbc774440cb53929cf7549dad8
```

`review_engine_live_cutover_project_allowlist` 当前只包含 30 章 pilot 项目 `e70538bbc774440cb53929cf7549dad8`。`engine_live_enabled()` 的语义是:

- `live_cutover=false`:legacy live,engine shadow。
- `live_cutover=true` 且 allowlist 非空:只有 allowlist 内项目 engine live;其它项目 legacy live,engine shadow。
- `live_cutover=true` 且 allowlist 为空:全局 engine live。只有 Phase 4 才允许这样设置。

### 3. Repair attempt 与 `body_truncated` 设置

`MAX_ATTEMPTS_PER_SCOPE` 按 scope 重量分级:

| scope | max attempts | 设置原因 |
|---|---:|---|
| `draft` | 2 | local 修复便宜,但两次仍失败通常不是单纯 draft 问题 |
| `chapter_plan` | 2 | chapter patch 仍可重试一次 |
| `band_plan` | 2 | band patch 成本中等 |
| `arc_plan` | 1 | arc patch 贵,失败直接升级 |
| `book_plan` | 1 | book patch 更贵,失败直接 manual |
| `subworld` | 2 | 元数据修复允许一次重试 |
| `active_rules` | 1 | 规则违反通常确定性强 |
| `operator` | 0 | operator / schema 错误直接 manual |

升级路径固定为:

```text
draft -> chapter_plan -> band_plan -> arc_plan -> book_plan -> manual_review
```

`body_truncated` 仍归 `draft` scope,但 local executor 不直接拼接正文;它返回 `needs_writer`,mode=`continue_from_last_complete_scene`,instruction 要求 writer 从最后一个完整 scene 继续写。若 draft scope 重试耗尽,按上面的升级路径进入 `chapter_plan`。

### 4. Arc/book budget 与 shadow/cutover 门槛

arc/book budget 默认值保留为:

```text
arc_max_p0_p1_per_arc = 2
arc_max_p1_p2_per_arc = 4
book_max_p0_per_book = 1
book_max_p1_p2_per_book = 3
```

启用 `FORWIN_REVIEW_ENGINE_ARC_BOOK_BUDGET_ENABLED=true` 前必须先跑:

```bash
python scripts/audit_obligation_distribution.py
```

本次 pilot 容器内审计结果:

```text
arc_buckets=0 arc_p0_p1_p95=0
book_buckets=0 book_p0_p1_p95=0
```

这说明当前历史样本没有可用于提高阈值的 arc/book obligation bucket,所以保留默认值。后续若 audit 显示默认值会阻断超过 5% 历史项目,必须先提高 defaults,再启用 budget flag。

cutover 推进仍按阶段执行:

| phase | 设置 | 进入下一阶段条件 |
|---|---|---|
| Phase 1 | 1 个 short pilot 项目 allowlist | ≥7 天 0 严重 mismatch |
| Phase 2 | 3 个 small 项目(`<50` 章) | ≥7 天 0 严重 mismatch |
| Phase 3 | 全部 small + medium(`<200` 章) | ≥7 天 0 严重 mismatch |
| Phase 4 | all,含长篇;allowlist 可置空 | Phase 3 达标 |

当前只完成 Phase 1 的单项目容器配置和 30 章 smoke。它**不满足**"60 章 live pilot 0 legacy safety-net fallback / 0 severe mismatch"门槛,因此不能据此删除 review legacy safety net。

### 5. 60 章 live pilot 审计字段

`DecisionEventType.REVIEW_ENGINE_DECISION` 的 `payload_json` 必须记录以下字段,用于证明每章是否真正由 review engine live 决策:

```text
live_or_shadow
live_source
shadow_source
engine_live
legacy_shadow_evaluated
legacy_safety_net_used
shadow_mismatch
severe_shadow_mismatch
legacy_outcome
engine_outcome
```

60 章测试的删除条件不是"项目跑完",而是审计脚本通过:

```bash
python3 scripts/audit_review_engine_cutover.py --project-id <project_id> --expected-chapters 60
```

脚本通过要求:

- 1..60 每章都有 `review_engine_decision` 事件。
- 每章 `live_or_shadow=live`。
- 每章 `live_source=engine` 且 `engine_live=true`。
- `legacy_safety_net_used=false`。
- `severe_shadow_mismatch=false`。

`legacy_shadow_evaluated=true` 只表示 legacy 被反向 shadow 用来审计 parity,不表示 legacy 接管 live 决策。真正禁止的是 `legacy_safety_net_used=true` 或 `live_source=legacy`。

当前代码边界:

- `review_engine.rules.review_outcome` 已经是 engine-native policy,不再包装 `ReviewOutcomeRouter`。
- `review_engine.rules.obligation_scope` 已经是 engine-native policy,orchestrator 不再直接调用 `ObligationScopeRouter`。
- `ReviewOutcomeRouter` 和 `RepairPolicy` 在 60 章 pilot 前仍可作为 flag-off fallback / reverse shadow 参考;pilot 审计通过后再删除这些 safety-net 入口。

## Scope

### 包含

- Gap 1:`repair_v2` 接入 orchestrator(rule 写了但没 wired)。
- Gap 2:engine DecisionEvent 持久化(audit 模块是死代码)。
- Gap 3:原 4 dispatcher 的 shadow → live cutover(strangler-fig 最后一步)。
- Gap 4:dashboard 三态 chip(缺 `系统阻断`)。
- Gap 5:`commit_with_obligation` 升级为 engine 一等 outcome。
- Gap 6:arc/book 维度的 obligation budget。
- 提级 A:local rewrite outcome 的真正执行器。
- 提级 B:auto-approve interval counter 纪律。
- 提级 C:cutover 稳定后的 legacy 代码删除时间表。

### 不包含

- LLM-based supervisor(仍然反对)。
- v2 plan 之外的新决策类型。
- Production scheduler quota 字段的进一步语义扩展(独立 spec)。

## Audit: 当前分支实际状态

基于 `codex/review-engine-upgrade` (tip `a5e6491`),相对 master 领先 4 个 commit:

| Plan 项 | 状态 | 关键证据 |
|---|---|---|
| P0.1 arc→band 降级 | ✅ DONE | `forwin/reviewer/hub.py:630` 已改 `preserve_v4=(merged_scope in {"arc","world_model"})` |
| P0.2 production review_chapters 执行 | ✅ DONE | `forwin/production/executor.py:127` |
| P0.3 deferred over_budget 传参 | ✅ DONE | `forwin/narrative_obligations/transaction.py:162` |
| P1 engine 骨架 + rule table | ✅ DONE(shadow) | `forwin/review_engine/engine.py`、`audit.py`、`parity.py` |
| P1 engine cutover 切流量 | ❌ 未做 | `quality_gates.py:517-528` 只 `compare_shadow_decisions()` 记日志,legacy outcome 仍是 live |
| P2 RepairPolicy issue-scope 重写 | ⚠️ rule 写了,没 wired | `repair_v2.py` + `compare_repair_v2_shadow` 存在,`grep repair_v2 quality_gates.py` 0 命中,`reviser/policy.py:50` 仍纯 attempts |
| P3 arc/book patcher | ✅ DONE | `arc_plan_patcher.py`、`book_plan_patcher.py` + validator |
| P3 arc/book outcome 一等化 | ✅ DONE(authoritative) | `quality_gates.py:530` `decide_structural_patch` 直接驱动 |
| P3 arc completion gate | ✅ DONE | `quality_gates.py:904` `unresolved_arc_patch_debt` |
| P3 writer arc-debt 注入 | ✅ DONE | `writer/prompt_core/constraints.py` `structural_patch_debt` section |
| P4.1 verifier 4 个生命周期方法 | ✅ DONE | `narrative_obligations/repository.py:124-190` |
| P4.1 waive 拒绝 system actor | ✅ DONE | `repository.py:178-179` |
| P4.1 accept 后自动 verify+expire | ✅ DONE | `orchestrator_loop_core/acceptance.py:97-108` |
| P4.2 auto-approve 两条规则 | ✅ DONE | `auto_approve.py` `copilot_safe_warn` + `review_interval_safe` |
| P5 review detail engine 决策展示 | ✅ DONE | `app_task_progress.js:511` 显示 rule_id/outcome/reason/missing |
| P5 dashboard rule_id 聚合 | ⚠️ 部分 | `dashboard.py` 实现了聚合,但**没有数据源**(见 Gap 2) |
| P5 dashboard 三态 chip | ❌ 只有 2 态 | `dashboard.py:52-57` 缺 `系统阻断` |
| audit 模块投产 | ❌ 死代码 | `audit.py` 的 `build_decision_event_payload` / `digest_decision_input` 整个 branch 0 调用 |

底线:**arc/book 一条路是真做了**(engine driving + verifier 闭环 + arc completion gate),其它都还在 shadow 或者纸面状态。

## Gap 1 — Wire `repair_v2` into orchestrator

### Current

- `forwin/review_engine/rules/repair_v2.py:decide_repair_v2()` 完整实现了 issue-scope 路由,映射 `IssueScope → DecisionOutcome`。
- `compare_repair_v2_shadow(old_scope, new_scope, enabled)` 已是 strangler-fig comparator 形状。
- `forwin/config.py` 有 `review_engine_repair_v2_enabled` flag。
- **整个 branch 里没有任何文件调用上述任意一个**。
- `forwin/reviser/policy.py:50` `RepairPolicy.decide()` 仍然:
  ```python
  default_scope = REPAIR_SCOPE_SEQUENCE[min(attempts_completed, len(REPAIR_SCOPE_SEQUENCE) - 1)]
  ```

### Tasks

1. 定位 `RepairPolicy.decide()` 的 orchestrator 调用点(预期在 `forwin/reviser/repair_loop.py` 或 `orchestrator_loop_core/quality_gates.py` 调 `_drive_repair_loop()` 处)。
2. 在该调用点插入:
   ```python
   v2_decision = decide_repair_v2(decision_input)
   v2_scope = v2_decision.sub_action.get("scope", "")
   shadow = compare_repair_v2_shadow(
       old_scope=legacy_decision.scope,
       new_scope=v2_scope,
       enabled=config.review_engine_repair_v2_enabled,
   )
   chosen_scope = shadow.live_scope
   ```
3. 写一条 `DecisionEvent`(`REVIEW_ENGINE_DECISION` 类型,见 Gap 2),payload 含 `v2_scope`、`legacy_scope`、`shadow_mismatch`。
4. flag-off:`chosen_scope == legacy_decision.scope`,纯 shadow。
5. flag-on:`chosen_scope == v2_scope`,真正 issue-scope driven。

### Acceptance

- Shadow mode 跑 ≥200 章 replay,parity log 落地,mismatch 类型分布在事件表里可查。
- flag-on:fixture identity_ambiguity issue 首次 repair 直接 `arc_plan`,不走 draft→chapter→band 阶梯。
- `tests/review_engine/test_repair_v2.py` + 新 `tests/review_engine/test_repair_v2_integration.py`,覆盖 5 类 issue × {first attempt, retry, exhausted}。

### `MAX_ATTEMPTS_PER_SCOPE` (locked)

```python
MAX_ATTEMPTS_PER_SCOPE: dict[IssueScope, int] = {
    "draft": 2,           # local 便宜,2 次修不好就不是 local 问题
    "chapter_plan": 2,
    "band_plan": 2,
    "arc_plan": 1,        # arc patch 贵,单次失败直接升 book 或 manual
    "book_plan": 1,
    "subworld": 2,
    "active_rules": 1,    # 规则违反通常是确定性的,重试无意义
    "operator": 0,        # operator 错误直接 manual
}
```

升级路径(`_escalate`):

```
draft → chapter_plan → band_plan → arc_plan → book_plan → manual_review
subworld → manual_review
active_rules → manual_review
operator → manual_review
```

`decide_repair_v2` 内嵌检查:
```python
if input.attempts_completed >= MAX_ATTEMPTS_PER_SCOPE.get(primary.scope, 2):
    next_scope = _escalate(primary.scope)
    if next_scope == "manual_review":
        return Decision(outcome="manual_review", rule_id="repair_v2_attempts_exhausted", ...)
    return Decision(
        outcome=_SCOPE_TO_OUTCOME[next_scope],
        rule_id=f"repair_v2_escalated_from_{primary.scope}_to_{next_scope}",
        ...,
    )
```

### Risk

- legacy `RepairPolicy.decide()` 的 attempt-count "限流"作用 v2 没继承——可能某些循环失败 chapter 缺少 max_attempts 截断。**缓解**:上面的 `MAX_ATTEMPTS_PER_SCOPE` + `_escalate` 序列。
- 重 scope(arc/book)只给 1 次可能在边界情况误升 manual。**缓解**:Gap 6 budget 通过时 arc/book 已经是"系统认为最小可控 scope",1 次失败仍然升级是设计意图,不算误升。

## Gap 2 — Persist engine DecisionEvent

### Current

- `forwin/review_engine/audit.py:11` `build_decision_event_payload(...)` 构造 payload。
- `forwin/review_engine/audit.py:29` `digest_decision_input(...)` 输入哈希。
- **整个 branch 里 0 调用**(`git grep build_decision_event_payload codex/review-engine-upgrade` 仅命中定义)。
- `forwin/review_engine/dashboard.py:16` 读 `payload.get("outcome")` 和 `payload.get("rule_id")` —— 现状下永远空。
- `forwin/project_ops/reviews.py:267` `_latest_review_engine_decision(decision_refs)` 同样依赖 `payload["rule_id"]` —— 现状下永远 `{}`。

也就是说,dashboard 和 review detail 的"engine 决策"现在是**接口通了但永远没数据**。

### Tasks

1. 新增 `DecisionEventType.REVIEW_ENGINE_DECISION`(在 `forwin/orchestrator_loop_core/common.py` 的枚举里)。
2. 在 `forwin/orchestrator_loop_core/governance.py` 旁加一个 helper:
   ```python
   def _record_engine_decision_event(
       self, *, decision: Decision, decision_input: DecisionInput,
       shadow_mismatch: bool = False,
       related_object_type: str = "", related_object_id: str = "",
   ) -> None:
       payload = build_decision_event_payload(
           decision=decision,
           input_digest=digest_decision_input(decision_input),
           shadow_mismatch=shadow_mismatch,
       )
       self._record_decision_event(
           updater=updater,
           project_id=decision_input.project_id,
           chapter_number=decision_input.chapter_number,
           event_family="review_engine",
           event_type=DecisionEventType.REVIEW_ENGINE_DECISION,
           scope="chapter",
           summary=f"engine decided {decision.outcome} via {decision.rule_id}",
           reason=decision.reason,
           related_object_type=related_object_type,
           related_object_id=related_object_id,
           payload=payload,
       )
   ```
3. 调用点:
   - `quality_gates.py:519`(shadow comparison)—— 写 shadow decision event,`shadow_mismatch=comparison.shadow_mismatch`。
   - `quality_gates.py:530`(`decide_structural_patch` 真驱动)—— 写 structural decision event。
   - Gap 1 完成后:repair_v2 site 也写一条。
   - Gap 5 完成后:`commit_with_obligation` rule site 写一条。
4. `dashboard.py:38` `_event_payload()` 已经能解析 payload dict,**不用改**——只要事件真的被写就有数据。

### Acceptance

- `tests/review_engine/test_audit.py` 扩展为集成测试:跑一次 mock chapter accept → DecisionEvent 表里有 `REVIEW_ENGINE_DECISION` 行 → dashboard 输出非空。
- `tests/test_api_pages_rendering.py` 加 fixture:启用 engine + 接受 chapter → API 返回 `review_engine_decision.rule_id` 非空字符串。

## Gap 3 — Cutover for the original four dispatchers

### Current

`quality_gates.py:517`:
```python
shadow_comparison = compare_shadow_decisions(
    live=decision_from_review_outcome(outcome),     # legacy drives
    shadow=AutoDecisionEngine(...).decide(decision_input),  # engine shadowed
)
```

对所有非 arc/book 的 review outcome,legacy 仍是 source of truth。

### Cutover criteria

允许 flip 的条件(必须**全部满足**):

- Gap 2 已上线,DecisionEvent 持久化通畅。
- 历史 replay 集 ≥1000 chapter shadow run 0 mismatch warning。
- 或生产 shadow 7 天 mismatch 率 < 0.1% 且 mismatch 类型已归档分析。
- Gap 1 已上线且本身的 v2 shadow 也已达稳定标准。
- `tests/review_engine/test_rule_parity.py` 覆盖原 4 个 dispatcher 的全部 branch。

### Tasks

1. 新增 config flag `review_engine_live_cutover_enabled`(默认 False)。
2. 改 `quality_gates.py:517-528`:
   ```python
   engine_decision = AutoDecisionEngine(build_review_outcome_rules()).decide(decision_input)
   legacy_decision = decision_from_review_outcome(outcome)
   if config.review_engine_live_cutover_enabled:
       live, shadow = engine_decision, legacy_decision
   else:
       live, shadow = legacy_decision, engine_decision
   comparison = compare_shadow_decisions(live=live, shadow=shadow)
   if comparison.shadow_mismatch:
       logger.warning(...)
   # 后续用 live 派发
   ```
3. 双向 mismatch log 都保留——cutover 后 legacy 作 shadow,保证可回退期间仍能比对。
4. 派发逻辑改为消费 `live.outcome`,不再读 `outcome.action`。
5. 完整 e2e 测试在 flag-on / flag-off 两种模式下都跑。

### Acceptance

- flag-off:行为字节级等同当前(`tests/test_orchestrator_*.py` 全过)。
- flag-on:同 fixture 集合下 engine drives,e2e 全过,mismatch 日志为空。

### Risk

- engine rule 漏一个 legacy branch → 静默丢决策。**缓解**:cutover 前必须通过 `test_rule_parity.py` 的全 branch 覆盖。
- 双向比对成本 → 加 `compare_shadow_decisions` 的 sampling rate(配置项,默认 100%,可降到 10% 在生产)。

## Gap 4 — Three-state chip

### Current

`forwin/review_engine/dashboard.py:52`:
```python
def _status_chip(payload):
    rule_id = str(payload.get("rule_id") or "")
    reason = str(payload.get("reason") or "")
    if "policy_disabled" in rule_id or "policy disabled:" in reason:
        return "可自动处理但策略关闭"
    return "需要人工判断"
```

缺 `系统阻断`(对应 `outcome == "system_block"`)。

### Tasks

1. 改 `_status_chip` 读 `payload.get("outcome")`:
   ```python
   outcome = str(payload.get("outcome") or "")
   if outcome == "system_block":
       return "系统阻断"
   if "policy_disabled" in rule_id or "policy disabled:" in reason:
       return "可自动处理但策略关闭"
   return "需要人工判断"
   ```
2. `build_waiting_review_breakdown` 返回的 row 加 `status_chip_count` 三态分布(可选,看 UI 需要)。
3. `forwin/api_pages_home.py:88` 渲染处不需要改,模板已经 `html.escape(item['status_chip'])`。
4. `forwin/ui_assets/home/page.css` 加 `.status-chip[data-chip=系统阻断]` 颜色(红/警示色)。

### Acceptance

- `tests/review_engine/test_dashboard.py` 加 3 种 outcome fixture × 3 种 chip。
- 浏览器测试(`tests/browser/test_governance_and_chapters.py`)断言三态都能渲染。

依赖 Gap 2(没数据写,chip 永远空)。

## Gap 5 — `commit_with_obligation` 成为一等 outcome

### Current

- `forwin/review_engine/types.py` 的 `DecisionOutcome` Literal 已含 `"commit_with_obligation"`。
- **没有 rule 会产出这个 outcome**。
- `DeferAcceptanceTransaction.run()`(`narrative_obligations/transaction.py:34`)实际达成 commit with obligation 的语义,但走的是 `outcome.action == "defer_with_chapter_plan_patch"` / `"defer_with_band_plan_patch"` 的间接路径。

### Tasks

1. 新文件 `forwin/review_engine/rules/commit_with_obligation.py`:
   ```python
   def decide_commit_with_obligation(input: DecisionInput) -> Decision:
       primary = classify_primary_issue(review=input.review, signals=input.signals)
       if primary.scope not in {"chapter_plan", "band_plan"}:
           return Decision(outcome="manual_review", rule_id="commit_with_obligation_wrong_scope", ...)
       if not input.plan_layer_health.has_plan_patch_for(primary.scope):
           return Decision(outcome="manual_review", rule_id="commit_with_obligation_missing_patch",
                           missing_evidence=["plan_patch"], ...)
       if input.budget is not None and input.budget.over_budget:
           return Decision(outcome="system_block", rule_id="commit_with_obligation_over_budget", ...)
       return Decision(outcome="commit_with_obligation", rule_id="commit_with_obligation_eligible", ...)
   ```
2. `quality_gates.py` 在 `decide_structural_patch` 之后、`outcome.action defer_*` 之前增加:
   ```python
   commit_decision = decide_commit_with_obligation(decision_input)
   if commit_decision.outcome == "commit_with_obligation":
       return _execute_commit_with_obligation(...)  # 包装 DeferAcceptanceTransaction
   ```
3. `_execute_commit_with_obligation` 复用 `_prepare_deferred_acceptance_if_needed()` 的实现,但显式以 engine outcome 入口。
4. 旧 path(`outcome.action == "defer_with_*"`)在 cutover 完成后下线。

### Acceptance

- Fixture:chapter-level identity issue + 有效 plan patch + budget 未超 → engine `commit_with_obligation` → `DeferAcceptanceTransaction.run()` 成功 → canon gate admit。
- Fixture:同上但 budget 超 → engine `system_block`,无 obligation 创建。
- `test_orchestrator_deferred_acceptance.py` 改写为通过 engine outcome 入口。

## Gap 6 — Arc/book obligation budget

### Current

- `forwin/narrative_obligations/budget.py:28` `evaluate_obligation_budget()` 衡量 chapter 和 band 维度的 P0/P1/P2 数。
- arc / book 维度 budget 不存在。
- `_persist_structural_patch_outcome`(`quality_gates.py:700`)不调用 budget evaluator,创建 obligation 时不检查 arc/book 层是否超额。

### Tasks

1. 扩 `ObligationBudgetPolicy`:
   - `arc_max_p0_p1_per_arc: int = 2`
   - `arc_max_p1_p2_per_arc: int = 4`
   - `book_max_p0_per_book: int = 1`
   - `book_max_p1_p2_per_book: int = 3`
2. `evaluate_obligation_budget()` 多查 origin_chapter 落在哪个 arc / book,计入对应 ledger。
3. `quality_gates.py:_persist_structural_patch_outcome` 创建 obligation 之前调 budget evaluator,超额时返回 `[f"arc_obligation_budget_exceeded:{arc_id}"]` 而非创建,**并触发 engine `system_block` 决策事件**(Gap 2 通路)。
4. `decide_structural_patch` 读 `input.budget`,在 budget 超额时直接 emit `system_block` outcome,跳过 patcher 调用。

### Acceptance

- Fixture:同一 arc 内连续 3 个 P1 identity_ambiguity → 第 3 个 engine emit `system_block`,无 arc patch 创建。
- `tests/test_obligation_budget.py` 加 arc / book 维度测试。

## 提级 A — Local rewrite outcome 的执行器

### Why promote

`decide_repair_v2` 已经把 `placeholder_leakage`、`body_truncated`、`body_duplicate_span`、`internal_state_key_leakage`、`subworld_admission_unauthorized_new_entity` 映射到 `draft` scope → `local_repair` outcome。但 **branch 里没有 executor 消费 `local_repair`**——这些问题章节在 Gap 1 上线后会得到正确的 outcome,但无人执行,实际效果等于停在 needs_review。

### Current state

- `forwin/orchestrator_loop_core/quality_gates.py` 有 `_apply_canon_name_drift_autofix()`、`_apply_subworld_admission_autofix()`、`_apply_placeholder_leakage_autofix()`——已经覆盖部分 issue。
- 但这些是 canon gate path 上的 autofix,不是 review_engine `local_repair` outcome 的统一入口。
- `body_truncated`、`body_duplicate_span`、`internal_state_key_leakage`、`style_repetition` 当前无 autofix。

### Tasks

1. 新文件 `forwin/reviser/local_rewrite_executor.py`:
   ```python
   class LocalRewriteExecutor:
       AUTOFIX_DISPATCH = {
           "placeholder_leakage": _rewrite_placeholder,
           "bare_role_placeholder_leakage": _rewrite_placeholder,
           "body_truncated": _rewrite_truncation,
           "body_duplicate_span": _drop_duplicate,
           "internal_state_key_leakage": _strip_json_keys,
           "subworld_admission_unauthorized_new_entity": _generalize_entity,
       }
       def execute(self, *, draft, issue_kind, signals, context_pack) -> RewriteResult: ...
   ```
2. orchestrator 在 `decision.outcome == "local_repair"` 时调 executor,生成新 draft,触发 re-review。
3. 与现有 canon-gate autofix 合并:同一 issue 类型只跑一遍,优先 engine outcome 入口。
4. flag `review_engine_local_rewrite_enabled`(默认 False)。

### Acceptance

- Fixture chapter `placeholder_leakage` → engine `local_repair` → executor rewrite → re-review pass → no manual。
- 关 flag 时 fall back 到 canon-gate autofix,行为不退化。

## 提级 B — Auto-approve interval counter discipline

### Why promote

`review_interval_safe` 规则上线后,如果"间隔计数"在每次 auto-approve 时被错误地重置,会导致 auto-approve 连续触发,review interval 失去作用。这个细节在 v1 plan 的 open question #3 留下未答。

### Tasks

1. 定位 review_interval 计数的更新点(应在 `accept_review` 中,无论 auto 还是 manual)。
2. 确保**每个 accept 都增加计数**,无论是 `human_approved` / `checkpoint_approved` / `auto_approved`。
3. `review_interval_safe` 检查:`(chapters_since_last_full_review % review_interval_chapters == 0)`,而不是 `since_last_auto_approve`。
4. auto-approve 写入 DecisionEvent 时,payload 含 `chapters_since_last_full_review` 用于 dashboard 诊断。

### Acceptance

- Fixture:interval=5,连续 12 章全部 warn-only + canon pass → 章节 5、10 命中 interval(必须走完整 review),其余 8 章 auto-approve。

## 提级 C — Legacy dispatcher 删除时间表

### Why promote

cutover 完成后如果保留两套并存,会出现:
- 增加新功能时不知道改哪边。
- 双写日志增加噪声。
- review parity 测试维护成本上升。

不在本 spec 实施,但在本 spec **写明删除触发条件**,避免遗忘。

### Trigger conditions

`Gap 3` flag-on 上线后,满足全部条件即可启动 review legacy safety-net 删除:

- 一个完整 60 章 live pilot 通过 `scripts/audit_review_engine_cutover.py --expected-chapters 60`。
- 60 章期间 0 `legacy_safety_net_used`。
- 60 章期间 0 severe mismatch。
- `tests/review_engine/test_rule_parity.py` 仍然全过。
- `review_engine_live_cutover_enabled` 在所有项目稳定开启。

### Targets

- `forwin/reviewer/outcome.py:ReviewOutcomeRouter` → 删除,引用切到 `build_review_outcome_rules()`。
- `forwin/reviser/policy.py:RepairPolicy.decide()` → 删除,引用切到 `decide_repair_v2`。
- `forwin/planning/obligation_scope_router.py:ObligationScopeRouter` → 删除,引用切到 engine。
- `forwin/reviser/final_acceptance.py:FinalAcceptanceGate` **保留**作为 callable 子函数,仅删 orchestrator 直接调用,改由 engine rule 调用。

## Phase order

```
Gap 2 (audit persistence)  ──┬─→ Gap 4 (三态 chip)
                             │
                             ├─→ Gap 1 (repair_v2 wire) shadow → flag-on
                             │       │
                             │       └─→ 提级 A (local rewrite)
                             │
                             └─→ 提级 B (interval counter)

Gap 5 (commit_with_obligation)  ─ Gap 2 后可启动
Gap 6 (arc/book budget)          ─ 独立

Gap 3 (cutover)  ─ Gap 1/Gap 2 稳定 ≥7d 后启动
                  └─→ 提级 C (legacy removal) cutover 后 ≥30d
```

推荐顺序:

1. **Gap 2** — audit persistence。骨牌起点,无行为变化,解锁 dashboard 真实数据。
2. **Gap 4** — 三态 chip,Gap 2 之后立即跟进。
3. **Gap 1** — repair_v2 wire,**shadow mode 先**,parity 数据稳定后切 flag-on。这是减少非 arc/book manual review 的最大杠杆。
4. **Gap 6** — arc/book budget。短工作量,补 P3 的语义缺口。
5. **Gap 5** — commit_with_obligation 一等化。把 deferred path 收回 engine 语义。
6. **提级 A** — local rewrite executor。Gap 1 一旦 flag-on,这一项的价值放大。
7. **Gap 3** — cutover。前置依赖全绿后启动。
8. **提级 B** — interval counter discipline,小补丁。
9. **提级 C** — legacy removal,长尾。

## Risk

| 风险 | 缓解 |
|---|---|
| Gap 1 v2 缺少 attempt 上限,导致同 scope 死循环 | `decide_repair_v2` 内嵌 `MAX_ATTEMPTS_PER_SCOPE`,超限升级或转 manual |
| Gap 2 DecisionEvent 写入失败导致 chapter 流程阻塞 | event 写入用 try/except,失败只 warn 不阻塞决策 |
| Gap 3 cutover 后 mismatch 漏检 | 反向 shadow 至少跑 30 天,sampling 默认 100% |
| Gap 5 与现有 `_prepare_deferred_acceptance_if_needed` 双入口冲突 | engine outcome 一旦命中,跳过老 path;否则才走老 path |
| Gap 6 budget 突然超额导致历史项目大量 system_block | budget 默认值需保守,提供 per-project override |
| 提级 A 改写后引入新 issue | re-review 必须跑通才算 success;否则 fall back 到 manual,不能"假完成" |

## Verification

每个 gap closure 都必须:

1. 不破坏现有测试。
2. 至少一个集成测试覆盖新路径(端到端,不只是单元)。
3. 涉及 outcome 变化的,必须有 shadow mode → flag-on 两阶段。
4. DecisionEvent 审计无遗漏(Gap 2 完成后这是硬要求)。

## Rollback

所有 gap 均由独立 config flag 控制,默认 False:

- `review_engine_repair_v2_enabled`
- `review_engine_live_cutover_enabled`
- `review_engine_local_rewrite_enabled`
- `review_engine_arc_book_budget_enabled`(Gap 6,新增)
- `review_engine_commit_with_obligation_enabled`(Gap 5,新增)

任一 gap 出问题,关 flag 即可回到当前 branch 的行为(arc/book 一路除外——那一路已经 authoritative,但有自己的 `review_engine_arc_patcher_enabled` / `review_engine_book_patcher_enabled` flag)。

## Resolved settings and ownership

- `MAX_ATTEMPTS_PER_SCOPE`:已按 scope 分级,见 `Current audit settings`。
- Gap 3 cutover:已确定 per-project allowlist 灰度,空 allowlist 只允许 Phase 4 全局 cutover。
- `body_truncated`:留在 `draft` scope,使用 `continue_from_last_complete_scene` writer continuation mode;本地 executor 不伪造正文。
- Gap 6 budget defaults:默认值见 `Current audit settings`;启用前必须跑 `scripts/audit_obligation_distribution.py`,若默认值阻断超过 5% 历史项目则先提高 defaults。
- Legacy removal ownership:单 owner,4 个独立 PR,按依赖顺序执行:
  1. `ReviewOutcomeRouter`
  2. `ObligationScopeRouter`
  3. `RepairPolicy`
  4. `FinalAcceptanceGate` 直调路径
