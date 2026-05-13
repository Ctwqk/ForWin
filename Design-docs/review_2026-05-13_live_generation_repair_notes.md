# Live Generation Repair Notes - 2026-05-13

来源：2026-05-12 到 2026-05-13 的 continue-generation / canon quality 实机生成、代码修补与分支合并过程。

用途：这份文档给后续模型或人工 reviewer 快速理解“这一轮到底发现了什么、已经补了什么、还没有补什么”。重点不是复述书本内容，而是分析这些内容问题背后的代码机制。

## 1. 当前状态摘要

- 当前主分支：`master`
- 相关合并后最新提交：
  - `18a8ce7 Merge branch 'codex/fix'`
  - `a334e93 Merge branch 'codex/continue-generation-patch'`
  - `c88c1c3 WIP canon quality and continue generation fixes`
  - `de9fa4f WIP home console frontend refinements`
- 本轮实机项目：
  - `project_id=d2338a0e8bfe4e00a068b03ce9e9b0bf`
  - 标题：`旧城遗档：白塔重置`
  - 目标章数：12
- 当前实机生成停在第 12 章 review 阶段：
  - 最新任务：`9e6afe7e47e6`
  - 最新第 12 章版本：`v23`
  - 状态：`needs_review`
  - v23 已修掉前一版的沈宴秋身份/性别漂移，但仍被倒计时一致性 gate 阻断。

重要说明：本轮最后一次代码补丁后执行了构建，但没有继续触发新的实机 retry，也没有把服务重启后再跑一版第 12 章。因此 archive_cleanup 倒计时 prompt 补丁尚未经过新实机章节验证。

## 2. 实机生成暴露出的模式

这一轮不是单个 reviewer 规则过严或过松的问题，而是一个更系统性的模式：

```text
章节不通过 review
  -> 当前系统倾向于整章重写
  -> 原问题经常被修掉
  -> 但整章重采样引入新的连续性/身份/倒计时/终局问题
  -> 再补一个局部规则
  -> 下一轮换一个表面问题暴露
```

这说明仅靠“发现一个问题，补一个 gate 或 prompt”会逐步改善底层能力，但仍然容易变成 patch-on-patch。更全局的方向应该是：

- 对硬性 canon 破坏继续阻断。
- 对可延后圆回来的叙事债务建立结构化 obligation，而不是一律打回整章重写。
- 对局部 deterministic 问题优先做 span-level / scene-level patch repair，而不是整章重写。
- 对终章或无后续章节的项目，不能随意把主线危机 deferred 到不存在的未来章节；需要显式 epilogue / extend-book 机制。

## 3. 本轮问题与补丁

### 3.1 `相关人员` / 泛称角色泄漏

发现的问题：

- 早期审计发现 unknown named entity autofix 可能把关键行动者替换成 `相关人员`。
- checker 又把 `相关人员` 当成安全 generic reference，导致占位符可能进入 accepted prose。
- 实机过程中还出现过类似 `洛庭若的手下` 这类阵营从属角色被错误当成新命名人物，造成 subworld admission 误报。

代码补丁：

- `forwin/canon_quality/placeholder.py`
  - 新增 placeholder leakage analyzer。
  - `相关人员` / `一名相关人员` 在正文中作为 blocker。
  - summary 中仅作为 warning 处理。
  - 新增 bare role / protagonist placeholder 检测。
- `forwin/checker/rules.py`
  - `相关人员` 不再是安全 generic character reference。
  - 扩展角色从属泛称识别，例如 `手下`、`下属`、`部下` 等。
  - 避免 `洛庭若的手下` 这种组织从属短语被当作必须建 canon 的命名人物。
- `forwin/orchestrator/loop.py`
  - placeholder leakage autofix 优先替换为稳定 alias，不再用 `相关人员` 假装通过。

测试覆盖：

- `tests/test_placeholder_leakage_gate.py`
- `tests/test_subworld_control.py`

### 3.2 倒计时回退与计时器混淆

发现的问题：

- v19 出现主线 `memory_reset` 倒计时从 24 小时回退到 5 天。
- v23 出现 `archive_cleanup` / 终端审计窗口从 180 分钟回退到 240 分钟。
- 根因不是单纯 reviewer 漏检，而是 writer 可能从旧 summary / plan 中拿到过期时间，并把不同计时器混在一起。

代码补丁：

- `forwin/canon_quality/countdown_ledger.py`
  - 扩展 countdown key 识别。
  - 增强中文时间解析与倒计时单调性检测。
  - 对 `archive_cleanup` 提供更具体的 repair instruction。
  - 区分主线重置周期与局部终端审计/授权窗口。
- `forwin/writer/prompts.py`
  - 明确倒计时 ledger 优先级高于旧 summary / plan。
  - 若同章出现多个计时器，必须明确区分用途。
  - 针对 `archive_cleanup` 增加硬性约束：不能把终端审计/授权窗口写成大于上一轮 ledger 的剩余时间。

测试覆盖：

- `tests/test_countdown_ledger.py`
- `tests/test_writer_prompt_contract.py`

