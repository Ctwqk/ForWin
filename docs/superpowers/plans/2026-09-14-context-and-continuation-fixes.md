# 正式上下文与续跑恢复修复计划

> **For agentic workers:** Use superpowers:subagent-driven-development to execute and review each task; steps use checkbox tracking.

**Goal:** 修复 `f8362da` 审查提出的五项问题，保留候选诊断、正式事实及可恢复等待之间的边界。

**Architecture:** 复用既有 Canon quality acceptance、CanonReadBaseline、知识投影和 continuation outbox。以接纳快照筛选正式冲突；业务载荷完整参加依赖指纹，单次构建共享准备工作；维护等待不形成永久停止决定；倒计时修复只引用该书合同与证据。

**Tech Stack:** Python、SQLAlchemy/PostgreSQL、pytest、现有 outbox/worker。

**Spec:** [三阶段设计](../specs/2026-09-09-forwin-three-stage-design.md)，[长篇可靠性设计](../specs/2026-09-12-longform-reliability-design.md)，以及用户授权修复的审查五项边界（本文各任务细化）。

## Global Constraints

- 在 `/Users/magi1/ForWin-source-github` 的 `codex/three-stage-improvements` 分支继续；基线 `1454e8ad9b1fd4bf020fdd85da08fc1b051febce`。保留已有未跟踪文件。
- 数据库 Canon 是唯一正式事实来源；候选诊断通过原 ReviewVerdict/RepairContract 供当前修复使用，不删除审计记录。
- 保留 Project→Task 锁顺序、lease fencing、唯一父任务、冻结策略、原子交接、发布冻结与有限内容修复预算。
- 测试使用 `FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test .venv/bin/python -m pytest`，测试自动建立隔离数据库；不修改真实作品或生产状态。
- 不增加监督进程、调度器、平行 Canon、运行时热补丁或故事专属规则。不创建定时任务。
- 当前修复提交并推送 `origin/for-review`；正式 smoke20→全新 L100→结局审读仍另有待验收状态，旧候选样本不冒充新源码通过。
- 每任务先记录真实失败再实现，跑相关测试并独立审查；整批完成只跑一次全量 QA。控制器负责更新文档和最后推送。

## Task 1: 正式世界冲突只读有效接纳来源

**Files:** 修改 `forwin/knowledge_system/context.py`、必要时 `forwin/canon_quality/acceptance.py` 的现有读取入口；测试 `tests/test_knowledge_system_v46.py` 或专用 `tests/test_accepted_quality_context.py`。核对 `forwin/canon_quality/repository.py`、`forwin/retrieval/source_identity.py` 与 `forwin/writer/world_context.py`。

**Interfaces:** `KnowledgeContextQuery.build(..., baseline=...)` 的冲突必须和页面共用 `as_of` 与 baseline；从 `acceptance.active_projections()` 的正式快照获取信号，不能将它的候选 context 或 legacy raw-row fallback 带入正式 Writer。先验证身份与时间，之后排序/限额。保留候选修复入口。

- [x] 在 PostgreSQL 的实际 build→Writer renderer 路径构造合法已接纳、被拒候选、未来、旧版本、未知来源、其他项目和 resolved 信号；较新无效信号超过限额仍不挤掉合法项。合法接纳快照在候选诊断更改 raw row 后仍保持正式内容。
- [x] 红测断言示例：
  ```python
  prompt = render_world_context(knowledge.build(project_id=project_id, chapter_number=3))
  assert "正式冲突" in prompt
  assert "未来信号" not in prompt
  assert "候选诊断" not in prompt
  ```
  使用真实函数签名及既有 Canon fixture 完成；保留当前 repair contract 能看到候选诊断的正向对照。
- [x] 最小修复复用已接纳质量投影，检查 baseline 在冲突读取后仍有效；未知来源不被填造成正式信号。补 stale baseline 失败对照。
- [x] 运行新增测试、knowledge、Canon quality acceptance、修订及相关 repair 回归；保存红绿证据并提交 `fix(context): read world conflicts from accepted quality projections`。`37aa9a9`：红测 2 项按预期失败，相关回归 98 passed，独立审查通过。

## Task 2: 维护等待保留持久续跑意图

**Files:** `forwin/generation/auto_continue.py`、`forwin/generation/continuation_events.py`、`forwin/maintenance/state.py`，仅必要时扩展 `forwin/outbox/worker.py`/store 的现有延期能力；`tests/test_generation_continuation_outbox.py`、`tests/test_deferred_maintenance.py`、`tests/test_post_canon_maintenance.py`。

**Interfaces:** 区分 recoverable maintenance pending/running/retryable failure 与语义/人工阻断。等待经现有 outbox 带退避再次消费，不写不可变 `AUTO_CONTINUE_DECISION` stop，也不创建孩子。恢复后原意图恰好创建一个孩子。用户暂停/取消/策略变更/目标完成/真实内容阻断仍持久停止。

实现前核对发现现有 phase3 outbox 只完成四步，order controls 的调用仅在已退出的 pipeline 内，等待本身不足以恢复。Task 2 因此包含最小 owner 收敛：将既有 order controls 编排提取到维护 owner，pipeline 与既有 phase3 outbox 共用；按需要修改 `forwin/maintenance/events.py`、`forwin/runtime/container.py`、`forwin/generation/pipeline_core/world_projection.py` 及直接依赖。续跑消费者仍不构建 Pipeline/LLM runtime，不另建调度器或复制检查算法。

