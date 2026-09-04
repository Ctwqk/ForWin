# V5 外部评审阶段一：实现与验证记录

日期：2026-09-04。基线：已推送的 `master@0a06cfa`。最终验证候选：`530824e245bdf43226947d532c85289a40a01fd3`。

本轮落实用户外部评审中的三个具体修复，冻结 V5 主架构。设计依据是[本轮规格](../superpowers/specs/2026-09-04-v5-autonomy-fixes-design.md)，当前系统说明见 [CURRENT_DESIGN](../../Design-docs/CURRENT_DESIGN.md)。没有增加 Agent、数据库表、迁移或 worker。

## 实现

| 提交 | 原问题 | 当前行为 |
| --- | --- | --- |
| `285941d` | 修复验证只读头尾，有限规则被当作全部合同的证明 | 提供完整原稿和修复稿，逐条记录 pass/fail/unknown、理由和引用；核对来源、逐字内容和偏移。有证据的反对最多复核一次，单次30秒，无隐藏超时重试 |
| `6486775` | trace 存储失败回滚业务维护，恢复时可能重跑模型 | 四步维护与 order controls 将冻结、脱敏 trace 同事务存入既有 outbox；独立 handler 只上传。内容 SHA 纳入事件及对象身份，迟到旧补传不覆盖新 trace |
| `7047a59` | 临时阻断提前消耗每日调度机会 | 无实际工作时保留同日再评估机会；项目调度锁串行检查，任务入队、Canon 发布 job 释放和成功日槽共同提交；旧 blocked/idle 日期记录可恢复 |
| `5a3b301` | 并发设置编辑可能写回旧 automation_json、覆盖新日槽 | 设置入口先取得 Project 行锁再读取并合并设置 |
| `dd37ab7` | review 持久化忽略 null，重载后 unknown 被旧默认值变为 false | 两处 review metadata 写入保留显式 null；真正缺字段的旧记录继续默认失败 |

Canon、BookState 和业务维护屏障继续有效。真正的维护失败或过期 lease 仍不能提交；trace 上传异常仅由 outbox 重试。L200 collector 区分 Canon 三类事件与新增诊断事件的数量，同时保留所有相关 outbox 的 pending/failed 检查，不能靠过滤诊断积压获得通过。

## 定向验证与交叉复核

所有修改先复现失败，再实现修复。测试采用已有本机 PostgreSQL 55432 的隔离随机库、内存依赖和可控模型替身；没有真实模型调用或章节生成。

| 范围 | 对照与最终定向结果 |
| --- | --- |
| 原基线 | 5个相关文件47项通过 |
| 合同覆盖 | 新回归先出现15项失败；修复后108项相关测试通过。覆盖正文中段、语义保护、超过8条合同、引用真伪、超时、预算和截断JSON；不自动修补截断JSON后把它当完整证据 |
| review 持久化 | 正常修复与耗尽后 final-residual 两条保存路径均先复现失败；补丁后12个相关文件116项通过。实际 commit、新 Session 重载、review API、Canon eligibility 均保留 unknown，旧缺字段仍为 false |
| trace | 默认新回归先9失败/1通过；相关默认116项通过，MinIO/L200等642项通过。覆盖业务成功后上传失败、补传不重跑业务、业务失败屏障、stale lease、乱序补传与上传确认丢失 |
| 调度 | 新回归先12失败/2通过；扩展相关83项通过。真实 PostgreSQL 并发/回滚验证同日恢复、任务约束冲突保存点、调用方事务和发布 job 释放 |
| 并发设置 | 实际调度事务未提交时，第二连接编辑设置，先复现1失败/22通过；锁后读取补丁后23项通过。任务随后结束也不会再次获得同日批次 |

独立复核发现并补齐最后两项边界问题；最终复核没有未解决的 P1/P2。复核者只读代码与测试，测试结果来自独立执行日志。

## 最终完整本地验证

首次完整运行在dd37ab7得到2216通过、2失败、1跳过：一处新注释触发旧术语清单，trace adapter缺少精确路径登记。530824e仅修订注释，并在现有ownership规则和文档中登记该窄adapter；不放开整个maintenance目录。该模块只读事件身份，仍通过outbox store入队、由worker控制状态与lease。两份架构测试40项通过，随后重跑全套。首次失败原始输出保存在`initial-full-default.*`，不改写为通过。

最终两套独立进程测试均在`530824e245bdf43226947d532c85289a40a01fd3`通过，之后仅更新Markdown文档：

| 检查 | 结果 |
| --- | --- |
| 默认完整 suite | **2218 passed / 1 skipped**，另6个subtests通过，177.18秒，退出码0 |
| 独立 RC harness suite | **1156 passed**，33.71秒，退出码0 |
| Ruff / compileall / diff check | 通过 |

合计3374项通过、1项跳过；默认真实API测试未启用。默认suite的5条warning和RC的1条warning保留在原始输出。

默认 suite 与 `.artifacts/rc-candidate` 分别运行，不合并加载已有 fixture。Ruff 范围为 `forwin tests`；编译包含这两处及 RC harness。改动的 RC 文件另与基线对照 Ruff：新增0条，保留5条原有诊断，不声称整个隐藏目录零诊断。

原始命令、环境覆盖、退出码、日志及 JUnit 位于源码工作区 `.artifacts/v5-autonomy-fixes-2026-09-04/`，包括 `repair/`、`trace/`、`scheduler/`、`settings-*.result.json` 和最终 `pytest-*.result.json`。这些本机证据不推送；回归测试、规格与本报告随代码交付。前轮原始长篇产物保持不变。

## 已知边界

- unknown 是验证覆盖不足，不能显示为合同全部通过。它不新增人工门或重写循环；主 review、hard residual、证实失败与 Canon 资格继续决定准入。完整合同和正文超过24000字符输入预算时返回 unknown，尚未实现定向片段检索。
- 已上传 trace 的冻结 payload 仍保留在既有 OutboxEvent，未增加清理策略。
- 调度保持每日一次成功批次，包括同步 review。它不保证生产后才出现的发布 job 或 review 后人工批准能触发当天第二批；也没有跨崩溃累计 review 额度账本。review 回调的独立事务以持久章节状态恢复，测试中的回调替身不代表完整模型或 Canon 停服恢复演练。
- 本轮没有中篇、L200、长期发布、生产停服恢复或部署；本地测试不证明文学质量提升或七天无人值守。阶段二以后的质量与发布实验仍是后续工作，不自动启动几十或几百章生成。

## 集成与复核

继续使用远端默认分支 `master`，远端不存在 `main`。采用从 `0a06cfa` 快进本轮提交并推送的方式；以服务器SHA与本地HEAD一致作为推送完成条件，实际结果保存在本机证据目录的`integration.json`。保留其他 worktree 和历史分支；原历史 publisher recovery 分叉已在前轮以 `471aa3c` 移植，不重复合入旧状态。代码推送与部署分开。
