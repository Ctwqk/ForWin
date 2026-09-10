# P2-4：单本 Markdown 与 manifest

2026-09-09。范围是批准设计 §3.5 / P2-4 的最小文件导出：DB Canon 仍是唯一事实源；本包不创建 Git 仓库、远端同步或导入入口。

## 实际调用与持久边界

`CanonAdmissionService.commit_plan`、历史整套替换与 `commit_world_edit` 在各自成功事务内追加一个 `novel.export.requested`。唯一事件身份为 `novel-export:{project_id}:{book_revision}`，请求冻结书名与书籍版本。既有三类 Canon recovery 事件及其身份未改；新增 observer 不参与接纳判断。事务内没有文件 IO、远端调用或历史正文扫描。

默认 outbox 注册具体 export handler，不初始化 Writer、模型、Qdrant 或 publisher。handler 使用既有 claim / retry / lease：先取 Project 共享锁，再锁 Outbox 行；核对项目、事件、worker、epoch、状态及 lease 到期时间。首次成功读取后，把有限 manifest 冻结到该请求的 `payload_json.snapshot`，提交后才做文件 IO。旧 claim 不能生成或覆盖另一 worker 的快照；同一请求重试复用持久快照。文件故障由现有 outbox 重试，已接受的 Canon 不回滚。

## 版本与内容

`snapshot.py` 依据 retained committed Canon 的 `acceptance_revision`、`base_book_revision` 重建目标 R：每个稳定章节选 `base_book_revision < R` 的最后接受版本，检查接受链与章节序列。当前 R 另核对 active 指针；历史 R 不使用当前指针选正文。整套历史后缀在相同 base 下同时切换；world edit 增加书籍版本，正文仍可指向同一组接受记录。

正文读取遵循 Canon → candidate → draft 的正向引用，并核对项目、稳定章、章节号和实际 SHA256。合法历史重接纳可以复用 candidate，因此不要求 candidate 的单个便捷回指等于此次 Canon。缺失或冲突的正文身份拒绝导出；不回退到最新草稿。

每章 manifest 包含稳定章 ID、章节号、Canon/candidate/draft ID、接受版本、base revision、冻结标题、正文 hash。`plan_revision_semantics=candidate_original_plan` 明确其为 candidate 原计划身份；此次 Canon 的有限 `frozen_plan` 只取精确匹配该 Canon/candidate/hash/revision 的原 commit-plan 中 policy version、acceptance mode、expected book revision。匹配证据缺失时为 `unknown`，不从当前可变计划补造。

发布引用是 `observed_at_capture`，不是目标 R 当时的发布状态或实时发布进度。只读取同一 Canon/章/hash 的持久 published protection，并保留其 ID、平台及远端书章 ID，以及精确匹配的 retained receipt IDs。旧任务/回执被清理而 protection 尚在时，`receipt_status=unavailable`；不虚构回执。后续发布变化不会改变已冻结的导出请求。原始评论、读者身份、prompt/trace、凭据、publisher evidence JSON 和备份都不导出。

## 文件与恢复

默认根目录为现有 `config.artifact_root/novel_exports`，是本地文件副本；即使 artifact backend 配置为对象存储，本包也不引入远端文件同步。多个 outbox worker 要共享此文件根目录，才能共同推进同一份本地指针。

```
novel_exports/<fixed-project-id>/
  revisions/<R>/book.md
  revisions/<R>/manifest.json
  current.json
```

目录根权限为 0700。项目 ID 只允许字母、数字、下划线和连字符；根目录及祖先、受管理目录/文件、锁路径拒绝符号链接。根目录从文件系统根逐段使用 no-follow 目录描述符创建/打开，后续 mkdir/read/write/fsync 均绑定已持有的根 FD，路径被改名或替换也不会重定向文件。复用现有 Obsidian managed-file 原语，仅窄增根 FD 输入的 dup 支持，既有 Path 行为和世界页面职责不变。每个项目的文件锁串行化不同进程/线程；两个版本文件均写入、校验并 fsync 目录后，才原子推进 current。重复相同内容不改文件；不同内容占用相同 R 时拒绝；乱序旧请求可以补齐旧 revision 文件，但不能回退 current。current 是读取入口；新版本文件完整前不会被它引用。

已冻结请求和 DB 历史保留时，`rebuild_export(session_factory, root, project_id=..., book_revision=...)` 可以恢复已丢失的文件，字节与原快照一致，不重置已 processed 事件的状态、次数或接纳记录。首次尚未冻结的请求继续由正常 outbox 消费，恢复函数不会另建快照或伪造历史捕获时点。此包未执行生产恢复或真实发布。

## 验证

新增 `tests/test_novel_export.py`：真实 PostgreSQL 普通 Canon、整套历史后缀及 world edit 的 R1–R4 重建；实际 outbox 故障/重试与 frozen snapshot；旧 epoch 拒绝、同 epoch 并发读取；四路文件写入与旧版本乱序；部分文件故障不推进指针；正文 hash 失配拒绝；发布引用/敏感 JSON 排除；计划证据 unknown；已 processed 请求原样重建；项目、文件、锁及根祖先 symlink 拒绝。

新增模块缺失用例先失败；历史 candidate 回指误判、macOS 并发锁文件创建、只读重建入口缺失和祖先 symlink 均有失败复现后修复。独立审查又实证两项失败：draft 与 candidate hash 一致漂移会与 retained accepted plan 的 hash 矛盾；检查后 root 被换成 symlink 会令基于路径的写入逃逸。两项已迁入常规 CI，先 2 RED，随后拒绝已存在的 hash 矛盾、以根 FD 贯穿写入，两项 GREEN。缺失精确计划仍 unknown 且合法历史重接纳可导出。

独立修复前 108 项导出/Canon/outbox/projection/revision/architecture 相关回归通过；最终 19 项导出与 11 项既有 Obsidian managed manifest 共 30 项通过。新模块/测试 Ruff 全通过；三个小接线文件 F/E9/I 通过，窄改 Obsidian helper 的 F/E9 通过（该旧文件既有 import spacing 提示未扩大修改）。整合后的最终全量 suite 由主任务统一运行。

独立最终复核重新运行原两项失败探针，连同 19 项导出、Canon 原子事务、完整后缀和 11 项 Obsidian 回归，共 96 项通过。测试后全部 11 个冻结文件 hash 与清单一致；无剩余阻断。以上计数存在重叠，不与包级回归相加，也不替代整合全量 suite。
