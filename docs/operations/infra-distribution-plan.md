# 三节点基础设施分配方案（A 方案 · 数据集中 150）

> 节点：**10.0.0.150**（Linux/GPU，合盖运行 24/7，primary）、**10.0.0.126**（Mac mini M4）、**10.0.0.127**（Mac mini M4）  
> 状态时间戳：2026-05-24（已经过两轮 audit 修正）

## 1. 设计决策摘要

| 决策 | 选择 | 理由 |
|---|---|---|
| 数据层 HA 策略 | **不做真集群，单实例集中 150** | mac mini 16 GB 装不下 cluster overhead；当前 SLA 不要求秒级 failover |
| 数据层备份 | 先落到 150 的 `/mnt/hdd-backup`，再视 127 是否有外接盘做二级镜像 | 127 当前没有 HDD mount，不能把它写成既定备份目标 |
| embedding 模型加载 | **唯一入口 embedding-gateway（150, CUDA）**，其它服务全调 gateway | 模型显存只占一次、版本统一、mac 服务零 GPU 依赖 |
| LLM 后端 | **exo 集群退役**，llm-gateway 降级为外部 API 转发 | exo 自 5/16 OOM 退出无人发现，业务实际不依赖 |
| 编排 | **先验证 Docker Swarm + Colima 可行性**，通过后再定 150 manager + 126/127 worker/manager 角色 | 126/127 当前无 docker/colima；Colima VM/NAT 对 swarm overlay 是硬前置风险 |
| 126 上原 shell-script 管理的服务（embedding-gateway / news-*） | **先改 endpoint，再改造为可调度 service** | 统一调度是目标，但不能在 gateway 和跨节点 endpoint 未验证前直接切 |
| GPU 服务暴露方式（**未来演进**） | 从 HTTP 同步 → redis stream 队列消费 | 复用 videoprocess 已有 `vp:tasks:*` 框架 |

## 2. 节点资源

| 节点 | OS / Arch | CPU | RAM | 磁盘 | GPU |
|---|---|---|---|---|---|
| 10.0.0.150 `ccttww-lap` | Linux x86_64 | i9-12900H · 20T | 32 GB（当前约 19 GB used / 11 GB available，但 swap 全满 → 仍需减载） | 937 GB NVMe 62% 用 + `/mnt/hdd-backup` 366 GB | RTX 3070 Ti 8 GB |
| 10.0.0.126 `CASPERs-Mac-mini` | macOS arm64 | M4 · 10c (4P+6E) | 16 GB | 228 GB（Data 约 62 GiB 可用；当前无 docker/colima） | — |
| 10.0.0.127 `Wenjies-Mac-mini` | macOS arm64 | M4 · 10c (4P+6E) | 16 GB | 228 GB（Data 约 31 GiB 可用；已清 ~/exo + ~/llama.backup-*；当前无 docker/colima） | — |

## 3. 最终服务分布

### 3.1 `10.0.0.150` — Infra + GPU + 浏览器 + 账户单点 + 调度

| 类别 | 服务 / 进程 | 备注 |
|---|---|---|
| **数据层** | postgres ×4（forwin / forwin-test / vp / shared） | 保持独立，不合并 |
| | qdrant ×2（forwin / shared） | 可考虑合并多 collection |
| | redis ×3（vp / shared_arb / shared_vp） | 同上可合并 |
| | minio ×3（forwin / vp / shared） | 同上可合并 |
| | redpanda | 单点 |
| **GPU 推理** | **embedding-gateway**（CUDA，唯一 embedding 入口）| 启用 `gpus: all` + `EMBED_DEVICE=cuda` |
| | vp_vision_worker | Whisper FP16，默认 medium（lazy 加载，最近不用，无显存压力）|
| **GPU 视频** | vp_ffmpeg_worker_go | `h264_nvenc` / `hevc_nvenc` |
| **浏览器栈** | forwin-publisher-browser、vp_platform_browser_manager (`:8898`)、vp_xiaohongshu_browser_manager (`:8897`) | chromium 容器 |
| **账户单点** | ibkr（**host network**，依赖 `127.0.0.1:5435/6379/7799/7701`）| 不能挪 |
| | arb-executor-polymarket（钱包 + VPN netns）| 不能挪 |
| **Host 进程** | `vnc-manager.service`（user systemd） | 管 Xvfb `:99` + x11vnc `:5999` + 受管 chrome（news-publisher 用），API `:7799` |
| | `forwin.codex_bridge`（`:8895`，python，host）| forwin 的 codex 桥接器 |
| | `codex-app-server.service`（user systemd，`:9234`）| Codex remote desktop |
| **Cron 调度** | `/var/spool/cron/crontabs/taiwei`（TZ=America/Los_Angeles）| 详见 §3.4 |
| **系统服务** | swarm manager #1（待 Stage 2.0 通过后启用）、samba `[forwin]` 共享、xrdp（用户保留作远程登录入口） | apache2 **保留**、mysql 先验证无人使用再关/删、x11vnc 保留（VNC Manager 依赖）|

