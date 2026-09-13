# 长篇可靠性实施与验证记录

更新：2026-09-13。实施基线：`for-review@f5dec620647c9a1690b1caaa9d99c0b38e6c695c`。依据：[补充设计](../superpowers/specs/2026-09-12-longform-reliability-design.md)和[执行计划](../superpowers/plans/2026-09-12-longform-reliability.md)。

四个工作包已实现，七项实施任务分别完成独立审查。整分支终审又复现了“旧评审在新会话中被当前 Canon 版本重新背书”的 P1，最终修复已通过真实 PostgreSQL 入口回归及全量测试；2026-09-13 独立复核确认原 P1 和整体验证阻塞已关闭，新增 Critical/Important/Minor 均为0。真实 smoke20、全新离线 L100 和结局审读尚未通过；工程测试不能代替这些资格。

## 实现范围

| 工作包 | 当前行为 | 保留边界 |
|---|---|---|
| A：义务证据与有限修复 | 正文、义务合同、候选与审阅绑定；兑现和 Canon 同事务；结构化阻塞进入现有修复路由 | unknown 不清账；同一章修复次数不因阶段、候选或 retry 重置；旧 resolved 不自动修改 |
| B：持久续跑 | 完成结果和 continuation outbox 同事务；唯一父任务及持久决定防止重复消费重复建任务 | 策略变化停止旧意图；停止请求与消费串行竞争；不补发旧 completed 任务 |
| C：版本检索与增量投影 | 派生来源回查 active Canon；只读变化正文；embedding 缓存先于索引写入持久化；模型空间隔离 | 未知来源不冒充有效；保留旧空间；模型变化后显式重建历史；无法识别未声明的权重变化 |
| D：有效上下文 | 已接纳认知与未来计划分开；必需对象、状态和关系贯穿实际提示及修复 | 歧义不猜；序列化不授予读取权限；硬输入错误不能经 retry/preview 绕过 |

原始评审的 Canon 基线保存到现有 `ChapterReview.review_meta_json`，经确切 candidate/review 关联进入不可变提交计划。评审开始前固定，结束后、保存时、新会话审批、准备及锁内接纳均复核原证明，不重新捕获当前版本。缺少证明的旧待审记录需重新评审；已经接纳的幂等读取和历史修订的完整后缀证据保留各自入口。

新增 schema 使用 `0008_generation_continuation`、`0009_knowledge_dependencies`、`0010_incremental_embeddings` 向前迁移。评审基线沿用 JSON 字段，无额外表。永久发布冻结、未知外部效果保护、完整后缀核验、5% 存稿和 offline 隔离仍由既有入口负责。

## 工程证据

- 整分支审查范围：`f5dec620..07524a8`；最终修复范围：`07524a8..413d63853834d39ea5c8c499fcd75da17dadeb23`。首次终审发现的 P1 使用真实 CandidateReviewService、独立会话世界编辑及 fresh `accept_review` 复现，修复前 2 个反例失败，修复后通过；另保留当前基线成功、证明损坏/替换、准备后变更及冷启动导入对照。
- 完整测试固定在 `64603f81b2a28bab2ce7e825a0f907ebe6ae27de`：`FORWIN_TEST_DATABASE_URL=postgresql+psycopg://forwin:forwin@127.0.0.1:55432/forwin_test .venv/bin/pytest -q`，**3693 passed、4 skipped、5 warnings、6 subtests passed，482.53 秒，exit 0**。警告来自 Authlib JOSE 和 Python SQLite datetime adapter 弃用。
- 随后的 `413d638` 只增强历史输入测试，明确区分缺失来源与未知依赖证明；相关 5 文件 **162 passed**，提交后两个明确对照 **2 passed**。该提交仅改一份测试文件，没有生产源码或锁文件差异；不把前一次完整测试记成这个提交重新跑过的结果。
- 最终修改的 150 个 Python 文件 F/E9、compileall、runtime source guard、diff 检查通过。严格 legacy inventory：30 条记录、81 处匹配、0 问题、0 警告。
- 相同 Ruff 0.16.7、相同 `--no-respect-gitignore` 配置，以相对路径、代码及消息的多重集比较：基线 1027 条，最终 1012 条，**新增 0、移除 15**。这不表示旧仓库全部 lint 清零。

