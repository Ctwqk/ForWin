# 三阶段路线图实施证据

当前路线图：[批准设计](../superpowers/specs/2026-09-09-forwin-three-stage-design.md)。本记录区分工程实现、隔离验证和实际生产，不宣称 Stage 1 已完成。

## 当前状态

- 开发分支：`codex/three-stage-improvements`；源码起点 `521228871a5752ebe8572c057caa9f4944bb0295`。
- 设计文档已更新，旧 L200/重复矩阵要求已被本轮 smoke + 离线 L100 替代。
- 已从 HEAD 退休三个过期路线图全文；固定 Git 历史入口在 `Design-docs/DESIGN_STATUS.md`。
- P1 版本/冻结、完整后缀修订、5% 容量及向前迁移、Stage 2 职责重构、Stage 3 合格反馈链路和可选 Markdown/manifest 导出均已提交并经独立审查。隔离实跑发现的规则生命周期读侧缺陷已在 `1f9a9ad` 修复；该冻结源码全量回归 2986 项通过、4 项跳过。真实长跑、最终浏览器镜像及生产切换尚未完成，不能用工程测试代替这些结果。
- 没有修改生产作品、发布新内容或把隔离测试回执写入生产。
- 外部 review 的正式历史读侧污染、世界编辑绕过后继核验及旧 Band 放行证据已补强，详见[review 跟进](../superpowers/reports/2026-09-09-review-followup.md)。冻结 `a2f780b` 完整回归 3063 项通过，运行与浏览器镜像均验证。此前 brief-only 的旧 L100 保留；新样本已在第 2 章后因正文与地图耗时矛盾安全暂停，不能算百章通过。
- 第一组 review 修复已提交为 `40bec1f`。后续 Band 旧放行证据、修订后继维护身份及短事务边界也已补强；根代理组合 117 项通过，独立入口/调用链 16 项通过。全量与完整镜像验证使用最终冻结候选另行记录，不把这些重叠局部计数当作真实百章结果。
- 2026-09-10 的地图修复保留明确路线、补齐 Writer/BODY reviewer 证据和时长解析，相关 78 项通过。实际原始 atlas 的前后纯函数回放证明 0.01 小时传送门已变为原设定的 10/15 分钟路径；自由文本复合位置仍未知。新修复的全量、镜像及新 smoke/L100 待单独验证，见[失败报告](../superpowers/reports/2026-09-10-stage1-map-failure.md)。

## 运行基线调查

只读容器源文件哈希比较：生产 `forwin/` 有575个 Python 文件，起点源码有585个，合并路径集合中289项不同。这是文件级差异数量，不是缺失修复数。

| 角色 | 调查时运行镜像 |
|---|---|
| API / generation / MCP | `forwin-forwin:compat-6809782` |
| publisher worker / outbox | `forwin-forwin:deploy-d4fceac68343` |
| publisher browser | `forwin-publisher-browser:deploy-d4fceac68343` |

compat 镜像 digest 为 `sha256:4b35847d8c3596761aa7e1db7c4cf30765971e385caaa9863eda4af4a9f4dbac`，其标签仍记录基础 `57241ff0e4e2` 与 hotfix `68097821d24f408c5132c2acd82d5d3c174d013b`。常规发布不能把这些补丁标签当作完整主线源码身份。

11个历史热修复目录均可关联源码提交（`00f6071`、`0b1f446`、`24a477b`、`3738779`、`456d209`、`4924bf5`、`49b008f8f210`、`4c1d32d`、`6809782`、`6c54ba4`、`af2cd7e`），主题为历史修订锚点/排序/重审证据/Writer位置元数据。已逐项确认这些提交均为基线祖先；在完整候选镜像及历史事务回归通过后，从当前树退休87个旧文件，保留Git历史和运行回滚镜像。

## P1-0 构建验证

Stage 1 候选 `c62f6d02329b5d766569f37159fe57f16907c7ad` 从冻结源码归档完整构建，未使用历史热修复装配；它不包含后续 Stage 2/3 代码：

| 产物 | immutable image ID |
|---|---|
| `forwin-runtime` | `sha256:f8a1875c9617391f2984bf1c66f90267a697a16a0c72c58f8bc01888221f7e20` |
| `publisher-browser-runtime` | `sha256:5a0aaeafcc4838a296dd1713c17b3b32e5f6666f461a85b48464596f3432f05e` |

