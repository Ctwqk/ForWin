# 60 章真实长篇小说端到端实测报告

## 结论摘要

- 测试时间：2026-05-11 16:30:00-20:38:43 PDT
- 当前分支：`master`
- 当前 commit：`e8e30ea40c50a77153958e7aa7ad130c4a826cb9`
- 工作区状态：存在本次未提交修复 `forwin/state/repo.py`、`tests/test_subworld_control.py`；另有未跟踪文档 `Design-docs/review_2026-05-12_continue_generation_patch_audit.md`
- 运行环境：`Linux ccttww-lap 6.12.57+deb13-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.12.57-1 (2025-11-05) x86_64 GNU/Linux`
- 测试项目 ID：`bbe070bc8eda49c9a551c3ce1c755391`
- 测试小说标题：`端到端实测长篇小说_60章`
- 使用真实生成：是
- 使用 mock 替代主流程：否
- 真实 LLM/API provider：DeepSeek (`env-deepseek`)
- 模型名称：`deepseek-chat`
- API key：本地 `.env` 中存在，未在报告中暴露
- 目标章节数：60
- 实际生成章节数：60
- 实际接受章节数：60
- 生成开始时间：2026-05-11 16:32:26 PDT
- 生成结束时间：2026-05-11 20:28:00 PDT
- 总耗时：3:55:34
- 30 章断点：2026-05-11 18:04:00 PDT 按用户要求暂停；用户确认后 18:21:40 PDT 继续后 30 章
- 最终结论：通过。主流程通过前端真实创建、Genesis、真实 LLM 写作、断点重启、继续生成到 60 章；60 章均保存为 `accepted`，章节列表和正文可在前端打开，刷新后可恢复。

## 项目梳理

已阅读或检查：`README.md`、Python `pyproject.toml`、前端 `frontend/world-studio/package.json` 与 lock 文件、`Dockerfile`、`docker-compose.yml`、测试目录、`.env.example`/`.env` 配置、数据库配置、LLM provider 配置、小说生成相关模块。仓库当前没有 root `package.json`、`requirements.txt`、`poetry.lock`、`pnpm-lock.yaml` 或 `yarn.lock`。

关键代码位置：

| 功能 | 代码位置 |
| --- | --- |
| 小说创建 | `forwin/ui_assets/home/app_library.js`, `forwin/api_project_routes.py`, `forwin/api_project_ops.py` |
| 小说配置 | `forwin/ui_assets/home/body.html`, `forwin/ui_assets/home/app_library.js`, `forwin/runtime_settings.py` |
| 世界观/人物/大纲生成 | `forwin/ui_assets/home/app_genesis.js`, `forwin/book_genesis/*`, `forwin/api_genesis_routes.py` |
| 章节生成 | `forwin/generation/*`, `forwin/writer/*`, `forwin/orchestrator/loop.py` |
| 章节列表 | `forwin/ui_assets/home/app_task_governance.js`, `forwin/api_project_ops.py:list_chapters` |
| 章节详情 | `forwin/ui_assets/home/app_task_progress.js:toggleChapterBody`, `forwin/api_project_ops.py:get_chapter` |
| 生成进度 | `forwin/ui_assets/home/app_task_progress.js`, `forwin/ui_assets/home/app_task_drawer.js` |
| 前端状态管理 | `forwin/ui_assets/home/app_state.js`, `forwin/ui_assets/home/app_core.js` |
| 后端任务状态 | `forwin/models/task.py`, `forwin/api_task_routes.py`, `forwin/api_task_ops.py` |
| 数据保存 | `forwin/models/project.py`, `forwin/models/draft.py`, `forwin/state/updater.py`, `forwin/state/repo.py` |
| 失败重试 | `forwin/api_review_routes.py`, `forwin/api_review_ops.py`, `forwin/api_project_ops.py:retry_chapter_review` |
| 暂停/继续 | `forwin/api_task_routes.py`, `forwin/api_task_ops.py`, `forwin/api_project_ops.py:continue_generation` |
| 导出功能 | `forwin/api_world_model_routes.py`, `forwin/api_obsidian_routes.py`, `forwin/world_model/api.py`；未发现小说正文 TXT/MD/DOCX/PDF 导出 UI |
| API 请求封装 | `forwin/ui_assets/home/app_core.js:requestJson` |
| 数据库 schema/ORM | `forwin/models/*`, `forwin/models/base.py` |
| LLM provider | `forwin/config.py`, `forwin/writer/llm_client.py` |

