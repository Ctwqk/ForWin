# Stage 3 第二包：同源聚合、方向与来源范围

本包落实已批准 P2-2 的评论方向、窗口聚合及唯一读视图。行动映射、Writer 输入、未来计划应用和效果比较由各自 owner 接线；本记录不宣称 Stage 3 完成，也不恢复已隔离的自动创作反馈入口。

## Owner 与字段

- `simulation/world.py::CommentAnalyzer` 仍是唯一分析器。`audience/directions.py` 只给它有限的类型/方向词汇和原文引用检查，不增加模型或第二个分析决策入口。默认版本升级为 `comment-v3-direction`（保留 `:llm` / `:keyword` 区别），触发首包已有的版本化消费。
- `audience/aggregation.py::aggregate_window` 是唯一窗口计算 owner；`SignalAggregator.aggregate` 只适配 short/medium/long 窗，`world.aggregate_and_level_signals` 只适配同一 short 窗，均消费 `aggregate_view`。world 展示 limit 与信号分页不再截断聚合输入。
- `CommentSignalCandidate.direction` 保存分析器产出的方向。既有 signals 不回填方向、不覆盖历史等级。`SignalWindowAggregate` 新增方向、聚合版本、证据 hash/JSON、全窗完成数、已知作者数、未知作者命中数、来源资格。
- `0006_feedback_aggregation` 前驱为 `0005_comment_analysis`。旧 signals/aggregates 原字段逐项保持，旧方向 unknown、来源资格 false。非空证据 hash 的部分唯一索引约束新快照；旧重复快照也保留。

## 有限方向与证据

| 类型 | 支持方向 |
| --- | --- |
| confusion | unclear / clear / unknown |
| pacing | too_slow / too_fast / balanced / unknown |
| character_heat | positive / negative / unknown |
| risk | concern / unknown |
| relationship_interest | want_more / want_less / unknown |
| prediction | predicts / unknown |

相反方向分别计数，key 由类型、目标类型、目标名称、方向共同组成，不会因含分隔符的名称发生键碰撞。模型方向必须附有完整评论内实际存在的非空原文摘录；缺失/不匹配引用只能 unknown，非法方向类别使该次分析进入首包有限失败路径。预测只保存读者预测及其引用，不等同于要求剧情按该预测发生。关键词模式置信度仍为 0.4，因此不能凭关键词自动形成合格共识。

## 分母、作者与来源资格

分母是窗口内所有明确来源章的 raw comments，包含零信号及尚未完成分析的评论。缺少来源章的评论不能猜进任一源章窗口，其 ID 单独记录为 unscoped，并明确阻止自动资格。pending、失败或版本/输入不一致的分析不伪装完成。全窗混用分析器版本时留下 mixed_analyzer_versions 原因。

一个 comment 对同一类型/目标/方向只有一个 hit。重复输出保留逐条引用，但不增加用户或 hit，置信度取这些重复断言的最低值。作者身份使用平台与明确 author_id；显示名称和 comment ID 不补作用户。已知平台账号数单独展示，共识用户数取单个平台内可证明的数量，跨平台账号不相加以假定不同真人；级别也只取单个平台内部支持的最高级别，严重度同样在各平台内计算，不能把 B 平台的严重风险借给 A 平台的作者数量。

自动资格要求源评论绑定的持久已发布事实与项目、稳定章、章序、Canon、平台及远端书章精确一致，并核对 Canon 对应候选/正文的稳定章归属，要求发布 content SHA-256、候选 BODY hash、保存正文的实际 SHA-256 三者相等。缺少旧 hash 证据不推测补齐，正文身份矛盾明确不合格。`chapter_known` 仍可观察，不能猜当前 active Canon 为读者所见版本。manifest 按稳定章保存各自的 Canon / publication IDs，跨章不同 Canon 完全合法；同一稳定章混入不同发布版本时不提升共识。迟到评论仍属于原来源章，不因第 99 章才收到而进入第 99 章窗口。

资格原因包括 unscoped_comments、incomplete_analysis、mixed_analyzer_versions、unproven_publication、mixed_publication_versions、unknown_direction、unknown_authors、low_confidence、invalid_confidence、ungrounded_evidence。不合格快照保留观察，级别为 noise。来源资格不是单独的自动许可：下游还必须消费既定 level 和对应方向，不再重算用户数或等级。

## 快照与效果读取契约

快照 hash 绑定计算版本、窗口、所有分母评论的正文/来源 hash 与 analysis identity、逐条信号证据、发布事实、来源/时间 manifest、读者规模估计与置信阈值。重复计算返回相同已存快照；编辑正文、补齐来源或其他实际输入变化生成新快照，绝不先删除旧快照。

