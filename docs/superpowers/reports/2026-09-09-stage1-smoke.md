# Stage 1 隔离 smoke：缺陷与安全暂停

本报告不是通过结论。发现确定的规则状态接口缺陷后，2026-09-10 04:16:40 UTC 经正式 MCP 请求安全暂停；04:23:24 的最终只读观察确认 `status=paused`、`current_stage=paused`、`error=null`、`failed_chapters=[]`。最终第 1—5 章 accepted，第 6 章为完成一次 repair 后的第 2 版 drafted，第 7 章 planned。原样本未完成 20 章，既定结尾未测试；本次证据收口时，全新 L100 未启动。采集器随后读取未生成的第 7 章而报错，不改变任务已暂停的事实。

| 身份 | 固定值 |
| --- | --- |
| 源码 | `c62f6d02329b5d766569f37159fe57f16907c7ad` |
| 源码树 | `5da6f38651decfef0cadb4b13b9e4346fc566ccb` |
| 运行镜像 | `sha256:f8a1875c9617391f2984bf1c66f90267a697a16a0c72c58f8bc01888221f7e20` |
| 项目 / 任务 | `693a1a7df3124c1088f2201e9581f023` / `08104286cb5b` |
| 范围 | 20 章现实悬疑《末班渡轮的交接单·隔离排障样本》；独立 PostgreSQL、Qdrant、数据卷及网络 |
| 模式 | 现有 `soak_test`，`isolated=true`，`run_until_chapter=20` |

Genesis 六阶段经标准 MCP generate/refine/lock 完成后，核对无活跃任务，再调用唯一 `project_start_writing` 入口。两次写作前 refine 分别澄清旧案人物责任与涨水地点顺序；未在写作中修改设定。完整有效策略逐字段回读，只修改现有 `manual_checkpoints=false`、`band_checkpoint_action=continue`、`gate_delegate=spark`，版本 1→2；字数、有限 repair、质量和 Canon 门保持 standard 原值。最终 gate 账本的 delegation opportunities/evaluations/fires 均为 0，不能凭配置认定 Spark 实际执行了委托审批。

样本没有 publisher/browser/automation 角色及生产业务卷，不产生真实平台发布。任务/章节/费用来自隔离运行内部的正式 ForWin MCP。只读观察器保留 15 秒采样、事件身份及窗口饱和标志；没有从生产 SQLite 或临时 HTTP 请求推断业务状态。

第 1 章实际接纳 3030 字，Canon `6f50dbaa9aa1bcf1a2cabe92d9999250c1527a3983691966d87aa1eb53e5cf40`，BODY SHA256 `082ca7cca49979b82b7475561ce83cf39da4d0b15d4ebee0dfa99c310edffe87`。它经过一次接纳，未走 repair，residual 为空；这些状态不等同于没有文学或连续性错误。

最终项目 `book_revision=5`，已物化 7 章、生成 6 章、接纳 5 章，`next_gate=chapter_6_accept`。五个 accepted 正文共 16,765 字，均为 `acceptance_revision=1`、`acceptance_mode=normal`、repair 次数 0；逐章重算 BODY SHA256 与最终 MCP 返回值一致，稳定 chapter/candidate/active Canon 身份完整。下表仅缩写身份，完整值及来源文件 hash 保存在私有 `smoke-terminal-summary-20260910.json`。

| 章 | accepted 字数 | active Canon 前 12 位 | BODY SHA256 前 12 位 |
| --- | ---: | --- | --- |
| 1 | 3,030 | `6f50dbaa9aa1` | `082ca7cca499` |
| 2 | 3,281 | `dabfd2165ad6` | `ebeca8330ef5` |
| 3 | 3,495 | `22e7dbfa93c6` | `ab8e1b77f2c9` |
| 4 | 3,661 | `cdb2412bcdc0` | `d17f5fa98c8b` |
| 5 | 3,298 | `dd3c61800d16` | `1fbbb779a790` |

第 6 章 drafted 为 3,669 字、version 2、repair 次数 1，BODY hash 重算一致，但没有 active Canon/candidate 接纳身份，不能计入 accepted。第 7 章没有 draft、review 或正文。

人工核对发现明确时间矛盾。第一场景要求“二十二点前提交日结数据”，尚余二十七分钟；后面场景出现 23:48 / 23:50 及 23:47 的同一收尾倒计时。拼接稿将前者改成“二十二点前提交首轮日结数据”，却仍在后文使用 23:52 与同一紧迫流程，未解释跨越时段。原场景另有 48−12 应余 36、实际 34 与后场景应余 48、实际 46 的账目冲突，以及许砚代词变化；stitch 修正了账目与代词，但未消除上述时间问题。正文、原场景、引用 offset 与 hash 已在私有运行证据中保留。评审未拦住这个问题，本次样本不能报告为连续性质量全通过。

独立复核前四章另确认三项连续性问题：第 3 章把同五批记录创建时间锁定为二十日 03:17—03:29，第 4 章把其中 0719 写成 04:07，未说明更正或另一记录；第 2 章连续五夜、第 24—20 日的五航班，被第 3 章缩成首尾近两天；第 2 章一句台词把“应存 36”说成“出仓 36”。最后一项仅证明该句字段混淆，不能推断全部重量计算错误。完整 BODY hash 与逐字引用 offset 保存在 `reader-review-1-4-findings.json`。目前没有证据表明这是 Canon/hash 错配或 MCP 读错版本，也尚未定位单一确定性源码成因。