显存预算：典型 ~2.5 GB / 8 GB；Whisper large-v3 + bge-m3 最坏 ~5 GB / 8 GB。

### 3.2 `10.0.0.126` — forwin 系 + 平台 + vp 数据流水线（Swarm 通过后全 service 化）

| 类别 | 服务 | 备注 |
|---|---|---|
| **forwin** | forwin、forwin-mcp | 现有 docker compose |
| **新闻 client**（重构）| news/collector、news/server | 当前在 126 shell-script 跑 (start.sh/stop.sh)；先改为 `EMBEDDING_GATEWAY_URL=http://10.0.0.150:8080`，Swarm 验证通过后才改 service name |
| **embedding-gateway 客户端** | 不在 126 本地（gateway 在 150）| 126 上原 shell 版本退役 |
| **平台/前端** | dashboard、llm-gateway（删除 EXO_* 配置）| llm-gateway 数据库里 exo-126/127 节点记录一并清 |
| **vp 流水线** | videoprocess-pds、event-outbox-relay、vp-feature-aggregator | |
| **系统** | swarm manager #2（仅在 Colima/Swarm 稳定性验证通过后）、nginx ingress 主、node_exporter | |

### 3.3 `10.0.0.127` — vp API/前端 + worker 池

| 类别 | 服务 | 备注 |
|---|---|---|
| **vp API/前端** | vp_api、vp_api_go、vp_frontend | 迁前必须去掉 `host.docker.internal` 假设，改成显式 150 LAN/overlay endpoint |
| **vp worker** | vp_channel_agent_runner、vp_youtube_manager | 同上；注意 OAuth/download 路径和 credentials |
| **arb 应用** | arb-resolver（改调 embedding-gateway，删除 GPU/model 本地加载）、arb-validator | 迁前必须拆掉 `network_mode: host` 和本机 `127.0.0.1` 假设；executor-polymarket 不迁 |
| **系统** | swarm worker（Stage 2.0 通过后）、nginx ingress 备、node_exporter | |

### 3.4 Cron 调度时间窗（不可忽略）

```
0:30  cycle-news-open       1:00  cycle-video-open      1:00  cycle-arb-close
6:00  cycle-video-drain     7:00  cycle-video-close     7:00  cycle-news-close
7:15  news-publisher-x-run  7:25  news-publisher-x-bot  8:00  sync-repos
19:00 cycle-arb-open        */15  schedule-status
TZ = America/Los_Angeles
日志：~/Constructure-repos/constructure-runtime-control/data/cronlogs/cronwrap.jsonl
```

**任何重启/迁移必须避开活跃时间窗**：
- 凌晨 0:30-1:30（news+video+arb open）
- 早晨 6:00-8:00（drain/close/publisher 高峰）
- 晚间 19:00-19:30（arb open）
- 每 15 分钟（schedule-status）— 影响小可不避

**推荐操作窗口**：美西 9:00-18:00（白天系统相对空闲）

### 3.5 内存预估（迁完后）

| 节点 | 当前 | 迁完后 |
|---|---|---|
| 150 | 31 / 32 GB（OOM 边缘） | ~22 GB |
| 126 | ~2 GB | ~4 GB（含 colima vm）|
| 127 | ~2 GB | ~4 GB |

## 4. 关键架构决策详解

### 4.1 embedding 统一入口

**Before**：news/collector、news/server、arb/resolver 各自加载 sentence-transformers，每个进程占 500-600 MB。

**After**：只有 150 上 embedding-gateway 加载（CUDA），所有 client 通过 HTTP 调它。

