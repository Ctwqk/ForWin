# 三节点生产运行分工（当前状态）

> 状态时间戳：2026-06-27
> 目标：应用层迁出 150，基础设施层 / GPU / 浏览器账户单点留在 150，源码入口以 GitHub 为准，126/127 只保留部署输出。

## 1. 机器分工

| 机器 | 角色 | 当前职责 |
| --- | --- | --- |
| `10.0.0.150` `ccttww-lap` | Swarm manager + shared infra + GPU/account node | Postgres、Redis、MinIO、Qdrant、Redpanda bridge、embedding-gateway、dashboard、IBKR、VNC/browser state、部署同步 |
| `10.0.0.126` `CASPERs-Mac-mini.local` | ForWin/news app node | `forwin-app-swarm`、`forwin-mcp-swarm`、`news-server-swarm`、按窗口启停的 `news-collector-swarm` |
| `10.0.0.127` `Wenjies-Mac-mini.local` | VideoProcess/PDS/arb app node | VP frontend/API/worker、PDS、VP feature aggregator、arb resolver/validator、Polymarket executor 的 Swarm 管理面 |

`Constructure` 作为运行时总控概念只保留在 150。126/127 上只保留项目自己的部署目录，不再建立新的 `Constructure` 工作根。

## 2. ForWin 代码和部署边界

| 类型 | 位置 | 说明 |
| --- | --- | --- |
| 源码入口 | GitHub `Ctwqk/ForWin` `master`，本机 fresh clone / isolated worktree | 建 Codex project、改 ForWin 代码、改 ForWin 数据库模型/迁移/业务逻辑都从这里开始 |
| 生产部署目录 | `10.0.0.126:/Users/magi1/ForWin-swarm` | 由 150 deploy sync 写入，不是源码工作区 |
| 生产 URL | `http://10.0.0.126:8899` | ForWin UI/API |
| MCP 端口 | `10.0.0.126:8896` | `forwin-mcp-swarm` |
| Swarm 服务 | `forwin-app-swarm`、`forwin-generation-worker-swarm`、`forwin-mcp-swarm`、`forwin-publisher-worker-swarm`、`forwin-outbox-worker-swarm`、`forwin-publisher-browser-swarm` | 由 150 Swarm manager 管理 |

ForWin 机器上没有 ForWin 自己的 Postgres/MinIO/Qdrant，并不代表代码里没有数据库相关内容。仓库仍然包含数据库模型、迁移、存储层、配置和本地 compose profile；只是生产运行时连接 150 提供的数据层。

## 3. 150 提供的共享基础设施

| 能力 | 当前生产 endpoint | 说明 |
| --- | --- | --- |
| PostgreSQL | `10.0.0.150:5435` | ForWin、VP、PDS、arb、news、IBKR 等共用单实例，多库/多 role 隔离 |
| Qdrant | `http://10.0.0.150:6333`，gRPC `10.0.0.150:6334` | 向量检索和匹配 |
| Redis for arb | `redis://10.0.0.150:6379` | arb 热路径状态 |
| Redis for VP | `redis://10.0.0.150:6380` | VP 队列和调度状态 |
| MinIO | API `http://10.0.0.150:9000`，console `http://10.0.0.150:9001` | 共享对象/产物存储 |
| Redpanda | overlay `redpanda:9092`，host bridge `10.0.0.150:19092` | PDS / aggregator / event relay |
| Embedding gateway | `http://10.0.0.150:8080` | CUDA embedding 唯一入口 |
| Dashboard | 150 本机 `127.0.0.1:7700` | 通过 SSH tunnel 访问 |
| IBKR | API `127.0.0.1:7701`，Gateway `127.0.0.1:4001`，VNC `127.0.0.1:5999` | 150-only，不能直接迁到 126/127 |

生产规则：126/127 不启动 Postgres、Redis、MinIO、Qdrant。应用代码可以包含这些组件的本地开发配置，但生产连接必须指向 150 infra 或 Swarm overlay service。

## 4. 当前 Swarm 服务

| 服务 | 目标机器/角色 | 常态副本 |
| --- | --- | --- |
| `forwin-app-swarm` | 126 ForWin app | `1/1` |
| `forwin-mcp-swarm` | 126 ForWin MCP | `1/1` |
| `news-server-swarm` | 126 news API | `1/1` |
| `news-collector-swarm` | 126 news collector | 按窗口启停，可为 `0/0` |
| `vp-frontend-swarm` | 127 VP frontend | `1/1` |
| `vp-api-swarm` | 127 VP API | `1/1` |
| `vp-channel-agent-runner-swarm` | 127 VP worker | `1/1` |
| `vp-event-outbox-relay-swarm` | 127 VP event relay | `1/1` |
| `vp-pds-swarm` | 127 PDS | `1/1` |
| `vp-feature-aggregator-swarm` | 127 aggregator | `1/1` |
| `arb-resolver-swarm` | 127 arb matching | 按 arb window scale |
| `arb-validator-swarm` | 127 arb validation | 按 arb window scale |
| `arb-executor-polymarket-swarm` | 150 wallet/VPN execution | `1/1` |
| `redpanda` | Swarm/150 bridge | `1/1` |

