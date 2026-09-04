# ForWin v5 收口复评与范围冻结

日期：2026-09-04。状态：本轮执行基线；用户授权直接调整设计、删除实现及消融。

最新任务范围：只写现有设计，完成本地消融与测试，再推送远端默认分支 master；不整理历史设计全集，不承担外部评审工作。本任务不启动几十或几百章真实生成。下文 L200/发布条件保留为正式发布设计的未完成项，不是本任务的执行清单；推送代码不代表已经部署或通过发布验收。

## 权威与代码基线

本修订覆盖《ForWin v5 收尾最终总方案（复核执行版）》的重复验收和新增验真门槛；未修改的架构约束、真实恢复证明及最终全新 L200 no-hotfix 要求继续有效。2026-07-09 架构收敛设计定义目标形态，CURRENT_ARCHITECTURE 定义当前实现。旧审计 Phase A-F 和旧 Final Roadmap 不重新成为待办。

已 fetch origin 并核对所有本地分支、远程分支和八个 worktree。最完整实现为 `codex/v5-r9-integration-candidate` / `fdaeaa687a0dc4c65550a01d030e7d97d43b0c5f`，领先 `master` 和 `origin/master` 147 个提交。旧 v5-final、v5-a2-convergence、v5-r8-publisher-recovery、v5-architecture-convergence 及 publisher-login-session-routing-fix 均为其祖先。唯一分叉的 `a3dc4e9` publisher recovery 修复已以 `471aa3c` 移植，publisher runner 和其测试完全相同，其余差异为候选后继的 Qdrant/证据校验修复。所有原 worktree 的 tracked state 均干净。

本轮在 `codex/v5-closure` 上执行，起点为上述最领先 SHA；原 R27 worktree、镜像、项目与报告保留。测试和复核通过后快进集成 master 并推送，按用户最新要求先完成代码交付；正式部署另行记录。

## 复评结论

1. Track A、B0 已完成实现：provisional preview、Scenario runtime、Obsidian reverse import 已物理删除；BookState extraction/read models 已归位；Generation Audit 固定 report-only；S1/S3/S2 已有实现和 MCP 查询。冻结这些功能族，停止扩建，S4-S8 仍属发布后。
2. A2/A4 已有代码决定，补录依据与验证结果即可，不重新等待矩阵后再删一次。Daily automation 保留。
3. 真正欠缺是当前候选的行为证明和集成发布。历史测试 PASS、旧 SHA 的故障恢复 PASS、runner 已实现与当前 RC 已通过必须分开记录。
4. 验证工程已经显著膨胀：tracked rc-candidate 共 44 文件，Python 约 60,337 行（含测试）。不再增加证明框架、运行模式或重写这些脚本；只修妨碍实际验证的具体缺陷。

## 验收范围修订

### 预 RC 诊断

30/60/60/100 保留为覆盖不同 profile/delegate 和长程行为的诊断矩阵。已有部分运行作为诊断证据保留，失败、人工动作和代码版本如实记录。它们不等于最终发布证明。

预 RC 的报告、文档或无运行行为影响的 harness 修正只重验受影响检查，不自动使所有既有诊断章节失效。涉及 runtime/prompt/model/policy/schema/规则的修改需要新的受影响行为验证；禁止混合不同版本结果声称原矩阵 PASS。原完整矩阵未完成就记录 partial，不能改表获得 PASS。

有内容缺陷的 needs_review 先分类：正文事实冲突、规则改写应由既有 repair/review 处理；通用实现缺陷必须修成因并冻结回归。不得为追求无人暂停而放宽 Canon、hard-floor 或 Review Form。

### RC 身份与证明

恢复原总方案的二选一：annotated tag **或不可变 commit record**。已有完整 Git commit SHA、tree hash、clean-tree 验证和镜像 revision 足以构成后者，不新增模式或 schema。有 tag 时必须继续验证其为 annotated tag 且指向相同 commit；无 tag 时不得放过错误 SHA/tree、dirty tree、镜像不一致或缺失的行为证据。

外部签名/第三方 attestation 不是本次发布门。本地哈希仅提供在操作者可信前提下的变更检测，不能声称防范恶意操作者。保留来源、镜像、策略、模型路由、prompt/skill/规则、证据文件校验和与实际调用记录。

### 本轮完成条件与后续正式发布条件

- 当前最终代码完整测试、Ruff、编译通过；测试环境不依赖真实生成服务。
- Canon 单一原子写入口、不可变 candidate、项目所有权隔离、lease fencing 和 Spark 无直接 Canon 权限。
- 现有设计文档与实际代码一致，本地消融记录收益和损失，复核后集成并推送 master。

以下为正式发布的后续工作，本轮不执行：

- 真实 generation pre/post-commit、投影、对象存储和 publisher 故障恢复；setup_blocked/旧 SHA PASS 不改称当前通过。每个实际缺口按已有 runner 执行并留下证据。
- 所有行为修复和 A2/A4 决策落地后冻结 RC，启动全新项目从第 1 章完成 200 章。运行期间代码、prompt、模型、配置、schema、规则和阈值均不可变；热修使该正式运行失效。
- 最终 Canon/候选/GraphDelta/outbox 身份无重复，投影收敛，publisher 无重复外部效果，S1/S3/S2/task 报告完整。不能用单元测试或 pulp60 替代此项。
- 发布门满足后通过 150 deploy sync 部署和生产 smoke；集成、推送 master 按本次用户要求提前完成。

## 当前运行事实

R27 来源为 `fdaeaa6`。2026-09-04 通过隔离 MCP `127.0.0.1:19196/mcp` 重新读取：L30 8/30、L60S 8/60、L60P 60/60、L100 9/100；三个 standard 项目各有一个 needs_review，active generation count 为 0。总 accepted 85/250。已有 R27 manifest 的部分 task_ids 只指初始任务，不能替代当前项目和完整任务清单。

L30/L60S 有正文或结构化 Canon 规则冲突，需要既有内容修复。L100 的原始 WriterOutput 已证明一个通用输入缺陷：stitch 后最终正文使用“辰正二刻完成、辰正三刻离津、巳初一刻送达”，但 `scene_outputs[1].text` 仍是 stitch 前“巳初二刻完成、巳初三刻离津”；review 错误引用后者。最终 `body` 是正文审查事实源，stitch 前场景文本不能作为并列正文再次提供给 reviewer。修复采用删除重复正文输入，不增加第二审查器。场景位置等结构信息与 Canon 结构化候选仍可保留，但不得把旧场景叙述当最终发生的事实。

另已确认第三次 repair 将受保护标题从“第10章”改为“第10章 空税入泽”，触发真实的标题保留合同；这与旧场景导致的正文假阳性分开处理，不删除保留校验。保留原始样本，不修改报告来消除错误。

R9 尚未正式冻结，L200 和部署尚未完成。当前设计可以收口，发布状态仍须由以上行为证据决定。