改动清单：
- **embedding-gateway**：迁 150，docker compose 加 `gpus: all`，env `EMBED_DEVICE=cuda`；当前 126 上 shell 版本停掉
- **news/collector**：先用 `EMBEDDING_GATEWAY_URL=http://10.0.0.150:8080`；Swarm overlay 验证通过后才改 `http://embedding-gateway:8080`
- **news/server**：同上
- **arb/resolver**：embedder 改调 gateway；删除本地 GPU/model 依赖；整体迁 127 前先完成 `network_mode: host` / `127.0.0.1` endpoint 改造

### 4.2 GPU 服务暴露：当前 HTTP，未来队列化

当前 HTTP `/embed` 同步调用。未来接入 redis stream（复用 videoprocess `vp:tasks:*` 模式）：

```
client → XADD vp:tasks:embed {...}
              │
              ▼
  embedding-worker (consumer group)
   - 按 GPU 容量限制 concurrency
   - 批处理 batch inference（吞吐 +5-10×）
   - 写回 result stream + 通知
              │
              ▼
client ← XREAD vp:results:embed
```

embedding-gateway 做"双协议"：保留 HTTP 兼容现有调用方，逐步把高频 client 切到队列。

### 4.3 漏掉过的关键依赖（已补回）

- **VNC Manager 体系**：`vnc-manager.service` 是用户级 systemd unit（`~/.config/systemd/user/` 或类似），管 Xvfb:99 + x11vnc + chrome；ibkr 通过 `VNC_MANAGER_URL=http://127.0.0.1:7799` 用它，news-publisher chrome 也是它启动的
- **ibkr `network_mode: host`**：依赖 `127.0.0.1:5435`(postgres) / `:6379`(redis) / `:7799`(VNC Manager) / `:7701`(自己 API) / `:4001`(IB Gateway TWS)。**永远绑 150**，不能用 swarm overlay
- **cron 调度**：见 §3.4
- **forwin.codex_bridge**（`:8895`）和 codex-app-server（`:9234`）：开发工具链一部分，留 150

### 4.4 跨节点 endpoint 矩阵（迁移前置门）

不能把 150 上的 `host.docker.internal` / `127.0.0.1` 配置直接搬到 126/127。Colima VM 里的 `host.docker.internal` 指向 Mac/VM 侧，不会自动指向 150。

| 依赖 | 当前典型写法 | 迁移后规则 |
|---|---|---|
| Postgres / Redis / MinIO / Qdrant | `host.docker.internal:*` 或 `127.0.0.1:*` | 若数据层继续在 150，先改为 `10.0.0.150:<port>`；只有同一个 swarm overlay 内才可用 service name |
| embedding-gateway | 126 本地 `127.0.0.1:8080` / `192.168.20.1:8080` | 先统一为 `http://10.0.0.150:8080`，Swarm 验证通过后再改 `http://embedding-gateway:8080` |
| VP frontend nginx | `host.docker.internal:18080/8897/8898` | 迁 127 前改成 env-driven upstream；确认 API/browser manager 是本机 service、150 LAN endpoint，还是 overlay service |
| browser/CDP | `127.0.0.1:9222`、`:8897/:8898` | 150 上保留浏览器状态；跨机调用走显式 endpoint 或 overlay，不再假设 localhost |
| arb resolver/validator | `network_mode: host` + 本机 redis/postgres/qdrant/LLM | 迁 127 前先完成 endpoint 参数化；executor-polymarket 和钱包/VPN netns 留 150 |
| dashboard / llm-gateway | 150 host network + EXO_* 残留 | 先删除 EXO_*；再把 dashboard/llm-gateway 的 DB/API endpoint 显式化后迁 126 |

每个服务迁移批次必须附带一张 endpoint checklist：旧值、新值、连通性测试命令、回滚值。

## 5. 执行计划

### 阶段 1：减载与本机清理（今天，~1 小时，低风险但非零风险）

