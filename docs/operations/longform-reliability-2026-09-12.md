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

首个可执行候选固定在 `64603f81b2a28bab2ce7e825a0f907ebe6ae27de`，与其后的 `413d638` 测试补强提交的生产代码相同。使用完整 Git archive 和 `uv.lock`，在独立 `colima-forwinqual20260912` 引擎构建两个原 Dockerfile target，未复制生产源码或应用文本补丁。

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

首个新 smoke 项目为 `5d59b0dce434406a8c6a163f501d7c81`（清河旧账），总目标100章，本次停在20章，每批2章以检验持久续跑。冻结策略版本2：standard、Writer `env-minimax/MiniMax-M2.7`、2500/2800/3200字、无人工检查点、Spark委托、原有限修复预算3；`soak_test/isolated=true`，没有发布角色或账号。六个 Genesis 阶段均经正式 MCP 生成及锁定，未手工修订；实际 Genesis 路由为 `gpt-5.3-codex-spark/codex_bridge`，不能根据 Writer 配置把它记作 MiniMax。

`64603f8` 首次 start-writing 返回409，尚未创建章节任务，MCP 确认仍为 `genesis_ready`、active_count=0。保存的最终 Genesis revision `6de822b3894f4906b9fd5770f29afbca` 经原函数重放，精确复现孤立 ID `loc_304fa7e15e39573d`，对应封存账房库室。它有一条 sealed、hidden、单向且带许可引用的明确通道；结构弱连通性帮助函数排除了 sealed 边，错误地把“不可通行”判成“没有连接”。修复进入计划 Task8Q1，保持实际封闭和人物可见性限制。原样本及失败身份保留；源码改变后必须重新冻结完整运行角色，不能把后续结果记成 `64603f8` 通过。

Task8Q1 修复提交为 `7f67eb6039aa1ae11b3f2d1737815161ac5fcf73`。现有遍历函数只在 authored 结构核验时包含阻断路线，普通可达性与可见性仍沿原逻辑。真实 PostgreSQL 导入和 standalone 校验的反例在修复前为9 failed、3个 hidden 对照通过，修复后12 passed；地图/Genesis 263项及移动相关21项均通过。独立规格及质量复核通过，0新增问题。完整 Ruff 比较另发现测试 import 排序1项，由测试格式提交 `e49b06e` 修正，65项覆盖测试及独立复核通过。最终全仓相同 Ruff 比较为1027→1012，新增0、移除15。

新源码完整 archive SHA-256 为 `37929ac02ceb5a7381a7ea5b7277f100ccff22c6f0086c332ebd2edf3bccb01b`，锁文件未变。完整应用镜像 `sha256:c7e8b4aeaf1ad5baf0a2d71e876f102ea203b45e092cbc7b340c49a1acc2af76` 和浏览器镜像 `sha256:9ecbaa58bd1d98e51a80ae9d8f3654668d045859e7fc7854f6519a3728ddcd58` 均构建成功，各849个源码文件和锁定解释器核验通过。

在 `7f67eb6` 重新运行同一完整 pytest 命令并保存 JUnit：**3710 passed、4 skipped、5 warnings、6 subtests，496.23秒，exit 0**，XML 无 failure/error。153个变更 Python 文件的 F/E9、编译、source guard、diff 和严格 legacy inventory 均通过。后续 `e49b06e` 仅展开同一个测试 import，不改变被复制运行文件或锁，不重记整套 QA 或重造同代码镜像。

MCP 确认没有活跃任务后，隔离四角色切换到 `7f67eb6` 完整应用镜像，模型 bridge 也切到同源 archive 和锁定环境；API/MCP/bridge 健康与源码身份核验通过。ready 脚本只因全局插件仍指向生产150而返回1；隔离 API/MCP 检查均成功，操作继续通过明确指向隔离端点的正式 MCP 客户端，全局插件及生产服务未改。

新项目 `7732e4a8a72f4f4f97aaa25f08751d35`（潮册疑踪）使用策略版本2，沿用标准策略及每批2章、到20章停止的设定，未复用旧项目或 Genesis。写前核对发现世界设定中“禁止终局句式”缺少时间范围，经一次正式定向修订限定为前20章，并明确允许百章收束；这是有记录的写前作者介入，不称为完全自主 Genesis。第一次使用了错误的 pack 前缀路径，被400拒绝；重新读取后使用阶段相对路径完成修订，没有改代码或手工写状态。新地图含11地点、15路线，纯导入预检通过。