第 5 章又把第 4 章已撤回的统一箱重倒推法用于同批证据，并在相同 05:43 时刻把分处码头和医院的人物写成同处医院；另把停航前二十天的期限写成已积累二十天的记录。这些问题有最终 BODY/hash 和精确引用支撑，不能仅因评审未阻断而当作合理变化。

其中规则问题已经取得完整源码因果链：第 4 章的真实 `state_event_extraction` 读取完整最终 BODY，明确产出 `status="已撤回"`；隔离 MCP `world_model_get(as_of_chapter=4)` 也返回相同持久状态。`canon_quality_context.py` 原先只排除少数英文停用值，并把其它任意值重新标为 `active`，于是第 5 章成功 Writer/stitch 请求反而要求遵守已经撤回的方法。新源码以明确规则生命周期值解释生效/停用/unknown，显式空值或未知值不回退成 active；仅修读侧解释，保留原 Canon 状态与历史，不增加模型门禁。43 项新增回归及 99 项相关回归通过；独立审查在相同真实 BookState fixture 上加载 c62 原函数，六类撤回/空/null/未知/非字符串均复现旧误判且新函数消除，原状态 JSON 保持不变。独立 99 项相关回归通过，实际正文、抽取与持久状态互相核对；计数重叠，不当作真实新长跑通过。修复未写入本次冻结运行，新候选仍须完整验证。

地点问题的证据边界不同：第 3 章正确记录两人在医院，第 4 章提取虽读取最终 BODY，却遗漏两人的位置变化；第 5 章因此继续收到医院状态，而前章摘要未包含码头地点。当前没有证据表明保存器丢弃了正确位置 delta，不能把它也算作已修复的状态解析 bug。第 6 章已有事件只报告 `canon_continuity` / `payoff_timing` 和 repair 开始，尚不能断言由撤回规则触发。

最终 `terminal-cost.json` 是全项目已记录账本，包含 Genesis、planning、第 1—5 章及第 6 章原稿和 repair，覆盖 23 个 trace、531 个事件。累计 153 次尝试、67 次成功，差额 86 次未成功；账本另记 retries=86、fallbacks=57。retry/fallback 是尝试属性，不能再相加为额外调用，也不能把 retries 一概当作失败次数。已记录 token 共 1,527,457，其中 prompt 1,413,112、completion 114,345；66 次 Codex usage、1 次 provider usage、86 次缺 usage，估算次数为 0。缺 usage 不是实际用量为零。累计尝试耗时 3,995,748 ms 是调用耗时之和，不是任务墙钟时长；没有金额估算。

| 实际模型 | 尝试 | 成功 | 未成功（差额） | 已记录 token |
| --- | ---: | ---: | ---: | ---: |
| gpt-5.6-sol | 66 | 66 | 0 | 1,522,224 |
| MiniMax-M2.7 | 1 | 1 | 0 | 5,233 |
| kimi-k2.5 | 58 | 0 | 58 | 缺 usage |
| deepseek-chat | 28 | 0 | 28 | 缺 usage |

其中 `task_family=writer` 子集为 140 次尝试、56 次成功、84 次未成功、56 次回退及 1,225,979 个已记录 token。第 6 章两次 Writer 产物合计 40 次尝试、16 次成功、24 次未成功、16 次回退，360,154 token。首章的 3 场景、8 次逻辑调用、466,158 ms 仅保留为单章取证；此前 33 次尝试的费用快照不是全程成本。运行确有 kimi 429、deepseek-chat 402 后的既有路线回退，不能描述为单模型或无失败。上述数字只覆盖持久账本可见的调用，不为缺失 trace/usage 补造成本。

最终 `terminal-gates.json` 同样统计 531 个事件：hard_floor 5 次机会/5 次评估/2 次触发，canon_quality 5/5/1，future_plan_audit 11/11/5；这些 gate 的 blocks、pauses、approvals、overrides 均为 0。band_checkpoint、manual_checkpoint、generation_audit、delegation 均为 0 次机会/评估，checkpoint_count=0。第 6 章仍是 draft，不能把五次 Canon gate 评估写成六章都接纳。费用账本 `gate_costs=[]` 只表示没有可归属的费用条目，不证明关卡没有模型成本；manual_action_count=19，19 次操作时长均未知，不能把 duration_ms=0 写成人工耗时为零。

旧观察器的终态采集没有完整结束：`observation-0337.json` 已保存 paused 任务，随后保存项目、费用、gate、全部七章状态和第 1—6 章正文。它又对 planned 第 7 章调用 `chapter_get`，得到“第7章尚未生成”的 404，最终记录 `observer-error.json` 的 RuntimeError；`observer-terminal.json` 未生成。这是采集器未跳过无正文计划章的错误，不是生成任务失败，也不使此前已落盘的终态证据失效。最终事件窗口含 200 条、饱和标志为 true，其中 58 条为该次新见事件；采样流可能有遗漏，不能宣称事件全历史无缺口。全历史 cost/gate 端点的 531 事件口径与采样窗口分开保留，正文 hash 核对也不等于文学质量或结尾验收。

运行证据在本机忽略目录 `.superpowers/sdd/2026-09-09-forwin-three-stage/runs/forwin-stage1-smoke-c62f6d02329b-8bea72/`，其中 manifest、策略前后读回、MCP 输出、BODY/场景产物、真实模型输入和只读评估相互关联。第 4/5 章产物路径来自 canonical `writer_output_artifact_saved` 事件，状态读取经隔离 MCP；没有通过原始数据库或临时 HTTP 推断运行事实。凭据及原始 trace 不进入代码库。暂停后保留原镜像、策略和证据；后续修复或新镜像不得沿用此候选的通过身份。
