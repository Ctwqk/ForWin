# ForWin 性能热点分析报告

## 摘要

- 分析时间：2026-05-11 20:42-20:45 PDT
- 分析对象：当前本机 Docker Compose 运行中的 ForWin 栈，以及 60 章真实生成项目 `bbe070bc8eda49c9a551c3ce1c755391`
- 结论：
  - 当前空闲/复核后的 ForWin API 本身不是主机 CPU 热点，`forwin` 容器 5 次采样为 0.13%-3.46% CPU，常驻内存约 240.7 MiB。
  - ForWin 栈内当前最大常驻内存热点是 `forwin-qdrant`、`forwin-publisher-browser`、`forwin` API、`forwin-postgres-test`。
  - 当前主机 CPU 最大热点不是 ForWin，而是其他容器/进程：`arb-resolver-1` 约 94.53% CPU，宿主机 `python main.py` 约 96.4% CPU。
  - 生成流程 wall-clock 热点集中在 LLM 请求、章节写作、continuity review、webnovel experience review 和修复重写；这些主要是等待模型和串行编排时间，不等价于本机 CPU 使用率。
  - 前端热点是任务详情抽屉：每次打开/刷新会加载项目详情、章节列表、治理卡、因果回放、洞察、章节流水线和项目章节列表；章节数增长到几百章后会变成明显 DOM/渲染热点。
  - 2026-05-11 22:38 PDT 已完成短期修复：慢 span 查询改为数据库按 duration 排序后 limit；章节列表最新 rewrite attempt 改为窗口函数取每章最新；任务抽屉章节流水线和项目章节列表改为每批 60 章渲染；`publisher-browser` 改为 `publisher` profile 按需启动。
  - 后续修复补齐：DB query metrics 默认启用；新增章节分页接口并让任务抽屉按页请求；项目列表只返回最近 3 章 preview，项目详情只返回首屏 60 章 preview；新增启动时可配置 retention cleanup。

## 采样方法

执行命令：

```bash
docker stats --no-stream
ps -eo pid,ppid,comm,%cpu,%mem,rss,vsz,args --sort=-%cpu | head -n 30
free -h
docker compose ps
docker exec forwin ps -eo pid,ppid,comm,%cpu,%mem,rss,vsz,args --sort=-%cpu
docker exec forwin-mcp ps -eo pid,ppid,comm,%cpu,%mem,rss,vsz,args --sort=-%cpu
docker exec forwin-publisher-browser ps -eo pid,ppid,comm,%cpu,%mem,rss,vsz,args --sort=-%cpu
docker stats --no-stream forwin forwin-mcp forwin-postgres forwin-postgres-test forwin-qdrant forwin-publisher-browser
GET /api/observability/performance/projects/bbe070bc8eda49c9a551c3ce1c755391?limit=10000
GET /api/observability/performance/llm?project_id=bbe070bc8eda49c9a551c3ce1c755391&days=7
GET /api/observability/performance/db?project_id=bbe070bc8eda49c9a551c3ce1c755391&days=7
```

限制：

- 当前采样不是在 60 章生成的高峰瞬间抓取 CPU flamegraph；系统里没有可用的 `py-spy`/`perf`。
- observability span 是 wall-clock duration，不是 CPU time。
- DB 表大小通过只读 `psql` 查询获取，用于定位存储/序列化压力，不作为项目/章节真相来源。

## 当前 CPU 热点

### ForWin 栈

5 次 Docker 采样：

| 容器 | CPU 范围 | 内存范围 | 说明 |
| --- | ---: | ---: | --- |
| `forwin` | 0.13%-3.46% | 240.7 MiB | API/首页/项目详情请求，当前不是 CPU 热点 |
| `forwin-mcp` | 0.63%-2.14% | 102.7 MiB | MCP HTTP server，低负载 |
| `forwin-postgres` | 0.01%-6.33% | 105.0-105.1 MiB | 主库，偶发 checkpoint/WAL/background writer |
| `forwin-postgres-test` | 1.70%-7.79% | 217.1-217.3 MiB | 测试库，刚跑完 pytest 后仍有后台活动 |
| `forwin-qdrant` | 0.33%-2.78% | 477.6 MiB | 常驻内存较高，CPU 低 |
| `forwin-publisher-browser` | 0.10%-113.54% | 298.4-349.8 MiB | Chromium 发布浏览器，出现一次单核以上尖峰 |

容器内进程：

