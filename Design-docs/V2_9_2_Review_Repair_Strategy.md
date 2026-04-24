# ForWin V2.9.2 Review / Repair 策略

更新时间：2026-04-22

适用范围：ForWin `V2.9.2+` 单章写作主链

说明：本文件记录当前主干已经落地的 review / repair 语义。若后续代码再改，需以本文件和 [V2_9_2.md](/home/taiwei/.codex/worktrees/d41d/ForWin/Design-docs/V2_9_2.md) 同步更新。

---

## 1. 设计结论

当前主干的 repair 链固定为三次：

1. 第 1 次 repair：`scene`
2. 第 2 次 repair：`band`
3. 第 3 次 repair：由 reviewer escalation 决定走 `band` 或 `arc`

普通 review 阶段不再自动把 repair scope 升到 `arc`。  
`arc` 只允许在第 3 次 escalation 中显式选中。

`blackbox` 仍保留“三次 review fail 后自动放行”的产品语义，但 force accept 只允许发生在下面这个条件组合下：

- 第 3 次 rewrite 真的产出了新正文
- 新正文真的重新跑过 review
- 最新 review verdict 仍然是 `fail`

以下情况不允许 force accept：

- writer 抛异常
- writer 返回空正文 / `None`
- repair 过程没有得到新的可 review draft
- 没有最新 review verdict，只有链路失败

这些情况统一进入 `needs_review / repair_failed` 语义，不写入 canon。

---

## 2. 三次 repair 的边界

| Attempt | Scope | 语义 |
| --- | --- | --- |
| 1 | `scene` | 维持当前章局部修补，不改更高层计划 |
| 2 | `band` | 允许调整当前 band 的局部安排后重写当前章 |
| 3 | `band` 或 `arc` | 由 reviewer escalation 决定；`arc` 的语义是“arc 指导的 band 重规划 + 当前章重写”，不是全局重排 |

补充规则：

- 第 1 次和第 2 次的行为保持稳定，不再由普通 review 自己漂移 scope。
- 第 3 次 escalation 默认返回 `band`；只有 reviewer 明确给出 `arc` 才升级。
- 若 reviewer 不支持 escalation、返回非法值或 LLM escalation 失败，系统安全回退到 `band`。

---

## 3. Reviewer Escalation

新增 reviewer 内部能力：

`choose_repair_escalation(...)`

输入至少包括：

- 最新 `ReviewVerdict`
- 最新 draft / writer output
- 前两次 repair scope 与结果
- 当前 chapter / band / arc 上下文

输出：

- `repair_scope = "band" | "arc"`
- `scope_reason`
- 可选 `design_patch`

设计要求：

- 普通 review 仍只产出 `scene / band` repair instruction。
- escalation 决策和普通 review verdict 分离，不复用同一层“自动升级到 arc”的旧逻辑。
- `scope_reason` 必须记录“为什么第 3 次还留在 band，或为什么必须升到 arc”。

---

## 4. Force-Accept Gate

### 4.1 允许强过的前提

仅当满足以下全部条件：

- `operation_mode == blackbox`
- 已完成第 3 次 rewrite
- 最新 rewrite draft 存在
- 最新 review verdict 为 `fail`

系统才允许把最新 draft 作为 `force_accept_after_repair` 写入 canon。

### 4.2 禁止强过的情况

以下情况一律不允许 force accept：

- writer exception
- writer returned none
- repair 执行失败，没有新 draft
- 没有新 review verdict

这一步的原则是：

> 可以容忍软质量 fail，不容忍“系统根本没写出来”。

---

## 5. 事件与持久化

repair 事件流继续保留，但失败类型要能区分：

- `review_fail_after_attempt`
- `repair_execution_error`
- `writer_returned_none`

`ReviewVerdict.repair_instruction` 新增：

- `scope_reason`

这个字段会进入 `review_meta_json`，并通过 chapter review detail API 暴露为：

- `latest_repair_scope`
- `latest_repair_scope_reason`

这样前端与治理面板可以解释：

- 当前章节最近一次 repair scope 是什么
- 为什么第 3 次 repair 留在 `band` 或升到了 `arc`

---

## 6. 与旧语义的差异

旧语义的问题是：

- 普通 review 会把 repair scope 自动推到 `arc`
- writer 异常也可能被当作 `blackbox` 可强过内容

当前语义改成：

- `arc` 只能由第 3 次 reviewer escalation 显式触发
- writer 异常 / 空正文只会停机等待处理，不会被当成“可接受内容”

这条边界的目标不是降低自动化，而是避免把“内容 fail”和“系统 fail”混为一谈。