## 环境与启动

| 项 | 内容 |
| --- | --- |
| 依赖安装 | Python 使用仓库 `.venv`；前端 `frontend/world-studio` 已执行 `npm ci` |
| 前端启动命令 | `docker compose up -d forwin forwin-mcp`；首页由后端服务 `http://127.0.0.1:8899/` 提供 |
| 后端启动命令 | `docker compose up -d forwin forwin-mcp` |
| 数据库/缓存/依赖 | Docker Compose 启动 `postgres`, `postgres-test`, `qdrant`, `minio`；无独立队列服务，生成任务状态持久化在 PostgreSQL |
| 浏览器自动化 | Playwright 真实浏览器访问 `http://127.0.0.1:8899/` |
| 前端连通后端 | 前端页面、项目详情、章节列表、章节正文接口均在最终复核中正常返回 |
| 真实 LLM 配置 | `.env` 有 `DEEPSEEK_API_KEY`；`DEEPSEEK_BASE_URL`、`DEEPSEEK_MODEL` 未显式设置，使用代码默认 `https://api.deepseek.com/v1` 与 `deepseek-chat` |
| 数据库连接 | `FORWIN_DATABASE_URL=postgresql+psycopg://forwin:forwin@postgres:5432/forwin` |
| MCP/operator | `python3 scripts/check_codex_operator_ready.py` 全部 OK |

遵循 ForWin operator 规则：项目、任务、章节真相通过 ForWin MCP (`project_get`, `chapter_list`, `chapter_get`, `task_active_generation_check`) 验证；未直接读取 SQLite 或直接写表。

## 基础质量检查

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `python3 scripts/check_codex_operator_ready.py` | 通过 | API health、MCP health、Docker 服务、MCP 注册、Python 环境均 OK |
| `cd frontend/world-studio && npm run build` | 通过 | `tsc -b && vite build`，相当于当前前端 typecheck/build；项目无 lint 脚本 |
| `.venv/bin/python -m pytest tests/test_subworld_control.py tests/test_project_operation_guards.py tests/test_generation_task_persistence.py tests/test_governance_decision_api.py tests/test_codex_operator_ready.py tests/test_env_llm_profiles.py tests/browser/test_mock_book_creation_generation_regression.py -q` | 112 passed, 3 subtests passed | 覆盖本次修复、任务持久化、checkpoint、operator、LLM profile、CI mock e2e |
| `.venv/bin/python -m pytest tests/browser/test_home_console.py tests/browser/test_genesis_workspace.py tests/browser/test_task_center_drawer.py tests/browser/test_governance_and_chapters.py -q` | 9 passed | 现有浏览器回归 |
| Playwright 真实前端复核脚本 | 通过 | 60 个项目章节行；逐章点击 60 个“查看正文”；刷新后重新打开第 60 章 |

## 测试小说参数

- 标题：`端到端实测长篇小说_60章`
- 类型：`架空奇幻悬疑`
- 目标章节数：60
- 主角：林澈，旧城档案修复师
- 背景：一座每隔十年会遗失一段历史的城市
- 核心冲突：林澈发现自己的家族记录被人为抹除，60 份残缺档案分别对应 60 个关键真相
- 主线目标：林澈必须在城市下一次历史重置前找回全部档案
- 反派力量：掌控城市记忆系统的隐秘组织“白塔”
- 章节要求：第 1 章到第 60 章连续推进剧情，每章有独立标题和正文
- Genesis 大纲：已通过前端真实生成并锁定；`book_blueprint` 为 5 个 arc，每个 arc 12 章，覆盖第 1-60 章

## 前端真实流程

已通过真实浏览器执行：

