# Stage 3 首包：评论来源与分析完成记录

本记录仅覆盖批准路线图 P2-1 的摄入来源、分析完成及重试语义。聚合分母、读者身份置信度、行动映射、计划应用和效果观察属于后续包；本包不代表 Stage 3 已完成，不恢复现有反馈隔离，也不新增自动分析入口。

后续事务集成已补齐：既有 post-Canon `feedback` 步骤先在独立 session 中消费最多八条待分析评论，提交完成记录、正常失败次数和该次模型用量 trace，然后继续原聚合事务。聚合失败或外层回滚不会清除已提交分析；三个相同错误调用各自以 analysis identity/attempt count 保留 trace。三个真实 PostgreSQL 故障测试先失败后通过，相关回归 69 项及独立 3 项通过。无评论不调用模型、不新增 trace；Writer/规划自动反馈资格仍由后续包完成。本段更新了下文首包的调用方事务限制；独立事务提交前的进程崩溃仍可能重复模型调用，不承诺 exactly-once。

## 真实 owner 与调用边界

- `publisher_runtime/comment_sync.py` 仍拥有评论摄入；`comment_source.py` 只解析明确远端身份和来源证据。
- `simulation/world.py::CommentAnalyzer` 仍是唯一评论分析器；`audience/comment_analysis.py` 只持久化版本、完成与有限失败状态，不再次分析正文或作行动决策。
- 既有 `build_reader_feedback_snapshot(..., analyze_missing=True)` 在分页之前选择未完成的输入，完成状态出现在其 `analysis_status` 读视图中。未显式请求分析时仅查询状态。
- `audience/feedback.py` 的变化仅为来源范围与当前分析版本筛选。等级、行动、冷却期等算法没有在本包重写。
- HTTP 既有 `PublisherRawCommentInput` 透传 `project_id/account_id/observed_at`，避免传输层丢失摄入需要的身份。

## 来源与时间契约

远端评论身份使用平台、账号或明确作品绑定范围、远端作品 ID、远端评论 ID。账号明确时采用账号范围；账号不可得但作品绑定明确时采用绑定范围。账号/绑定或作品身份缺失时，每次观察分别保留，标为 unknown，不能用共同空字符串合并无关来源；因此不能承诺对不提供可靠身份的平台实现可靠去重。

项目只来自有效显式 project ID、明确同步任务或唯一的远端作品绑定，不用作品同名推项目。章节只来自该作品的精确远端章绑定。绑定只证明本地稳定章节时保存 `chapter_known`，Canon ID 留空；唯一、对应的持久已发布保护事实才提供旧发布的 Canon identity。当前 active commit 不用于猜测读者看到的版本。远端发布事实与当前章绑定矛盾时保存 unknown。仅有可靠作品绑定、尚无远端章节的评论可以独立补齐项目/作品来源，进入该项目队列，章节状态继续保持 unknown。

原 `remote_created_at` 保留评论发生时间原文；`observed_at` 保留本次观察时间，无法解析的平台发生时间不伪造；`ingested_at` 保留首次入库时间，`synced_at` 继续表示最近摄入。有明确观察时间时，较旧观察不覆盖较新编辑正文。分析记录分别保存 attempted/analyzed 时间和分析时的生成章号。信号的 chapter number 来自可靠来源，unknown 为 0，不使用生成进度。

## 分析版本与当前读视图

`CommentAnalysisRecord` 唯一键包含 comment ID、完整正文 SHA-256、来源 SHA-256、分析器版本。来源指纹绑定平台、账号/绑定、作品/远端章/评论身份、本地稳定章、发布 Canon、来源状态与作者 ID；观察时间变化本身不制造新的来源版本。

每条记录保留输入正文和来源快照。正文编辑、显式分析器升级或 unknown 来源后来可靠补全产生新记录，不覆盖旧分析证据。零信号也是 completed。raw comment 的 active analysis 指针只在成功完成时切换；摄入修改正文或来源指纹会清除过期指针。若后续观察恢复为已有的精确正文/来源版本，该记录重新进入 pending；消费只重新激活原 completed analysis，不重跑模型、不改变原次数和 analyzed 时间。未激活的完成证据不会把这项工作从 pending 状态中隐藏。

当前信号读视图要求完整匹配的 completed analysis、active 指针、正文/来源指纹、可靠来源章。未版本化旧 signal 不能因为 raw 来源后来变成 known 而重新混入；旧 signal、旧 analysis 和原始内容仍留在数据库供审计。

## 失败、并发与事务边界

配置了 LLM 时，调用失败、无效 JSON/envelope/index/type 或明确截断不再被当作零信号成功，也不静默使用关键词结果伪装该 LLM 版本完成。无 LLM 的既有关键词分析是独立版本，允许正常完成。

逻辑分析最多重试 3 次，默认失败间隔 60 秒，达到上限为 exhausted。错误、次数、下次重试时间可从 snapshot 的 `analysis_status` 获取。模型调用继续使用现有有限 timeout 和 `retry_on_timeout=False`；分析 attempt 指逻辑分析调用，不等同于底层路由的网络尝试数。