失败记录保留：首次整体验证 `07524a8` 为 26 failed；首次最终修复全量 `97450a7` 为 5 failed。逐项修正保持进度/终态归属、并发调用者事务、冻结历史、修复预算、schema 和来源验证原目的。其中一次静态清理误用了 SQLAlchemy ColumnCollection 的迭代语义，已纠正；不能把它归为旧夹具自然变化。

## 镜像、运行角色与迁移

可执行候选固定在 `64603f81b2a28bab2ce7e825a0f907ebe6ae27de`，与最终测试补强提交的生产代码相同。使用完整 Git archive 和 `uv.lock`，在独立 `colima-forwinqual20260912` 引擎构建两个原 Dockerfile target，未复制生产源码或应用文本补丁。

| 产物 | 身份 |
|---|---|
| 完整源码 archive SHA-256 | `f64a3368a674b82776358928b032f247f2151a58ac9618d47e1daae8514dc528` |
| uv.lock SHA-256 | `6b5abad0b424e86e64f2d5384de2df1f7c273906fc8e25e24e94f77f632f225c` |
| 应用镜像 ID | `sha256:2581d71b946c411ec175069c7cab95e839ae1936318f28e02d5e6e5ddc2a3757` |
| 浏览器镜像 ID | `sha256:480b35ae45c4dc43c3ff7e2414385b847dfacc94cd310e1710af24a2907c382d` |

每个镜像核对 revision label、环境变量、锁定解释器及 849 个实际复制文件的哈希；无网络、只读容器导入 SQLAlchemy、psycopg、uvicorn、FastMCP、迁移模块通过，浏览器镜像另检查 Playwright 和 Chromium。构建警告指出 Dockerfile 中空的 `MINIMAX_API_KEY` ENV；真实凭据没有放入构建输入。

隔离 API、MCP、generation、outbox 使用上述同一应用镜像。模型 bridge 使用同一完整 archive 和 `uv sync --frozen --no-dev --extra runtime --no-editable`，仅监听带新凭据的本机回环端口。原生产服务和原模型 bridge 保持运行。部署健康与源码身份核对通过；尚无项目或生成任务时的空闲检查不能证明长篇运行成功。

2026-09-12T22:44:39Z 从生产 PostgreSQL 取得一致性只读数据库备份，16,849,294 字节，SHA-256 `8f6b4bf3c8bec3420d48919dc6c3230a0b016a036b5b674e72c33a30b701fdd4`。在全新卷恢复为隔离 `forwin_legacy_snapshot`，用候选完整镜像的正式迁移入口推进到 `0010_incremental_embeddings`。

迁移前后核对 101 张原表的行数及原字段哈希，**0 保留问题**。原字段变化只在版本表和事先明确允许的发布 job 状态、binding 章节号中出现；正文、旧摘要、旧 review、回执及其余历史字段保持。新增 23 条 reserved 发布保护均有 Canon 引用，无孤立引用。最终候选再次执行迁移退出 0。这个数据库从未接入应用；生成样本使用另一个全新数据库。该备份不包含 MinIO/Qdrant，也不代表备份时点之后的生产变化已经验证。

## 真实生成资格与交付

当前尚待新 smoke20，再通过独立全新离线 L100 和结局审读。使用固定标准策略、独立存储和同源码运行角色；记录实际模型、完整策略、项目/任务身份、停止原因及正文审读。旧失败书、程序替身返回和 accepted 行数不能替代新样本结果。

改动交付目标仍为 `origin/for-review`。不自动合并 master、不修旧生产作品、不公开发布。生产替换须满足原路线图的资格并走既定 150 同步路径；这份隔离镜像证据本身不意味着生产已更新。
