# 60 章真实长篇小说端到端实测报告

## 结论摘要

- 测试时间：2026-05-11 16:30:00-18:12:01 PDT
- 当前分支：`master`
- 当前 commit：`d9d0be5862e7b4679eafeeae1cd02a5fec8e06dd`
- 运行环境：`Linux ccttww-lap 6.12.57+deb13-amd64 #1 SMP PREEMPT_DYNAMIC Debian 6.12.57-1 (2025-11-05) x86_64 GNU/Linux`
- 测试项目 ID：`bbe070bc8eda49c9a551c3ce1c755391`
- 测试小说标题：`端到端实测长篇小说_60章`
- 使用真实生成：是
- 使用 mock 替代主流程：否
- 真实 LLM/API provider：DeepSeek (`env-deepseek`)
- 模型名称：`deepseek-chat`
- API key：本地 `.env` 中存在，未在报告中暴露
- 目标章节数：60
- 当前实际生成章节数：30
- 当前实际接受章节数：30
- 生成开始时间：2026-05-11 16:32:26 PDT
- 当前断点时间：2026-05-11 18:04:00 PDT
- 到 30 章耗时：约 1 小时 31 分 34 秒
- 最终结论：部分通过。已通过前端真实创建、Genesis、真实 LLM 写作并保存 1-30 章；按用户要求在 30 章断点停下，60 章完整流程尚未完成，不能标为通过。

## 项目梳理

已阅读或检查：`README.md`、Python `pyproject.toml`、前端 `frontend/world-studio/package.json` 与 `package-lock.json`、`Dockerfile`、`docker-compose.yml`、测试目录、环境变量配置、数据库配置、LLM provider 配置、小说生成相关模块。

关键代码位置：

| 功能 | 代码位置 |
| --- | --- |
| 前端首页/书库 | `forwin/ui_assets/home/body.html`, `forwin/ui_assets/home/app_library.js` |
| 前端 Genesis 工作台 | `forwin/ui_assets/home/app_genesis.js` |
| 前端任务进度/状态同步 | `forwin/ui_assets/home/app_task_progress.js`, `forwin/ui_assets/home/app_task_drawer.js`, `forwin/ui_assets/home/app_state.js` |
| 前端治理/章节列表/章节正文 | `forwin/ui_assets/home/app_task_governance.js` |
| Vite React world-studio | `frontend/world-studio/src/App.tsx` |
| 后端入口 | `forwin/api.py` |
| 路由注册 | `forwin/api_route_registry.py` |
| 项目创建/启动写作/继续生成 | `forwin/api_project_routes.py`, `forwin/api_project_ops.py` |
| Genesis 生成/锁定 | `forwin/api_genesis_routes.py`, `forwin/book_genesis/*` |
| 章节生成 | `forwin/generation/*`, `forwin/writer/*` |
| LLM 客户端 | `forwin/writer/llm_client.py` |
| LLM/API provider 配置 | `forwin/config.py`, `.env` |
| 后端任务状态 | `forwin/models/task.py`, `forwin/api_task_routes.py`, `forwin/api_task_ops.py` |
| 数据保存 ORM | `forwin/models/project.py`, `forwin/models/draft.py`, `forwin/models/base.py` |
| 数据状态更新 | `forwin/state/updater.py` |
| 失败重试/章节 review | `forwin/api_review_routes.py`, `forwin/api_review_ops.py` |
| 暂停/继续 | `forwin/api_task_routes.py`, `forwin/api_task_ops.py`, `forwin/api_project_ops.py` |
| API 请求封装 | `forwin/ui_assets/home/app_core.js` |
| 导出功能 | 发现 world model / Obsidian 导出端点；未发现当前产品面向小说 TXT/Markdown/DOCX/PDF 的导出入口 |

## 环境与启动