```bash
# 1.0 preflight：必须避开 §3.4 时间窗；先记录现场
date
free -h
df -h / /mnt/hdd-backup
docker ps --format '{{.Names}}\t{{.Status}}\t{{.Mounts}}' > /tmp/docker-ps-before-cleanup.tsv
docker system df -v > /tmp/docker-system-df-before-cleanup.txt
docker volume ls > /tmp/docker-volumes-before-cleanup.txt

# 1.1 docker 清理 —— 不带 --volumes
docker system prune -af                 # 清未用镜像 + stopped 容器 + 部分 build cache
docker builder prune -af                # 单独清 build cache

# 1.2 volume 白名单策略
# 用户确认：只保留 arb 盘口匹配数据和对应新闻数据。
# 白名单：arb_qdrant-data、news_news-pgdata
# 其它 volume 没有数据保留需求。
KEEP_RE='^(arb_qdrant-data|news_news-pgdata)$'

# 先删未被容器使用的 volume；这一步不会动正在运行服务挂载的 volume。
docker volume ls -q \
  | grep -Ev "$KEEP_RE" \
  | while read -r v; do
      if docker ps -a --filter "volume=$v" --format '{{.Names}}' | grep -q .; then
        printf 'skip in-use volume: %s\n' "$v"
      else
        docker volume rm "$v"
      fi
    done

# 后续迁移/停服务时，仍按同一白名单删除服务退役后释放出来的 volume。
# 对仍在使用的 volume，不在本阶段强行 stop/remove owning container。

# 1.3 mysql：先证明无人使用，再停/删
systemctl is-active mysql || true
systemctl is-enabled mysql || true
ss -ltnp | grep -E ':(3306|33060)\b' || true
grep -RIn --exclude-dir=.git --exclude-dir=node_modules --exclude-dir=.venv \
  -E 'mysql|3306|33060' ~/Constructure ~/Constructure-repos ~/ForWin 2>/dev/null | head -100

# 如果没有监听端口、没有运行依赖、没有需要保留的 DB：
sudo systemctl disable --now mysql
# 可选：确认不需要包和数据目录后再 purge，不和本阶段强绑定。
# sudo apt-get purge -y 'mysql-*'
# sudo rm -rf /var/lib/mysql
# 保留 apache2、xrdp、x11vnc(非 systemd)、samba

# 1.4 清 llm-gateway exo 残留
# 编辑 ~/Constructure-repos/constructure-llm-infra/llm-gateway/docker-compose.yml 或 .env
# 删除：EXO_ENDPOINTS / EXO_SHARDING / LLM_GATEWAY_REMOTE_HOSTS_JSON
#       LLM_GATEWAY_SSH_IDENTITY_FILE / LLM_GATEWAY_SSH_KNOWN_HOSTS
docker compose -f .../llm-gateway/docker-compose.yml up -d --force-recreate
# 验证 /var/log 不再每 15 秒打 'exo cluster unreachable'

# 1.5 验证
free -h
df -h / /mnt/hdd-backup
docker system df
docker volume ls
systemctl --failed
```

**执行时机**：避开 cron 时间窗（见 §3.4），推荐美西 9:00-18:00。

### 阶段 2：节点准备与 Swarm/Colima 可行性验证（半天）

**Stage 2.0 是硬门槛**：126/127 当前没有 docker/colima。先只拿 126 做 proof，不直接把生产服务迁进去。

```bash
# 2.0.1 126 先装运行时
brew install colima docker docker-compose docker-buildx
colima start --cpu 8 --memory 12 --disk 80 --arch aarch64 --vm-type vz
docker context use colima
docker info

# 2.0.2 网络连通性验证
# swarm 需要至少验证这些端口在 150 <-> Colima VM/节点之间可达：
# 2377/tcp(manager), 7946/tcp+udp(gossip), 4789/udp(VXLAN overlay)
# 如果 Colima/NAT 让这些端口不可达，停止 Swarm 方案，回退到 SSH + compose/systemd 编排。

# 2.0.3 150 启用 swarm proof
docker swarm init --advertise-addr 10.0.0.150
# 复制输出的 worker 和 manager token

# 2.0.4 126 先以 worker 加入，不立刻升 manager
docker swarm join --token <WORKER-TOKEN> 10.0.0.150:2377

# 2.0.5 部署最小 hello service，验证 overlay DNS、placement、跨节点请求
docker network create --driver overlay --attachable backend
docker node update --label-add role=primary --label-add gpu=true <150>
docker node update --label-add role=app <126-hostname>
docker service create \
  --name swarm-hello \
  --network backend \
  --constraint 'node.labels.role == app' \
  --publish published=18088,target=80 \
  nginx:alpine
docker service ps swarm-hello
curl -fsS http://10.0.0.150:18088/ >/dev/null
docker service rm swarm-hello

# 2.0.6 只有 proof 通过后，才把 127 加入
# 127 执行同样 brew/colima/docker context 步骤后：
docker swarm join --token <WORKER-TOKEN> 10.0.0.150:2377
docker node update --label-add role=worker <127-hostname>

# 126 是否升 manager 单独决定：
# 只有在 Colima VM 自启动、重启后 node identity 稳定、manager quorum 行为验证后才 promote。
# docker node promote <126-hostname>
```

