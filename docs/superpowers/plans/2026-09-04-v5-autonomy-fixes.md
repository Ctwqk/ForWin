# V5 运行自主性缺陷修复实施计划

> **For agentic workers:** 使用 superpowers:subagent-driven-development，任务结束后独立复核，全部结束后整体验证。

**Goal:** 修复合同验证覆盖、trace存储阻断及每日调度漏跑三项问题。

**Architecture:** 复用RepairVerification、OutboxEvent和ProductionScheduler。Canon、任务租约、维护顺序和publisher identity不变；不加通用框架。

**Tech Stack:** Python / Pydantic / SQLAlchemy / PostgreSQL / pytest；现有UI状态展示。

**Spec:** [三个修复的设计](../specs/2026-09-04-v5-autonomy-fixes-design.md)

## 全局约束

- 基线0a06cfa；工作树codex/v5-autonomy-fixes；原运行栈、历史样本不变。
- 无真实LLM、章节生成、平台发布或部署；测试DB仅本机55432隔离随机库。
- 主测试与.artifacts/rc-candidate分别调用，避免已知fixture加载冲突。
- 未验证与证实失败分开，不用LLM无证据反对制造重写循环。
- 每项代码只stage本人拥有的文件，提交时与控制者协调索引。

## Task 1：RepairVerifier合同覆盖

**Files:** forwin/protocol/review.py、forwin/review/repair/verification.py、forwin/generation/pipeline_core/review_autofix.py、forwin/canon/eligibility.py；实际final_residual/API/read model/UI消费者及相关测试。

**Interfaces:** verify继续接受原/修WriterOutput、前/后ReviewVerdict、RepairInstruction并返回RepairVerification；两个aggregate为bool|None，新增逐条checks与最多一次复核次数。消费者仅对is False制造修复失败，unknown保留覆盖状态。

- [ ] 测试中段错误、标题相同但语义保留破坏、真实/伪造引用、无证据反对、timeout、超过8条条件、超预算JSON、API/UI unknown。
- [ ] 先运行新回归，确认旧版失败；例如修复正文为头1500字+中段秘密泄露+尾1500字，断言引用可定位中段且不会被规则标题pass吞掉。
- [ ] 实现完整输入/逐合同结果/证据核对/有界复核；保留持续error、硬review及Canonical边界。
- [ ] 运行修复验证、repair service、eligibility和review API消费者测试，Ruff，独立复核后单独提交。

## Task 2：Post-Canon trace durable补传

**Files:** forwin/maintenance/post_canon.py、新forwin/maintenance/trace_upload.py、forwin/outbox/handlers.py、forwin/runtime/container.py、tests/test_post_canon_maintenance.py、trace/outbox相关测试；.artifacts/rc-candidate/minio_recovery.py、recovery_evidence.py及相关测试。

**Interfaces:** 注册maintenance.trace.upload.requested；冻结payload包含脱敏trace/commit/step/key/SHA。result.trace保存引用。handler依赖ArtifactStore provider，不依赖PostCanonMaintenanceService provider。

- [ ] 故障注入存储异常，断言业务结果已提交、业务调用一次、trace outbox pending；新worker补传后同内容/key/hash且业务次数不增加。
- [ ] 测试实际业务失败仍阻断、stale lease不提交、order_controls重跑的乱序trace不覆盖、上传成功但确认丢失可幂等重试。
- [ ] 观察旧耦合失败，再实现同事务入outbox及只上传handler；不改outbox通用状态机。
- [ ] 更新MinIO恢复断言，保留真实identity/fencing/恢复检查；运行相关默认与RC测试、Ruff，独立复核后单独提交。

## Task 3：同日调度再评估与幂等预留

**Files:** forwin/production/scheduler.py、planner.py、executor.py、repository.py、forwin/api_schema/project.py、forwin/application/generation.py及generation/production相关测试；必要的自动化读写归一化消费者。

**Interfaces:** Project automation持久保存最近检查与当日预留；复用task/job身份。GenerationApplicationService.enqueue可接收调用方Session，单独调用保留原事务行为；scheduler的同一项目检查/预留/入队/释放在同事务串行执行。

- [ ] 同日第一次active/waiting_review阻断，解除后第二次恰好入队，第三次及重启后不重复；先验证旧版失败。
- [ ] 测试并发两个tick、提交失败回滚、入队成功后重启、发布重复释放和每日额度不超发。
- [ ] 实现事务预留和重新评估，保持受支持的应用入口及现有策略，不靠进程内锁证明幂等。
- [ ] 运行production/generation/outbox边界测试，Ruff，独立复核后单独提交。

## Task 4：整合、复核与交付

- [ ] 独立审查三项交互及最终diff，修复可验证问题。
- [ ] 最终默认完整suite、独立RC suite、Ruff和compileall；保存准确SHA和原始日志。
- [ ] 更新CURRENT_DESIGN和本轮验证记录，提交；快进master并push后核对远端SHA。
