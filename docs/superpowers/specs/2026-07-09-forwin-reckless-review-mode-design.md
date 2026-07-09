# ForWin 鲁莽审核模式设计

## 目标

为项目增加显式的“鲁莽模式”。启用后，所有原本需要人工作出放行判断的生成 gate 都交给 `gpt-5.3-codex-spark`，生成任务不再因为常规人工 review 而等待操作员。每次委托必须留下可重放的完整日志。

默认仍为人工模式。鲁莽模式是项目级治理设置，可从任务抽屉或 MCP 显式开关。

## 成功标准

- 项目治理数据可以表达 `human` 与 `reckless` 两种 review delegation mode。
- 鲁莽模式下，章节 review gate、固定章节间隔 review、manual checkpoint、band checkpoint 和 generation audit pause 都会调用 Spark。
- Spark 只能返回 `approve` 或 `reject`，不能要求人工补充信息。
- `approve` 使用该 gate 现有的人工放行语义；`reject`、模型不可用、非 Spark 路由、无效 JSON 或日志持久化失败都会保留阻断并停止生成。
- hard floor、canon commit failure、future-plan hard block、用户 pause/terminate 等确定性系统阻断不被鲁莽模式绕过。
- 每次调用都持久化完整 prompt、完整 gate input、原始模型输出、原始 provider/Codex 事件、重试信息、解析结果、实际模型、耗时和最终状态。
- DecisionEvent、PromptTrace、chapter ledger 和 audit bundle 能串起同一个审核因果链。
- 模式可通过现有项目治理 API、首页任务抽屉和 ForWin MCP 操作。

## 方案比较

### 方案 A：gate 内嵌统一审核器（采用）

在状态机即将进入人工暂停前调用统一 `RecklessReviewAgent`。审核器只返回结构化决定，gate 自己执行已有的放行或暂停动作。

优点：决策与状态变更位于同一事务边界；没有异步 watcher 竞态；每类 gate 保留原本的业务所有权；容易精确记录因果链。

代价：需要在几个明确的 gate 接入点调用统一接口。

### 方案 B：任务暂停后由 watcher 自动处理

监听 `needs_review` 和 checkpoint，再调用审批 API 并创建下一任务。

优点：对写作循环侵入较少。

缺点：暂停与 watcher 之间存在窗口；可能重复创建任务；无法在 canon apply 前复用当前完整上下文；日志会被拆成两个不稳定的生命周期。

### 方案 C：无条件自动 override

启用模式后直接跳过所有人工 gate。

优点：实现最少、吞吐最高。

缺点：没有真正把判断交给 Spark；无法解释为什么放行；也无法满足完整日志要求。

## 治理模型

`ProjectGovernanceSettings` 新增：

```text
review_delegation_mode: "human" | "reckless" = "human"
```

固定审核模型常量：

```text
gpt-5.3-codex-spark
```

模型不作为项目可编辑字段，避免用户误以为鲁莽模式允许换成其他模型。项目治理更新、继续生成临时 override 和任务 payload 都必须保留该 mode。

## 统一审核器

新增独立模块 `forwin/reckless_review.py`，提供三个稳定类型：

- `RecklessReviewRequest`：gate kind、项目/任务/章节/band 身份、可执行动作、完整 gate snapshot 和关联对象。
- `RecklessReviewDecision`：`approve | reject`、reason、risk level、findings、evidence。
- `RecklessReviewOutcome`：是否成功、是否放行、实际模型、backend、trace/event id 和失败原因。

审核器通过现有 routed LLM adapter 发起结构化 JSON 调用，但请求固定首选 `gpt-5.3-codex-spark`。只有实际成功响应模型精确匹配该值时，决定才有效。路由器若回退到 Kimi、MiniMax 或其他模型，响应只进入日志，不参与 gate 决策。

审核器使用低温度、只读 permission profile 和封闭 schema。提示词明确要求：只评估给定快照，不发明补丁，不请求人工信息，不把确定性 hard gate 当作可 override 对象。

## Gate 接入

### 章节审核

下列现有分支统一生成 `chapter_review` 请求：

- checkpoint operation mode 的逐章确认；
- copilot 模式的非 pass verdict；
- blackbox 修复耗尽后的 fail verdict；
- `should_apply_canon` 为 false；
- `review_interval_chapters` 命中。

Spark `approve` 后继续现有 canon apply，章节 acceptance mode 写为 `reckless_approved`。Spark `reject` 或调用失败后保留 `needs_review` 并暂停。后续操作员仍可人工介入，但系统不会伪装成已放行。