- `forwin` 主进程：`uvicorn forwin.api:app`，RSS 246220 KiB，HWM 257568 KiB，27 threads。
- `forwin-mcp` 主进程：`uvicorn forwin.mcp.http:app`，RSS 118456 KiB，HWM 121444 KiB，27 threads。
- `forwin-publisher-browser`：由 Chromium 多进程构成，GPU/renderer/extension/network utility 进程合计形成 CPU 尖峰和 300 MiB 级常驻内存。
- `forwin-postgres`：`walwriter`、`checkpointer`、`autovacuum launcher` 是后台 CPU 来源，当前连接多为 idle。

### 主机级非 ForWin 热点

宿主机 `ps` 快照显示：

- `python main.py`：约 96.4% CPU，RSS 533 MiB。不是 ForWin 容器进程。
- `arb-resolver-1`：Docker stats 约 94.53% CPU，RSS 515 MiB。不是 ForWin 栈。
- Chromium renderer：有一个宿主机 Playwright/Chrome renderer RSS 约 1.65 GiB，CPU 约 0.5%；另有 publisher browser renderer 约 23.8% CPU 的瞬时采样。

这意味着当前机器整体卡顿或高 CPU，优先看 `arb-resolver-1` 和外部 `python main.py`，不是 ForWin API。

## 生成流程 Wall-Clock 热点

60 章项目 observability 报告：

| 组件/阶段 | count | total duration | p50 | p95 | max | 解释 |
| --- | ---: | ---: | ---: | ---: | ---: | --- |
| `llm.request` | 766 | 8,931,552 ms | 11,400 ms | 23,170 ms | 33,545 ms | 最大 wall-clock 来源，主要是模型网络/推理等待 |
| `writer.write_chapter` | 74 | 7,992,251 ms | 107,327 ms | 135,781 ms | 163,041 ms | 单章写作串行路径；包含多次 LLM、解析、校验 |
| `stage.writing_chapter` | 62 | 6,717,344 ms | 108,544 ms | 135,805 ms | 163,079 ms | 章节写作阶段 |
| `review.webnovel_experience` | 81 | 2,244,357 ms | 25,471 ms | 55,080 ms | 79,686 ms | 体验审查，调用模型/规则 |
| `stage.continuity_review` | 61 | 1,804,908 ms | 21,693 ms | 71,702 ms | 105,389 ms | 连续性审查，包含实体/状态校验 |
| `stage.repairing_chapter` | 12 | 1,277,165 ms | 104,724 ms | 126,661 ms | 147,089 ms | 修复重写，成本接近一次完整写章 |
| `stage.running_post_acceptance` | 60 | 728,562 ms | 8,481 ms | 30,431 ms | 45,905 ms | 接受后 world/canon/索引后处理 |
| `stage.repair_review` | 12 | 531,036 ms | 39,203 ms | 73,860 ms | 75,101 ms | 修复后复审 |

最慢单项：

- task wrapper：`task.operation` 最长 2,592,318 ms（第 49-60 章任务）。
- 单章写作：第 54 章 `writer.write_chapter` 163,041 ms；第 58 章 151,718 ms。
- LLM 单请求：第 58 章 33,545 ms；第 60 章 32,982 ms；第 54 章 32,753 ms。

判断：

- 端到端耗时主要受模型请求次数和串行章节 pipeline 控制。
- 本机 CPU 在生成过程中不是主要瓶颈；真正需要优化的是 LLM 调用数量、重试/修复次数、review 串行度和每章上下文大小。

## 内存热点

### 主机内存状态

`free -h` 快照：

```text
Mem: 31Gi total, 25Gi used, 1.0Gi free, 13Gi buff/cache, 5.7Gi available
Swap: 976Mi total, 976Mi used
```

风险：

- Swap 已满，说明机器近期有过真实内存压力。
- 当前 ForWin 栈约 1.4-1.6 GiB 常驻，不是 31 GiB 主机的主要内存消费者。
- 但 publisher browser、Qdrant、测试库和外部 Chrome/其他服务叠加，会让系统可用内存低于 6 GiB。

### ForWin 栈常驻内存

| 热点 | 当前量级 | 说明 |
| --- | ---: | --- |
| `forwin-qdrant` | 477.6 MiB | 栈内最大常驻服务；即使当前 CPU 低，也会占用近 0.5 GiB |
| `forwin-publisher-browser` | 298-350 MiB | Chromium 多进程；发布功能不用时仍常驻 |
| `forwin` API | 240.7 MiB | Python/FastAPI/SQLAlchemy/应用代码常驻 |
| `forwin-postgres-test` | 217 MiB | 测试库；跑完测试后仍常驻 |
| `forwin-postgres` | 105 MiB | 主库正常 |
| `forwin-mcp` | 103 MiB | MCP server 正常 |

### 本地数据目录

