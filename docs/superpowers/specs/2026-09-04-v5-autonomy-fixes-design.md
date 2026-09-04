# V5 运行自主性：三个已核实缺陷的修复

日期：2026-09-04。来源：用户对 master@0a06cfa 的外部评审及“根据这个进行下一步开发”的授权。范围为评审阶段一；不执行中篇/长篇生成、平台发布或部署。

V5 主架构冻结。Canon、BookState、有限修复、任务租约、四步维护屏障和发布身份约束继续有效。用既有消费者、持久记录和本地故障注入验证改动，不建立新的 Agent、世界事实库或验证平台。

## 1. 修复验证覆盖

RepairVerifier 接收完整原稿与最终修复正文、全部合同条件，不能固定截头尾、截掉第9条条件或从中间截断JSON。每条 must_fix / must_preserve / must_not_reveal 有稳定ID、pass/fail/unknown、判断方法、理由和可核对的当前正文引用。超出输入预算、超时、缺条目或无效引用明确为unknown。

规则只对实际有覆盖的条件给出确定结论：受保护的exact标题、绑定的持续错误签名等。语义条件不能凭“没有检测到错误”推导为已满足。语义判定引用须对应本次原稿/修复稿；有证据反对最多再调用同一verifier一次，单次30秒且禁用隐藏超时重试。不存在无界复核或为补证据调用writer。

RepairVerification两个既有聚合值使用True/False/None表达已验证通过/证实失败/未验证；缺失旧字段仍按原False默认。unknown是覆盖状态，不新建质量错误或人工门。主review、hard residual和已证实的修复失败仍阻断；unknown透传API、修复记录与界面，不能显示“合同全部通过”。无证据LLM反对不能推翻有覆盖的规则结果。所有相关消费者同步使用显式失败判定，避免None被隐式转成失败或成功。

## 2. 业务维护与trace补传

同时修改四步维护和order_controls的事务内上传。业务结果、completion fence、step成功状态与已脱敏冻结trace的OutboxEvent同事务提交；事务中不调用对象存储。真实业务异常仍回滚并阻断下一章。不能只吞上传异常丢掉诊断数据。

新增窄事件maintenance.trace.upload.requested，复用既有outbox租约、退避和worker。payload携带完整冻结内容及其SHA；event_id和对象key含commit/step/content身份，避免重跑order_controls后的旧补传覆盖新trace。handler只校验并上传冻结内容，不能构造LLM/规划服务，不能重跑业务，不能覆盖维护result_json。维护记录保留event_id/key/hash，上传状态以outbox为准。无attempts时不创建事件。

不增加表、迁移或worker。同步MinIO恢复runner和证据判定：存储恢复只增加trace上传重试，不应增加已完成业务步骤的尝试次数。

## 3. 同日重新调度

把最近检查与当日已预留的生产/发布额度分开。active task、waiting review、临时失败或暂时无内容不消耗当日机会；状态解除后同日可以再次评估。每日上限按已经持久预留的工作计算，重复tick、重启和并发不能再次获得相同额度。

复用Project自动化持久状态、既有GenerationTask和Canon发布job，不创建第二套调度器。项目级事务串行化检查、额度预留、任务入队与发布job释放；必要时GenerationApplicationService接收调用方session，保持应用层唯一入队入口，避免独立事务间的提交窗口或锁等待。保留任务active唯一约束、发布幂等和原质量/策略条件。

## 验证与交付

已核实0a06cfa相关基线47项通过。每项先写能复现缺陷的测试并观察失败，再做最小实现，分别提交。三项交叉复核后，完整运行默认tests和独立RC harness、Ruff与编译；默认真实API测试不启用。只用本机随机测试库/内存依赖，保留命令与原始结果。当前设计和操作说明按最终代码更新，集成并推送既有master；不把本地PASS称为文学质量、长期无人值守或正式发布证明。