两镜像 OCI revision 与运行环境源码 SHA 均一致，`uv.lock` SHA256 均为 `6b5abad0b424e86e64f2d5384de2df1f7c273906fc8e25e24e94f77f632f225c`。无网络、只读容器中的实际依赖/角色导入与 CLI 帮助通过；浏览器镜像的系统 Chromium、Playwright 真正启动并读取测试页，以及登录 shell 经绝对 Python 路径导入 Playwright 均通过。首次角色导入探针误写了不存在的模块名，保留失败记录后按 CLI 的实际 `forwin.runtime.workers` 入口重验通过。

以下为先前 P1-0 构建记录，不替代最新候选：

源码 `6d37047873bec116fa0f0b32319a5025f71693eb` 从 `git archive` 的干净内容构建，无历史补丁参与；这是本工作包的构建检查，尚未包含后续未提交的版本/容量改动。

| 产物 | digest |
|---|---|
| `forwin-runtime` | `sha256:aecf6dbf8b5b52732df8f413885ba9e2429674e582263d599e60c440d4ae1f5f` |
| `publisher-browser-runtime` | `sha256:332befda62dd892876d69a22f5076690b2d553f7e500988c22ed4be83948a67d` |

两者 OCI revision 标签均等于上述完整源码 SHA。Dockerfile 从同一 `uv.lock` 使用 frozen install；Python、Node和uv镜像输入固定到构建实际解析的digest；Python打包后端版本固定。apt仓库内容仍受发行版仓库更新影响，因此这些证据不承诺未来逐字节相同镜像digest。

CLI `forwin --help`、生成/Pubisher服务导入和浏览器 `chromium --version` 均成功。第一次构建的 CLI 缺失由显式 build-system 修复，失败没有被当作通过。

新增 `scripts/check_runtime_source.py` 已接入既有 pre-PR 检查：拒绝新生产源码副本、修改保留热修复目录、以compat为普通镜像基底，以及运行时源码字符串替换；允许普通源码改动和经核验后的旧材料删除。此检查是发布卫生约束，不代替运行时事务边界。

## 备份和迁移调查

- 已创建生产 PostgreSQL 一致性备份并核对manifest/checksum及archive目录；本地副本仅保存在被Git忽略且去掉组/其他读取权限的工作证据目录。
- 原库PostgreSQL16.13，旧镜像pg_dump17.10生成archive1.16。隔离PG16恢复时，pg_restore17输出的`SET transaction_timeout = 0`不被PG16支持；仅在恢复流中删除这一条会话设置后成功恢复，原备份未改动。
- 隔离恢复得到101张表；schema revision=`0001_v5_baseline`；负章节Canon记录数=0；publisher job表尚无`canon_commit_id`等新身份列。
- 最初直接运行主迁移链明确失败：无法定位`0001_v5_baseline`。现已增加独立的、事务内执行的旧基线向前桥接；两个原 baseline 均未改写，不重建数据库或盲目stamp。
- [迁移入口和核验依据](legacy-forward-migration.md)：`python -m forwin.migrations` 只在读取到准确的旧版本和 schema 指纹后选择旧桥接，再接主迁移链；任何身份歧义或后续失败都回滚整个事务。
- Stage 1 时生产备份的全新隔离副本成功升级至 `0004_revision_validation`，Stage 3 又用全新副本验证至 `0007_feedback_actions`：23条旧上传保留为 `uncertain`，23条独立 `reserved` 保护，15处原上传标题与候选标题的差异保留；18条 binding 仅通过原结果保存的直接 ID 恢复章号。没有生成发布回执或已公开保护。
- 已逐表核对原始行数及列值摘要；除授权的旧 job 状态和 binding 章号变化外无差异。额外历史表保留。迁移另复现并修复了 Alembic 关闭应用日志的问题。
- 恢复用临时测试数据库已由测试harness回收。备份与源码哈希证据保留；MinIO/Qdrant未做全量备份，此次未改变它们。
- **生产切换前仍需**：最终候选的统一回归、完整角色镜像和源码身份验证，并安装经审查的部署接入。当前150活动脚本尚未改变；源码已提供构建身份、停妥、备份和显式迁移的接入及最小补丁，不能把“代码已提交”当作“线上已切换”。继续保留回滚材料。

