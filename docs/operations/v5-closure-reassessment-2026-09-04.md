# ForWin v5 收口复评与验证记录

评估日期：2026-09-04。实现起点：`fdaeaa687a0dc4c65550a01d030e7d97d43b0c5f`。当前工作：`codex/v5-closure`。

结论：核心架构收敛、Track A/B0 和 A2/A4 实现已完成。本轮只写[现有设计](../../Design-docs/CURRENT_DESIGN.md)，做本地消融与回归，随后集成并推送默认分支 master。正式运行验证与部署另行记录，不启动几十或几百章生成。

## 分支与 worktree

初次执行 `git fetch origin` 时，远程 master 为 b38331d。下表是开始收口前的快照；核对时八个原 worktree 全部 tracked-clean。

| 分支 | HEAD | 相对领先候选 | 结论 |
| --- | --- | --- | --- |
| master / origin/master | b38331d | 落后147提交 | 已发布来源，非最新实现 |
| publisher-login-session-routing-fix | 7b63a8b | 落后373提交 | 已包含 |
| codex/v5-architecture-convergence | 147e9ac | 落后164提交 | 已包含 |
| codex/v5-final | 34a9ffc | 落后111提交 | 已包含 |
| codex/v5-a2-convergence | 80af1c8 | 落后90提交 | 已包含 |
| codex/v5-r8-publisher-recovery | e17ed39 | 落后88提交 | 已包含 |
| codex/publisher-recovery-evidence-generalization | a3dc4e9 | 分叉：候选独有6，分支独有1 | 该修复已以471aa3c移植；publisher runner/test blob完全相同，其余差异为后继Qdrant校验 |
| codex/v5-r9-integration-candidate | fdaeaa6 | 最新完整原候选 | 本轮实现基线 |

所有已 fetch 的其他远程分支均为 fdaeaa6 祖先。新增 `codex/v5-closure` 从 fdaeaa6 派生；不丢弃、不重置、不删除其他 worktree，不改 R27 镜像/配置/项目/产物。

## 当前完成情况

| 项目 | 结果 |
| --- | --- |
| Provisional、Scenario、Obsidian reverse import | 已物理删除，架构边界测试存在 |
| Canon / BookState / ownership | candidate→accepted 集中在commit_plan；extraction/read models已归位；保留durable post-Canon恢复边界 |
| S1/S3/S2 | 已有实现及MCP；unknown denominator、incident proxy和project-scoped规则语义保留 |
| A2/A4 | 已决定并落地，不重新列为等待删除 |
| V1、generation pre/post-commit恢复 | 旧SHA已有真实PASS，不等于当前候选验收 |
| 其他V5故障 | 部分runner已实现，真实PASS不齐；Qdrant有setup_blocked记录 |
| R27矩阵 | 85/250，partial；pulp60目标达到，其他三格needs_review |
| 最终RC、全新L200、发布 | 未完成 |

R27状态来自2026-09-04隔离MCP的project_get/chapter_get/task_active_generation_check；active_count=0。每格accepted分别8、8、60、9。通过服务端event_type过滤取得13个Generation Audit checkpoint，全部是report-only（evaluated/fired/blocked均false），其中pulp60有完整10个cadence checkpoint。该数据支持保留当前轻量报告，无需恢复第二决策面。

## 设计收缩

