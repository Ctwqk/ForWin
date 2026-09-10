# Stage 3 反馈消费与关联观察接线

自动链路使用既有 post-Canon feedback step：有限评论分析及其 trace 在独立事务完成；随后取得 Project 锁，计算同源窗口，再由唯一 ActionMapper 提议/选用动作，尝试版本化修改下一章计划。合格且已选的 canonical hints 重新进入 Writer ContextProvider；原始评论、旧字符串提示、全局自动校准、世界规则改写和反馈 review blocker 不随之启用。

Mapper 必须看到本次全部窗口，再在 selection 应用冷却。独立回归曾证明先过滤 cooled 信号会藏掉相反方向；接线修复后，即使某方向处于冷却，它仍是冲突观察依据。risk watchlist 也只观察；不会把一位作者反复催促转成自动改纲。具体来源和动作契约见[聚合](2026-09-09-stage3-comment-aggregation.md)、[行动与 Writer](2026-09-09-stage3-feedback-actions.md)报告。

计划成功仅记录 plan application。post-Canon 的正文观察检查实际 Canon、BODY hash、原计划版本和真实输入证据，但默认 assessment 为 unknown，不自动推断语义落实。损坏或无法解释的旧观察在维护结果中报告 `body_observation_unavailable`，保留原文；它不增加正文门，也不将观察错误当成需要内容 repair 的问题。数据库事务故障仍走原有失败/重试语义。

效果 owner 只比较行动保存的冻结 baseline 和同信号/方向/窗口的后续快照，不重新聚合或映射行动。至少 3 条完整分析的评论、2 位已知信号作者是最低描述性样本要求，绝不是显著性检验。比较要求不重叠的来源章节、评论 ID、评论发生/观察/摄入时间，并核对后续 publication 与已观察 BODY 的 Canon/稳定章/hash。缺少后续快照不能填成零；没有 BODY 落实证据、不同发布版本、重叠、迟到回填、时间未知、相反 BODY 评估都明确返回证据不足。

desired_signal_change 由 Mapper 唯一定义，正向热度增加与负向问题减少不混淆。输出为 `associated_desired_change`、`associated_opposite_change` 或观察性变化，始终 `causal_claim=false`。同一后续章窗使用实际快照创建时间排序；缺少时间或多个最新时间并列时不猜测，新快照不合格时也不回退到旧的较好结果。被排除的快照及原因保留在输出中。

关联记录在调用方事务追加，按相同内容去重。独立 PostgreSQL 两会话复现过旧 ORM 对象覆盖新审计历史；修复在 Project→Action 锁后刷新，并在整个 dirty 身份检查中禁用 autoflush，防止读取 expired 属性时偷偷写入。另有真实 post-Canon 反例证明 `[null]` 等损坏 BODY 历史会在效果读取中重新抛异常；现在归为 unknown，原始审计保留。已提交观察与未 flush 的调用方修改均不被静默吞掉。

独立复核运行原始缺陷 probes、效果、接线、BODY、评论消费共 **67 passed / 3.39s**；再运行[真实 owner 有限闭环](2026-09-09-stage3-feedback-finite-loop.md) **5 passed / 3.14s**，没有剩余审查阻断。根代理额外验证计划/CAS/Band/Subworld/Trope/接线 **98 passed / 5.91s**。这些计数有重叠，不代替最终全库候选验证。

恢复原生产备份的隔离副本至 `0007_feedback_actions` 已通过，并保留全部既有表/字段证据；具体许可变更和发布保护计数见[运行实施记录](../../operations/three-stage-implementation-2026-09-09.md)。尚未进行生产部署。当前代码和有限夹具验证不代表真实读者收益，也不代替正在进行的 Stage 1 smoke 与未启动的 L100。
