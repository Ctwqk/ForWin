# Review Engine: Unified Decision Layer for Reducing Manual Review

## Goal

减少人工审核压力,通过两步:

1. 修掉三个已经验证存在的真 bug,这些 bug 让本来该自动处理的章节被错误压到人工或被错误降级。
2. 抽出 `AutoDecisionEngine` 作为单一决策入口,取代当前分散在四个模块的 dispatch 逻辑,把 `manual_review` 从"兜底"变成"显式分类"。

最终结果:对每一个 `(issue_type, severity, scope, mode, state, budget)` 组合,系统都能给出确定性、可审计、可配置的 outcome,而不是 fall through 到人工。

## Scope

### 包含

- P0:三个已验证 bug 的修复。
- P1:`AutoDecisionEngine` 骨架 + decision table v1,**不改变现有行为**,只统一入口和审计。
- P2:`RepairPolicy` 从 attempt-count driven 重写为 issue-scope driven。
- P3:Arc/book plan patcher 落地,通过 engine 注册为新 outcome。
- P4:`ObligationResolutionVerifier` 闭环 + auto-approve policy(safe warn / review interval)。
- P5:UI / dashboard 消费 engine 输出。

### 不包含

- LLM-based supervisor。本 plan 明确避免引入额外的 LLM 判断层,所有决策都是规则驱动、可重放。
- `waived` obligation 的自动化(必须保留人工)。
- 跨项目策略共享 / 多租户策略管理(后续 plan)。
- 重写 canon admission gate 内部逻辑——engine 包装它,不重写它。

## Background

### 当前的四个 dispatcher

| 模块 | 文件 | 决策依据 | 输出 |
|---|---|---|---|
| `ReviewOutcomeRouter` | `forwin/reviewer/outcome.py:71` | review verdict + signals + obligations | `commit_clean` / `local_rewrite` / `defer_with_*` / `manual_review_required` / `block` |
| `RepairPolicy` | `forwin/reviser/policy.py:29` | **attempts_completed** + verdict + mode | `repair(scope)` / `pause_for_review` / `final_force_accept_gate` |
| `ObligationScopeRouter` | `forwin/planning/obligation_scope_router.py:55` | issue type + scope hint | `defer_with_chapter_plan_patch` / `defer_with_band_plan_patch` / `manual_review_required` / `block` |
| `FinalAcceptanceGate` | `forwin/reviser/final_acceptance.py:23` | repair verification + mode + residual | `force_accept` / `manual_review_required` |

orchestrator(`forwin/orchestrator_loop_core/quality_gates.py`)串联它们,但每个模块独立决定 manual fallback,导致 manual 是默认而非显式分类。

### 已验证的 bug(grep 过)

1. **`forwin/reviewer/hub.py:637`** — `merged_scope = "band" if merged_scope == "arc" else merged_scope` 把 arc 级 repair 强制降为 band。违反设计文档中"identity / artifact / countdown / terminal state 默认至少 band,触及主线则 arc"。
2. **`forwin/production/executor.py`** — `ProductionPlan.review_chapters` 由 `planner.py:49` 填充,但 `executor.execute()` 不消费它。UI/config 给用户"自动 review quota"的错觉。
3. **`forwin/narrative_obligations/transaction.py:141`** — `DeferAcceptanceTransaction.run()` 调用 `evaluate_canon_admission()` 时**不传** `over_budget` 参数,默认 False。`evaluate_obligation_budget()` 的结果在这条路径上被丢弃,obligation budget 没有真正成为 deferred 准入约束。

### 半成品:verifier worktree

`obligation_verifier.py` 已经存在于 worktree(75 行,见 `.claude/worktrees/practical-engelbart-90482d/forwin/canon_quality/obligation_verifier.py`),提供 `verify()` 方法返回 `pass/warn/fail`。但**没有**对应的状态流转方法(`mark_obligation_resolved` / `expire_obligation` / `waive_obligation`)。需要在 P4 补齐。