1. 打开首页 `http://127.0.0.1:8899/`。
2. 通过前端创建 `端到端实测长篇小说_60章`。
3. 通过前端依次生成并锁定 Genesis stages：`brief`, `world`, `map`, `story_engine`, `book_blueprint`, `bootstrap`。
4. 通过前端启动写作任务，真实 LLM 生成第 1-12 章。
5. 通过前端继续生成第 13-24 章。
6. 通过前端继续生成第 25-29 章，并用安全暂停验证暂停路径。
7. 检查 band checkpoint 后 override 警告 checkpoint，再通过前端继续生成第 30 章。
8. 按用户要求停在 30 章断点。
9. 用户确认后，通过前端继续生成第 31-36 章。
10. 通过前端继续生成第 37-46 章；第 47 章触发真实 `needs_review` 阻断。
11. 修复 `sub_world_unknown_named_entity` 问题，重建并重启服务。
12. 通过前端重试并继续生成第 47-48 章。
13. 通过前端继续生成第 49-60 章。
14. 用 Playwright 真实前端复核：进入书本、确认 60 行章节、逐章点击 60 个“查看正文”、抽查第 1/2/10/30/59/60 章、刷新页面后重新进入并打开第 60 章。

真实生成任务：

| task_id | 触发方式 | 状态 | 章节 | 时间 |
| --- | --- | --- | --- | --- |
| `1cb173fa06a9` | 前端启动写作 | completed | 1-12 | 16:32:26-16:58:26 PDT |
| `f989a175f66f` | 前端继续生成 | completed | 13-24 | 17:01:56-17:35:31 PDT |
| `6fd0a31a9e64` | 前端继续生成后安全暂停 | paused | 25-29 | 17:36:23-17:59:29 PDT |
| `e98357a44f2a` | 前端继续生成单章 | completed | 30 | 18:01:29-18:04:00 PDT |
| `f5db3c12c64a` | 前端继续生成 | completed | 31-36 | 18:21:40-18:37:12 PDT |
| `655ba8d603b2` | 前端继续生成 | review blocker | 37-46 完成，第 47 章阻断 | 18:41:48 之后 |
| `846219d96b55` | 修复后前端继续生成 | completed | 47-48 | 19:35:53-19:41:02 PDT |
| `6c52ef6c04bb` | 前端继续生成 | completed | 49-60 | 19:44:47-20:28:00 PDT |

前端状态变化观察到：初始/空闲、loading、生成中、章节完成、checkpoint 阻断、needs_review 阻断、暂停、完成。未发现独立 SSE/WebSocket streaming UI；当前页面通过轮询任务/项目状态同步。

## 每章生成状态