剩余风险：

- v23 阻断后新增的 `archive_cleanup` prompt 约束尚未实机重跑验证。

### 3.3 终章 closure gate 误判与漏判

发现的问题：

- v18 里正文已经写出类似“记忆重置系统已经失效 / 旧城将不再有记忆重置”，但 gate 没有识别为 final resolution。
- 同一轮又出现 post-resolution handoff：“去档案公会，把最后一段记忆记录交给所有人”，这类尾部任务会让终章主线闭合不完整。
- v20 出现“关闭系统但人物仍被困”的尾声。
- v21 gate 又把 summary 中“进入地下旧轨第五层，利用密钥成功关闭”误判成 unresolved hook。

代码补丁：

- `forwin/canon_quality/final_completion.py`
  - 新增 final completion analyzer。
  - 明确终章主危机必须有 executed resolution，而不是只写“准备去解决”。
  - 识别主线危机 resolution phrase。
  - 分析 resolution 之后的 tail，阻断新的主线 handoff / pursuit / trapped ending。
  - 避免只因为 summary 提到进入核心地点就误判为未完成。
- `forwin/canon_quality/service.py`
  - 在最终章或疑似最终章调用 final completion gate。
- `forwin/writer/prompts.py`
  - 对 final chapter 增加“必须执行解决，不得新增前置任务”的约束。
  - 禁止 unresolved trapped sacrifice 作为终章收束。

测试覆盖：

- `tests/test_final_completion_gate.py`
- `tests/test_canon_quality_service.py`
- `tests/test_writer_prompt_contract.py`

### 3.4 命名人物身份 / 性别 / 亲属关系漂移

发现的问题：

- v22 自动 accepted 后，人工阅读发现沈宴秋从此前女性角色漂移成“叔叔 / 男人”。
- 这说明 reviewer/gate 只看局部章节质量时，可能遗漏跨章稳定身份变量。

代码补丁：

- `forwin/canon_quality/identity.py`
  - 新增 identity role analyzer。
  - 检测亲属关系漂移。
  - 检测性别/代词标记漂移。
  - 允许明确伪装、误导、揭示等 bridge，但无 bridge 时输出 error。
- `forwin/canon_quality/service.py`
  - 从已提交章节中提取 previous identity facts。
  - 对当前草稿做跨章对比。
- `forwin/context/assembler.py`
  - 将 identity continuity 相关信息注入上下文。
- `forwin/writer/prompts.py`
  - 增加人物身份连续性约束：不得突然改变已登场命名人物性别、代词、亲属关系或辈分。

测试覆盖：

- `tests/test_identity_role_ledger.py`
- `tests/test_canon_quality_service.py`
- `tests/test_context_provider_chain.py`
- `tests/test_writer_prompt_contract.py`

实机结果：

- v23 中沈宴秋身份/性别漂移没有再出现，说明该类补丁对当前触发样本有效。

### 3.5 Reviewer payload 与 deterministic evidence

发现的问题：

- 只靠 head/tail 或 reviewer 主观判断，很难稳定检出全文中部的 ledger / identity / final closure 问题。
- 需要让 deterministic analyzer 的证据进入 reviewer payload，而不是把 reviewer 当作唯一发现者。

代码补丁：

- `forwin/reviewer/hub.py`
  - 将 canon quality / deterministic signals 纳入 reviewer 处理路径。
- `forwin/protocol/context.py`
- `forwin/protocol/experience.py`
- `forwin/context/assembler.py`
  - 扩展上下文与 experience payload，携带 residual / evidence 信息。
- `tests/test_canon_quality_reviewer_payload.py`
  - 覆盖 reviewer payload 中质量信号暴露。

当前限制：

- 这仍是“把 deterministic evidence 喂给 reviewer”的增强，不等价于完整的 debt-aware planning。

### 3.6 Continue-generation workset 与 requested count

发现的问题：

- 审计中指出 `requested_chapters` 在 API、worker progress、review continue、scheduler 之间容易分歧。
- 进度 payload 可能覆盖 task contract。

代码补丁：

- `forwin/generation/continue_workset.py`
  - 建立 continue workset 统一计算。
  - 支持 active arc pending、future arc materialization、review blocker、retry chapter 等。
  - `requested_chapters` 来自实际 workset，而不是全书总数或未 scoped plan 数。
- `forwin/orchestrator/loop.py`
  - continue 路径使用 workset helper。
  - worker progress 使用 `resolved_workset_count` / `pending_chapter_count` 等诊断字段，减少对 task contract 的污染。

测试覆盖：

- `tests/test_continue_generation_workset.py`

需要继续确认：

- REST、MCP、scheduler、review approve/retry continue 的全路径一致性仍应跑完整集成测试确认。

## 4. 已验证内容

本轮合并前后已执行过的关键验证包括：

```bash
git diff --check
.venv/bin/python -m pytest tests/test_world_studio_frontend.py tests/test_api_pages_rendering.py -q
cd frontend/world-studio && npm run build
```

最近一次结果：

