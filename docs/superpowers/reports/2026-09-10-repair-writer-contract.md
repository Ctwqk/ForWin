# 当前重写合同传递

日期：2026-09-10。修复基线：`0b3c49a04f987ceb2b915346473dd2120867cb8e`。

该候选全量 QA 为 3133 passed / 4 skipped，两类完整镜像均已验证。但实际 smoke 项目 `f11fbd5218d847858a4ea537c9821d5f` / task `de009e01ad26` 在第 3 章用完三次常规修复后进入 `needs_review`，只接纳第 1–2 章。不能继续声称 smoke 或 L100 通过。

## 复现与边界

原计划补丁只把前三条 `must_fix` 写进体验计划的规则锚点；`must_preserve` 和修复专用 `must_not_reveal` 没有送进 Writer，verifier 却检查完整合同。标题另有精确恢复逻辑；已有世界保密约束仍在，不能把它们也算作全部漏传。

独立回放调用真实 `RepairPlanPatchService.apply` 与 RetrievalBroker，仅替换重建上下文的数据来源。相同脚本分别固定到原 archive 和当前源码，核对全部已加载 ForWin 模块所属目录，覆盖 draft、chapter、band 有/无 schedule 四条路径，再生成五类提示。每条路径的第四项纠错、独有保留条件、修复保密条件均从 0/5 变为 5/5；已有世界保密仍为 5/5，后续章节计划没有写入当前合同。该回放没有模型调用或业务数据库访问。

真实失败不只有传递问题。最终 Writer 输入仍包含“卷夹已撤出封箱并列入复核清单”的 Canon 定义；第二次修复建议围绕扩大范围展开，与前一正式章节已经扩大到通知流转的细节存在语义张力。最终未接纳正文还有重复撤卷和未经前文建立的三个月空白。不能据传递回归通过，就把这些模型行为或 reviewer 建议一并判定已修复。

## 当前实现

- `RepairContract` 统一定义三列表，`RepairInstruction` 继承该结构；当前重写 context 保存独立副本，verifier 仍使用同源完整 instruction。
- 计划补丁完成重建后才附加合同，纳入现有软预算。五类 Writer 提示共用一处完整渲染；下一次修复替换当前合同，普通初稿不携带。
- 通用纠错文本不再累积为持久化计划锚点。现有倒计时专用提示、Canon 优先级、世界保密、重写预算、verifier 和接纳门均保持。

## 验证记录

新增 owner → prompt 回归先得到 **21 failed / 1 passed**，失败指向缺失第四条及完整合同预算，而非导入或运行异常。实现后增加跨轮替换/世界保密用例；与修复验证、标题、Writer、Genesis 相关组合 **108 passed**，计划/委托/事务组合 **39 passed**，独立合同/计划/无 preview/字数预算组合 **30 passed**，数字有重叠。旧 owner 测试的 SimpleNamespace/object 占位已改用真实 typed context 和 broker，保留原事务、计划替换和无 preview 断言。F/E9、diff 检查通过，改动文件相对原 lint 基线没有新增诊断签名。

私有证据位于 `.superpowers/sdd/2026-09-09-forwin-three-stage/runs/forwin-stage1-smoke-0b3c49a04f98-f7e056/`：终态报告 `53-smoke-terminal-assessment.json`，正文版本与 hash、成本/门禁导出，以及 `repair-contract-diagnosis/` 下的失败回归和双源回放。四版 Writer 原始产物另按逐文件 SHA 留存；事件采样窗口存在潜在缺口，不宣称完整 trace 覆盖。

修复提交 `5524e362ed757a270801dd560d1984d185e7c7eb` 后，全量 QA 实际通过 3156 passed / 4 skipped，两类完整角色镜像均已验证。其新 smoke 在写作前的 Map revision 7 又发现路线字段合同不一致，尚未产生章节任务；详见[路线合同修复记录](2026-09-10-genesis-route-contract.md)。这轮工程验证不能替代 smoke20、独立 L100 或结尾验收。没有热修改失败运行、手工放行或生产部署。