## 测试证据

| 范围 | 命令 | 结果 |
|---|---|---|
| 修改前基线 | `.venv/bin/python -m pytest -q tests/test_v5_live_migration.py tests/test_production_planner.py` | 8 passed |
| P1-0检查/诊断 | `.venv/bin/python -m pytest -q tests/test_v5_recovery_schema.py tests/test_runtime_source_policy.py` | 14 passed |
| 运行入口 | `.venv/bin/python scripts/check_codex_operator_ready.py` | API/MCP健康、插件配置、Swarm角色、Python环境通过 |
| 活跃生成任务 | `forwin.task_active_generation_check` | 0；仅说明调查时状态 |

`c62f6d02329b5d766569f37159fe57f16907c7ad` 的独立源码归档（tree `5da6f38651decfef0cadb4b13b9e4346fc566ccb`）完成全量 pytest：**2658 passed、4 skipped、6 subtests passed**，用时287.98秒。归档导入路径已经核对，未从正在进行后续开发的 checkout 导入。compileall、F/E9 Ruff 和源码守卫通过；默认完整 Ruff 为1179项，同规则原始基线1219项，新引入诊断已修正，不能称为全库零告警。[20章隔离 smoke](../superpowers/reports/2026-09-09-stage1-smoke.md)在接纳五章后发现规则生命周期读侧缺陷及时间/位置连续性问题，已通过正式 MCP 安全暂停；20章及结尾未完成。旧运行没有热修复或冒充通过，修复后的全新离线 L100 单独记录身份和结果。

整合导出和规则修复后的 `1f9a9ad197a88ff7907c63b9d3692496a1c53072`（tree `fac2a1ca6161dfc000ba37746e7c262ca0bb7a62`）在冻结归档运行完整 pytest：**2986 passed、4 skipped、5 warnings、6 subtests passed**，用时325.15秒。导入路径及642个生产 Python 文件 hash 已核对。compileall、F/E9 和源码守卫通过；默认 Ruff 1035项，相对原始1219项基线没有新增路径/代码/消息诊断，行号漂移不计作新问题。此前 `bb1d2b4` 全量的五项失败来自新独立导出事件和旧路径清单的测试接线；`dbed88e` 保留原三个 Canon 恢复事件的完整断言、另验第四个导出事件并修正精确路径清单，原五项复验通过后才进行本次全量运行。

同一 `1f9a9ad` 完整运行镜像为 `sha256:0088fcf995f845f1a1e3d6d86b21f16556da8009a0026259d5646248f96a00e2`，实际只读、无网络容器验证源码文件、锁文件、完整 revision、arm64架构、各角色导入和 CLI 通过，迁移链 head 为 `0007_feedback_actions`。这不代表浏览器角色也已通过：浏览器构建触发低磁盘保护，之后的验证也因空间不足停止；未验证产物及两个经核实的独占构建缓存已精确清理，生产/回滚镜像、数据卷与旧 smoke 镜像保留。最终浏览器镜像与真实运行验收仍待完成。

Stage 3 的向前迁移另在原生产备份的全新隔离 PostgreSQL 副本验证至 `0007_feedback_actions`：101 张原表及全部原字段值保留（仅旧发布任务状态与已核实的 binding 章号按已审契约转换），额外 `npc_intent_snapshots` 原样保留；23 个不确定任务对应 23 个 reserved 保护、15 处标题差异保留、18 个已核实章绑定、0 个伪造公开保护或回执。分析器/聚合/行动新增字段没有把旧证据猜成合格。私有报告保存完整迁移源码 hash，本次未修改生产数据库；最终部署仍须停妥后重新备份。

## 版本、发布和容量基础

当前接纳通过稳定 ChapterPlan 身份与 active commit 指针选择；历史接纳、增量和快照保留。修订请求保留原接纳状态，完整后缀在私有候选投影重验，通过唯一 Canon 接纳入口原子切换；未知或冲突拒绝候选且保留原主线。

P1-2 的范围、真实调用路径、正负回归和旧测试替代关系见[修订核验证据](../superpowers/reports/2026-09-09-p1-2-revision-evidence.md)。完整正文覆盖没有普通 Writer 的窗口截断，最终事务再次核对身份、版本、策略和发布保护。独立语义样本覆盖钥匙、秘密、生死、时间、地点与叙事义务，但冻结模型响应不代表真实模型质量评估。

