# 旧 v5 基线向前迁移

当前实施依据是[三阶段设计](../superpowers/specs/2026-09-09-forwin-three-stage-design.md)。此入口只改变 schema 和经过核验的内部内容身份、恢复状态；不修改小说正文或远端内容，不把旧上传结果猜测成已确认发布。

## 支持的入口

先备份并在隔离副本验证，再停妥运行角色和外部发布 worker，执行：

```bash
python -m forwin.migrations
```

入口从 `InfrastructureConfig` 读取既有数据库配置，不通过命令参数传递凭据。应用启动的 `require_v5_schema` 只检查版本，不自动迁移。

- 全新空库使用原有主迁移链。
- 只有正向读取到唯一版本 `0001_v5_baseline`，才选择独立的 `forwin:legacy_migrations` 链。
- 旧链的第一节点是只用于识别的锚点，不能创建旧 schema。第二节点执行实际向前 DDL，然后接上主链的 `0001_v5_recovery → 0002_chapter_revisions → …`。
- 整个旧桥接和主链位于同一个 PostgreSQL 事务；任何后续身份核验失败都会回滚所有桥接变化和暂停记录。
- 未知版本和已有数据但无版本的库直接拒绝；不存在自动 stamp 或重建分支。`alembic upgrade head` 仍仅识别主链，部署旧库须用上述统一入口。

两个既有 baseline 均未改写。旧 schema 的测试输入来自 `Ctwqk/ForWin@57241ff0e4e2:forwin/migrations/versions/0001_v5_baseline.py`，SHA256 为 `e382386a1130878df4ee16280b85e1f4ed7b3c454fde9c38ccf2267cbfe2174f`。精确文件以确定性 gzip 保存在 `tests/fixtures/migrations/0001_v5_baseline.py.gz`，只用于隔离测试，不参与运行时打包。

## Schema 与历史保留

迁移补齐 generation/outbox fencing、新 publisher 恢复表和身份字段、post-Canon maintenance/checkpoint 表以及 project/subworld 复合外键。保留所有已退出当前模型的历史表，也保留旧 outbox 的 `locked_by/locked_at` 值；为旧必填列增加空默认值，使新 ORM 插入无需伪造旧 lease。

在任何修改前，核对已审计的99张旧表的列类型、nullability与外键指纹。额外历史表保持原状，例如备份中的 `npc_intent_snapshots`。已知表结构不匹配、跨项目 subworld 引用、重复旧 Canon 身份或仍活跃的 generation/outbox 工作都拒绝迁移。

## 旧上传的有限身份恢复

有外部变更可能的 chapter upload，必须同时满足以下条件才能恢复内容身份：项目相同、上传正文与已提交 Canon 对应的 draft 逐字相等、正文 SHA256 等于候选所存 hash、Canon/candidate/ChapterPlan 的项目与正章号一致，且仅有一个匹配。零个或多个匹配都拒绝；不能按标题中的“第几章”猜章号，也不能用当前计划覆盖不可变候选身份。

匹配后仅补齐 job 的 Canon/candidate/章号/hash，将其置为不可自动 claim 的 `uncertain`，原因 `legacy_reconciliation_required`。保留原始正文、上传标题、结果 payload、publish 请求值及原有时间戳；原状态和身份恢复依据写入现有 `publisher_operator_actions`。候选标题缺失或互相冲突会拒绝；上传标题与候选标题不同则明确保留两者和 `title_matches=false`，不改写任一标题。

旧 binding 的章号仅能通过上传结果中保存的 `chapter_binding.id` 关联恢复，并复核持久 binding/work 的项目、平台、标题与原有章号。关联到同一 binding 的多个 job 必须指向同一 Canon。无法直接关联、关联冲突或与已有正章号矛盾时拒绝。原有0章号保存在迁移审计中。

`0002_chapter_revisions` 再次验证这些证据，生成独立的 `reserved` 保护并复制身份/标题差异审计。保护不随 job、attempt、binding 或本地审计清理而消失，不因 lease 过期解除。另一个 job 即使使用新队列身份，也不能绕过相同章节/平台已有保护开始外部变更；冲突由 publisher 协议持久暂停，不得确认 `mutation_started`。

迁移不生成 attempt 或 receipt，不把旧 `official_status=drafted` 扩大解释为目前未公开。submitted/review_pending/drafted/unknown binding 保持冻结；旧 published binding 在缺乏可验证的不可变发布回执时仍拒绝迁移。未知状态的后续核对必须走支持的只读对账及运营流程，不能通过改状态或删队列记录绕过。

完全未开始且没有外部证据的 chapter upload 保留原文，迁移到 `paused`，原因 `legacy_identity_unresolved`，使用现有行动表记录暂停；不为它虚构 Canon 身份或外部效果。

## 隔离验证

`tests/test_legacy_forward_migration.py` 验证精确旧 schema 向前迁移、与新建 recovery schema 的列/外键/索引对等、历史行与 lease 保留、新 outbox ORM 插入、schema 漂移/跨项目/活跃任务/身份歧义拒绝，以及主链后续失败的全事务回滚。已知正文与不同标题、submitted/drafted/unknown binding、删除队列后冻结、未关联或冲突 binding、错误候选 hash 都有独立覆盖。

2026-09-09 的生产备份副本恢复为101张表：99张原始表、Alembic 标记和额外 `npc_intent_snapshots`。原始表列结构无差异。23条旧上传各有一个项目/正文/hash匹配，共18个 Canon；15条上传标题与不可变候选标题不同，全部保持显式未知和冻结。

最初的保守桥接正确拒绝旧上传；追加上述可核验身份恢复后，隔离副本成功升级至 `0003_serial_capacity`。这不代表生产已经切换：生产作品和远端发布均未修改，最终候选仍需统一回归、完整镜像验证和部署前审查。

最终隔离完整性检查得到23条 `uncertain` job、23条 `reserved` 保护、15条持久标题差异证据，以及18条通过直接 ID 关联恢复正章号的 binding；`published` 保护和新 receipt 数均为0。除明确允许的旧 job.status 与 binding.chapter_number 变化外，全部原始表、行数和原有列值的摘要均相同。迁移、保护、发布 fence 与 lease 相关回归共69项通过；这组结果仍由最终集成回归和审查承接。
