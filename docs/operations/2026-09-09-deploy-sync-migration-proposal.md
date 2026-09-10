# 150 部署入口的迁移接入调查

日期：2026-09-09。状态：调查后已准备[源码接入、150 patch 与隔离测试](../../deploy/swarm/README.md)，待独立审查；尚未应用到远程或部署。依据是[当前三阶段设计](../superpowers/specs/2026-09-09-forwin-three-stage-design.md)和[旧基线迁移说明](legacy-forward-migration.md)。以下保留调查时的入口证据和接入边界，实际操作约定以源码接入说明为准。

结论：现有 150 入口不能直接安全部署本轮迁移。它既没有传入完整源码 revision，也没有停妥、备份、迁移阶段。最小维护方案是在 ForWin 源码仓库拥有一个部署扩展，由 150 的固定入口加载；150 只增加明确的 ForWin 接入点，最终操作命令保持不变：

```bash
ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'
```

本调查没有修改远程脚本、服务、数据库或部署输出，也没有执行构建和部署。

## 已核对的入口与限制

调查对象是 150 上 `/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh`，文件 SHA256 为 `45bee197b1d3bcadef33981472f59bd279b9afd0ef8248b6fbe5c3a513892fae`。以下行号对应该快照；执行前应重新核对脚本身份。

| 证据位置 | 当前行为 | 对本轮的影响 |
| --- | --- | --- |
| 19–43，100–117 | 有 apply/dry-run、build-only、sync-only、no-build/no-update/no-health、ROOT/ENV_FILE 覆盖和统一 flock；没有 ForWin hook 或 migration 参数 | 单靠现有参数、环境文件不能补齐迁移顺序 |
| 173–205，777–806 | ForWin 拉取 `Ctwqk/ForWin` 的 master；VideoProcess 已有仓库内 `deploy/swarm/deploy-sync-extension.sh` 加载机制，ForWin 没有 | 可采用已有的“固定调度器加载仓库扩展”模式，但必须显式新增 ForWin 接入 |
| 212–226，299–349 | 从无 `.git` 的 staging 同步到 Mac 部署输出；成功部署后写源码 marker，no-update 不写 marker | 构建身份应来自已冻结的仓库 HEAD，不能从目标目录 Git 或旧 marker 推断 |
| 352–394 | app/browser 标签只有提交前 12 位；宿主 Docker 和 Colima 回退构建都没有 source build arg | Dockerfile 的 OCI revision 和运行时 revision 会保持 `unknown` |
| 450–510，546–562 | 保留既有 ServiceSpec，依次滚更；app/MCP 使用 start-first | 新旧 schema 的写入角色可能重叠；旧环境变量继续覆盖新镜像 |
| 507–513，704–707 | ForWin 更新失败后无条件 rollback 六个服务 | 迁移已提交后可能重新启动不兼容的旧代码 |
| 534–562 | ForWin 验证只看任务出现 Running | 没有证明 HTTP 就绪、数据库版本、依赖解释器和源码身份一致 |

远程 `overlays/deploy_github_sync.sh` 与正在执行的 bin 文件不同；调查时该目录也没有可识别的 Git worktree。接入修改须针对核验后的活动脚本，不把 overlay 或历史备份猜成权威版本。环境文件在函数定义前被 source，不应借助 shell 环境注入来覆盖函数或执行迁移。

## 六个角色的 Python 选择

调查时六个服务均为单副本，所有 ContainerSpec.Command 为空，停止宽限期为 10 秒。下表只列运行时选择相关的非敏感字段。

| 服务后缀 | 保存的 Args 或镜像默认命令 | 当前健康检查的解释器 |
| --- | --- | --- |
| app | Args 为空；候选镜像默认 `uvicorn forwin.api:app …` | ServiceSpec 禁用健康检查 |
| mcp | `python -m uvicorn forwin.mcp.http:app …` | 裸 `python` |
| generation-worker | `python -m forwin.cli -v generation-worker …` | 禁用 |
| publisher-worker | `python -m forwin.cli -v publisher-worker …` | 禁用 |
| outbox-worker | `python -m forwin.cli -v outbox-worker …` | 禁用 |
| publisher-browser | Args 为空；候选镜像默认 `bash -c 'exec scripts/launch_linux_extension_browser.sh'` | 裸 `python` |

