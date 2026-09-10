# Stage 2 A0 职责重构记录

本包实现批准路线图的行为保持重构；消融结论另行记录。Writer 已在 `de3d854` 提交，本包接续 Review/Repair 与 Canon 准备，不改变质量策略、模型路由或修复预算。

`CandidateReviewService` 依次执行实体登记计划、评审、两种自动修复及各自重新登记/评审；修复稿最后仍经过 `RepairVerifier`。配对的最终 BODY 与 review 一同持久化。`RepairPlanPatchService` 接管原有临时正文提示、chapter/band 计划补丁及前后快照。`RepairExecution` 从十九个回调字段收为六个具体协作者，暂停和进度通知有明确边界。

`CanonQualityPreparer` 接管原 quality gate、共享分析缓存、deferred acceptance、义务检查、artifact 和 trace；`BookStateCanonPreparer` 保留图增量抽取与 review。准备服务每次接收显式候选请求及策略/模型/存储/recorder，缓存服务不绑定任务状态。正常准备的事务提交、历史修订核验与独立 `CanonAdmissionService.commit_plan` 原子接纳保持原顺序。旧反向回调桥、重复 helper 和 Stage 文件已删除。

验证结果：

- 新 owner 合同先复现缺失/失败，再实现通过；可在不构造完整 Pipeline 时验证。
- 合并相关回归 387 项通过；另有 26 项 trace、修复起点提交、回滚和失败/暂停 span 验证通过。
- 独立验证 153 项通过，包含完整历史后缀和真实 PostgreSQL 准备事务边界；另一连接的 NOWAIT 锁测试证明准备释放锁后才进入独立接纳事务。
- Canon 中十四个迁移 helper 与原 AST 相同；独立差异审查未发现阻断问题。标题、phase/cycle 修复预算、候选身份、异常、暂停、outbox 及共享 quality cache 均有现有回归覆盖。

这些测试使用受控响应，证明职责迁移与业务契约，不证明实际模型准确率或性能收益。阶段末全量回归、正式消融与隔离长跑状态继续在当前实施计划中记录。