随后 story_engine 的8次实际模型尝试全部失败：Spark 的5小时额度耗尽（bridge502，账户窗口100%，2026-09-13 16:20:13 America/Los_Angeles 恢复），Kimi 4次429明确为余额不足，DeepSeek 2次402余额不足。MiniMax 被原有 planning 路由排除，没有被调用，不能据此判断它是否可用。没有更改路由、充值或使用额度重置。

失败 trace `7100843f0182497790b789c7e4438dbe` 明确标为 backend=failed、fallback_used=true，但 workspace 把 deterministic_scaffold 保存成 generated，将既定主角陆砚替换为模板人物“抉择”。该阶段未锁定，MCP 确认没有活跃生成任务、没有章节；不能把这个样本计作 smoke20。Task8Q2 修复这个真实暴露的“调用失败却记录成功”问题。原失败样本、实际模型尝试及版本身份保留，正式新样本仍受模型可用性限制。

Task8Q2 源码修复为 `e8d8600` 和 `763341a`：六个正式阶段及生成、重跑、完整/定向修订在模型不可用、传输或解析失败后返回明确错误，保留原内容、版本、锁状态和 trace；应用入口将识别出的模型失败映射为503。实际接口 JSON/响应结构错误在原 adapter 边界保留原始原因和调用诊断，普通程序 RuntimeError 直接传播。正常响应归一化、初始模板、内部 launch_arc 物化、模型路由和原有重试策略保留。

独立审查发现初版错误分类过宽/过窄，修订后两项均关闭，复核无新增 Critical/Important/Minor。最终相关回归 **699 passed，51.27秒**；包含实际 adapter 经 application 的接口解析、程序错误、版本保持和 token/attempt 诊断对照。原 `e8d8600` 全量在审查指出问题后主动中断，保留 **1597 passed、1 skipped、1 warning、exit2** 的未完成记录；它不是整仓通过结果。最终 `763341a` 全量 **3990 passed、4 skipped、5 warnings、6 subtests passed，511.30秒，exit0**；JUnit 无 failure/error。4个跳过项为一个需显式启用的真实浏览器测试和三个要求 Linux Bash 的启动器测试；5个警告仍为 Authlib JOSE/Python SQLite datetime adapter 弃用。160个变更 Python 文件的 F/E9、compileall、source guard、diff、严格 legacy inventory 均通过；Ruff 0.16.7 同基线由1027条降至1011条，新增0、减少16。

最终候选使用完整 archive `4c9f2bbe9c1803222010bb0f5be9b2dbad1598b1ec2b3f41fdabe7db4d07d0cd` 和未变的依赖锁。应用镜像 `sha256:66245448888d2da8de75f3528540d577e6a473d407b24c65668260b4877d0b8c`、浏览器镜像 `sha256:9dec23f2a3c2e1e192f35088d2713ab30a969bb2858961177a6e3ce4dd9c1a7c` 各849个运行文件及依赖核验通过。MCP 确认无活跃生成后，隔离 API/MCP/generation/outbox 和 bridge 均切换到 `763341a1d99117f33aeb1d4865a6775ff47da2f8`，健康检查成功。没有运行发布角色。

2026-09-13 13:05（America/Los_Angeles）复查 Spark 5小时窗口仍为100%，重置时间仍为16:20:13，账户无重置额度。Kimi/DeepSeek 的余额失败按先前实际响应保留，未再次调用验证；MiniMax 的规划路由排除状态不等于模型不可用。原两个失败样本保持不变，没有将失败模板修成通过；正式新 smoke20、独立 L100 与结局审读继续阻塞，需模型资源恢复后使用这个冻结候选创建新样本。没有新建定时任务。

改动交付目标仍为 `origin/for-review`。不自动合并 master、不修旧生产作品、不公开发布。生产替换须满足原路线图的资格并走既定 150 同步路径；这份隔离镜像证据本身不意味着生产已更新。