## 5. GitHub deploy sync

150 上的定时任务负责把 GitHub 最新代码同步到生产部署目录：

```bash
/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply
```

ForWin 的 GitHub sync 以 `Ctwqk/ForWin` `master` 为源码入口。部署时必须同时构建
`forwin-forwin:deploy-<commit>` 和
`forwin-publisher-browser:deploy-<commit>`；后者使用 Dockerfile 的
`publisher-browser-runtime` target。服务更新范围必须包含
`forwin-publisher-browser-swarm`，否则浏览器扩展代码会落后于后端和 worker。

当前项目映射：

| 项目 | GitHub 分支 | 部署目标 |
| --- | --- | --- |
| ForWin | `Ctwqk/ForWin` `master` | `10.0.0.126:/Users/magi1/ForWin-swarm` |
| VideoProcess | `Ctwqk/videoprocess` `main` | `10.0.0.127:/Users/wenjieliu/VideoProcess-app` |
| PDS | `Ctwqk/policy-decision-service` `main` | `10.0.0.127:/Users/wenjieliu/.deploy-build/policy-decision-service` |
| VP feature aggregator | `Ctwqk/vp-feature-aggregator` `main` | `10.0.0.127:/Users/wenjieliu/.deploy-build/vp-feature-aggregator` |
| Arb | `Ctwqk/arb` `main` | `10.0.0.127:/Users/wenjieliu/arb-swarm-src` |
| News | `Ctwqk/news` `main` | `10.0.0.126:/Users/magi1/Constructure/news` |

目标目录里的 `.deploy-sync-project` 和 `.deploy-sync-source-commit` 是部署证明。不要把这些目录当作长期开发工作区。

## 6. Codex project 建议

| 如果要改 | 建 Codex project 的位置 |
| --- | --- |
| ForWin | clone GitHub `Ctwqk/ForWin` 到本机源码目录，或使用隔离 worktree；不要在 `10.0.0.126:/Users/magi1/ForWin-swarm` 里长期改源码 |
| VideoProcess | clone GitHub `Ctwqk/videoprocess`，或使用 `10.0.0.150:/home/taiwei/Constructure-repos/videoprocess`；`10.0.0.127:/Users/wenjieliu/VideoProcess-app` 是部署输出 |
| PDS | `10.0.0.150:/home/taiwei/Constructure-repos/policy-decision-service` |
| VP feature aggregator | `10.0.0.150:/home/taiwei/Constructure-repos/vp-feature-aggregator` |
| Arb | `10.0.0.150:/home/taiwei/Constructure-repos/arb` |
| News | 建议单独 clone `Ctwqk/news`；当前 deploy mirror 在 `10.0.0.150:/home/taiwei/deploy-github-sync/repos/news` |
| IBKR | `10.0.0.150:/home/taiwei/Constructure-repos/ibkr` |
| Dashboard / shared infra | `10.0.0.150:/home/taiwei/Constructure-repos/constructure-runtime*` |

## 7. 运维检查

```bash
# Swarm 节点和服务
ssh 10.0.0.150 'docker node ls && docker service ls'

# ForWin 浏览器检查
open http://10.0.0.126:8899

# ForWin 2 小时只读运行看护（不会发布或写业务状态）
python scripts/monitor_forwin_runtime.py \
  --api-base http://10.0.0.126:8899 \
  --mcp-url http://10.0.0.126:8896/mcp \
  --mcp-health-url http://10.0.0.126:8896/health \
  --docker-context swarm-manager-150 \
  --colima-profile swarmbridged \
  --expect-platform-connected fanqie \
  --expect-platform-connected qidian \
  --duration-minutes 120

# News health
curl http://10.0.0.126:6551/health

# VP 浏览器/API 检查
open http://10.0.0.127:3001
curl http://10.0.0.127:18080/api/v1/node-types

# IBKR dashboard/VNC tunnel
ssh -L 7700:127.0.0.1:7700 -L 5999:127.0.0.1:5999 10.0.0.150
open http://127.0.0.1:7700
```

## 8. 调度窗口

150 上仍有分时运行机制。重启/迁移应避开：

- `0:30-1:30` news/video/arb open
- `6:00-8:00` drain/close/publisher 高峰
- `19:00-19:30` arb open

普通文档更新不受这些窗口限制；生产服务重启、Swarm scale、Colima restart 要避开这些窗口。