摄入以数据库唯一约束和 PostgreSQL conflict handling 实现并发幂等。分析选择使用 `FOR UPDATE SKIP LOCKED`，两个消费者不会同时处理同一被锁输入。

**事务保证的准确边界：**分析状态及 signals 使用调用方事务。正常模型失败返回后，调用方提交会持久化失败次数，真实 snapshot consumer 的测试证明 3 次提交后 exhausted。外层 rollback 或进程在 commit 前崩溃不计入持久次数，模型调用也不能宣称 exactly-once。恢复自动反馈消费前，后续包必须明确选定合法的独立提交边界；本包没有另建分析任务平台或擅自恢复自动调用。

## 数据迁移

新增 `0005_comment_analysis`，前驱是 `0004_revision_validation`。迁移不重写 baseline、不删除 raw comments/旧 signals，也不从旧书名、章标题或 signal 的旧章号回填来源。旧行保留全部原字段，内容 hash 由原正文精确计算，来源范围标为 `legacy:<row_id>`，来源章/Canon/观察时间保持 unknown；不伪造旧分析完成记录。首次已知入库时间沿用保存的 synced 时间，不宣称它是平台观察时间。

有评论数据时拒绝向下丢弃这些证据；空库升级、降级、再升级保留原迁移测试契约。

## 证据覆盖

| 场景 | 常规测试位置与断言 |
| --- | --- |
| 100 条、每批 8 条 | `test_comment_consumption_contract.py`：真实 pending 查询先排除完成再 limit；15 次消费恰好处理全部 100 个正文，每个一次 |
| 正文恢复 | `test_comment_source_identity.py`：A→B→A 重选并重新激活同一原完成分析，保留 signal ID、次数与 analyzed 时间 |
| 作品来源补齐 | 先入库无项目书评，唯一作品绑定建立后重复摄入保留 raw ID、补齐项目，章节继续 unknown |
| 零信号 | 相同输入再次调用不重复模型；completed 及时间持久化 |
| 编辑与分析器升级 | 同 comment 保留三个版本正文/分析记录，旧结果退出当前读视图 |
| 来源补全 | 同正文 unknown→可靠远端绑定保留 raw ID，source hash 改变，pending 重选，新旧来源快照同时存在 |
| 错误与截断 | 无效 index/type、模型失败、合法零结果但截断均不计成功；真实 consumer 提交三次失败后 exhausted |
| 来源章与生成进度 | 来源 unknown 不填当前第 90 章；实际远端第 2 章迟到至第 99 章仍归第 2 章 |
| 发布版本 | `test_comment_source_identity.py`：旧发布 Canon 与当前 active 不同，评论保留旧发布 ID；无发布证据不猜当前版本 |
| 来源矛盾 | 远端发布章与本地绑定矛盾时 unknown；缺远端章不能用同标题猜章 |
| 去重范围 | 同 remote ID 的不同账号/作品分别保存；未知范围不错误合并；两个客户端同时摄入只产生一行 |
| 并发消费 | 两个真实 PostgreSQL session 对 16 条各取 8 条，无重叠，全部完成 |
| 旧证据 | 未版本化旧 signals 永不进入当前读；原旧行仍可读取 |
| 迁移 | `test_comment_analysis_migration.py`：逐项比较原 raw 字段、unknown 来源、不伪造完成记录、带数据拒绝丢弃 |
| 保持隔离 | `test_feedback_quarantine.py`：既有自动反馈隔离行为不变 |

测试中的模型是冻结响应/故障 fixture，验证的是摄入、完成、版本及消费契约，不声称已经证明真实模型的读者意见理解或反馈效果。

## 本候选验证结果

- `.venv/bin/python -m pytest -q tests/test_comment*.py tests/test_audience_feedback_alignment.py tests/test_publisher_runtime_comment_sync.py tests/test_feedback_quarantine.py tests/test_v5_live_migration.py tests/test_large_module_boundaries.py tests/test_legacy_inventory.py .superpowers/sdd/2026-09-09-forwin-three-stage/review/comment-review/test_comment_review.py --tb=short`：**75 passed / 13.24s**，日志 `/tmp/comment-final-regression.log`。其中 73 项来自常规 CI，2 项为独立审查原始复现；两项复现均已另纳入常规 CI。
- `tests/test_architecture_boundaries.py -k legacy`：**6 passed, 33 deselected / 1.38s**。
- 新增 source helper、analysis store、0005 和三个新测试文件 Ruff 全通过；原有改动文件相对 HEAD 的 Ruff 新增项为 **0**（`/tmp/comment-ruff-added.json`）。编译与 `git diff --check` 通过。
- 独立代码审查发现的正文恢复与作品来源补齐两项均先在常规 CI 复现 RED，再修至 GREEN；本记录不提前写成 Stage 3 验收通过。