- [x] 使用真实 application 完成事务、维护行、outbox worker：先消费时维护 pending，随后维护恢复，再投递相同 event；断言第一次没有最终决定、事件未 ack；恢复后恰好一个孩子，重复消费仍一个。
  ```python
  first = consume(factory)
  assert decision(factory, parent_id) is None
  # 将同一隔离测试中的维护通过正式 owner 更新为成功后重放。
  assert child_count(factory, parent_id) == 1
  ```
- [x] 红测包含维护失败可重试、order controls 尚未成功、真正 checkpoint/future-plan blockers、用户在等待期间 pause/cancel、策略改变、目标达到、并发消费和 ack 丢失。测试不把不完整维护都硬当成功，也不无限重试内容阻断。
- [x] 最小实现复用现有维护状态 owner，有限退避沿既有 outbox；缺失/损坏状态保留可解释行为，不新建后台轮询入口。正常等待不吞异常或自行构建 LLM runtime。
- [x] 通过实际 phase3 outbox handler 和维护 owner 恢复四步及 order controls，再消费原续跑事件；证明父任务退出后仍有持久恢复入口，不能仅靠测试直接把 controls 标记成功。模型依赖使用冻结边界替身，检查顺序、冻结策略、人工/内容门和失败重试保留。
- [x] 委托调用在无 Project 行锁的事务边界外进行；沿既有控制 JSON 持久记录非最终等待，保留独立审计事务，在最终写入前重新核验检查点输入。真实 band-end warning 回归覆盖委托审计、并发续跑、崩溃重放及恰好一个子任务。
- [x] 跑 continuation/application/recovery/maintenance/outbox lease 回归，保存红绿证据并提交 `fix(generation): defer continuation until recoverable maintenance completes`。

## Task 3: 完整业务依赖与单次构建共享索引

**Files:** `forwin/knowledge_system/dependencies.py`、`page_repository.py`、`forwin/obsidian/exporter.py`、必要时 `forwin/llm_kb/compiler.py`/`source_validation.py`；测试 `tests/test_knowledge_dependencies.py`、既有 knowledge/LLM-KB/export 回归。

**Interfaces:** 业务 `state`/metadata/角色档案/证物中的 `created_at`、`updated_at`、`refreshed_at` 全部参与 fingerprint。只按明确 schema 边界排除真正系统 audit 字段。manifest 规则版本升级，旧证明 fail closed，现有投影重建后恢复有效。提供单次 runtime/baseline 作用域的依赖准备对象用于批量验证与导出，禁止进程全局缓存。

- [x] 为三个同名业务时间字段各写 manifest 失效反例，普通 owner 变化与真正系统更新时间噪声作对照；旧 version manifest 被拒、重新投影后的新 manifest 可用，原 Markdown/审计保持。
  ```python
  manifest = page_dependencies(runtime, scope="node", node_id="evidence")
  runtime.world.patch_state("evidence", {"created_at": "04:07"})
  assert not dependencies_valid(manifest, runtime)
  ```
  用实际 runtime 更新接口替换示例，并覆盖实际 list_valid_rows→Writer 和 LLM-KB 读侧。
- [x] 操作次数测试经真实 list_valid_rows 与 export 批路径验证 100/200 节点不会出现每页整世界复制；map-only 页不物化无关世界节点。邻接关系增删、认知改变、事实变化仍使相应证明失效；同一对象下一次构建也不能误复用旧状态。
- [x] 在现有 dependencies owner 统一规则和共享索引；局部页只取局部依赖，全书 scope 保留完整依赖，避免仅减少正确性检查来伪装优化。业务内容保留原语义，不改 schema 或改写旧记录。
- [x] 跑新增依赖测试、knowledge/LLM-KB/Obsidian/source-validation 相关回归，保存红绿证据并提交 `fix(knowledge): preserve business timestamps and reuse dependency preparation`。

## Task 4: 倒计时修复去除专属剧情假设

**Files:** `forwin/review/repair/plan_patch.py`、`tests/test_repair_plan_patch_owner.py`，必要的 countdown/future-plan prompt 回归；核对 `RepairInstruction` 和既有 countdown ledger 的身份/证据字段。

**Interfaces:** `countdown_repair_rule_anchors()` 保留具体倒计时的单调、当前值和明确 reset/branch 证据要求，不自动引入记忆重置、系统日志或伪数据。现有修复合同/ledger 传递具体身份，候选修复不能改写正式事实。

- [ ] 用港口潮汐/药效/设备电量等正常题材输入触发旧倒计时与回升修复分支，检查实际 ChapterExperiencePlan.rule_anchors 及 Writer 修复输入不加入无依据的记忆重置/系统日志/伪数据，仍包含具体上界和禁止无证据回升。
- [ ] 保留真实已给 reset/branch 证据时的合法区别、多倒计时不串线及无倒计时输入无新增规则对照。
- [ ] 删除通用路径的剧情假设，复用当前 repair contract；已有与错误措辞绑定的测试按业务行为更新，不宽松删除有效倒计时验证。
- [ ] 跑 repair-plan owner、future-plan auditor、countdown、Writer prompt 回归，保存红绿证据并提交 `fix(review): ground countdown repairs in the supplied contract`。

## 集成交付

- [ ] 全部任务独立审查通过后，针对 `1454e8a..HEAD` 做一次整体复核；只因明确新发现扩展测试。
- [ ] 同一最终源码跑全量 pytest、compileall、修改文件 F/E9、source guard、diff 和 Ruff 基线比较；不把既有警告当作零警告。
- [ ] 更新当前设计、执行状态和运行记录；推送并 fetch 核对 `HEAD` 与 `origin/for-review` 一致。旧 smokeD 仍保留旧源码和未通过结果。