采用 [收口修订](https://github.com/Ctwqk/ForWin/blob/521228871a5752ebe8572c057caa9f4944bb0295/docs/superpowers/specs/2026-09-04-v5-closure-design.md)：冻结主架构和B0，取消外部签名前置门，恢复不可变commit record，预RC诊断仅按受影响范围复验。保留最终全新200章、无热修、真实停服恢复、Canon事务与lease fencing、publisher不重复外部效果。

现有严格matrix collector仍要求完整输入；保留旧诊断证据不会让partial矩阵自动通过。新正文修复改变运行行为，必须使用新候选验证，不能把R27归档重新盖章为新版本PASS。

## R27根因与消融

L100第10章原始writer artifact显示：

- 最终body：辰正二刻完成摘报，辰正三刻离津，巳初一刻送达，巳初二刻收讫。
- stitch前scene2文本：巳初二刻完成，巳初三刻离津。
- reviewer收到完整旧scene文本，却只收到最终body的头尾片段；错误引用了旧时间线。

这不是需要放宽连续性检查的案例。修复删除重复场景正文输入，以完整body作为最终叙述事实源，同时保留状态、事件、time_advance与Canon invariants交叉核验。repair escalation使用相同事实源。

消融fixture故意保留矛盾旧scene、将正确时间放在body中段：新prompt包含完整body，旧时间和scene:2旧锚点消失；结构化候选不变。另测最终body真的矛盾时，引用draft:body的error仍产生blocking fail。这证明输入与判定边界，未把确定性prompt测试夸大为真实LLM质量胜率。

独立问题：第三次rewrite把受保护标题从“第10章”改为“第10章 空税入泽”。修复执行者应遵守标题保留合同；不删除verifier检查，不把显式计划改名静默改回。

L30/L60S另有Canonical规则定义改写，需要内容修复；本轮未强行批准旧章节，也未为特定书追加全局规则。

## 原候选测试基线

Python3.13.12 / pytest9.0.3 / Ruff0.15.22。隔离真实模型与生产接口，使用已有本机测试PostgreSQL的随机测试库。

| 检查 | fdaeaa6结果 |
| --- | --- |
| 默认collect | 2154项，0 collection error |
| 默认完整pytest | 2152 passed、1 failed、1 skipped，175.72秒；另6个subtests通过 |
| 唯一失败 | 本次隔离环境全局ARTIFACT_ROOT/RUNTIME_SETTINGS_PATH覆盖了backup fixture；只移除这两项覆盖后，原测试单独通过 |
| 隐藏RC harness | 1130 passed，16.23秒 |
| Ruff / compileall | 通过 |

原完整运行exit=1如实保留，不用一次单测通过改写为全量绿。最终closure验证已移除上述测试环境污染。

完整命令、日志、JUnit、原始MCP与只读artifact快照保存在源workspace的 `.artifacts/v5-closure-2026-09-04/`。故事和运行配置不纳入本次tracked文档。

## 本轮具体修改与消融结果

| 提交 / 范围 | 对照与结果 | 实际收益和边界 |
| --- | --- | --- |
| fbf0ee8 / RC身份 | collector 4项与L200 1项先失败；修复后两文件162项通过 | 删除tag必填要求，保留完整真实commit/tree及所有运行证据校验；错误或轻量tag仍拒绝 |
| 6c48428 / review最终正文 | 3项先失败再通过；真实R27/L100产物3296字最终body完整进入prompt，旧scene时间消失 | 减少矛盾叙述来源，保留time/state/event/Canon候选；无真实LLM调用，不声称文学或长程质量提升已证实 |
| b40fbd7 / repair标题 | 旧版2失败/5通过，新版7通过，关联77通过 | 只有受保护且未显式改名的title被恢复；其他WriterOutput字段不变，正文错误和显式改名合同冲突仍阻断 |
| 6161e4c / PlanHealth死接口 | 同一4文件测试删前45通过，删后44通过；存活逻辑AST一致 | 删除from_patch_validation/combine及唯一死接口测试，净删47行；实际future audit阻断保留 |
| post-Canon屏障 / 无代码修改 | 25个内存输入调用生产函数，25项通过；无DB/API/LLM调用 | 四步及order controls缺失/失败均阻断；continue仅放过warn。证明当前依赖，不能证明关掉某步仍保持写作质量 |

全部四项代码修改及当前设计均完成独立复核，未发现尚未解决的P1/P2。PlanHealth删除不新增替代抽象、运行开关或absence guard。当前设计复核修正了两处表述：新名字允许经完整准入登记；第2章起Canon强制检查前章维护屏障。

四步维护的world pressure和feedback有实际下游消费者，本轮保留。trace上传与维护成功绑定、RepairVerifier只看正文头尾、部分计划修订绕过门面，仍是明确的后续消融对象；本轮未假定删除它们安全。

## 最终本地验证

最终生产代码 `6161e4c4d1a9d9ec057a0713842f54af100dfbb1` 全量默认测试 **2162 passed / 1 skipped**（175.04秒，另6个subtests通过），退出码0；Ruff与编译通过。独立隐藏RC测试 **1147 passed**（23.32秒），退出码0，在b40fbd7运行后该目录Python代码未再变化。合计 **3309 passed / 1 skipped**。随后提交只更新Markdown文档。

删除前b40fbd7默认测试2163通过/1跳过也完整保留；少的一项恰为已删除死接口的专用测试，不是忽略失败。生产代码本轮合计增加22行、删除43行，净减21行；回归测试的增加另计。

两套测试分别运行。曾尝试合并调用 `pytest tests .artifacts/rc-candidate`，结果3294 passed、1 skipped、16 setup errors：hidden L200 DB测试的plugin fixture与已收集模块发生加载冲突。原始失败保留，未修改测试掩盖；分进程运行原有两套scope全部通过。

可复现的检查范围（在收口源码树，使用仓库已安装依赖及本机隔离测试PostgreSQL）：

```sh
python -m pytest -q
python -m pytest -q .artifacts/rc-candidate
python -m pytest -q tests/test_planning_facade.py tests/test_gate_outcome_producers.py tests/test_gate_outcome_contract.py tests/test_future_plan_auditor.py
python -m pytest -q tests/test_review_final_body.py tests/test_repair_preserved_title.py
ruff check forwin tests
python -m compileall -q forwin tests .artifacts/rc-candidate
```

真实API opt-in测试未开启；使用随机临时测试库及网络隔离，不加载候选生产.env。完整解释器、环境、命令、SHA与原始JUnit位于源workspace的 `.artifacts/v5-closure-2026-09-04/final/`；PlanHealth前后结果位于 `plan-health-ablation/`，真实输入消融位于 `r27/`。这些原始故事及本机运行信息不推送，tracked回归测试与本报告随代码交付。

## 后续正式发布条件（不在本轮执行）

1. 在新候选上完成受影响的standard生成验证，按内容/代码/基础设施分类处理阻断。
2. 执行现有runner补齐真实V5故障恢复；失败和setup_blocked分别记录，不增加新验真平台。
3. 冻结最终RC并运行全新L200 no-hotfix，完成Canon/投影/publisher/账本证据。
4. 通过150同步部署和生产smoke，记录实际发布与回滚身份。代码集成、推送按本次用户要求提前完成。

本报告中的设计收口、本地测试、代码推送、pulp60或旧SHA恢复PASS都不替代上述正式发布证据。