| 项 | 内容 |
| --- | --- |
| 依赖安装 | `npm ci` 于 `frontend/world-studio` 通过；Python 使用仓库 `.venv` |
| 前端启动命令 | 通过 `docker compose up -d forwin forwin-mcp` 启动；首页 `http://127.0.0.1:8899/` |
| 后端启动命令 | `docker compose up -d forwin forwin-mcp` |
| 数据库/缓存/依赖 | Docker Compose 启动 `postgres`, `postgres-test`, `qdrant`, `minio`；未观察到独立队列服务，生成任务状态持久化在 DB |
| 浏览器自动化 | Playwright，真实浏览器访问 `http://127.0.0.1:8899/` |
| 前端连通后端 | `/api/settings/llm`, `/api/projects`, `/api/task-center/items` 均 200 |
| 真实 LLM 配置 | `/api/settings/llm` 显示 DeepSeek provider、`deepseek-chat`、`has_api_key=true` |

## 基础质量检查

| 命令 | 结果 | 备注 |
| --- | --- | --- |
| `python3 scripts/check_codex_operator_ready.py` | 通过 | Docker 重建前后均通过 |
| `npm ci` | 通过 | `frontend/world-studio` |
| `npm run build` | 通过 | `frontend/world-studio` |
| `.venv/bin/python -m pytest tests/test_world_v4_aliases.py -q` | 2 passed | 修复兼容 alias 后通过 |
| `.venv/bin/python -m pytest tests/test_codex_operator_ready.py tests/test_env_llm_profiles.py tests/test_project_operation_guards.py tests/test_generation_task_persistence.py` | 40 passed | 基础后端/任务持久化检查 |
| `.venv/bin/python -m pytest tests/browser/test_home_console.py tests/browser/test_genesis_workspace.py tests/browser/test_task_center_drawer.py tests/browser/test_governance_and_chapters.py` | 9 passed | 浏览器回归 |
| `.venv/bin/python -m pytest tests/test_governance_decision_api.py tests/test_mcp_server.py -q` | 22 passed, 3 subtests passed | checkpoint status 修复后通过 |
| `.venv/bin/python -m pytest tests/browser/test_mock_book_creation_generation_regression.py -q` | 1 passed | 新增 CI mock 回归，不替代真实 60 章实测 |

## 测试小说参数

- 标题：`端到端实测长篇小说_60章`
- 类型：`架空奇幻悬疑`
- 目标章节数：60
- 主角：林澈，旧城档案修复师
- 背景：一座每隔十年会遗失一段历史的城市
- 核心冲突：林澈发现自己的家族记录被人为抹除，60 份残缺档案分别对应 60 个关键真相
- 主线目标：林澈必须在城市下一次历史重置前找回全部档案
- 反派力量：掌控城市记忆系统的隐秘组织“白塔”
- Genesis 大纲：已通过前端真实生成并锁定；`book_blueprint` 为 5 个 arc，每个 arc 12 章，覆盖第 1-60 章

## 前端真实流程

已通过真实浏览器执行：

1. 打开首页 `http://127.0.0.1:8899/`。
2. 通过前端创建 `端到端实测长篇小说_60章`。
3. 通过前端依次生成并锁定 Genesis stages：`brief`, `world`, `map`, `story_engine`, `book_blueprint`, `bootstrap`。
4. 通过前端启动写作任务，真实 LLM 生成第 1-12 章。
5. 通过前端继续生成第 13-24 章。
6. 通过前端继续生成第 25-29 章，并在接近第 30 章时用安全暂停验证暂停路径。
7. 检查 band checkpoint 后 override 警告 checkpoint，再通过前端继续生成第 30 章。
8. 按用户要求停在 30 章断点，未继续第 31-60 章。
9. 重启服务后通过真实浏览器重新打开首页、进入书本、展开第 1、2、10、30 章正文，刷新后再次进入书本验证恢复。

真实生成任务：

| task_id | 触发方式 | 状态 | 章节 | 时间 |
| --- | --- | --- | --- | --- |
| `1cb173fa06a9` | 前端启动写作 | completed | 1-12 | 16:32:26-16:58:26 PDT |
| `f989a175f66f` | 前端继续生成 | completed | 13-24 | 17:01:56-17:35:31 PDT |
| `6fd0a31a9e64` | 前端继续生成后安全暂停 | paused | 25-29 | 17:36:23-17:59:29 PDT |
| `e98357a44f2a` | 前端继续生成单章 | completed | 30 | 18:01:29-18:04:00 PDT |