- `git diff --check`：通过。
- `tests/test_world_studio_frontend.py tests/test_api_pages_rendering.py`：`8 passed`。
- `frontend/world-studio` build：通过。

本轮补丁开发过程中还执行过相关后端单元集，最后一次记录为：

```text
51 passed in 2.11s
```

该 51 个测试主要覆盖 writer prompt contract、countdown、identity、final gate 等本轮局部补丁。

未完成的验证：

- 没有在最后一个 `archive_cleanup` prompt 补丁后重启服务并重新生成第 12 章。
- 没有重新跑完整 60 章真实生成。
- 没有跑全量 pytest。

## 5. 本轮最重要的系统性结论

### 5.1 不能只靠“review 不过 -> 整章重写”

整章重写会修掉一个确定性问题，但也会重新采样：

- 人物身份表达。
- 终章尾声。
- 倒计时数值。
- 角色牺牲/救援/被困状态。
- 对旧 summary 的误读。

因此它适合结构性失败，不适合所有问题。

更合理的 repair ladder：

```text
deterministic issue
  -> classify severity and deferability
  -> if local span issue: localized patch repair
  -> if soft narrative debt and future chapter exists: accept_with_obligation
  -> if hard canon contradiction: block current chapter
  -> if global plot path broken: full regenerate
```

### 5.2 “后续章节圆回来”应该是结构化机制，不是放宽 review

可以 deferred 的问题：

- 角色动机尚不清楚。
- 公开真相后的社会后果尚未完全展开。
- 轻量 relationship ambiguity。
- 非终章的情绪回声不足。
- 非关键 style / sensory repetition。

不应该 deferred 的问题：

- 命名人物性别、亲属、代词突然变化且无解释。
- 倒计时从少变多且无 reset / branch clock。
- 正文中出现 `相关人员` 这类占位符。
- 终章主危机未关闭。
- final chapter 引入新的主线前置任务，但没有 epilogue / chapter 13。

如果要允许“后续章节再圆”，需要显式落库：

```text
StoryObligation
  origin_chapter
  deadline_chapter
  severity
  required_resolution_evidence
  status=open/resolved/expired/waived
  repair_scope=next_chapter/arc/book
```

并且下一章 writer context 必须把 open obligations 当成 must-resolve input，而不是普通 warning 文本。

### 5.3 终章的 defer 需要 epilogue / extend-book 机制

当前项目是 12 章，v23 卡在最后一章。此时“后续章节圆回来”在数学上不存在。

如果系统要支持这类情况，应该提供明确动作：

- `extend_with_epilogue`
- `materialize_epilogue_chapter`
- `accept_finale_with_epilogue_obligation`

否则 final completion gate 应继续硬阻断。

## 6. 建议 ChatGPT Pro 重点评审的问题

1. 当前 hard blocker / soft debt / deferable obligation 的分类是否合理？
2. 是否应该新增 `accepted_with_obligation` 状态，还是继续保持 chapter status 只有 `accepted / needs_review / failed`，把 obligation 作为并行状态？
3. localized patch repair 应该放在 orchestrator 的哪一层？
   - LLM reviewer repair 后？
   - deterministic gate fail 后？
   - 还是作为 full regenerate 之前的独立策略？
4. countdown、identity、final completion 这类 deterministic gate 是否应该允许 LLM reviewer override？
5. 对 final chapter，是否应该允许自动扩展 epilogue？如果允许，谁决定：gate、scheduler、用户，还是 project policy？
6. 如何证明不是“每章重复修同一类问题”？
   - 建议指标：同类 blocker recurrence rate、full rewrite count、localized patch success rate、open obligation resolution rate、new issue introduced per rewrite。
7. 目前 prompt constraint 增强是否会挤压全局创作质量？是否需要把硬规则从 prompt 中移出，改成 structured planning input + deterministic post-check？

## 7. 下一步建议

优先不要继续堆单点 prompt。建议下一轮实现顺序：

1. 新增 `QualityDisposition` 分类器。
2. 给现有 signal 标注：
   - `block_current_chapter`
   - `repair_in_place`
   - `accept_with_obligation`
   - `defer_to_next_chapter`
   - `extend_with_epilogue`
3. 在 full regenerate 前加入 localized patch repair。
4. 新增 `StoryObligation` / `QualityDebt` 持久化模型或先复用 `canon_quality_signals` 承载 open obligations。
5. writer context 读取 open obligations，并在下一章后由 deterministic checker 验证是否解决。
6. 对当前第 12 章样本，先不要盲目整章重写；更适合测试 localized patch repair：
   - 将 `四小时` 这类 archive_cleanup 回退 span 改为小于等于 180 分钟，或明确声明为不同 branch clock。
   - 保持 v23 已修好的身份连续性和主要终局结构。

## 8. 给后续 reviewer 的一句话结论

本轮代码补丁已经把多个已观测内容错误转成 deterministic gate / prompt contract / reviewer evidence，但当前架构仍偏向“失败后整章重写”。真正的全局改进应从“更严格地打回”升级为“问题分类、局部修复、可追踪叙事债务、终章扩展策略”四件事。