已知支持边界：旧数据缺少可追溯的 CQ/实体来源或义务历史时返回 unknown；修改已产生叙事义务的原章正文而缺少新稿保留/删除该义务的可靠判定时，也拒绝自动接纳。没有为通过措辞修订而复制旧义务或默默丢弃承诺。

发布在外部动作开始前持久保存保护；未知结果、已删除队列记录、多平台状态和旧接纳身份不能绕过。旧排队载荷在动作前取消或进入只读对账，重复身份冲突持久暂停；前序公开回执未齐时释放客户端占用并延后重领，允许前章推进。

串行容量使用显式主平台的连续确认前缀，任务 lease/epoch 约束预留，Canon 事务再次核对。等待不是生成失败；恢复不会错误采用前一任务的已接纳章，离线模式的 outbox 不进入真实发布。审查发现的上述恢复和队列阻塞问题均已添加回归。

根代理发布/迁移/容量组合验证131项通过；另有容量/API/MCP组合133项通过，覆盖稳定修订、主/旧迁移、发布 fence/重复保护/lease、容量及 worker 恢复；P1-1 独立暂存树65项通过；发布评审组127项通过，旧 fixture 适配74项通过。这些数字存在重叠，均不是全量候选验收。首轮全量诊断2335项通过，但仍有旧历史修订测试依赖 P1-2 接入；不会用局部通过代替该工作。

随后在干净的 `beeb32b` 源码快照上运行除历史 Canon 事务文件外的大范围回归：2386 passed、2 failed、1 skipped。两项失败都是旧上下文 fixture 缺少真实 Canon/active 指针；提交 `3700074` 补齐身份且保留原断言，22项相关回归通过。P1-2 的历史事务正负路径仍需单独接入后进入最终全量测试。

该快照 compileall 通过。默认 Ruff 扫描报告1221项；同规则下原始 `5212288` 基线已有1219项。新增诊断已在后续提交修正，F/E9关键检查通过；默认宽规则扫描不能报告为全库零告警。最终候选应同时记录完整诊断与相对基线变化，不以风格告警数代替行为验证。

## 部署接入与运行策略

提交 `4dd0c9e` 提供[源码部署入口](../../deploy/swarm/README.md)及受原脚本哈希约束的150补丁。它在停机前核对镜像和真实依赖；停妥后执行备份校验、统一迁移和六角色验证。迁移后失败保持停止，状态写失败也不能跳过停止操作；历史 ServiceSpec 证据受限保留。尚未修改远程部署脚本，也未执行生产迁移。

独立审查与根代理各自完成42项部署、备份、源码守卫和 profile 回归；审查新发现的“磁盘满导致记录错误失败，从而跳过停机”已先复现，再通过6项故障路径回归。两组计数重叠，不是48项独立测试。

浏览器启动脚本不再因 profile 失效或心跳失败清空整个目录。Linux隔离容器实际执行3项行为测试，确认未回传发布 journal 的字节保留，旧自动reset设置也不能删除它；新profile仍可初始化。容器以旧镜像作Linux工具宿主、只读挂载新脚本测试，不属于候选镜像验收。

提交 `26e1166` 增加完整运行策略的MCP读取/更新接口，复用既有API、策略字段、版本冲突与原因审计。根代理51项MCP/策略回归及6个子测试通过；没有改动生产项目策略。隔离长跑将通过这些支持的字段配置自动化，同时保留标准质量与修复限制。

位置元数据回退、快照顺序及历史锚点的55项相关回归通过；旧重审入口由完整后缀验证取代，不能恢复仅凭旧增量重放的接纳。上述 `c62f6d0` 全量回归覆盖历史事务正负路径，完整候选镜像不依赖旧目录。退休后的历史入口见[设计状态](../../Design-docs/DESIGN_STATUS.md)。

## 评论隔离及合格输入恢复

Stage 1 曾暂停未经修正的反馈输入，保留采集、展示和历史，不增加长期开关。Stage 3 已恢复独立事务的完整评论分析、合格 canonical Writer 提示和有版本 CAS 的未来计划应用；旧世界模拟不重复执行分析，旧全局校准、世界规则自动改写和 review 反馈阻断继续不参与生产。

