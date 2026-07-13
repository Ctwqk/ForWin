# ForWin v5 收尾最终路线（Final Roadmap）

> **For agentic workers:** 这是 v5 收敛后的总协调计划。按 Step 0 → Track A → Track B（前置子集）→ Track C 顺序执行；
> 每个 Track 的细节在其引用文档中，本文件只定顺序、边界与验收。任务用 checkbox 跟踪。

**日期：** 2026-07-12
**代码基线：** `master @ 4b6102f`（v5 hard-cut：Phase A-D 完成，E 主体完成）
**三份关联文档：**
- 历史论证：`forwin_architecture_consolidation_audit.md`（已标 historical-plan，不再执行其 Phase A-F）
- 度量回路：`docs/superpowers/plans/2026-07-12-forwin-measurement-loop.md`（S1-S8）
- 本文件：删除清单（Track A）+ 验证计划（Track C）+ 总顺序

**三轨关系：**

```text
Track A 删除计划：减少表面积
Track B 度量回路：建造仪表（S1/S3/S2 为长跑前置子集）
Track C 验证计划：用仪表证明剩余路径可靠（V1-V6）
```

---

## Step 0 — 测试债清偿（一切的前置）

**现状（2026-07-12 实测）：** 全量 1601 用例，102 失败（约 63 个用例）。已分类：

- ~55 个为过时测试：38× 调用已删私有方法（`test_subworld_control.py` 的 `ContinuityChecker._candidate_character_name` 等）、27× `StateUpdater.create_project()` 缺新必填参数 `runtime_policy`、2× 读已删源文件路径、1× monkeypatch `forwin.api` 旧属性、2× 断言旧文档文本、若干浏览器测试断言旧 UI；
- 4-6 个为**行为变更断言，需要 owner 决策**：chapter review form 已从宽松改为 fail-closed（schema 无效 / 未提问角色作答 / 缺 countdown 现在 `blocking=True`，错误码 `form_answer_rejected` → `form_schema_invalid`，对应 07-12 "Harden canon review contracts" 系列）。**决策：保留新严格行为则改测试；认为过严则回调**——这是唯一需要人拍板的项；
- 2 个 MCP 测试：环境（LAN embedding gateway 不可达）+ fixture 未按 v5 建 BookState snapshot。

**任务：**
- [ ] owner 决策：review form fail-closed 行为保留与否
- [ ] 修复全部过时测试（机械替换为主）
- [ ] MCP 测试 fixture 修复；embedding gateway 依赖改为可注入/跳过
- [ ] 全量测试绿（这是 V1 的进入条件）

---

## Track A — 必做删除（2026-07-12 复核版清单）

> 本清单以逐项调用链核验为准，**替代**审计报告与外部评估中的旧削减清单。
> 旧清单中的"合并 pre-audit / 无条件折叠 Scenario / 删除 daily automation / hard floor 去重"均已被核验推翻，见各项说明。

### A1 确定删除（无争议，立即可做）

- [ ] **Provisional preview 整段删除**：`PlanningPolicy.provisional_preview` 在 standard/pulp 均为 False 且 `with_user_settings` 无开启入口——不可达特性。牵连：`planning/provisional_preview_service.py`、`planning/service.py` 与 `arc_envelope*` 分支、`runtime/{container,factories,services}.py` 的 `provisional_writer` 注入、`ProvisionalBandExecution` 模型与 `PROVISIONAL_GATE_EVALUATED` 事件、`http/routes.py:797` 读接口、`api_task_history.py`/`api_schema` 残留。约 800-1,200 行。补一条边界测试防回归。

### A2 数据决策项（长跑后按 S1 账本定档）

- [ ] **Scenario rehearsal**：与 future_plan_audit 语义不重合（结构完整性 vs canon 一致性），重合的是外围 patch 管线/存储/blocking 检查。三档：
  - 保守：合并两套 patch 应用/审批管线为一个 PlanHealth 门（~800-1,000 行胶水）；
  - 中档（默认推荐）：rehearsal 确定性检查并入 future_plan_audit 作 band 级 mixin，director LLM 模拟挂同一入口，删独立 run/resolution/trigger 体系（~2,000 行）；
  - 激进：整删——仅当 30/60/100 章期间 `SCENARIO_REHEARSAL_BLOCKED` 命中率为零。
- [ ] **Generation audit interval**：同样看 S1 账本命中率后定去留。

### A3 归位整理（不省行数，纯可读性，低优先级可并入任意批次）

- [ ] 4 个 `*_pre_audit.py` 移入 `planning/future_plan_audit/` 包内（它们的唯一调用方就是该包）
- [ ] `world_v4_review_gate`（448 行）+ `extractor/` 并入 `book_state/`，消灭 v4 历史命名
- [ ] `project_payloads/` 归位进 `application/`
- [ ] 检视类 http adapter（book_state/world_model/map）薄化到 handler 注册表形态（project 路由已达标）

### A4 Owner 确认项（产品决策，不是技术决策）

- [ ] **Obsidian import/proposal 半边**（~800 行 + 2 路由文件）：是运行期世界编辑进 Canon 的入口。确认使用习惯：不用则删（导出保留），用则保留并纳入 S8 人时统计。
- [ ] **Daily automation（production/ + long_run_policy + http/automation）**：默认**保留**——它入队生成配额与 publish jobs，是日更连载主链。仅当确认从不用日更自动化才删。

### A5 明确不删（复核后从旧清单移除）