六个 ServiceSpec 都保存了同一个旧 PATH：

```text
/usr/local/bin:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
```

它缺少 `/app/.venv/bin`，并会覆盖 [Dockerfile](../../Dockerfile) 的镜像 PATH。镜像将依赖安装在 `.venv`，因此仅更新 image 无法保证裸 python/uvicorn 选择锁定依赖。服务 Args 中没有保存的 `bash -lc`，不需要针对这种不存在的配置增加兼容分支。

最小统一处理是在更新六个 ServiceSpec 时移除旧的 `PATH` 覆盖（Docker 的 `--env-rm PATH`），让镜像提供 PATH，然后检查实际容器的 `sys.executable` 和关键依赖导入。迁移命令直接使用 `/app/.venv/bin/python -m forwin.migrations`。如果以后将角色启动统一为源码 entrypoint，应由一个公共入口解析现有命令；本次 PATH 纠正不需要六套 wrapper。

六个 ServiceSpec 均未设置 `FORWIN_EXTENSION_PYTHON`。候选 browser 镜像已设置 `/app/.venv/bin/python`，[browser launcher](../../scripts/launch_linux_extension_browser.sh) 的 `find_python` 会优先使用它。因此 browser 主脚本具备正确的显式解释器路径；现存 browser 健康检查仍依赖 PATH，不能遗漏统一修正。

## 最小接入结构

建议新增源码文件 `deploy/swarm/deploy-sync-extension.sh`，由它拥有完整 ForWin 发布阶段、身份验证、迁移与失败恢复。不要向运行容器复制另一套源文件，也不要添加运行时字符串替换。

150 活动脚本只需接入三处职责：在 ForWin `prepare_repo` 后加载该扩展；在 ForWin `deploy_project` 分支调用扩展控制的发布流程；移除该分支对通用 `rollback_services` 的无条件调用。其他项目的部署行为继续由现有 owner 管理。扩展应复用现有 flock、日志、状态记录和目标同步约定，并由测试检查接口契约。

发布流程需要持久记录以下阶段：`built → quiesced → backed_up → migration_started → migrated → verified → deployed`。记录完整 source commit、app/browser image ID 或 digest、迁移前后版本、备份标识与校验、旧服务规格的受保护快照以及阶段结果。记录中不输出连接凭据；实际 ServiceSpec 快照需要受限存储，公开证据只保留非敏感字段。发生中断后先读取阶段和数据库迁移版本，不能只按进程退出码推断事务是否提交。

### 构建与身份

1. 从刚拉取并确认干净的 ForWin master 冻结完整 40 位 HEAD；两次构建使用同一个值。在宿主 Docker 和 Colima 回退两条路径都传 `--build-arg FORWIN_SOURCE_REVISION=<完整提交>`，保留锁定的 Dockerfile 和 `uv.lock`。
2. 在停服务前完成两个完整镜像的构建和隔离检查。核对 OCI revision、镜像环境中的 revision、实际 image ID、迁移模块和关键 runtime imports；任何 `unknown` 或不一致都在此拒绝。
3. 冻结实际镜像身份。标签仍可用于发现，但后续迁移与六个角色验收必须证明使用刚验证的 image ID/digest。不要重新构建同名标签后沿用之前的验收结果。

### 停妥、备份与迁移