## 每章生成状态

| 章节 | 标题 | 正文 | 字符数 | 状态 | 前端打开 |
| ---: | --- | --- | ---: | --- | --- |
| 1 | 被抹除的姓氏 | 是 | 3075 | accepted | 是，已抽查 |
| 2 | 六十天的倒计时 | 是 | 3593 | accepted | 是，已抽查 |
| 3 | 白塔的阴影 | 是 | 2467 | accepted | 列表可见 |
| 4 | 第一份残片 | 是 | 3402 | accepted | 列表可见 |
| 5 | 地下黑市的入口 | 是 | 2694 | accepted | 列表可见 |
| 6 | 沈砚的试探 | 是 | 3654 | accepted | 列表可见 |
| 7 | 潮汐钟楼的秘密 | 是 | 3721 | accepted | 列表可见 |
| 8 | 激进派的追杀 | 是 | 3735 | accepted | 列表可见 |
| 9 | 第四份残片：家族遗言 | 是 | 2869 | accepted | 列表可见 |
| 10 | 黑市的陷阱 | 是 | 2249 | accepted | 是，已抽查 |
| 11 | 温和派的筹码 | 是 | 3329 | accepted | 列表可见 |
| 12 | 残片拼图的开端 | 是 | 2454 | accepted | 列表可见 |
| 13 | 地下入口 | 是 | 2775 | accepted | 列表可见 |
| 14 | 黑市交易 | 是 | 3171 | accepted | 列表可见 |
| 15 | 档案战争残片 | 是 | 3216 | accepted | 列表可见 |
| 16 | 白塔的眼线 | 是 | 3416 | accepted | 列表可见 |
| 17 | 温和派的橄榄枝 | 是 | 3321 | accepted | 列表可见 |
| 18 | 第14份档案：背叛者名单 | 是 | 4527 | accepted | 列表可见 |
| 19 | 旧轨追捕 | 是 | 4392 | accepted | 列表可见 |
| 20 | 顾岚的过去 | 是 | 3667 | accepted | 列表可见 |
| 21 | 第15-18份档案：战争拼图 | 是 | 3479 | accepted | 列表可见 |
| 22 | 沈砚的危机 | 是 | 3887 | accepted | 列表可见 |
| 23 | 陷阱 | 是 | 3956 | accepted | 列表可见 |
| 24 | 代价与抉择 | 是 | 3221 | accepted | 列表可见 |
| 25 | 白塔阴影 | 是 | 4071 | accepted | 列表可见 |
| 26 | 档案库暗道 | 是 | 3380 | accepted | 列表可见 |
| 27 | 密钥觉醒 | 是 | 2787 | accepted | 列表可见 |
| 28 | 温和派陷阱 | 是 | 2957 | accepted | 列表可见 |
| 29 | 撤离遇袭 | 是 | 4889 | accepted | 列表可见 |
| 30 | 潮汐钟楼 | 是 | 3016 | accepted | 是，已抽查 |
| 31 | 钟楼密档 | 否 | 0 | planned | 未生成 |
| 32 | 沈砚被捕 | 否 | 0 | planned | 未生成 |
| 33 | 黑市交易 | 否 | 0 | planned | 未生成 |
| 34 | 激进派内讧 | 否 | 0 | planned | 未生成 |
| 35 | 正面交锋 | 否 | 0 | planned | 未生成 |
| 36 | 代价与觉醒 | 否 | 0 | planned | 未生成 |
| 37 | 未物化 | 否 | 0 | pending | 未生成 |
| 38 | 未物化 | 否 | 0 | pending | 未生成 |
| 39 | 未物化 | 否 | 0 | pending | 未生成 |
| 40 | 未物化 | 否 | 0 | pending | 未生成 |
| 41 | 未物化 | 否 | 0 | pending | 未生成 |
| 42 | 未物化 | 否 | 0 | pending | 未生成 |
| 43 | 未物化 | 否 | 0 | pending | 未生成 |
| 44 | 未物化 | 否 | 0 | pending | 未生成 |
| 45 | 未物化 | 否 | 0 | pending | 未生成 |
| 46 | 未物化 | 否 | 0 | pending | 未生成 |
| 47 | 未物化 | 否 | 0 | pending | 未生成 |
| 48 | 未物化 | 否 | 0 | pending | 未生成 |
| 49 | 未物化 | 否 | 0 | pending | 未生成 |
| 50 | 未物化 | 否 | 0 | pending | 未生成 |
| 51 | 未物化 | 否 | 0 | pending | 未生成 |
| 52 | 未物化 | 否 | 0 | pending | 未生成 |
| 53 | 未物化 | 否 | 0 | pending | 未生成 |
| 54 | 未物化 | 否 | 0 | pending | 未生成 |
| 55 | 未物化 | 否 | 0 | pending | 未生成 |
| 56 | 未物化 | 否 | 0 | pending | 未生成 |
| 57 | 未物化 | 否 | 0 | pending | 未生成 |
| 58 | 未物化 | 否 | 0 | pending | 未生成 |
| 59 | 未物化 | 否 | 0 | pending | 未生成，按用户要求停在 30 章 |
| 60 | 未物化 | 否 | 0 | pending | 未生成，按用户要求停在 30 章 |