### `RepairPolicy` 的根本设计偏离

```python
default_scope = REPAIR_SCOPE_SEQUENCE[min(attempts_completed, len(REPAIR_SCOPE_SEQUENCE) - 1)]
```

scope 由尝试次数决定,而不是由 issue 类型决定。一个本质上是 arc-level identity ambiguity 的问题,会被当作 chapter draft 重试,失败后升到 chapter plan,再失败才到 band。整个升级过程不解决根因。这是 P2 的核心改造目标。

## Design

### `AutoDecisionEngine` 接口

新模块:`forwin/review_engine/engine.py`

```python
@dataclass(frozen=True)
class DecisionInput:
    project_id: str
    chapter_number: int
    review: ReviewVerdict
    signals: list[CanonQualitySignal]
    open_obligations: list[NarrativeObligation]
    operation_mode: Literal["blackbox", "copilot", "checkpoint"]
    attempts_completed: int
    prior_scope_history: list[str]
    budget: ObligationBudgetResult | None
    target_total_chapters: int
    plan_layer_health: PlanLayerHealth  # 各层 patch 数 / overdue obligation 数

@dataclass(frozen=True)
class Decision:
    outcome: Literal[
        "auto_approve",
        "local_repair",
        "chapter_patch",
        "band_patch",
        "arc_patch",
        "book_patch",
        "commit_with_obligation",
        "manual_review",
        "system_block",
    ]
    reason: str
    rule_id: str            # 触发哪条 decision rule,用于审计
    missing_evidence: list[str]  # 比如 ["payoff_test", "arc_patch", "deadline"]
    routed_from: str        # 兼容观察:原本会落到哪个 dispatcher 的什么 outcome
    sub_action: dict        # 比如 {"scope": "band", "patch_id": "..."},供执行层消费

class AutoDecisionEngine:
    def __init__(self, rules: DecisionRuleTable): ...
    def decide(self, input: DecisionInput) -> Decision: ...
```

### Decision rule table

每条 rule 形如:

```python
DecisionRule(
    rule_id="copilot_safe_warn_auto_approve",
    when=lambda i: (
        i.operation_mode == "copilot"
        and i.review.verdict == "warn"
        and not _has_error_signals(i.signals)
        and not _has_blocking_obligations(i.open_obligations)
    ),
    outcome="auto_approve",
    reason="warn-only, no error signals, no blocking obligations",
)
```

规则按优先级线性扫描,第一个 match 即返回。**所有规则可读、可审计、可单测**。

### 与现有模块的关系

```
┌─────────────────────────────────┐
│  AutoDecisionEngine.decide()    │
│  ┌─────────────────────────────┐│
│  │  DecisionRuleTable          ││
│  │  ├── facts (from signals)   ││
│  │  ├── budget                 ││
│  │  ├── obligation lifecycle   ││
│  │  └── plan layer health      ││
│  └─────────────┬───────────────┘│
│                │                │
│  ┌─────────────▼───────────────┐│
│  │  Specialized rule modules   ││
│  │  (查表式调用,非 fallback)   ││
│  │                             ││
│  │  ReviewOutcomeRouter        ││  ← 仍然提供 facts → outcome 子函数
│  │  ObligationScopeRouter      ││  ← 仍然提供 scope hint 子函数
│  │  RepairPolicy               ││  ← 重写为 scope-aware decide()
│  │  FinalAcceptanceGate        ││  ← 仍然提供 force_accept 判定子函数
│  └─────────────────────────────┘│
└─────────────────┬───────────────┘
                  │
                  ▼
   orchestrator_loop_core/quality_gates.py
   只调 engine.decide(),按 outcome 派发到执行
```

orchestrator 只看 `Decision.outcome`,不再分别调四个 dispatcher。

### 审计接口

每次 `decide()` 写一条 `DecisionEvent`:

```
project_id, chapter_number, rule_id, outcome, reason,
missing_evidence, input_digest, timestamp
```

支持后续:
- 回放(给定 input,验证 outcome 仍然相同)
- 策略调优(哪些 rule 最常 hit、哪些 manual 占比最高)
- UI 展示(为什么这章是 manual——给出 rule_id + missing_evidence)

## Phases

### P0:Bug 修复 (1 PR each, 独立可合)

#### P0.1 — Remove arc → band downgrade

- **File:** `forwin/reviewer/hub.py:637`
- **Change:** 删除 `merged_scope = "band" if merged_scope == "arc" else merged_scope` 这一行。
- **Risk:** 删除后 `arc` scope 会流向下游 `RepairPolicy`,而 `RepairPolicy` 当前 `REPAIR_SCOPE_SEQUENCE` 是否含 `arc` 需要 check。如果不含,会触发 fallback;P0 阶段允许 fallback,P2 阶段消化。
- **Acceptance:**
  - 现有测试不退化(`pytest tests/test_reviewer_hub.py` + `tests/test_repair_*`)。
  - 新增 unit:输入一个 reviewer arc-level identity_ambiguity 的 ReviewVerdict,merged scope 不被压成 band。

#### P0.2 — Wire production review execution

- **File:** `forwin/production/executor.py`
- **Change:** 新增 `execute_review_jobs(plan: ProductionPlan)` 方法,被 `execute()` 调用。对每个 `review_chapters` 元素:
  - `drafted_unreviewed` 状态:跑一次 review。
  - `needs_review` 状态:读 latest review,直接走当前 `approve_chapter_review()` 入口的等价路径(**P0 不接 engine**,P1 再切)。
- **Risk:** 可能与 scheduler 的 `stop_when_review_pending` 默认行为冲突。需要确认 policy 字段语义。
- **Acceptance:**
  - 新测试:`production/quota.review=2` 时,planner 填 2 个 review,executor 执行 2 次。
  - 现有 `test_production_executor.py` 不退化。

#### P0.3 — Pass over_budget into DeferAcceptanceTransaction's gate call

- **File:** `forwin/narrative_obligations/transaction.py:141`
- **Change:** 在 `run()` 内调 `evaluate_obligation_budget()`,把结果的 `over_budget` 传给 `evaluate_canon_admission()`。
- **Risk:** 之前能通过的 deferred case 可能现在被 budget 阻断。要 check 当前测试是否假设 budget=False。
- **Acceptance:**
  - 新测试:budget 超额时 `DeferAcceptanceTransaction.run()` 返回 success=False,blocking reason 含 `obligation_budget_exceeded`。
  - 现有 `test_obligation_budget.py` + `test_deferred_acceptance.py`(如有)不退化。

#### P0 deliverable

三个独立 PR,任意顺序可合。完成后:
- arc 级问题不再被静默降级。
- production review quota 真正生效。
- deferred 路径正确尊重 budget。

行为变化但不引入新抽象。**先做完 P0 再启动 P1**。

---

### P1:`AutoDecisionEngine` 骨架(行为不变)

#### Goal

抽出 engine + decision table,但 **rule table 的初始版本必须完全复现现有四个 dispatcher 的行为**——也就是 orchestrator 改成只调 `engine.decide()` 后,所有现有 e2e 测试通过、生产行为不变。

这是 strangler-fig 模式:先建一个统一入口,**不**改决策内容。

#### Tasks

1. 创建 `forwin/review_engine/` 模块,含:
   - `types.py` — `DecisionInput`, `Decision`, `DecisionRule`
   - `engine.py` — `AutoDecisionEngine`
   - `rules/` — 按现有 dispatcher 一对一翻译的规则
     - `rules/review_outcome.py` — 复刻 `ReviewOutcomeRouter.route()` 的每个 branch 为独立 rule
     - `rules/repair.py` — 复刻 `RepairPolicy.decide()` 的每个 branch
     - `rules/obligation_scope.py` — 复刻 `ObligationScopeRouter`
     - `rules/final_acceptance.py` — 复刻 `FinalAcceptanceGate.evaluate()`
   - `audit.py` — `DecisionEvent` 持久化,复用 `canon_quality/repository.py` 的 schema 风格