`/app/data` 总量 774 MiB：

| 路径 | 大小 | 说明 |
| --- | ---: | --- |
| `/app/data/chrome_profiles` | 413 MiB | Chromium profile，publisher browser 相关 |
| `/app/data/chrome_profiles/forwin-extension` | 290 MiB | 当前发布浏览器 profile 主体 |
| `/app/data/publisher_profiles` | 226 MiB | 平台发布 profile |
| `/app/data/artifacts` | 131 MiB | 项目/审计 artifact |
| `/app/data/world_vaults` | 1.3 MiB | WorldModel/Obsidian 投影，小 |
| `/app/data/llm_kb` | 1.1 MiB | LLM KB，小 |

### DB 表大小

主库最大表：

| 表 | 大小 | 备注 |
| --- | ---: | --- |
| `candidate_draft_records` | 26 MiB | 最大表，候选草稿/修复尝试相关 |
| `decision_events` | 9064 KiB | 决策时间线，60 章项目贡献 3571 条 |
| `prompt_traces` | 5696 KiB | prompt trace，60 章项目 273 条 |
| `world_model_snapshots` | 4792 KiB | world state 快照 |
| `world_compile_runs_v4` | 3472 KiB | world compile 记录 |
| `performance_spans` | 2152 KiB | 60 章项目 1707 条 |
| `chapter_drafts` | 1576 KiB | 60 章项目 73 条 draft |

60 章项目行数：

- `candidate_draft_records`: 73
- `chapter_drafts`: 73
- `chapter_reviews`: 73
- `prompt_traces`: 273
- `performance_spans`: 1707
- `decision_events`: 3571

判断：

- 单个 60 章项目的正文存储不大；真正增长快的是决策事件、prompt trace、performance span、candidate draft。
- 对几百章连载，任务详情页如果每次加载并渲染决策链、章节列表、治理洞察，前端内存和后端序列化会随章节数明显上升。

## 代码级热点

### 前端任务抽屉

文件：[app_task_governance.js](/home/taiwei/ForWin/forwin/ui_assets/home/app_task_governance.js:652)

热点：

- `renderGenerationDrawer` 每次打开抽屉会加载项目详情和章节列表，并渲染 governance、causal replay、insights、macro progress、chapter timeline、automation form、项目章节列表。
- 项目章节列表在 [app_task_governance.js](/home/taiwei/ForWin/forwin/ui_assets/home/app_task_governance.js:955) 直接对 `visibleChapters.forEach` 全量创建 DOM。
- 每行还包含 summary、按钮和空的 `.chapter-body` 容器；几百章时 DOM 节点数量会线性增长。

风险：

- 当前 60 章还能接受。
- 300-500 章时，抽屉打开、刷新轮询、DOM diff、滚动和按钮事件数量会成为前端 CPU/内存热点。

修复状态：

- 已在 [app_task_progress.js](/home/taiwei/ForWin/forwin/ui_assets/home/app_task_progress.js:1) 给章节流水线增加 60 章分批渲染和“加载更多章节”。
- 已在 [app_task_governance.js](/home/taiwei/ForWin/forwin/ui_assets/home/app_task_governance.js:1) 给项目章节列表增加 60 章分批渲染和“加载更多章节”。
- 已新增章节分页请求：任务抽屉不再依赖项目详情里的全量 `chapters`，首屏请求 `/api/projects/{project_id}/chapters/page?offset=0&limit=60`，点击“加载更多章节”继续请求下一页。
- 后端项目详情只携带首屏 60 章 preview，项目列表只携带最近 3 章 preview，避免列表/详情 payload 随章节数线性膨胀。
- 仍未做真正虚拟滚动；当前用分页和首屏裁剪解决主要 DOM 与网络 payload 热点。

### 章节列表 API

文件：[api_project_ops.py](/home/taiwei/ForWin/forwin/api_project_ops.py:1150)

热点：

- `list_chapters` 一次性加载项目全部 `ChapterPlan`。
- 再批量加载 latest draft、review id。
- 然后加载项目所有 `ChapterRewriteAttempt` 并在 Python 侧 `setdefault` 取每章最新。

风险：

- 60 章问题不大。
- 几百章、多轮 repair 后，`ChapterRewriteAttempt` 全量排序和 Python 侧去重会增加 DB/CPU/内存成本。

修复状态：