说明：`project_get.chapter_count=36` 表示当前已物化计划章节到 36；Genesis `book_blueprint` 已覆盖 1-60。第 37-60 章将在后续继续生成时物化。

## 抽查章节结果

| 章节 | 结果 |
| ---: | --- |
| 1 | 前端可打开；正文 3075 字符；非空；不是占位符；未出现 `undefined/null/[object Object]` |
| 2 | 前端可打开；正文 3593 字符；非空；不是占位符；未出现 `undefined/null/[object Object]` |
| 10 | 前端可打开；正文 2249 字符；非空；不是占位符；未出现 `undefined/null/[object Object]` |
| 30 | 前端可打开；正文 3016 字符；非空；不是占位符；未出现 `undefined/null/[object Object]` |
| 59 | 未抽查正文；按用户要求 30 章处停止，尚未生成 |
| 60 | 未抽查正文；按用户要求 30 章处停止，尚未生成 |

浏览器复核截图：

- `/tmp/forwin_60_screens/final-30-chapter-list.png`
- `/tmp/forwin_60_screens/final-30-sampled-bodies.png`
- `/tmp/forwin_60_screens/final-30-after-refresh.png`

## 状态、刷新与持久化验证

遵循 ForWin operator 规则，未直接读取 SQLite 或直接写表；项目、任务、章节真相通过 ForWin MCP/API 验证。

- `task_active_generation_check(project_id)`：`has_active_generation_task=false`, `safe_to_restart=true`
- `project_get(project_id)`：`creation_status=writing`, `generated_chapter_count=30`, `accepted_chapter_count=30`, `next_gate=chapter_31_write`
- `chapter_list(project_id)`：第 1-30 章连续、无重复、均为 `accepted`、均有标题、均有正文；第 31-36 章为 `planned`
- 服务重启后，前端首页能重新加载该小说
- 服务重启后，任务中心能打开 `project-bbe070bc8eda49c9a551c3ce1c755391`
- 刷新页面后，章节列表仍存在，第 1、2、10、30 章正文仍可打开
- 未发现 1-30 章 orphan record、重复章节或顺序错乱

## Console、Network、后端日志

- 最终 30 章断点浏览器复核：console warning/error 为空
- 最终 30 章断点浏览器复核：Network `/api/*` 4xx/5xx 为空
- 生成过程中一次前端 continue 被 checkpoint 正常阻断：`409 Conflict`，原因是 `band:22:24` checkpoint warning；检查后已 override
- 生成过程中发现一次操作员误传 `status=approved`，后端旧 schema 接受非法字符串并导致任务中心 500；已用合法 `overridden` 修复数据状态，并补上代码校验
- 重启后后端日志显示测试项目相关请求均 200；仍有另一个客户端轮询旧任务 `17e4526acb13` 返回 404，和本项目无关

## 导出功能验证

