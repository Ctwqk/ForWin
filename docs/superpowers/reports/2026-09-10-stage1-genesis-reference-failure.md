# Stage 1 smoke：Genesis 历史事实漏传

日期：2026-09-10。此次 smoke 未通过；任务已安全暂停，不能作为长跑通过的证据。

## 冻结运行与失败

源码 `23b8cb78ed99966ce9b75ac110925fa49816bbcf`，tree `a49715e06d6eb67b1ef7970adcc87c61f8f8abe7`；该源码完整回归 **3105 passed、4 skipped、6 subtests passed**。精确运行镜像 `sha256:3e734ddc9972d01dfe9ecfbeaad6aa9fcbf3c9b417c34cd2dbabe8c995ade8ac`，浏览器镜像 `sha256:db4cdbf6d8c12b7c2a095b8c9f34eaeae31b318ce125da8763a2f711d0dd075a`，均通过源码、锁文件、实际角色和浏览器启动验证。

隔离项目 `9061342c41ed414d94fd404e0a3a4ad9`，task `df7d3ce8d88d`，书本目标 100 章、smoke 停点 20。六阶段 Genesis 冻结在 revision 15 / `88b80226f057456385699a9026c4b693`。运行只改变已支持的三个自动化 pause/delegate 字段，保留标准质量、模型路由、长度和有限修复策略。没有生产发布角色或生产数据写入。

| 证据 | 内容 |
| --- | --- |
| 锁定 `core_cast[0].secret` | 程岚在“四年前”明知附件缺页，仍因截止压力签下范围未充分标注的核查意见 |
| 锁定世界历史 | 四年前船厂事故；两年前经营重组与账户清理 |
| 正式第 1 章 | 开头和结尾均将那份不完整核查意见写为“两年前” |
| 正式第 2 章 | 再次写到调取“两年前由自己出具的核查意见” |

正文没有交代另有一份四年前意见与两年前意见。按现有证据，这是同一核心历史事件的时间线漂移；不能靠事后设想第二份意见来认定一致。两章均以 normal 模式、零次修复接纳。第 1 章 BODY hash 为 `db344942af42191fadd33e82b8a6c0fd29449dd351c5b021b38d737588e840f0`；第 2 章为 `e69dace278a4445e548bc3ca2d7d4705f9b0c30aef674273cf196b56ed87c9cf`。

只发送一次 canonical `task_pause`。终态确认于 14:32 UTC：paused，正式接纳 1–2，第三章未接纳。原观察器完成 BODY、版本身份、cost/gates 与任务导出，导出前后 book revision 一致；事件来自最近 200 条采样窗口，存在历史缺口，不伪称完整 trace。原样本保持冻结。

## 根因与修复边界

独立代码审查、冻结源码回放和实际 Writer 请求相互印证：

1. Genesis provider 只取世界 `overview`，没有历史片段；完整 story engine 暂存在内部 draft 中，但 assembler 只输出 long arcs 摘要，人物秘密没有进入 ChapterContextPack。
2. Reviewer context 已转发总览等旧字段，最终模型 payload 却没有输出这些内容。只改 provider 或 protocol 仍不能修复主审查输入。
3. 实际第 1 章的 22 个 trace 请求中没有完整人物历史事实；所有分场、场景写作与拼接请求都没有“四年前”。首次成功的场景写作响应引入“两年前”，随后拼接保留。部分配置供应商返回限额/请求错误并按既有路由回退，这不表示每个供应商均完成了有效生成。

修复沿现有 provider → context → Writer / main reviewer 传递同一份有界原文事实和版本/字段引用。世界历史、根规则、作者掌握的人物秘密均显式分类；当前状态、人物知情与揭示许可仍由既有运行事实和约束决定。整条省略与遗漏数避免悄悄截断含否定或时间条件的事实。独立审查又复现了来源预算挤占当前历史、审查缺少具体揭示限制两处边界：实际 RetrievalBroker 现在将来源压到调用者总预算的四分之一，整条省略并累计遗漏，压力下无关秘密先于当前 Canon 被移除；主 reviewer 同时接收已有本章禁止揭示、人物知情与允许线索。没有增加第二套 Canon、额外模型审查、年份专用规则或新质量门。

新回归先在缺失输入处失败，再验证五种写作消息及两条 reviewer context 路径。fixture 使用不同事件与角色谎言，证明原文和最终正文完整传递，并不声称确定性测试可以判断小说语义。实际冻结 Genesis 与原 BODY 的同一脚本回放显示，新实现各层均能收到原来的“四年前”及历史片段；此回放未调用模型、未重新接纳旧稿。

相关组合验证 **140 项通过**，包含实际预算裁剪、原地图传递、prompt 快照和修复合同；独立复审 **28 项通过**，两轮追加发现均已闭合。已有嵌套认知经 retrieval merge 传递，但旧 world-model builder 的 beliefs 当前为空；本次没有新增认知加载器，不能宣称运行时完整人物认知已经建立。最终全量 QA、两类镜像及新 smoke 的状态以[执行计划](../plans/2026-09-09-forwin-three-stage.md)为准。smoke 验收后仍需全新独立 L100 与结尾审读。输入完整不等于真实模型必然遵守，旧样本不得恢复后冒充替换候选的成功结果。

私有证据：`runs/forwin-stage1-smoke-23b8cb78ed99-ce0b85/` 下的 canonical Genesis/BODY、`body-review-0001.json`、`body-review-0002.json`、`backstory-diagnosis/` 与 terminal 导出。报告不包含凭据、供应商账户信息或完整原始请求。

最后的 helper 提取复审发现条目分隔符计费与裁剪后人物相关性重算的细微差异。两项先以实际边界测试复现，再恢复提取前语义。此前启动的 `28f34c3` 全量运行由操作者中断，仅有 329 项通过的部分结果，不计作完整验收；中断记录和冻结文件校验保留，后续源码重新冻结全量。

`b8f9487` 的冻结全量结果为 **4 failed、3119 passed、4 skipped、6 subtests**，351.33 秒。四项均为旧测试的精简 Pack/SimpleNamespace 缺少新增协议字段；实际 Writer/Retrieval 调用使用 ChapterContextPack。后续仅将四处测试替身替换为真实 ChapterContextPack、EntitySnapshot 和 PlotThreadSnapshot，保留全部原断言及裁剪参数；88 项相关测试通过，生产代码未为测试替身增加兼容分支。该全量失败与准备但未启动的 smoke 保留，替换提交须重新全量验证。