`aggregate_view(row)` 提供 aggregate ID/version/hash、类型/方向/目标、已算 level、全评论/完成/hit/作者计数、资格与原因、源章 manifest、comment/analysis IDs、完整 input_comments、publication_evidence 与 accepted_body_evidence。`source_scope.timing` 分别报告 received_at、observed_at、remote_created_at 的 start/end/unknown_count；每个 input comment 还保存原平台时间、最新观察、首次入库、分析完成时间及原分析来源快照。时间缺失不伪造发生时刻，下游效果 owner 可据此拒绝不可靠、重叠或迟到的前后比较；该快照本身不推断留存或因果效果。

有限聚合读对原始评论取得共享行锁并刷新其 Session 缓存，随后读取的分析、信号与发布/Canon 正文来源也强制刷新；原始评论共享锁与首包摄入/分析的原始评论写锁序列化，不在锁内调用模型。新评论在捕获后到达属于后续快照。结果沿用调用方事务；持久化模型消费已由 root 的独立事务 owner 先提交，聚合后续失败不会倒扣已完成或失败次数。

## 实际测试与范围

`tests/test_comment_aggregation_contract.py` 首轮六项 RED 分别复现：分母 1 而应 3、相反方向合为一桶、无名评论变成 3 用户、同输入重算换 ID、world 没有同源快照 ID、未完成覆盖仍无来源资格限制。修复后加入以下真实 PostgreSQL 集成边界：

- 零信号分母、重复 signal 的单 comment hit、编辑后的旧证据保留；两消费者并发得到相同三窗快照。
- 同平台跨两个实际稳定章的不同 Canon IDs 可形成 confirmed；同章不同发布版本、不同平台账号相加、未知作者、弱置信度、无原文引用均不提升共识。
- 六种类型的十个方向用冻结模型响应与独立正文对应检查；无引用的方向降 unknown；模型 Infinity 不可被钳制成 1.0，持久 NaN 不可绕过资格或产生 NaN JSON。
- 205 条信号加 5 条零信号，world 展示 limit=3 仍读取 hit=205 / denominator=210。
- 来源时间证据、仅部分评论升级分析器的显式未知资格、迟到原章评论不进入后续当前窗。
- `tests/test_comment_aggregation_migration.py` 对旧列逐项比较，unknown 不猜方向，保留重复旧快照，新增证据存在时拒绝降级丢弃。
- 原候选等级回写测试迁移为“旧候选证据不变，目标类型仍精确分桶，新快照保留相应 hit 数”；不恢复以新窗等级覆盖原分析的旧路径。

上述模型均为固定响应或错误 fixture，证明传输、方向证据、来源与聚合契约，不能据此声称已验证真实模型的观点理解能力或反馈效果。

冻结前实测：

- `.venv/bin/python -m pytest -q tests/test_comment*.py tests/test_feedback_consumer_transaction.py tests/test_audience_feedback_alignment.py tests/test_feedback_quarantine.py tests/test_publisher_runtime_comment_sync.py tests/test_v5_live_migration.py tests/test_large_module_boundaries.py tests/test_legacy_inventory.py .superpowers/sdd/2026-09-09-forwin-three-stage/review/aggregation-review/test_aggregation_review.py --tb=short`：**116 passed / 23.51s**，日志 `/tmp/aggregation-reviewed-final.log`。包含 root 独立消费事务、行动 owner 已更新的资格/隔离回归与独立审查原始 3 probes；三项边界也全部纳入常规 CI。
- 新 owner、方向 helper、0006、两个新增测试文件及复用来源 fixture Ruff 通过；四个既有修改文件相对 HEAD 新增 Ruff 项为 **0**（`/tmp/aggregation-ruff-added.json`）。编译与 `git diff --check` 通过。
- 发布 hash / 候选 hash / 正文三项独立篡改测试 **3 RED → GREEN**；包含合法真实跨章来源及保留旧发布版本的来源/聚合集共 **39 passed / 8.66s**。
- 独立审查实证的“旧 Session 混新 signal/旧 analysis”和“跨平台借用严重度”均 **2 RED → GREEN**；模型 confidence=8/-1 不再钳制成合法置信，额外 **2 RED → GREEN**。新聚合集加原始 3 probes 为 **39 passed / 8.57s**。
- 效果 owner 的后续集成仍由 root 独立处理；未把尚未验收的 Stage 3 整体写成完成。


独立审查最终复核：原始三个真实 PostgreSQL probes **3 passed / 2.43s**，确认缓存刷新、分平台严重度及置信范围修复，无剩余聚合阻断。