- 当前产品面未发现小说 TXT、Markdown、DOCX、PDF 导出入口。
- 代码中存在 world model / Obsidian 相关导出端点，但不等同于本次要求的小说正文导出。
- 因为 60 章尚未完成，未对完整小说导出做通过判定。

## 发现的问题

| 问题 | 影响 | 状态 |
| --- | --- | --- |
| `BandCheckpointApproveRequest.status` 允许任意字符串，误传 `approved` 会写入非法 checkpoint 状态并引发任务中心 500 | 影响错误处理和状态同步 | 已修复 |
| 部分 world/reviewer v4 alias `__init__` 存在兼容导入问题 | 影响测试与兼容入口 | 已修复 |
| 前端 continue 启动响应中曾短暂显示 `requested_chapters` 与 MCP 权威 task state 不一致 | 状态展示易误导；实际任务以 MCP/task_get 为准 | 未修复 |
| 安全暂停在第 30 章开始前停住，需再单章 continue 才生成第 30 章 | 这是当前暂停语义，断点测试需理解 | 未修复，已记录 |
| 小说 TXT/Markdown/DOCX/PDF 导出入口未发现 | 导出要求未覆盖 | 未修复 |
| 60 章完整生成尚未完成 | 用户要求先停在 30 章 | 暂停中 |

## 已修复的问题

- 修复 `forwin/world_v4_review_gate/__init__.py` 自引用导入问题。
- 修复 `forwin/reviewer_v4/__init__.py` 兼容导入。
- 新增 `forwin/reviewer_v4/gate.py` 兼容转发。
- 修复 `forwin/world_v4_compat/__init__.py` 自引用导入。
- 修复 `forwin/world_model_v4/__init__.py` 兼容导入。
- 新增 `forwin/world_model_v4/bootstrap.py`, `compiler.py`, `export.py`, `projection.py`, `provisional.py`, `repository.py` 兼容转发模块。
- 收紧 `BandCheckpointApproveRequest.status` 为 `Literal["pass", "overridden"]`。
- 收紧 MCP `band_checkpoint_approve` 类型和 client 运行时校验。
- 新增非法 checkpoint status 的单元测试。
- 新增可持续运行的前端 mock 回归测试。

## 新增/修改测试文件

- `tests/browser/test_mock_book_creation_generation_regression.py`
- `tests/test_governance_decision_api.py`

说明：新增 browser 测试使用 mock backend/provider，仅用于 CI 回归验证创建小说、传递章节数、启动生成、章节列表、正文非空、刷新恢复；它不是本次真实 LLM 30 章断点实测的替代品。

## 执行过的主要命令

```bash
python3 scripts/check_codex_operator_ready.py
cd frontend/world-studio && npm ci
cd frontend/world-studio && npm run build
.venv/bin/python -m pytest tests/test_world_v4_aliases.py -q
.venv/bin/python -m pytest tests/test_codex_operator_ready.py tests/test_env_llm_profiles.py tests/test_project_operation_guards.py tests/test_generation_task_persistence.py
.venv/bin/python -m pytest tests/browser/test_home_console.py tests/browser/test_genesis_workspace.py tests/browser/test_task_center_drawer.py tests/browser/test_governance_and_chapters.py
.venv/bin/python -m pytest tests/test_governance_decision_api.py tests/test_mcp_server.py -q
.venv/bin/python -m pytest tests/browser/test_mock_book_creation_generation_regression.py -q
docker compose build forwin forwin-mcp
docker compose up -d forwin forwin-mcp
docker compose ps
docker compose logs --since 20m forwin | tail -200
date '+%Y-%m-%d %H:%M:%S %Z'
git branch --show-current
git rev-parse HEAD
uname -a
```

## 最终结论

部分通过。

本次没有用 mock 替代主流程，已通过真实前端操作和真实 DeepSeek `deepseek-chat` 生成并保存 1-30 章，章节连续、可展示、可刷新恢复，当前没有 active generation task，写作系统停在 `chapter_31_write` 断点。由于用户明确要求“跑完前三十章停一下”，第 31-60 章尚未继续生成；因此 60 章完整端到端结果不能标为通过。