| 章节 | 标题 | 正文 | 字符数 | 状态 | 前端打开 |
| ---: | --- | --- | ---: | --- | --- |
| 1 | 被抹除的姓氏 | 是 | 3075 | accepted | 是 |
| 2 | 六十天的倒计时 | 是 | 3593 | accepted | 是 |
| 3 | 白塔的阴影 | 是 | 2467 | accepted | 是 |
| 4 | 第一份残片 | 是 | 3402 | accepted | 是 |
| 5 | 地下黑市的入口 | 是 | 2694 | accepted | 是 |
| 6 | 沈砚的试探 | 是 | 3654 | accepted | 是 |
| 7 | 潮汐钟楼的秘密 | 是 | 3721 | accepted | 是 |
| 8 | 激进派的追杀 | 是 | 3735 | accepted | 是 |
| 9 | 第四份残片：家族遗言 | 是 | 2869 | accepted | 是 |
| 10 | 黑市的陷阱 | 是 | 2249 | accepted | 是 |
| 11 | 温和派的筹码 | 是 | 3329 | accepted | 是 |
| 12 | 残片拼图的开端 | 是 | 2454 | accepted | 是 |
| 13 | 地下入口 | 是 | 2775 | accepted | 是 |
| 14 | 黑市交易 | 是 | 3171 | accepted | 是 |
| 15 | 档案战争残片 | 是 | 3216 | accepted | 是 |
| 16 | 白塔的眼线 | 是 | 3416 | accepted | 是 |
| 17 | 温和派的橄榄枝 | 是 | 3321 | accepted | 是 |
| 18 | 第14份档案：背叛者名单 | 是 | 4527 | accepted | 是 |
| 19 | 旧轨追捕 | 是 | 4392 | accepted | 是 |
| 20 | 顾岚的过去 | 是 | 3667 | accepted | 是 |
| 21 | 第15-18份档案：战争拼图 | 是 | 3479 | accepted | 是 |
| 22 | 沈砚的危机 | 是 | 3887 | accepted | 是 |
| 23 | 陷阱 | 是 | 3956 | accepted | 是 |
| 24 | 代价与抉择 | 是 | 3221 | accepted | 是 |
| 25 | 白塔阴影 | 是 | 4071 | accepted | 是 |
| 26 | 档案库暗道 | 是 | 3380 | accepted | 是 |
| 27 | 密钥觉醒 | 是 | 2787 | accepted | 是 |
| 28 | 温和派陷阱 | 是 | 2957 | accepted | 是 |
| 29 | 撤离遇袭 | 是 | 4889 | accepted | 是 |
| 30 | 潮汐钟楼 | 是 | 3016 | accepted | 是 |
| 31 | 钟楼密档 | 是 | 3645 | accepted | 是 |
| 32 | 沈砚被捕 | 是 | 2315 | accepted | 是 |
| 33 | 黑市交易 | 是 | 4080 | accepted | 是 |
| 34 | 激进派内讧 | 是 | 3637 | accepted | 是 |
| 35 | 正面交锋 | 是 | 2459 | accepted | 是 |
| 36 | 代价与觉醒 | 是 | 3303 | accepted | 是 |
| 37 | 第37章 绝境联盟 | 是 | 3246 | accepted | 是 |
| 38 | 第38章 地下通道 | 是 | 3083 | accepted | 是 |
| 39 | 第39章 温和派的暗桩 | 是 | 3492 | accepted | 是 |
| 40 | 第40章 渗透计划 | 是 | 2906 | accepted | 是 |
| 41 | 第41章 档案库陷阱 | 是 | 2966 | accepted | 是 |
| 42 | 第42章 沈砚的抉择 | 是 | 2286 | accepted | 是 |
| 43 | 第43章 失忆广场集结 | 是 | 3832 | accepted | 是 |
| 44 | 第44章 系统后门 | 是 | 2508 | accepted | 是 |
| 45 | 第45章 激进派的疯狂 | 是 | 4181 | accepted | 是 |
| 46 | 第46章 记忆洪流 | 是 | 2670 | accepted | 是 |
| 47 | 第47章 最后的交易 | 是 | 2550 | accepted | 是 |
| 48 | 第48章 重置倒计时 | 是 | 3403 | accepted | 是 |
| 49 | 第49章 重置倒计时 | 是 | 2507 | accepted | 是 |
| 50 | 第50章 家族遗物 | 是 | 3477 | accepted | 是 |
| 51 | 第51章 温和派的抉择 | 是 | 3307 | accepted | 是 |
| 52 | 第52章 黑市记忆 | 是 | 3558 | accepted | 是 |
| 53 | 第53章 地下旧轨深处 | 是 | 1993 | accepted | 是 |
| 54 | 第54章 记忆反噬 | 是 | 4004 | accepted | 是 |
| 55 | 第55章 激进派的陷阱 | 是 | 3119 | accepted | 是 |
| 56 | 第56章 白塔核心 | 是 | 2755 | accepted | 是 |
| 57 | 第57章 最后的档案 | 是 | 2996 | accepted | 是 |
| 58 | 第58章 真相的代价 | 是 | 3755 | accepted | 是 |
| 59 | 第59章 记忆重建 | 是 | 2636 | accepted | 是 |
| 60 | 第60章 新秩序 | 是 | 3559 | accepted | 是 |

前端逐章打开验证 artifact：`/tmp/forwin_60_artifacts/final_frontend_verify.json`。

截图：

- `/tmp/forwin_60_screens/final-home-project-card-and-drawer.png`
- `/tmp/forwin_60_screens/final-60-chapter-list.png`
- `/tmp/forwin_60_screens/final-60-sampled-bodies.png`
- `/tmp/forwin_60_screens/final-60-after-refresh.png`

## 抽查章节结果