2. orchestrator 切换:
   - `forwin/orchestrator_loop_core/quality_gates.py` 中四个 dispatcher 的调用点,改为 `engine.decide(input)` + 按 outcome dispatch。
   - **保留原 dispatcher 类的实现**,作为 rule 实现细节。不删除。

3. Shadow mode 跑一段时间:
   - 新增配置 `review_engine.shadow_mode=true`,engine.decide() 跑出 decision,与原 dispatcher chain 的实际行为比对,差异写日志。**默认开 shadow,不真正切流量。**
   - 跑 24-48 小时(或一批历史 chapter replay),确认零差异。
   - 切换到 `review_engine.shadow_mode=false`,engine 成为唯一决策源。

#### Acceptance

- 所有现有测试通过,无改动。
- 新增 `tests/review_engine/test_rule_parity.py`:对一组手工 fixture,engine 输出与原 dispatcher chain 输出 byte-equal。
- shadow log 在选定 replay 集上 0 差异。
- `DecisionEvent` 表填充,每个 chapter draft 都有对应记录。

#### Out of scope for P1

- 不新增 outcome(arc_patch / book_patch / commit_with_obligation / auto_approve 这些只是 enum,P1 阶段没有 rule 会产出它们)。
- 不改 RepairPolicy 的 attempt-count 逻辑。
- 不接 verifier。

---

### P2:`RepairPolicy` 重写为 issue-scope driven

#### Goal

让 repair scope 由 issue 类型决定,而不是由 attempts_completed 决定。

#### Design

新决策函数(在 `review_engine/rules/repair.py` 内,**取代**原 `RepairPolicy.decide()`):

```python
def decide_repair_scope(input: DecisionInput) -> str:
    primary_issue = _classify_primary_issue(input.signals, input.review)
    # 表驱动:
    # local_rewrite issues (placeholder_leakage, body_truncated, ...) → "draft"
    # chapter-level (single-chapter pacing, single-chapter callback) → "chapter_plan"
    # band-level (identity_within_band, foreshadow_band) → "band_plan"
    # arc-level (identity_ambiguity, countdown_explanation, ...) → "arc_plan"
    # book-level (book_structure_violation) → "book_plan"
    scope = ISSUE_TO_SCOPE.get(primary_issue.kind, "chapter_plan")
    # attempts 只用来防止死循环:
    if input.attempts_completed >= MAX_ATTEMPTS_PER_SCOPE.get(scope, 2):
        return _escalate(scope)  # 同 scope 内重试用尽 → 升一级,而非滑动序列
    return scope
```

attempts 仅作为**同 scope 内**的 retry 计数,而不是 scope 升级的主驱动。

#### Tasks

1. 建立 `ISSUE_TO_SCOPE` 映射表,放在 `forwin/review_engine/issue_taxonomy.py`。
2. `_classify_primary_issue()` 从 signals + review verdict 中提取主导 issue,需要决定**多 issue 时的优先级**(建议:scope 更大的优先)。
3. 删除 `REPAIR_SCOPE_SEQUENCE` 作为线性滑动的语义。保留为合法 scope 集合。
4. 关掉 P1 中复刻 attempt-count 的 rule,启用新 rule。
5. 加 feature flag `review_engine.repair_v2_enabled`,默认 false。
6. shadow mode 跑新 rule,与旧 rule 输出对比,确认升级路径符合设计文档(arc-level 不被压到 band 等)。

#### Acceptance

