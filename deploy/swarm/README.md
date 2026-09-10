# ForWin 的 150 部署接入

本目录是源码内的 ForWin 发布控制代码，不是运行时源码副本。当前活动 150 脚本需要先接入随附 patch；本工作包只准备文件，尚未修改远程或部署。

- `150-deploy-sync-forwin.patch`：针对调查时活动脚本的三个接入点。其原始 SHA256 是 `45bee197b1d3bcadef33981472f59bd279b9afd0ef8248b6fbe5c3a513892fae`。应用前必须重新核对该身份，先在本地受限副本上执行 `patch --dry-run --fuzz=0` 和 `bash -n`；不匹配时重新审查差异，不能强行 fuzz 应用。
- `deploy-sync-extension.sh`：复用 150 的 repo/staging、SSH、日志、状态和 marker 接口。ForWin 的两条构建路径都收到同一个完整提交；构建失败不会进入停机阶段。其他项目不加载本扩展。
- `forwin_release.py`：仅管理当前六个 ForWin 单副本服务。停机前用无网络、无业务挂载的临时容器检查两镜像的真实依赖。维护任务与更新使用已验证的不可变 image ID；任务被固定在已验证的镜像节点，禁用拉取解析、自动重启和应用健康检查。服务仍保留自己的网络、挂载、业务配置及命令，统一删除旧 PATH 和 FORWIN_SOURCE_REVISION 覆盖，使解释器和源码身份来自候选镜像。
- [`forwin_deploy_maintenance.py`](../../scripts/forwin_deploy_maintenance.py)：在候选镜像中备份、验证和显式迁移。使用启动它的绝对 `.venv` 解释器，调用 `python -m forwin.migrations`，不启动 API scheduler 或 worker。

## 首次切换的前置证据

本扩展不自动改变项目设置，也不把杀进程当作任务暂停。在切换前，运营者先通过已支持的 MCP/运营接口暂停生成、确认没有活跃 generation，暂停自动化、关闭新写入入口，确认 publisher 无在途外部动作并停止新的发布 claim。旧角色保持运行，以便正常完成暂停和回执。浏览器 journal、未知发布状态及旧备份不得清理。

完成这些操作后，在 150 的既有 `STATE_DIR` 写入权限为 `0600` 的 `forwin-quiescence.json`。这是已完成运营检查的证据，不是通过填写布尔值请求自动暂停。必填内容：

| 字段 | 内容 |
| --- | --- |
| `source_revision` | 本次已冻结源码的完整 40 位提交 |
| `expires_at` | Unix 秒；读取时必须未过期且距离现在不超过一小时 |
| `generation_paused`、`publisher_idle`、`admission_closed`、`automation_paused` | 四项已核验事实，均为布尔 `true` |
| `evidence` | 非空运营检查记录引用；不写连接凭据 |
| `service_versions` | 六个服务全名到当前 Docker `Version.Index` 的精确映射 |

代码检查证据的权限、期限、源码和 ServiceSpec 版本。它无法替运营者证明数据库之外的外部动作已停止，缺少可靠证据时必须保持未切换。证据在构建完成、停服务之前读取，因此较长的构建可能需要重新核验。不要为绕过检查伪造/延长记录。

只有经过上述检查，控制器才依次停 browser、publisher、generation、outbox、MCP、app，并确认六个角色的副本为零、旧任务实际退出。旧迁移入口会再次拒绝 queued/running/capacity_wait generation 和 running/processing outbox；禁止 SQL 改状态、stamp 或重建生产库绕过这个检查。

## 执行与证据

接入经审查并安装、代码推送 master 后，正式操作仍使用用户规定的唯一入口：

```bash
ssh 10.0.0.150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'
```

`--dry-run`、`--sync-only`、`--build-only` 保持相应的非部署语义。ForWin 实际部署拒绝 `--no-health`。已有镜像的 `--no-build` 仍必须验证完整源码身份。失败时不会调用旧的 ForWin 滚更/盲 rollback 分支，也不会写成功 marker。

`STATE_DIR/forwin-release.json` 使用临时文件、`fsync`、原子替换和目录 `fsync` 保存，权限为 `0600`。它包含旧 ServiceSpec，可能含凭据，禁止加入 Git 或输出到普通日志。阶段记录包含 source revision、image ID、运营证据、目标节点/数据卷、备份标识、迁移前后 schema 和失败边界。迁移任务的结构化结果只有非敏感字段。

下一次发布覆盖当前记录前，会将已完成记录保留到权限为 `0700` 的 `forwin-releases/` 目录，文件继续为 `0600`。停止某个角色失败时仍会尝试停止其余角色，并报告未能确认停止的服务。

备份放在持久数据卷的 `deploy-backups/<release_id>/` 下；每次产生新备份并执行清单/hash/`pg_restore --list` 校验。备份文件受私有目录保护。MinIO/Qdrant 等外部存储继续遵循[既有备份边界](../../docs/operations/2026-09-09-deploy-sync-migration-proposal.md)，本控制器不把它们假定成已被 PostgreSQL dump 覆盖。

阶段依次为 `built → quiesced → backed_up → migration_started → migrated → verified → deployed`。备份失败不迁移；维护任务只有一次确定成功且产生唯一、匹配源码的结果证明才可推进。任务超时、零退出但无证明、迁移失败和新角色健康失败均保持停止，不启动旧镜像。失败的维护任务保留供受限检查。

发现已有非 `deployed` 阶段时，下一次运行直接拒绝，并要求运营核对该次结果；不会重新执行可能已提交的迁移。提交结果未知时不要删除状态文件来重试。迁移后失败需保留数据库、journal 和镜像证据，向前修复。恢复旧数据库与旧镜像必须作为单独的受控恢复决定，不能依靠 Swarm 的单级 rollback。

恢复服务时先验证 API/MCP 和三类 worker，再启动 browser。核对实际容器 image ID、源码 revision、PATH、`.venv` 导入、API/MCP 健康和 browser heartbeat。控制器不会自动恢复运营暂停的项目 automation 或外部入口；全角色通过后由运营者按原设置恢复。只有全角色通过后才写 `deployed`，随后 150 外层才允许写成功 marker。

浏览器启动脚本在已验证 profile 失效或启动心跳失败时保留整个 profile 并退出。旧的自动 reset 环境设置不再触发清空操作；未知外部动作的本地 journal 必须留给原工作流恢复。新 profile 仍可正常初始化。

## 隔离验证

```bash
.venv/bin/pytest -q tests/test_forwin_deploy_sync.py
python scripts/check_runtime_source.py --base 521228871a5752ebe8572c057caa9f4944bb0295
bash -n deploy/swarm/deploy-sync-extension.sh
```

测试替换 Docker/SSH 的进程边界，实际执行发布状态机和 shell 失败传播；真实统一迁移入口连接新建的隔离 PostgreSQL 旧基线，验证向前迁移、schema 检查及活跃任务拒绝。它不构建镜像、不连接生产数据库，也不调用实际发布。