`llm_kb`（写作检索主链）、`knowledge_system`（outbox 投影 refresher）、`hard_floor`（与 readability 零重叠的确定性底线门）、`llm_eval`（转 S7 提示词 A/B 用途）、`checker/reference_classifier`（转为 S2 规则档案的输入）。

---

## Track B — 度量仪表（长跑前置子集）

细节见 `2026-07-12-forwin-measurement-loop.md`。长跑（V4）前必须完成：

- [ ] **S1** GateOutcome 契约 + 门禁效力账本（扩展既有 `build_audit_insights`，不从零建）
- [ ] **S3** 成本账本（字符代理即刻可用；adapter 补 usage 采集以支持 V4 token 统计）
- [ ] **S2** 规则出身/作用域制度（换书体检必须在验证跑之前，防上本书伤疤规则污染长跑）

S4-S8 不阻塞验证，长跑后按数据推进。

---

## Track C — v5 发布验证与稳定性证明（V1-V6）

> 骨架采纳自 2026-07-12 外部评估，带三处修正：V2 只补缺口不重写；V4 统计层指向 Track B 报表；全程遵守"验证计划不得大量新增 framework/service/mode"的约束。

### V1 — Fresh-schema release gate
- [ ] 空 PostgreSQL 上 `0001_v5_baseline` upgrade/check/downgrade/upgrade
- [ ] API / generation worker / MCP / publisher worker+browser 全角色启动 + 健康检查
- [ ] RuntimePolicy / Genesis / task / candidate / Canon / BookState / outbox 基础 CRUD
- [ ] 非 v5 schema 启动 fail-fast；无旧表、无 compatibility import
- [ ] fresh project 完成 Genesis handoff

### V2 — Canon 事务与恢复（补缺口，不重写）
已覆盖（`tests/test_canon_atomic_transaction.py`）：失败全回滚、幂等 replay、stale plan 回滚、原子一次写。**补齐三项：**
- [ ] outbox 写入异常路径
- [ ] worker 在 commit 前/后崩溃的时序（重启后 lease 恢复不产生双 accepted chapter / 重复 entity/GraphDelta）
- [ ] EntityAdmissionPlan stale revalidation

### V3 — Spark 委托边界
- [ ] fail/error 或不 eligible 的 candidate 不调用 Spark
- [ ] Spark 只代批原本需人工暂停的 pass/warn 门
- [ ] model mismatch / schema error / timeout / transaction error 全部 reject（fail-closed）
- [ ] 每次调用有 PromptTrace + DecisionEvent；approve 后 Canon preparation/admission 仍完整执行
- [ ] Spark 不能直接改变 candidate/canon 状态（边界测试固化）

### V4 — 长跑矩阵（统计层 = S1/S3 报表）

| Profile | Delegate | 章数 | 目的 |
|---|---|---:|---|
| standard | human | 30 | 基础质量与人工门 |
| standard | spark | 60 | 委托门与审计 |
| pulp | human | 60 | 低成本路径 |
| standard | spark | 100 | band/arc/repair/canon 稳定性 |
| 生产选定 profile | 选定 delegate | 200 | **no-hotfix 总验收** |

- [ ] 每跑产出 S1 门禁绩效 + S3 成本报表（含 accepted/needs_review/failed、repair 分布、Canon block 类型、entity conflict、PlanHealth blocker、Spark 三态、task retry、projection lag、publisher backlog）
- [ ] 30/60/100 章数据回填 A2 决策（rehearsal / generation audit 去留）
- [ ] 200 章全程零代码热修

### V5 — 投影与发布恢复
- [ ] 依次停止/破坏 Qdrant、MinIO、knowledge projection 消费、publisher backend、browser session
- [ ] Canon 按明确策略提交或阻断；outbox 保留；恢复后可重放；projection 可从 BookState 重建
- [ ] publisher 恢复后不重复上传；CAPTCHA/MFA/risk gate 正常停机

### V6 — 发布决策（全满足才部署）
- [ ] Step 0 + Track A(A1) 完成，全量测试绿
- [ ] V1-V3、V5 通过；100 章无严重数据错误；200 章 no-hotfix 通过
- [ ] rollback runbook + release commit 固定；push 到 GitHub master
- [ ] 150 生产环境同步部署 + 全角色 smoke
- [ ] DESIGN_STATUS 与实际代码一致

---

## 总执行顺序

```text
Step 0 测试债清偿（含 form 行为决策）
→ A1 删除 provisional（+A3 归位可搭车）
→ B: S1 + S3 + S2（仪表与换书体检）
→ V1 fresh-schema → V2 补故障注入 → V3 委托边界
→ V4: 30/60/100 章（产出报表 → A2/A4 决策落地）
→ V5 故障恢复
→ V4: 200 章 no-hotfix
→ V6 发布
```

## 禁止事项（全程护栏）

1. 不重跑审计报告的 Phase A-F；不重新引入任何兼容 shim、旧命名、旧接口（checkpoint/copilot 映射、RuntimeSettingsStore、governance_json、`review_engine_*` 旗标、`WritingOrchestrator`、`world_model` facade、request-level policy override）。
2. 不新增任何默认阻断门；新信号一律 observing 起步，晋升走 S1 生命周期。
3. 验证轨道（Track C）以补测试、故障注入、审计脚本、runbook 为主；若开始大量新增 framework、service 或 mode，即视为偏航，停下重审。
4. 事故响应阶梯（S2 治理条款）：修成因 ＞ fixture 回归测试 ＞ 运行时规则（observing）。任何长跑中的问题响应都按此阶梯，200 章验证期间禁止运行时热修。