1. 先通过支持的 ForWin MCP/运营接口读取活跃任务，使用 `task_pause` 并确认安全停止；保存并暂停会继续产生任务的自动化设置。外部发布需停止新 claim，并让已越过外部动作边界的 attempt 完成回执或保留可恢复的未知状态；数据库冻结事实和浏览器 journal 都必须保留。不得把超时当作外部未发生。
2. 关闭新的写入入口并等待现有工作收敛，再将六个角色停至零副本、等待所有任务实际退出。保留原副本数及完整旧规格。单次 idle 查询与 Docker 默认 10 秒 stop grace 均不足以证明停妥。
3. 当前源码没有部署级 admission/claim drain 开关。API 在 [HttpRuntime.startup](../../forwin/http/runtime.py) 中无条件启动 30 秒 automation loop；generation/outbox 的 heartbeat stop event 不是全局停止接单机制。因此不能把 `service scale 0` 描述为已有的优雅 drain。首次切换可以由支持的运营暂停、入口隔离和可验证 idle 门共同完成；若不能证明无新工作和外部动作已停止，部署扩展必须在迁移前拒绝。持续自动化部署需要一个共享维护协调入口，而非各角色的临时兼容逻辑。
4. 停妥后，用冻结候选镜像创建一次性维护任务，继承必要数据库配置、目标节点/网络与持久卷引用，使用绝对 `.venv` Python；关闭该任务的应用健康检查和自动重启。维护任务不发布 API 端口，不启动 API scheduler，也不启动 worker。
5. 使用 [backup_forwin_data.py](../../scripts/backup_forwin_data.py) 产生新备份，再运行 [verify_forwin_backup.py](../../scripts/verify_forwin_backup.py) 验证清单、hash 和 `pg_restore --list`。在受限发布记录中额外写入完整 source/image 身份：备份脚本在不含 `.git` 的镜像里得到的 `git_commit` 可能为空。已有隔离恢复验证不能替代停妥后的新备份；完整恢复测试只指向独立空测试库。若运行使用 MinIO/Qdrant，按脚本说明保留对应数据备份或已核验的可重建边界。
6. 同一候选镜像、同一数据库配置下执行 `/app/.venv/bin/python -m forwin.migrations`，等待唯一维护任务确定退出，并检查实际迁移 head。旧库不能用单独 `alembic upgrade head` 或 stamp 代替统一入口。

旧桥接在 [0002_recovery_bridge.py](../../forwin/legacy_migrations/versions/0002_recovery_bridge.py) 修改前锁定旧表，并拒绝 queued/running/capacity_wait generation 和 running/processing outbox。仅停进程、等待 lease 超时不会自动改变这些行，迁移仍会拒绝。该拒绝是恢复边界；应让旧 owner 通过支持流程处理后重试，不能临时 SQL 改状态。整条桥接与主迁移链由 [run_migrations](../../forwin/models/base.py) 的一个事务覆盖。

### 启动与失败边界

在零副本状态更新六个服务的候选 image 和 PATH 清理，然后按依赖顺序启动、核对实际 image ID、源码 revision、schema 与端口健康。恢复写入入口、项目自动化与外部 claim 必须处于受控的最后阶段；API 启动会创建 scheduler，应保留此前暂停的自动化设置，不能把 API 就绪误认为生产仍自动暂停。

worker/browser 还需验证真实运行进程的 `.venv` 解释器、依赖和 heartbeat；不能只看 Swarm 的 Running。成功后才恢复原副本数/运营设置、写 `deployed` 和 `.deploy-sync-source-commit`。没有确定通过的角色不得写成功 marker。

| 最后确定阶段 | 允许的失败处理 |
| --- | --- |
| 构建、隔离检查失败，尚未停妥 | 退出，旧服务继续；不更新成功 marker |
| 停妥或备份失败，尚未开始迁移 | 依据保存的原规格和运营状态恢复；不要依赖经过多次 update 后的单级 service rollback |
| 已开始迁移、提交结果未知 | 保持角色停止，核对数据库 head/事务结果；不可盲目启动旧镜像或重跑外部动作 |
| 明确迁移失败并完整回滚 | 确认旧 schema 后恢复旧规格；保留失败证据与备份 |
| 迁移已提交，任一新角色启动或健康失败 | 保持生产写入/外部发布暂停，保留数据库和 journal，向前修复；禁止通用 service rollback 自动启动旧代码 |

数据库恢复属于单独的受控恢复决定，需要同时核对迁移后的写入和外部发布事实。本接入不能将自动丢弃新数据、重建生产库或恢复旧备份伪装为镜像回滚。

## 接入验收证据

实现接入后至少用隔离环境证明：两条构建路径收到完整 revision；六个旧 PATH 覆盖被统一移除且命令语义不变；活跃任务拒绝迁移；备份失败时不执行迁移；迁移失败时事务回滚；提交结果未知时保持停止；迁移已提交后的启动失败不触发旧镜像 rollback；重试能从持久阶段恢复；所有角色验证成功后才写成功 marker。最后在生产备份的隔离副本运行实际统一迁移入口，再按固定 150 命令执行正式切换。