- 新增 `tests/review_engine/test_repair_v2.py`,至少覆盖 5 类 issue × {first attempt, retry}。
- shadow log 在 replay 集上的 scope 分布:arc-level issue 的 scope=arc_plan 占比从 0 上升到 ≥80%。
- flag 切换后 e2e 测试通过。

---

### P3:Arc / book plan patcher

#### Goal

让 `arc_patch` / `book_patch` 成为可执行的 outcome,不再 fall back 到 manual。

#### Tasks

1. 新增 `forwin/planning/arc_plan_patcher.py` 和 `book_plan_patcher.py`,接口对齐现有 `chapter_plan_patcher.py` / `band_plan_patcher.py`。
2. 新增 validator:`forwin/planning/arc_patch_validator.py`(检查 patch 不破坏 accepted canon、不与 arc completion gate 冲突)。
3. 扩展 `_prepare_deferred_acceptance_if_needed()`(`forwin/orchestrator_loop_core/quality_gates.py:463`)处理 `arc_patch` 和 `book_patch` 两个 outcome。
4. arc-level future context injection:writer prompt 在跨 arc 时显示 arc patch debt。
5. arc completion gate:arc 结束章节必须清算 active arc patch,未清算的进入 system_block。

#### Acceptance

- 新测试覆盖:identity_ambiguity issue → engine 输出 `arc_patch` → patcher 生成有效 patch → gate admit → 后续 chapter writer prompt 显示该 debt → arc 结束时 gate 检查。
- 现有 arc-level issue 在 replay 集上,manual_review 占比从 100% 下降到 ≤20%(其余 80% 走 arc_patch)。

---

### P4:Verifier 闭环 + auto-approve policy

#### P4.1 — `ObligationResolutionVerifier` 状态流转

1. 从 worktree(`.claude/worktrees/practical-engelbart-90482d/forwin/canon_quality/obligation_verifier.py`)拉回 `verify()` 实现。
2. 在 `forwin/narrative_obligations/repository.py` 补齐:
   - `mark_obligation_resolved(obligation_id, verifier_result, evidence_refs)`
   - `expire_obligation(obligation_id, reason)`
   - `block_expired_obligation(obligation_id)`
   - `waive_obligation(obligation_id, reason, actor)` — `actor` 必须非 system
3. 闭环触发点:
   - 每次 chapter accept 后,跑 verifier on active obligations,pass → `mark_resolved`。
   - 每次 chapter accept 后,跑 expiry check,过期且未解决 → `expire_obligation` 或 `block`。
   - `waive` 只通过 `project_ops/reviews.py` 中显式 API,要求 actor + reason,落 DecisionEvent。

#### P4.2 — Auto-approve rules

在 `review_engine/rules/auto_approve.py`:

- `copilot_safe_warn`:copilot mode + verdict=warn + no error signals + no blocking obligations + canon gate strict pass → `auto_approve`。
- `review_interval_safe`:review_interval_chapters 命中 + canon gate pass + future plan audit pass + obligation audit pass → `auto_approve`(reason 注明 "interval-safe")。

加 feature flag `review_engine.auto_approve_enabled`,按 project 配置默认值。

#### Acceptance

- 新测试:active obligation 在后续章节满足 verifier markers → 自动 mark_resolved → canon gate 不再因此章节阻塞。
- 新测试:warn-only chapter 在 copilot mode + flag on → engine 输出 auto_approve,落 DecisionEvent。
- waived obligation 永远要求 actor 字段非空,system 调用拒绝。

---

### P5:UI / audit surface

- production dashboard 增加 "waiting_review breakdown":按 `Decision.outcome=manual_review` 的 `rule_id` 聚合。
- review detail 页显示:
  - `Decision.rule_id` + `Decision.reason`
  - `missing_evidence` 列表(缺哪个 patch / deadline / payoff test / verifier)
  - "为什么没自动处理":如果存在 outcome 因 flag off 被降级为 manual,显式说明 "policy disabled: review_engine.auto_approve_enabled=false"。
