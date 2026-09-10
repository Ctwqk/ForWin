# Stage 1 隔离 smoke：进行中及已知缺陷

本报告不是通过结论。截至 2026-09-10 03:37 UTC，20 章隔离样本已接纳第 1—3 章，正在写第 4 章；完整结尾尚未取得。全新 L100 未启动。

| 身份 | 固定值 |
| --- | --- |
| 源码 | `c62f6d02329b5d766569f37159fe57f16907c7ad` |
| 源码树 | `5da6f38651decfef0cadb4b13b9e4346fc566ccb` |
| 运行镜像 | `sha256:f8a1875c9617391f2984bf1c66f90267a697a16a0c72c58f8bc01888221f7e20` |
| 项目 / 任务 | `693a1a7df3124c1088f2201e9581f023` / `08104286cb5b` |
| 范围 | 20 章现实悬疑《末班渡轮的交接单·隔离排障样本》；独立 PostgreSQL、Qdrant、数据卷及网络 |
| 模式 | 现有 `soak_test`，`isolated=true`，`run_until_chapter=20` |

Genesis 六阶段经标准 MCP generate/refine/lock 完成后，核对无活跃任务，再调用唯一 `project_start_writing` 入口。两次写作前 refine 分别澄清旧案人物责任与涨水地点顺序；未在写作中修改设定。完整有效策略逐字段回读，只修改现有 `manual_checkpoints=false`、`band_checkpoint_action=continue`、`gate_delegate=spark`，版本 1→2；字数、有限 repair、质量和 Canon 门保持 standard 原值。gate delegate 是否实际触发须看最终事件，不能凭配置认定已经执行。

样本没有 publisher/browser/automation 角色及生产业务卷，不产生真实平台发布。任务/章节/费用来自隔离运行内部的正式 ForWin MCP。只读观察器保留 15 秒采样、事件身份及窗口饱和标志；没有从生产 SQLite 或临时 HTTP 请求推断业务状态。

第 1 章实际接纳 3030 字，Canon `6f50dbaa9aa1bcf1a2cabe92d9999250c1527a3983691966d87aa1eb53e5cf40`，BODY SHA256 `082ca7cca49979b82b7475561ce83cf39da4d0b15d4ebee0dfa99c310edffe87`。它经过一次接纳，未走 repair，residual 为空；这些状态不等同于没有文学或连续性错误。

人工核对发现明确时间矛盾。第一场景要求“二十二点前提交日结数据”，尚余二十七分钟；后面场景出现 23:48 / 23:50 及 23:47 的同一收尾倒计时。拼接稿将前者改成“二十二点前提交首轮日结数据”，却仍在后文使用 23:52 与同一紧迫流程，未解释跨越时段。原场景另有 48−12 应余 36、实际 34 与后场景应余 48、实际 46 的账目冲突，以及许砚代词变化；stitch 修正了账目与代词，但未消除上述时间问题。正文、原场景、引用 offset 与 hash 已在私有运行证据中保留。评审未拦住这个问题，本次样本不能报告为连续性质量全通过。

首章 Writer 实际 3 个场景、8 次逻辑调用，记录用时 466,158 ms。首章接纳时全项目费用账本（包含 Genesis / planning 等，不能当作 Writer 单项费用）为 33 次尝试、19 次成功、14 次失败重试、9 次回退，共 464,766 个实际记录 token；436,594 prompt、28,172 completion。18 次 Codex usage、1 次 provider usage、14 次失败缺 usage，没有估算补零。kimi 的 429 和 deepseek-chat 的 402 由既有路线回退；不能写成全程单模型或无失败。没有报告金额、真实留存收益或模型普遍质量。

运行证据在本机忽略目录 `.superpowers/sdd/2026-09-09-forwin-three-stage/runs/forwin-stage1-smoke-c62f6d02329b-8bea72/`，其中 manifest、策略前后读回、MCP 输出、首章 BODY/场景产物和只读评估相互关联。凭据及原始 trace 不进入代码库。本次运行保持原镜像与策略继续采样；后续修复或新镜像不得沿用此候选的通过身份。