- 已新增 [api_project_ops.py](/home/taiwei/ForWin/forwin/api_project_ops.py:122) `latest_rewrite_attempts_by_chapter`，使用 `row_number() over(partition by chapter_number order by attempt_no desc, created_at desc, id desc)` 在数据库侧取每章最新 attempt。
- `list_chapters` 不再加载项目所有 rewrite attempt 后在 Python 侧去重。
- 已新增 `/api/projects/{project_id}/chapters/page?offset=&limit=`，默认 60，最大 200，返回 `total/offset/limit/has_more/chapters`。
- 旧 `/api/projects/{project_id}/chapters` 保持兼容，仍用于 MCP/老调用方需要完整列表的场景。

### 章节正文 API

文件：[api_project_ops.py](/home/taiwei/ForWin/forwin/api_project_ops.py:1202)

热点：

- `get_chapter` 每次只取一章 draft/review。
- 前端逐章打开正文会产生 N 次请求；本次验证中打开 60 章就是 60 次章节详情请求。

风险：

- 用户阅读单章时合理。
- 批量导出、批量检查或自动化逐章展开时，需要批量正文接口或导出专用路径。

### Continuity/Subworld 检查

文件：[checker/rules.py](/home/taiwei/ForWin/forwin/checker/rules.py:422)、[orchestrator/loop.py](/home/taiwei/ForWin/forwin/orchestrator/loop.py:5332)、[state/repo.py](/home/taiwei/ForWin/forwin/state/repo.py:715)

热点：

- continuity check 会扫描 `entity_mentions`、`state_changes`、`new_events`、`scene_outputs`，并查 allowed names。
- `_world_pressure_character_names` 会遍历 active entities 并在 pressure text 中匹配名称/alias。
- `get_allowed_entity_snapshots` 会遍历 active entities 过滤角色。

风险：

- 当前项目实体数量不大。
- 长篇后 active entity 和 alias 增多时，这里会变成每章 review/repair 的 CPU 热点。

### Observability 查询

文件：[observability/query_service.py](/home/taiwei/ForWin/forwin/observability/query_service.py:48)

热点/盲点：

- `slow_spans` 先按 `created_at` limit，再在 Python 里按 duration 排序；当 span 超过 limit 时，可能漏掉真实最慢 span。
- `db_performance_report` 当前返回空，说明 DB span instrumentation 不足；无法从内置报告直接看 SQL 慢点。

修复状态：

- 已修正 [query_service.py](/home/taiwei/ForWin/forwin/observability/query_service.py:48) `slow_spans`：现在直接在数据库层 `order_by(duration_ms desc, created_at asc, id asc).limit(...)`。
- SQLAlchemy query probe 已默认启用：`FORWIN_OBSERVABILITY_RECORD_DB_SPANS` 默认值改为 `true`。运行时会把 DB query count、duration、slowest query hash/preview 写入 active span metrics，使 `/api/observability/performance/db` 有数据来源。仍可通过环境变量显式关闭。

## 优先优化建议

1. 前端任务抽屉分页/虚拟化
   - 章节列表和章节流水线已先做 60 章分批渲染，章节列表已改为按页请求。
   - 后续仍建议对决策时间线做分页或虚拟滚动。
   - 轮询刷新时保留 DOM，只更新变化字段，不整抽屉重绘。
   - 对几百章项目，这是最直接的前端 CPU/内存收益。

2. 发布浏览器按需启动
   - 已将 `publisher-browser` 放入 Docker Compose `publisher` profile。
   - 默认 `docker compose up` 不再启动发布浏览器；需要发布时使用 `docker compose --profile publisher up publisher-browser`。

3. 章节列表 API 优化
   - `ChapterRewriteAttempt` 已使用窗口函数只取每章最新 attempt。
   - 已提供章节分页接口，项目列表/详情 payload 已裁剪。
   - 检查/补充索引：`chapter_plans(project_id, chapter_number)`, `chapter_drafts(chapter_plan_id, version desc)`, `chapter_reviews(draft_id)`, `chapter_rewrite_attempts(project_id, chapter_number, attempt_no desc, created_at desc)`。

4. 降低生成 wall-clock
   - 优先减少每章 LLM 调用次数，而不是先优化 Python CPU。
   - 对 `review.webnovel_experience`、`continuity_review`、`repair_review` 做条件触发或并行化评估。
   - 对 repair 前置规则做更强约束，减少完整重写次数；本次 60 章有 12 次 `stage.repairing_chapter`，每次成本接近写一章。

5. 加强 CPU/DB observability
   - 在下一次长篇生成时加入 `py-spy` 或类似采样 profiler，采集真实 CPU stack。
   - SQLAlchemy query probe 已默认启用，DB 性能报告不再默认为空。
   - `slow_spans` 查询已修正为数据库 `order_by(duration_ms desc)` 后 limit。