**Stage 2.0 失败时的 fallback**：保留 150 为生产控制面；126/127 用 launchd/systemd + SSH 脚本运行容器或原生服务，统一由 150 cron/runtime-control 调度，不把 Swarm 当硬依赖。

### 阶段 3：迁移（1-2 周，渐进）

**3.1 embedding 收口**
1. 在 150 启动 embedding-gateway docker 镜像，`gpus: all` + `EMBED_DEVICE=cuda`，验证 `/health` 返回 `device: cuda`
2. news/collector 在 126 加 `EMBEDDING_GATEWAY_URL=http://10.0.0.150:8080`，重启验证
3. news/server 同上
4. 150 gateway 连续通过 `/health` 和一次真实 embed 请求后，**停掉 126 上 shell 版本**：`cd ~/Constructure/services/embedding-gateway && ./stop.sh`
5. Swarm proof 通过后，news/collector 和 news/server 才改成 `http://embedding-gateway:8080`
6. arb-resolver 改 embedder 代码调 gateway，删本地 GPU/model 加载分支
7. 验证：126/127 上无 torch/sentence-transformers 常驻进程；`nvidia-smi` 看到 150 的 embedding-gateway 进程显存

**3.2 126 shell 服务 → service 化**
1. 给 news-collector / news-server 写 Dockerfile（如尚无）；embedding-gateway 的生产实例在 150
2. `docker buildx build --platform linux/amd64,linux/arm64 --push` 推到内网 registry（或 swarm 节点本地构建）
3. 写 swarm-compatible compose（`deploy.placement.constraints` 限定 role==app）；如果 Swarm proof 失败，则写 launchd/compose 版本
4. `docker stack deploy -c news-stack.yml news`（或 fallback compose/launchd）
5. 老的 shell 进程 `stop.sh` 停掉，`cycle-news-open/close` 从 remote shell start/stop 改成 service scale/start/stop 或健康检查 URL

**3.3 应用层迁移**（按业务域分批，每批避开 cron 窗口）
1. forwin 系：forwin、forwin-mcp → 126
2. dashboard、llm-gateway → 126；llm-gateway 先删除 EXO_* / remote host 配置，改为外部 API 转发
3. videoprocess 流水线：pds、event-outbox-relay、feature-aggregator → 126
4. videoprocess API/前端：vp_api、vp_api_go、vp_frontend → 127；先完成 frontend nginx 和 API env 的 endpoint 参数化
5. videoprocess worker：channel-agent-runner、youtube-manager → 127；先确认 OAuth credentials、download/storage、browser manager endpoint
6. arb：arb-resolver、arb-validator → 127；executor-polymarket、钱包和 VPN netns 留 150

**3.4 入口 + 备份**
1. nginx ingress 部署到 126/127（mode: global）
2. cron：nightly `pg_dump` / `mc mirror` / qdrant snapshot 先落到 `10.0.0.150:/mnt/hdd-backup/constructure-backups`
3. 如果 127 后续接入外置盘，再从 150 mirror 到 127；不要写死“127 hdd 路径”
4. 每类备份至少做一次 restore drill：Postgres restore 到临时库、MinIO mirror 读回、Qdrant snapshot restore 到临时 collection

### 阶段 4（未来）：GPU 服务队列化

参考 §4.2，复用 videoprocess worker 框架。

## 6. 跨机调用要点