canon quality gate 若在 Spark 放行后仍拒绝 commit，继续按 system block 处理；鲁莽模式不得绕过它。

### Manual checkpoint

chapter start、chapter accepted 和 band end manual checkpoint 在暂停前委托 Spark。

Spark `approve` 后将对应 checkpoint 标为 `overridden`，写入模型理由、resolved time 和审计事件，然后继续。`reject` 或失败保留 checkpoint 与暂停。

### Band checkpoint

只有本来会暂停的 `warn`、`fail`、`error` checkpoint 才调用 Spark。`approve` 映射为 `overridden`；`reject` 或失败保留原状态并暂停。原始 evaluator issues 全量进入审核输入。

### Generation audit pause

当 generation audit 本身完成、且策略要求暂停人工检查时，调用 Spark。`approve` 仅解除这一次 pause，不改变 audit 配置；`reject` 或失败按原策略暂停。

### 明确不接管

以下不是人工 review 决策，不交给 Spark：hard floor failure、canon commit/apply failure、future-plan hard block、world-model compile failure、active task conflict、用户 pause/terminate、provider/runtime crash。

## 完整日志

每次审核创建同一 causal root 下的事件链：

1. `reckless_review_requested`
2. `prompt_trace_recorded`
3. `reckless_review_decided` 或 `reckless_review_failed`
4. 放行时追加 `reckless_gate_overridden`

PromptTrace 使用 `trace_scope=reckless_review`，并保存：

- `effective_system_prompt`：完整系统提示词；
- `prompt_layers`：完整 user message；
- `input_snapshot`：完整 gate request，包括 draft、verdict、issues 和 checkpoint 数据；
- `model_profile`：requested/actual model、backend 和 permission profile；
- `attempts`：完整请求 payload、完整响应文本、provider request id、Codex raw events、HTTP 状态、重试和耗时；
- `output_summary`：原始模型输出、结构化决定、解析状态和最终 gate action。

DecisionEvent 只放索引、摘要和 trace id，避免时间线载荷膨胀。PromptTrace detail、chapter observability ledger 和 audit bundle 继续作为完整内容的读取出口。日志中不得写入 API key、Authorization、cookie、bridge token 或浏览器 session。

若完整 PromptTrace 无法持久化，不执行 `approve`。这是鲁莽模式唯一的日志硬门槛。

## API、MCP 与 UI

- `ProjectGovernanceUpdateRequest` 和 `ProjectContinueGenerationRequest` 接受 `review_delegation_mode`。
- ForWin MCP 新增 `project_set_reckless_mode(project_id, enabled, reason)`，内部调用项目治理 API。
- 项目详情继续通过现有 `governance` 返回 mode。
- 任务抽屉治理卡增加“鲁莽模式”checkbox；启用时显示 `Codex 5.3 Spark 审核` badge。
- 时间线沿用 DecisionEvent 展示，事件 payload 中的 trace id 可进入现有 PromptTrace detail/audit bundle。

## 失败语义

- Spark 不可用：记录请求、错误和尝试，保留 gate，暂停。
- 实际响应来自其他模型：记录 `model_mismatch`，忽略响应，保留 gate，暂停。
- JSON/schema 无效：保存原始响应，记录 `parse_or_schema`，保留 gate，暂停。
- Spark `reject`：记录模型理由，保留 gate，暂停。
- 日志保存失败：事务回滚，不放行。
- gate override 保存失败：事务回滚，不放行。

不做静默自动批准，也不把失败降级成人工批准事件。

## 测试

- 治理模型默认值、规范化、API round trip 和任务 runtime override。
- 审核器成功、reject、模型 mismatch、invalid JSON、调用异常和完整 trace 内容。
- 章节 checkpoint/copilot/blackbox/interval gate 在 human 与 reckless 两种模式下的差异。
- manual checkpoint、band warn/fail/error 和 generation audit pause 的 approve/reject 行为。
- hard floor 与 canon system block 在鲁莽模式下仍阻断。
- DecisionEvent 与 PromptTrace 的 parent/causal/related object 关联。
- MCP 开关和 UI 渲染。
- 现有 governance、review engine、generation auto-continue 和 observability 回归测试。

## 发布与验收

代码通过目标测试和全量测试后提交、推送并经 150 sync 路径部署。运行态验收使用一个小型项目：启用鲁莽模式，制造至少一个章节人工 gate 和一个 band warn checkpoint，确认两者由 Spark 处理、生成继续、实际模型精确匹配，并从 PromptTrace detail/audit bundle 复核完整日志。
