# ForWin V2.8.1 完成情况

更新时间：2026-04-18

评估对象：`V2_8_1.md`。

状态定义：

- `完成`：主路径已落地，有测试或维护日志验证。
- `基本完成`：可用，但仍有增强项。
- `部分完成`：已有实现，但未达到完整设计语义。
- `未完成`：当前代码未覆盖。

---

## 1. 总体状态

| 模块 | 状态 | 结论 |
| --- | --- | --- |
| V2.3 生产主链 | 基本完成 | 主链可运行，scene-era contract 未补满。 |
| V2.6 反馈校准层 | 基本完成 | A/B/C 主骨架都已落地，真实读者规模校准仍偏代理。 |
| V2.7 体验审查层 | 基本完成 | WNER、overlay、lint、repair 可用，证据闭环仍需强化。 |
| V2.8 治理层 | 基本完成 | strict gate、checkpoint、constraints、decision log 已形成闭环。 |

当前不能声明所有 phase 100% 完成。

当前可以声明：

> V2.8.1 已经具备长篇连载写作系统的生产链、反馈链、体验审查链和治理链。

---

## 2. V2.3 Phase 状态

| Phase | 状态 | 已完成 | 缺口 |
| --- | --- | --- | --- |
| 阶段 0.5 | 完成 | 最简 Writer、State Repo、State Updater、Context Assembler、continuity checks、单章生成、人工兜底。 | 无关键缺口。 |
| 阶段 1 | 基本完成 | Arc Director、typed schema、协议对象、多章闭环、project 隔离。 | 大规模长篇边界测试不足。 |
| 阶段 2 | 部分完成 | Retrieval Broker、Qdrant、MinIO、ContextPack、scene breakdown/generation/stitch、scene fallback。 | 缺 `scene continuation`、`lore_candidates`、`timeline_hints`、`writer_notes`、完整 scene-era extraction。 |
| 阶段 3 | 部分完成 | Pacing Strategist、patch/reband/rearc、cooldown、stage analysis、post-acceptance 分析。 | phase3/phase4 仍偏后分析，对下一 band/arc 的强驱动不足。 |
| 阶段 4 | 部分完成 | NPC Intent、World Simulator、publisher API、blackbox retry/fallback/forced accept。 | 平台链路、世界模拟、blackbox 长期自治仍需强化。 |

---

## 3. V2.6 Phase 状态

| Phase | 状态 | 已完成 | 缺口 |
| --- | --- | --- | --- |
| Phase A | 基本完成 | `PublisherRawComment`、`CommentSignalCandidate`、六类 signal schema、多标签 signal、fallback。 | LLM signal quality 需要真实平台数据校准。 |
| Phase B | 基本完成 | `SignalWindowAggregate`、`ReaderScaleSnapshot`、多窗口聚合、reader estimate、reader tier、cooldown、trend。 | reader estimate 仍是代理值。 |
| Phase C | 基本完成 | `score_v1`、`ActionMapper`、`AudienceHintPack`、experience-aware actions、phase24/ArcDirector trend 接入。 | feedback 对所有导演模块的注入不均衡，action 效果追踪不足。 |

---

## 4. V2.7 功能组状态

| 功能组 | 状态 | 已完成 | 缺口 |
| --- | --- | --- | --- |
| Experience overlay | 基本完成 | `ReaderPromise`、`ArcPayoffMap`、`BandDelightSchedule`、`ChapterExperiencePlan`、旧 JSON 兼容。 | overlay patch 与 audience confirmation 仍可加强。 |
| Trope registry | 部分完成 | JSON seed、starter pack、`/api/tropes/templates`、稳定导入格式。 | 未内置完整运营级 trope 库。 |
| Planning 接入 | 基本完成 | Arc 结构草案、band/chapter experience 派生、band experience override API。 | audience trend 仍偏校准，不是强约束。 |
| WNER | 基本完成 | `ReviewContextPack`、LLM-first、heuristic fallback、7 维 scores、evidence refs、review notes、repair instruction。 | `confirmed_signals` 未完全一等化，部分判断仍依赖 heuristic。 |
| Lint integration | 基本完成 | `LintSignalCollector`、工具适配、工具缺失空信号、WNER 采纳后升级 issue。 | 真实工具链误报率需要长期校准。 |
| Rewrite 闭环 | 基本完成 | checkpoint 不 rewrite、copilot 暂停、blackbox forced accept、rewrite 异常计数、`scene -> band -> arc`。 | band/arc patch 后的 experience plan 再生成仍可增强。 |

---

## 5. V2.8 Phase 状态

| Phase | 状态 | 已完成 | 缺口 |
| --- | --- | --- | --- |
| P0 | 完成 | strict governance 默认值、三档 progression、前章 accepted gate、跨 band checkpoint gate、auto/manual checkpoint、decision event 基础链路。 | 无关键缺口。 |
| P1 | 基本完成 | `PlanTaskItem`、chapter/band task contract、task contract API/UI、`NarrativeConstraint`、hard/soft/hint、constraint validation、inclusive `protect_until_chapter`、next-band compatibility、future preservation。 | 未来需求仍依赖显式 constraint。 |
| P2 | 基本完成 | `DecisionEvent` taxonomy、reason contract、causal replay、governance insights、issue group、future preservation 分类、runtime observation、evaluator error、drawer 决策解释。 | override 自动校准、arc 级诊断、director imbalance 规则仍可增强。 |

---

## 6. 优先补完项

1. V2.3 scene-era contract
   - `scene continuation`
   - `lore_candidates`
   - `timeline_hints`
   - `writer_notes`

2. V2.7 evidence loop
   - `confirmed_signals` 一等化
   - WNER 证据框架稳定化
   - audience confirmation 与 overlay patch 对齐

3. V2.6 calibration quality
   - reader estimate 从代理值升级
   - action 效果追踪
   - feedback 对导演模块的均衡注入

4. V2.8 director governance
   - override 统计用于规则校准
   - arc 级 replay 解释增强
   - director imbalance 自动规则增加

5. 运维稳定性
   - 容器重启仍可能影响进程内 generation task。
   - 部署前必须确认没有 active task。

---

## 7. 验证状态

最近一次本地全量回归：

```bash
PYTHONPATH=. pytest -q
```

结果：

```text
181 passed, 8 subtests passed
```

最近一次治理/API 回归：

```bash
PYTHONPATH=. pytest -q tests/test_generation_control_payload.py tests/test_governance_review_and_checkpoint.py tests/test_governance_decision_api.py tests/test_api_pages_rendering.py tests/test_continue_project_orphan_review.py tests/test_project_operation_guards.py
```

结果：

```text
27 passed, 8 subtests passed
```

---

## 8. 最终判断

V2.8.1 已完成统一设计收束。

已具备：

- 长篇规划生产链。
- scene-aware Writer。
- review/rewrite/canon 主闭环。
- feedback calibration。
- WNER 体验审查。
- strict governance。
- band checkpoint。
- future constraints。
- task contract。
- decision timeline。
- causal replay。

未完全具备：

- 完整 scene-era Writer contract。
- 完整运营级 trope library。
- 全自动未来需求推断。
- 完全稳定的 WNER 证据判断。
- 基于 override 的自动规则校准。