原隔离工作包的真实 PostgreSQL 59 项相关回归覆盖无评论、旧强信号和原有阻断；恢复后的资格、裁剪、版本、并发和实际正文证据见下方 Stage 3 报告。两个时期的通过记录不能互相替代。

## 构建期间磁盘故障与恢复

本轮重复角色构建触发宿主磁盘耗尽，随后本地 Colima 虚拟机报告 I/O 错误，测试 PostgreSQL/Qdrant 和 MCP 一度不健康。释放未使用磁盘块后，正常停止并启动 swarmbridged 虚拟机，服务恢复；没有删除数据卷、重建业务库或改写任务结果。恢复后 operator readiness 通过，MCP确认 active generation task=0；数据库回归重新运行。

只清理本轮创建的两张检查镜像和按ID核对的本轮构建缓存，保留生产镜像、回滚材料、备份和用户文件。镜像digest证据仍保留。

独立评审还发现浏览器登录shell会丢失虚拟环境PATH。提交 `66e7218` 固定现有 `FORWIN_EXTENSION_PYTHON=/app/.venv/bin/python` 并取消Docker默认登录shell；该修复已在上述 `c62f6d0` 完整浏览器镜像中通过实际复验。

最新两角色构建前回收虚拟机已删除文件的空闲块，并按记录的本轮旧检查 cache ID 及其后继逐项清理。构建设置低磁盘停止保护，未触发；没有清理生产镜像、数据卷、原始备份或用户文件。

浏览器镜像验证完成后，回收本轮暂不运行的浏览器检查镜像及其两个独占构建层，保留上述验证记录；隔离smoke继续使用冻结的完整运行镜像。生产部署需从最终源码构建其完整角色镜像，不能把已回收检查镜像当作现存部署产物。

后续 `1f9a9ad` 浏览器构建再次触发空间保护后，用户授权清理宿主。已清理可重建包缓存、未使用的旧程序版本及构建产物，并回收虚拟机已删除文件的空闲块。最后一次宿主实测可用 14.14 GiB；该次 trim 前后净增 1.84 GiB，未把 guest 报告数当成宿主实际收益。源码、运行数据、凭据、冻结证据及生产回滚镜像保留，未因清理修改生成状态。

## Stage 2 / Stage 3 已提交工作

`de3d854` 完成 Writer owner，`59b2bbb` 完成 Review/Repair 与 Canon 准备职责拆分：真实调用者迁移，旧 Stage 删除，保留有限输入、原异常/取消、预算和同事务 trace。Writer 相关344项、独立110项、根代理73项；后续 Review/Repair/Canon 相关387项、根代理153项通过，计数重叠。详见[A0 报告](../superpowers/reports/2026-09-09-stage2-owner-refactor.md)。

`45faa90` 记录同九个固定响应键的消融：A1 保留，A2/A3 证据不足保留，A4 尚无同输入预算的真实模型对照，不保留第二套 Writer。不能把输入字符节省写成已验证 token/质量收益。详见[消融报告](../superpowers/reports/2026-09-09-stage2-ablation.md)。

Stage 3 的 `99dcd6b`、`83ab5cb`、`34d1ea0` 分别实现评论来源/版本化完成、独立提交消费和合格反馈链路。聚合按全部评论计分母、按平台区别作者；冲突和证据不足只观察。选用、实际输入、计划应用、正文观察和关联变化分别留证；正文默认 unknown，关联不声称因果。真实 owner 的有限样本包含五次唯一 Canon 接纳及四类合理拒绝输入，模型/质量/发布响应为显式冻结 fixture；见[有限样本](../superpowers/reports/2026-09-09-stage3-feedback-finite-loop.md)。独立整合审查72项及根代理最终相关91项通过，计数重叠；[整合报告](../superpowers/reports/2026-09-09-stage3-feedback-integration.md)记录 stale ORM、malformed 历史、快照时间顺序修复。已包含在上述 `1f9a9ad` 同源码全量回归中。

`bb1d2b4` 完成可选的[Markdown 与 manifest 导出](../superpowers/reports/2026-09-09-novel-export.md)：唯一 Canon 事务内只追加独立 outbox 请求，处理者先冻结保留的书本版本，再做受文件描述符保护的原子 IO。重试、乱序、原根路径被替换及保留的正文 hash 冲突均有回归；没有创建 Git 远端、小说仓库或绕过 Canon 的导入入口。