| 章节 | 结果 |
| ---: | --- |
| 1 | 前端可打开；正文 3075 字符；非空；不是占位符；不是错误 JSON；未出现 `undefined/null/[object Object]` |
| 2 | 前端可打开；正文 3593 字符；非空；不是占位符；不是错误 JSON；未出现 `undefined/null/[object Object]` |
| 10 | 前端可打开；正文 2249 字符；非空；不是占位符；不是错误 JSON；未出现 `undefined/null/[object Object]` |
| 30 | 前端可打开；正文 3016 字符；非空；不是占位符；不是错误 JSON；未出现 `undefined/null/[object Object]` |
| 59 | 前端可打开；正文 2636 字符；非空；不是占位符；不是错误 JSON；未出现 `undefined/null/[object Object]` |
| 60 | 前端可打开；正文 3559 字符；非空；不是占位符；不是错误 JSON；未出现 `undefined/null/[object Object]` |

## 后端和数据验证

- `task_active_generation_check(project_id)`：`has_active_generation_task=false`, `active_count=0`, `safe_to_restart=true`
- `project_get(project_id)`：`creation_status=writing`, `chapter_count=60`, `generated_chapter_count=60`, `accepted_chapter_count=60`, `needs_review_chapter_count=0`, `next_gate=completed`
- `generation_control`：`plan_state=completed`, `writing_state=completed`, `review_state=none`, `current_chapter=60`, `next_chapter=0`
- `chapter_list(project_id)`：第 1-60 章连续、无重复、均为 `accepted`、均 `has_draft=true`、均 `has_review=true`
- 前端最终复核：项目章节区域 60 行；刷新后仍为 60 行；刷新后第 60 章正文可打开
- 未发现缺失章节、重复章节、孤儿章节、章节顺序错乱或刷新后状态丢失
- 本次按 AGENTS/ForWin operator 规则未直接读写 SQLite/DB 表；数据库持久化通过 MCP/API 权威状态和刷新恢复验证

## Console、Network、后端日志

- 最终 Playwright 前端复核：console warning/error 数量 0
- 最终 Playwright 前端复核：page error 数量 0
- 最终 Playwright 前端复核：Network 4xx/5xx 数量 0
- 生成过程中出现过预期内的 checkpoint/review 阻断，不是静默失败
- 后端最终窗口未发现测试项目相关 `Traceback` 或 `ERROR`
- 后端日志中存在旧客户端持续轮询历史任务 `17e4526acb13` 的 404，和本测试项目无关；最终浏览器复核未产生 4xx/5xx
- MCP 服务日志中存在 `/mcp` 探测 404，operator readiness 仍为 OK
- reviewer 对第 51、55、57 章给出内容一致性 warning，但章节最终通过并保存；这些属于内容质量风险，不是流程阻塞

## 真实失败处理

真实主流程中发现并处理了一个阻断：

- 失败位置：继续生成后 30 章过程中，第 47 章
- 失败类型：后端 review/continuity blocker
- 失败原因：`sub_world_unknown_named_entity`；第 46 章后的 world pressure 提到了 canon 角色“洛庭若”，第 47 章 writer 合理使用该角色，但 subworld 严格命名角色白名单没有把 world pressure 中引用的现有 canon 角色纳入允许集合
- 影响：第 47 章进入 `needs_review`，后续 47-60 章无法继续自动写作
- 修复：`forwin/state/repo.py` 中 `get_allowed_entity_names` 和 `get_allowed_entity_snapshots` 纳入最新 world pressure 引用的现有活跃 canon 角色名称/别名
- 回归测试：`tests/test_subworld_control.py::SubWorldControlTests::test_world_pressure_referenced_canon_character_is_subworld_allowed`
- 验证：修复前定向测试 red，修复后 `tests/test_subworld_control.py` 与综合测试通过；重建/重启后，通过前端重试第 47 章并继续到第 60 章

## 导出功能验证

- 当前产品面未发现小说正文 TXT、Markdown、DOCX、PDF 导出入口。
- 代码中存在 WorldModel/Obsidian Markdown 投影导出端点，但不等同于小说正文导出。
- 用户后续明确说明“pdf 导入什么的不需要”，因此 PDF 导入/相关流程未纳入本次验证。
- 本次最终结论不把不存在的小说正文导出入口计为 60 章生成主流程失败；该能力仍列为未实现/未验证项。

## 发现的问题