- 三类状态 chip:`需要人工判断` / `系统阻断` / `可自动处理但策略关闭`。

依赖 P1 完成的 `DecisionEvent`。

## Risk

| 风险 | 缓解 |
|---|---|
| P0.1 后 arc scope 流入 `RepairPolicy` 但 sequence 不含 arc | P0 阶段允许 fallback 到 chapter_plan 并记录 warn 日志;P2 完成后消化 |
| P0.2 production executor 行为变化触发 scheduler 死锁 | 加 `production.review_execution_enabled` flag,默认 false 上线,灰度开启 |
| P0.3 budget 阻断使现有 deferred case 失败 | 在 staging replay 集上比对 deferred 成功率,异常下降 → 调 budget policy 而非回滚 |
| P1 shadow mode 发现差异 | rule 实现按原 dispatcher 1:1 翻译,差异 = 翻译 bug,修翻译不改原模块;rule parity 测试覆盖所有 branch |
| P2 changeover 后 arc/book scope 大量出现但 P3 还没好 | P2 + P3 同一个 release 上线,或 P2 期间 arc/book scope 临时降为 manual_review with `rule_id=p3_not_ready` |
| Verifier 误判把未解决的 obligation 标 resolved | `verifier_result=warn` 不写 resolved,只 `pass` 才写;`waive` 永远要人工 |

## Verification

每个 phase 完成的判定:

- **P0** — 三个 PR 各自单独通过 CI;在 staging 跑 24 小时 replay,arc-level scope 出现、review quota 被执行、budget 阻断生效。
- **P1** — shadow mode 跑选定 replay 集(建议:近 200 章,覆盖 blackbox/copilot/checkpoint 三种 mode)0 差异;`DecisionEvent` 表对每个 chapter 都有记录。
- **P2** — replay 集上 arc-level issue 的 scope=arc_plan ≥80%;repair v2 flag 切换后 e2e 通过。
- **P3** — arc-level issue manual_review 占比 ≤20%;arc completion gate 在 fixture arc 上正确阻塞未清算 patch。
- **P4** — verifier 闭环在 fixture obligation 上 pass 后 canon gate 不再阻塞;auto-approve 在 warn-only 上正确触发,落 DecisionEvent。
- **P5** — UI 显示 rule_id + missing_evidence + policy disabled 三态;dashboard 聚合数对得上 `DecisionEvent` 表。

## Rollback

- 每个 phase 由独立 feature flag 控制:`review_engine.shadow_mode` / `review_engine.repair_v2_enabled` / `review_engine.arc_patcher_enabled` / `review_engine.auto_approve_enabled`。
- 关 flag 即回到上一阶段行为。
- engine 本身的开关:`review_engine.enabled=false` → orchestrator 走原始四 dispatcher 路径(只要 P1 没删原 dispatcher,这条路径永远可用)。

P1 阶段**严禁删除**原 `ReviewOutcomeRouter` / `RepairPolicy` / `ObligationScopeRouter` / `FinalAcceptanceGate` 实现。它们最早在 P5 完成 + 稳定运行 30 天后才能考虑删。

## Open Questions

1. `_classify_primary_issue()` 多 issue 时的优先级:scope-largest-wins 还是 severity-highest-wins?需要看 signals 现实分布。
2. `arc_patch` 的 budget 怎么算?当前 `evaluate_obligation_budget()` 按 chapter / band 维度,arc / book 维度需要扩展。
3. `auto_approve` 的 review interval 触发后,下一个 interval checkpoint 是否需要重置计数(否则会一直 auto-approve)?
4. verifier worktree 里只有 `verify()` 没有状态流转,P4.1 是从 worktree 拉回 + 重写,还是绕过 worktree 全部从 scratch?需要先 diff worktree 上下游 commit 看耦合情况。
5. shadow mode 跑多久算"足够":24h 实流量 vs 历史 replay 集,优先级哪个?