6. 数据保留策略
   - `candidate_draft_records`、`decision_events`、`prompt_traces`、`performance_spans` 会随长篇规模线性增长。
   - 已新增 retention cleanup：默认启动时执行，`performance_spans` 和 `prompt_traces` 默认保留 30 天，`candidate_draft_records` 默认每项目每章保留最近 5 条。
   - 可通过 `FORWIN_RETENTION_CLEANUP_ON_STARTUP`、`FORWIN_PERFORMANCE_SPAN_RETENTION_DAYS`、`FORWIN_PROMPT_TRACE_RETENTION_DAYS`、`FORWIN_CANDIDATE_DRAFT_KEEP_PER_CHAPTER` 调整。

7. 主机资源隔离
   - 当前主机 swap 已满，且其他项目占用明显。
   - 对非 ForWin 高 CPU 容器设置 CPU limit，或把 ForWin 写作环境与交易/浏览器工作负载隔离。
   - 长篇生成时建议保留至少 6-8 GiB available memory，避免 Chrome/Qdrant/Postgres 叠加导致 swap 抖动。

## 本次修复验证

执行命令：

```bash
.venv/bin/python -m pytest tests/test_observability_phase_f_performance_api.py::test_slow_spans_orders_by_duration_before_limit tests/test_phase05_regressions.py::Phase05RegressionTests::test_latest_rewrite_attempts_by_chapter_returns_one_latest_attempt_per_chapter tests/test_docker_compose_profiles.py::test_publisher_browser_is_profile_gated tests/browser/test_mock_book_creation_generation_regression.py::test_large_project_chapter_list_renders_in_batches -q
.venv/bin/python -m pytest tests/test_observability_phase_f_performance_api.py tests/test_phase05_regressions.py::Phase05RegressionTests::test_api_list_chapters_uses_latest_draft_values tests/test_phase05_regressions.py::Phase05RegressionTests::test_latest_rewrite_attempts_by_chapter_returns_one_latest_attempt_per_chapter tests/test_docker_compose_profiles.py tests/browser/test_mock_book_creation_generation_regression.py -q
.venv/bin/python -m pytest tests/test_observability_phase_f_spans.py tests/test_observability_phase_f_performance_api.py tests/test_phase05_regressions.py::Phase05RegressionTests::test_api_projects_include_chapter_summaries tests/test_phase05_regressions.py::Phase05RegressionTests::test_api_projects_only_include_recent_chapter_preview tests/test_phase05_regressions.py::Phase05RegressionTests::test_api_list_chapter_page_returns_page_metadata tests/test_phase05_regressions.py::Phase05RegressionTests::test_api_list_chapters_uses_latest_draft_values tests/test_phase05_regressions.py::Phase05RegressionTests::test_latest_rewrite_attempts_by_chapter_returns_one_latest_attempt_per_chapter tests/test_docker_compose_profiles.py tests/test_retention_cleanup.py tests/browser/test_mock_book_creation_generation_regression.py -q
node --check forwin/ui_assets/home/app_task_governance.js
node --check forwin/ui_assets/home/app_task_progress.js
node --check forwin/ui_assets/home/app_task_drawer.js
.venv/bin/python -m py_compile forwin/api_project_ops.py forwin/api_project_payloads.py forwin/api_project_routes.py forwin/api_route_registry.py forwin/config.py forwin/runtime/container.py forwin/state/query_helpers.py forwin/maintenance/retention.py tests/test_retention_cleanup.py
docker compose config
```

结果：

- 针对性红灯用例：4 passed。
- 相关回归：7 passed。
- 第二轮相关回归：18 passed。
- 三个前端 JS 文件语法检查通过。
- Python 编译检查通过。
- Docker Compose 配置解析通过。

## 结论

当前 ForWin API 在完成 60 章后处于低 CPU、低内存状态。本次已修掉能在当前代码层安全落地的 CPU/内存热点：任务抽屉首屏 DOM 爆量、任务抽屉章节全量 payload、publisher browser 默认常驻、slow span 查询失真、章节列表 rewrite attempt 全量去重、DB 性能报告默认无数据、observability/candidate draft 缺少保留策略。剩余主要热点是：

1. 生成流程的 LLM 调用次数和串行 review/repair pipeline。
2. Qdrant 的 0.5 GiB 常驻内存。
3. 决策时间线、governance insights 等非章节区块后续仍可继续分页/虚拟滚动。
4. `decision_events` 尚未加入自动 retention，原因是它们是治理审计链的一部分，直接默认删除会影响可追溯性，应另做归档策略。

下一步最值得做：对 writer/reviewer 的 LLM 调用数量和 repair 触发率做策略级优化，并为 `decision_events` 设计归档而不是直接删除。