| 问题 | 影响 | 状态 |
| --- | --- | --- |
| 第 47 章因 world pressure 引用的 canon 角色“洛庭若”未进 subworld 白名单而 `needs_review` | 阻断后续章节继续生成 | 已修复并通过真实续写验证 |
| 继续生成任务响应曾显示 `requested_chapters` 与实际 `max_chapters` 不一致 | 前端状态展示易误导 | 已修复并有测试覆盖 |
| checkpoint approve 曾允许非法 `status=approved` | 可能导致任务中心状态异常 | 已修复并有测试覆盖 |
| 部分 V4/world/reviewer 兼容 alias 导入问题 | 影响测试与兼容入口 | 已修复 |
| 旧客户端轮询历史任务产生 404 日志 | 日志噪音 | 未修复，非本项目阻塞 |
| 第 1 章正文多处使用“相关人员”而非“林澈”；后段 reviewer 对若干章节有内容一致性 warning | 内容质量风险 | 未修复，流程层面不阻塞 |
| 小说正文 TXT/Markdown/DOCX/PDF 导出入口未发现 | 导出能力缺口 | 未修复/未实现 |

## 已修复的问题

- 修复 `forwin/state/repo.py`：world pressure 中引用的现有 canon 角色纳入 subworld 严格命名角色允许集合。
- 新增 `tests/test_subworld_control.py` 回归测试，覆盖“洛庭若”这类 world pressure 引用角色不应被误判为未知命名实体。
- 修复 continue generation 响应中的 `requested_chapters` 计算，使其尊重 `max_chapters`。
- 收紧 `BandCheckpointApproveRequest.status` / MCP `band_checkpoint_approve` 为合法状态，避免非法 checkpoint 状态污染。
- 修复 world/reviewer v4 兼容 alias 导入路径。
- 新增前端 mock 回归测试，覆盖创建小说、传递章节数、启动生成、章节列表、正文非空和刷新恢复。

## 新增/修改测试文件

- `tests/test_subworld_control.py`
- `tests/test_project_operation_guards.py`
- `tests/test_governance_decision_api.py`
- `tests/browser/test_mock_book_creation_generation_regression.py`

说明：`tests/browser/test_mock_book_creation_generation_regression.py` 使用 mock backend/provider，仅用于 CI 回归；它不是本次真实 LLM 60 章主流程实测的替代品。

## 执行过的主要命令

```bash
find . -maxdepth 3 \( -name 'README*' -o -name 'package.json' -o -name 'pyproject.toml' -o -name 'Dockerfile*' -o -name 'docker-compose*.yml' -o -name '.env*' \) | sort
python3 scripts/check_codex_operator_ready.py
cd frontend/world-studio && npm ci
cd frontend/world-studio && npm run build
docker compose ps
docker compose build forwin forwin-mcp
docker compose up -d forwin forwin-mcp
.venv/bin/python -m pytest tests/test_subworld_control.py -q
.venv/bin/python -m pytest tests/test_project_operation_guards.py tests/test_generation_task_persistence.py tests/test_governance_decision_api.py tests/test_codex_operator_ready.py tests/test_env_llm_profiles.py tests/browser/test_mock_book_creation_generation_regression.py -q
.venv/bin/python -m pytest tests/browser/test_home_console.py tests/browser/test_genesis_workspace.py tests/browser/test_task_center_drawer.py tests/browser/test_governance_and_chapters.py -q
docker compose logs --since '2026-05-11T18:30:00-07:00' forwin
docker compose logs --since '2026-05-11T20:20:00-07:00' forwin forwin-mcp
git status --short
git rev-parse HEAD
```

MCP/前端操作摘要：

```text
ForWin MCP: project_get, chapter_list, chapter_get, task_active_generation_check, band_checkpoint_get, band_checkpoint_approve, chapter_review_retry
Playwright: 打开首页、创建书本、Genesis 阶段生成/锁定、启动写作、继续生成、逐章打开 60 个正文、刷新恢复验证
```

## 最终结论

通过。

判定依据：

- 主流程没有使用 mock provider、fake model、dry-run 或 stub response。
- 60 章生成均由真实前端操作触发，真实 LLM provider 为 DeepSeek。
- 章节记录数量为 60，编号 1-60 连续，无重复、无缺失。
- 60 章均有标题、正文、review，最终状态均为 `accepted`。
- 60 章正文均通过前端“查看正文”打开验证。
- 刷新页面后小说、章节列表、正文和完成状态仍可恢复。
- 真实失败第 47 章已定位、修复、重启并通过前端继续生成验证。
