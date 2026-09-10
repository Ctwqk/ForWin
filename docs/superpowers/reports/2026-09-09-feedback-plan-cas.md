# 反馈计划与既有计划写 owner 的 CAS 修复

独立审查在真实 PostgreSQL 复现：旧 session 捕获章节体验计划，另一事务合法应用并提交反馈 hint，旧 session 随后调用 `StateUpdater.update_chapter_experience_plan` 保存旧 typed payload，覆盖新 hint，而反馈应用审计保留 applied。原反馈服务 26 项通过并不能覆盖不遵守版本检查的其他计划 writer。

本包不新增计划计数器、模型审核或第二个规划入口。`experience/plan_guard.py` 复用现有 `candidate_plan_revision`，要求明确捕获的原版本，在 `no_autoflush` 下按 Project→Chapter 顺序锁定并重新读取，版本不同抛出 `PlanRevisionConflict`。未 flush、未持久化或来自其他 session 的章节输入不被刷新吞掉。

- `ExperiencePersistence.save_chapter_experience_plan` 必须传 `session` 与 `expected_plan_revision`，在写整个体验 JSON 前执行该检查。
- `StateUpdater.update_chapter_experience_plan` 必须传原版本，初始查询也禁用 autoflush；无法知道原版本时不能用当前版本猜测代填。
- `FeedbackPlanService` 由其 owner 传入已验证的 `before_revision`，保留自己的未来章、Canon、发布、预约和动作检查。
- `BandPlanService` 在任何 proposal 生成之前捕获每个 active-band 章节的原版本。完整 schedule/chapter proposals 在取 Project 锁之前生成；全 band 按章序重新锁定验证后才进入会写 registry/roster 的 activation、band schedule、trope 和章节保存。当前 scheduler/chapter planner/calibration 实现均无模型调用；没有新增锁内模型工作。
- savepoint 在本次 band 方法的所有派生写入之前建立；后位冲突或后续 world-contract 异常撤销本次整个 band 的写入，异常原样交回调用方，不转换成无限模型重试。调用者早已提交的变化及竞争 owner 的新计划保留。

`tests/test_experience_plan_cas.py` 的四项真实 PostgreSQL 回归覆盖旧 typed payload 拒绝、dirty caller 保留、生成过程中最后一章变化拒绝且前两章无部分更新、registry/band/chapter 已写入后下游异常的整包回滚。原 updater 与 band 的测试调用已提供真实捕获版本；trope 参数传递单元测试改用附着于实际 session 的章节，保留原参数断言。

冻结前验证：

- `.venv/bin/python -m pytest -q tests/test_experience_plan_cas.py tests/test_feedback_plan_service.py tests/test_band_plan_service.py tests/test_trope_selector.py tests/test_subworld_control.py tests/test_feedback_consumer_transaction.py tests/test_experience_planning_service.py tests/test_band_plan_obligation_patch.py tests/test_large_module_boundaries.py --tb=short`：**104 passed / 5.80s**，日志 `/tmp/plan-cas-final.log`。
- 新 guard/测试 Ruff 通过；五个既有修改文件相对 HEAD 新增 Ruff 项 **0**（`/tmp/plan-cas-ruff-added.json`）；编译及 `git diff --check` 通过。

此包保证这些已接线 owner 的版本化保存和事务边界，不能把 `plan_application.applied` 称为实际进入最终 Writer 输入或正文落实；这些仍由独立输入/正文证据记录。

独立只读审查再次运行四项 PG 回归：**4 passed / 1.53s**；确认真实 ChapterExperiencePlanner 不依赖 activation 后追加字段，前移 proposal 未改变其派生语义。