mac 上应用调 150 上数据层 / GPU / chromium 时：
1. **代码审计**：grep 所有 `localhost`、`127.0.0.1:9222`、`127.0.0.1:8897/8898` 等 hardcode，改 env var
2. **数据层 endpoint**：跨机服务不能继续用 `host.docker.internal`；Swarm 通过前统一用 `10.0.0.150:<port>`，Swarm 通过后再逐个改 service name
3. **绑定**：`forwin-publisher-browser` 的 `127.0.0.1:9222` 只有在同 overlay 内才可不暴露宿主；否则用受控 LAN endpoint 或 SSH tunnel
4. **文件路径**：浏览器下载/截图改走 minio bucket
5. **chrome user-data-dir**：留 150 docker volume，cookies 通过 CDP 读
6. **VNC 调试**：`ssh -L 5999:127.0.0.1:5999 10.0.0.150`，mac 端连 localhost:5999
7. **chromium GPU 加速**：headless 模式加 `--disable-gpu`，省显存

## 7. 风险与注意事项

1. **Swarm/Colima 未验证**：这是 Stage 2.0 的硬门槛；overlay/ingress 失败就回退 SSH + compose/launchd
2. **arm64 镜像**：自建镜像 `docker buildx --platform linux/amd64,linux/arm64`；第三方先验 `docker manifest inspect`
3. **150 单点**：数据/GPU/账户/浏览器全在 150，挂了系统全停 → 接受 trade-off，靠备份 + 监控弥补
4. **跨节点 endpoint**：`host.docker.internal` 和 `127.0.0.1` 迁机后语义会变；每批迁移必须先完成 endpoint checklist
5. **跨节点延迟**：千兆/Thunderbolt 0.6-2 ms 可忽略；CDP chatty 调用放大 10-20%
6. **cron 时间窗**：所有重启/迁移避开 §3.4 列出的时间
7. **ibkr host network**：永远绑 150，依赖 `127.0.0.1:*` 一堆端口
8. **第三方账户类**（ibkr / polymarket）：保持单实例，HA = 风控
9. **150 笔记本电源**：合盖运行需电源管理不休眠（当前已是）
10. **secret 管理**：迁移时各 service 的 `.env` / `secrets/` 不要被 prune 清掉；先 git/手动备份
11. **volume 删除**：只保留 `arb_qdrant-data` 和 `news_news-pgdata`；其它 volume 可删，但在用 volume 需要先停/退役 owning container
12. **回滚**：每阶段前打 git tag；删 volume 不可逆，靠白名单和 preflight 记录控制风险
13. **VNC Manager 重启**：会重启所有受管 chrome（含 news-publisher），影响 cron 任务 — 避开 7:15/7:25 publisher 窗口

## 8. 可选的家目录清理（用户独立决定，与本计划解耦）

| 项目 | 大小 | 状态 | 建议 |
|---|---|---|---|
| openclaw-source | 1.4 GB | 2 个月没动 | 可归档 |
| truth-monitor | 53 MB | 4 个月没动 | 可归档 |
| ibgateway | 227 MB | 跟 Jts 重复？ | 看 |
| home_bak / molthumanside / tmpt / output / forwin-worktree-backups | 各 <500 MB | 旧 | 可清 |
| **venv** (32 GB) | 3 个月没动 | 保留（用户决定） | — |

## 9. TODO 进度

- [x] 127 清理 `~/exo` 和 `~/llama.backup-*`
- [x] 确认 exo 已退役（5/16 OOM 退出）
- [x] Audit：xrdp/apache2 保留、mysql 需先证明确认无人使用、x11vnc 保留、cron 时间窗、VNC Manager 体系、ibkr host network、volume 白名单归类
- [ ] **阶段 1**：docker prune（不带 `--volumes`）+ 按白名单删可删 volume + 验证/关闭 mysql + 清 llm-gateway exo 配置
- [ ] **阶段 2.0**：126 单机 Colima + Swarm proof（overlay/ingress/placement/重启稳定性）
- [ ] **阶段 2.1**：Stage 2.0 通过后再让 127 加入；失败则回退 SSH + compose/launchd
- [ ] **阶段 2.2**：按结果决定 126 是否升 manager，避免未验证 Colima VM 进入 manager quorum
- [ ] **阶段 3.1**：embedding-gateway 启用 CUDA + news/arb 改调 gateway
- [ ] **阶段 3.2**：126 上 news shell 服务改造为 service（Swarm 或 fallback）
- [ ] **阶段 3.3**：补齐 endpoint checklist 后按业务域迁 mac（含 dashboard/llm-gateway）
- [ ] **阶段 3.4**：nginx ingress + nightly 备份到 `/mnt/hdd-backup` + restore drill
- [ ] **阶段 4**：GPU 服务队列化（未来）
