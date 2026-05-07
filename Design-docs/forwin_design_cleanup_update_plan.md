# ForWin 设计文档收束与冗余模块更新计划

> 结论：`reviewer/reviewer_v4`、`world_model/world_model_v4` 不是最新目标态下应继续扩张的“双主链设计”。它们的形成原因是 V4 迁移期的 side-by-side 过渡；在最新设计中应被解释为“主链 + 兼容/迁移/投影层”。目标态的唯一 canon 主路径是 `BookState + GraphDelta + Snapshot`，旧 `world_model_v4` 是 compatibility projection / migration source，旧 `world_model` 是 legacy projection / export / wiki 兼容层。

---

## 1. 最新设计应当是什么样

### 1.1 当前权威口径

当前设计入口应固定为：

```text
Genesis / Writer / Review 主链
+ Skill Runtime
+ Observability
+ Final BookState Runtime
+ Scheme C BookMap
= 当前工程基线
```

最新文档明确：

- 书本根真值来自 `Genesis`。
- 章节生产仍由 `Arc -> Band -> Chapter -> Scene` 主链驱动。
- `BookState + GraphDelta + Snapshot` 是当前 canon 主路径。
- 旧 `world_model_v4` 只作为 compatibility projection / migration source。
- `BookMap = SubWorld -> Region -> MapNode -> MapEdge` 是地图最终语义。
- `Obsidian Vault` 和 `LLM KB` 都只是可重建投影，不是 canon。

依据：V4.5 Markstone 对当前工程基线、BookState 主路径、旧 `world_model_v4` 降级、BookMap 语义和三层知识系统权威关系已有明确说明。fileciteturn23file0

### 1.2 模块目标边界

| 模块 | 最新定位 | 是否应继续作为主设计扩展 |
|---|---|---:|
| `forwin/book_state/` | 最终 canon runtime；GraphDelta ledger、snapshot/replay、compiler、review gate | 是 |
| `forwin/map/` | Scheme C BookMap；地图生成、路径、认知路径、movement review | 是 |
| `forwin/reviewer/` | 正常章节审查聚合器：continuity、governance、experience、map movement、personality、lint | 是 |
| `forwin/reviewer_v4/` | V4 lightweight world-delta / reveal / cognition gate；应视为 `world_v4` adapter gate | 暂保留，不新增长期能力 |
| `forwin/world_model_v4/` | V4 ledger/projection/debug/export/BookState adapter bridge | 暂保留，逐步只读/兼容化 |
| `forwin/world_model/` | 旧 wiki / snapshot / Obsidian-style export / legacy projection | 保留兼容，不作为 canon |
| `forwin/skills/` | prompt-layer skill runtime；不写 canon，不越过 gate | 是，限 instruction-only |
| `forwin/personality/`、`forwin/characters/` | V4.7/V4.8 人物性格与创建维护层，挂在 BookState character profile 上 | 是 |
| `Obsidian`、`LLM KB` | 从 BookState 导出的知识投影 | 是，但不能写 canon |

---

## 2. “冗余”到底是过渡还是设计

### 2.1 `reviewer` vs `reviewer_v4`

结论：这是迁移期遗留的“普通章节 review 聚合器 + V4 world-delta gate”，不是两个平级 reviewer 主系统。

实际代码中：

- `forwin/reviewer/hub.py` 的 `HistoricalReviewHub` 聚合 continuity、governance、experience、map movement、personality、lint 等，是当前章节级 review facade。fileciteturn18file0
- `forwin/reviewer_v4/gate.py` 的 `V4ReviewGate` 聚合 world_delta、cognitive、reveal、reader_cognition reviewers，输入是 `ExtractedWorldChangeSet`，输出 `ApprovedWorldChangeSet` 或 `world_model` repair instruction。fileciteturn16file0
- V4 原始实施计划明确要求 v4 模块先 side-by-side 放在 `world_v4`、`world_model_v4`、`extractor`、`reviewer_v4` 下，等测试覆盖后再切 canon path。fileciteturn41file0

因此：

```text
reviewer      = 章节文本 / 体验 / 治理 / 地图 / 人格 review 聚合器
reviewer_v4   = 旧 V4 world change extraction 后的 deterministic gate
BookStateGate = 最终 GraphDelta canon gate
```

`reviewer_v4` 不应继续扩张成另一个总 reviewer。后续应改名或移动成 `world_v4_compat_gate` / `world_delta_review_gate`，并保留 import alias 作为过渡。

### 2.2 `world_model` vs `world_model_v4` vs `book_state`

结论：`book_state` 是最新 canon；`world_model_v4` 是 V4 轻量 ledger/adapter/compat；`world_model` 是更旧的 projection/export 层。

实际代码中：

- `forwin/world_model_v4/compiler.py` 仍写 V4 world deltas / beliefs / gaps / reveals，并通过 `BookStateDeltaAdapter` 同步到 `BookStateReviewGate` 和 `BookStateCompiler`。fileciteturn21file0
- `forwin/book_state/adapter.py` 明确是 “Bridge the existing V4 extraction result into append-only BookState patches”。fileciteturn49file0
- `forwin/book_state/compiler.py` 才是 append GraphDelta rows 并 materialize BookState snapshots 的最终 compiler。fileciteturn45file0
- `forwin/book_state/reviewer.py` 是 BookState graph patches 入 canon 前的 deterministic guardrail。fileciteturn46file0
- `forwin/world_model/compiler.py` 仍从 Genesis、EntityState、RelationEdge、CanonEvent 等旧结构编译 snapshot/export 页面，属于 legacy projection。fileciteturn22file0

最新设计文档已经把旧 `world_model_v4` 明确降级：最终世界模型是 typed property graph + append-only delta ledger + sparse cognition overlay + materialized snapshot；旧 v4 rows 只作为迁移来源、兼容投影和历史审计证据。fileciteturn25file0

### 2.3 `provisional` 相关冗余

结论：legacy provisional 已降级，不再是新主线。

`provisional_mechanism_check.md` 明确：旧 `ProvisionalBandExecution` / `ProvisionalChapterLedger` / `ProvisionalPromotionRecord` 保留用于历史兼容、legacy preview 和审计展示；新主流程把写前预检交给 `Scenario Rehearsal`，写后入 canon 判断交给 `Candidate Draft Review`，legacy provisional 不默认阻断正式写作，除非显式开启 legacy 开关。fileciteturn47file0

---

## 3. 设计文档状态总表

| 文档 | 当前状态 | 更新解释 |
|---|---|---|
| `V4.8_character_creation_personality_maintenance.md` | 活跃维护文档 | 维护人物创建统一入口、自动 personality assignment、coverage/backfill/reassign、metrics、World Studio 人物工作流。命名 BookState character 必须有有效 `personality_loadout`。fileciteturn39file0 |
| `V4.7_character_personality_skill.md` | 活跃设计文档 | 引入 Character Personality Skill Library；不改变 canon 边界，只约束人物表现倾向。后续重点是补完 41 个 skill、扩展 reviewer rule pack、接入 OOC 反馈闭环。fileciteturn36file0 |
| `V4.7_character_personality_maintenance.md` | 活跃维护文档 | 维护 skill 内容、runtime compression、loadout、reviewer、World Studio。V4.8 维护创建/分配，V4.7 维护 reusable skill 内容。fileciteturn37file0 |
| `V4.6_knowledge_system.md` | 活跃设计文档 | 三层知识系统：BookState DB Canon、Obsidian Vault、Karpathy-style LLM KB。DB canon 是唯一真相，后两者是可重建投影。fileciteturn35file0 |
| `V4.5_markstone.md` | 当前总入口 | 用于替代旧 V2/V3 历史设计索引，是当前代码与设计差距统一入口。fileciteturn23file0 |
| `V4.5.1_markstone.md` | 残余设计收束 | 只追踪 V4.5.x 后端 contract、迁移审计、文档口径、治理策略；UI/dashboard/script/native extractor/World Studio 排除。fileciteturn24file0 |
| `V4_final_book_state_runtime.md` | BookState 最终规格 | 规定最终 canon 为 Typed Property Graph + Append-only Delta Ledger + Cognition Overlay + Snapshot；旧 v4 是迁移/兼容来源。fileciteturn25file0 |
| `map_scheme_c.md` | 地图最终规格 | 固定 Scheme C Graph-based Weighted Map Generation；SubWorld 是大尺度容器，城市/客栈/遗迹入口是 MapNode/Region，不再是 SubWorld。fileciteturn26file0 |
| `writing_flow_state_machine.md` | 当前写作任务状态机 | 单章流水线中 BookStateReviewGate、BookStateCompile 是主 canon 节点；LegacyProjection 只是旧 `world_model_v4` compatibility projection。fileciteturn32file0 |
| `V3_8.md` | 后端观测/审计规格 | 决定 DecisionEvent、PromptTrace、ArtifactStore、GenerationTask 的职责分层；dashboard/SLO/UI 是 V4.6+。fileciteturn34file0 |
| `V2_9_3_skill_runtime.md` | Skill prompt-layer 规格 | ForWin-native instruction-only skills；参与 prompt assembly，不写 canon，不绕过 DecisionEvent / gate；Skill API/UI/script/tool-backed 是后续。fileciteturn33file0 |
| `V2_9_2.md` | Genesis / Writer / Review / Governance 历史主链基线 | 仍保留主链语义，但 SubWorld、world model、部分待办需由 V4.5 / V4_final / Scheme C 覆盖解释。fileciteturn9file0 |
| `provisional_mechanism_check.md` | legacy provisional 边界说明 | 不再作为新主线；Scenario Rehearsal / Candidate Draft Review / BookState gate 才是当前判断路径。fileciteturn47file0 |
| `forwin_decoupling_plan.md` | 重构计划 / 架构解耦建议 | 建议拆 Arc/Band/Chapter planning、Genesis handoff、Context providers、Reviewer、Production Scheduler、Publisher runtime 等耦合点。fileciteturn48file0 |
| `docs/superpowers/plans/2026-04-24-forwin-v4-world-model.md` | 历史实施计划 | 解释 `reviewer_v4`、`world_model_v4` 为什么 side-by-side 创建；已被 V4_final/BookState 目标态覆盖。fileciteturn41file0 |
| `docs/superpowers/plans/2026-04-24-forwin-v4-1-runtime-hardening.md` | 历史 hardening 计划 | 完成 V4.1 loop、reviewer、projection、debug API；其中“V4 为 source semantics”的口径已被 BookState final 覆盖。fileciteturn42file0 |

---

## 4. 特性归类

### 4.1 当前应保留并继续建设的特性

```text
Genesis root truth
Arc -> Band -> Chapter -> Scene 主链
BookState GraphDelta canon
BookStateReviewGate / BookStateCompiler
Scheme C BookMap
Map movement deterministic reviewer
Scenario Rehearsal
Candidate Draft Review
DecisionEvent / PromptTrace / ArtifactStore
instruction-only Skill Runtime
V4.6 Obsidian / LLM KB projection
V4.7/V4.8 character personality runtime and assignment
```

### 4.2 被新设计覆盖的旧特性

```text
旧 SubWorld = 城市 / 宗门外门 / 客栈 / 遗迹入口
旧 world_model = canon source
world_model_v4 = 最终 canon source
EntityState / CanonEvent = 真相源
legacy provisional = 默认阻断正式写作的主线 gate
reviewer_v4 = 总 reviewer
V4.1 WorldDelta/Belief/Gaps 作为最终 source semantics
```

这些内容应统一标注为 `legacy`、`compatibility`、`migration source`、`projection` 或 `historical implementation plan`。

### 4.3 已实现但仍留有旧兼容层的特性

```text
world_model/                  legacy projection/export/wiki snapshot
world_model_v4/               V4 ledger/debug/export/BookState adapter bridge
reviewer_v4/                  V4 world-delta/reveal/cognition gate
entities/entity_states        legacy materialized/current views
relation_edges                legacy relation projection / migration input
CanonEvent                    derived compatibility summary
ProvisionalBandExecution      legacy preview/audit
SubWorld.region_drafts        promoted to map_regions but legacy drafts retained
StateUpdater                  projection/materialized-view helper in v4 path
```

### 4.4 文档中仍提到但不应算作当前未完成项的内容

```text
World Studio 完整图谱/地图/认知 UI
metrics dashboard / SLO 看板
Skill API / UI / script-backed / tool-backed execution
native GraphDelta extractor
完整 world/map/cognition rule pack
复杂交通工具体系、多层 route policy、可视化地图编辑器
Neo4j 主存储、tile renderer、真实地理板块模拟
图片优先地图、Genesis 深层 workflow editor
自动 retcon accepted canon
reviewdog UI / 完整 LLM editorial reviewer 产品化
```

这些应归入 V4.6+ / V4.7+ / V4.x product backlog，而不是 V4.5.1 缺口。

### 4.5 仍需补齐或收束的真实差距

1. **模块命名与职责不清**：`reviewer_v4` 和 `world_model_v4` 名字仍像新主链，容易误导继续扩展。
2. **兼容层缺少代码级标记**：应在 package README / docstring / tests 中声明 `world_model`、`world_model_v4`、`reviewer_v4` 的 allowed usage。
3. **BookState 直达路径尚未完全替代 adapter 路径**：当前 `BookStateDeltaAdapter` 仍把 V4 extraction result 转成 GraphDelta。fileciteturn49file0
4. **Genesis map_atlas -> BookMap merge contract 仍需稳定**：source id、冲突报告、重复 ensure 行为仍在 V4.5.1 residual 中。fileciteturn24file0
5. **movement policy v1 还需持续固化**：字段语义、issue code、trace payload 要保持稳定。fileciteturn26file0
6. **skill governance 后端口径仍需文档/测试锁定**：项目级 policy、strictness、启用/禁用、PromptTrace 可解释性。
7. **personality skill 内容未完全补齐**：V4.7 文档明确后续要补完 41 个 skill、扩展 reviewer rule pack、建立 OOC 反馈闭环。fileciteturn36file0
8. **架构解耦仍未完成**：`forwin_decoupling_plan.md` 指出 ArcEnvelopeManager、BookGenesisService、ContextAssembler、WritingOrchestrator、PublisherManager 等仍有职责过宽问题。fileciteturn48file0

---

## 5. 更新计划

### P0：建立“当前设计索引”与文档状态清单

新增：

```text
Design-docs/CURRENT_ARCHITECTURE.md
Design-docs/DESIGN_STATUS.md
```

`CURRENT_ARCHITECTURE.md` 固定写：

```text
唯一 canon source: BookState DB Canon
地图 source: BookMap / Scheme C
上下文 source: BookState + map + Genesis + approved projections
review 主链: reviewer.HistoricalReviewHub + BookStateReviewGate
兼容层: world_model, world_model_v4, reviewer_v4, legacy provisional, legacy EntityState/CanonEvent
投影层: Obsidian, LLM KB, legacy wiki/export
```

`DESIGN_STATUS.md` 为每份文档标注：

```text
active-current
active-maintenance
baseline-with-overrides
legacy-compatibility
historical-plan
future-product-backlog
```

验收：

- README 指向 `CURRENT_ARCHITECTURE.md`。
- V4.5/V4.5.1 仍保留，但不再让开发者从 V2/V3 文档自行判断新旧。
- 文档 grep guard 增加规则：禁止把 `SubWorld` 写回城市/客栈/遗迹入口语义。

### P1：给冗余包加职责标记

新增或更新：

```text
forwin/book_state/README.md
forwin/world_model/README.md
forwin/world_model_v4/README.md
forwin/reviewer/README.md
forwin/reviewer_v4/README.md
forwin/map/README.md
```

建议声明：

```text
book_state       CANON: only BookStateCompiler may append final GraphDelta canon.
world_model      LEGACY: read/export/projection only; no new canon semantics.
world_model_v4   COMPAT: V4 ledger/debug/adapter bridge; do not add final canon features.
reviewer         MAIN REVIEW: chapter-level facade.
reviewer_v4      COMPAT GATE: world_v4 extraction review before adapter; not the main reviewer.
map              CANON MAP: Scheme C BookMap runtime and pathing.
```

验收：

- 每个 package 的 `__init__.py` docstring 与 README 一致。
- 搜索 `world_model_v4` 时能立即看到 compatibility 说明。
- 新贡献者不会把 `reviewer_v4` 当作 `reviewer` 的新版替代品。

### P2：加架构守护测试

新增：

```text
tests/test_architecture_boundaries.py
tests/test_design_status_docs.py
```

测试规则：

1. `world_model/compiler.py` 不允许被 orchestrator 当作 canon writer 调用，只允许 legacy projection/export。
2. `world_model_v4/compiler.py` 允许存在，但 active canon 成功必须落到 `BookStateCompiler`。
3. `reviewer_v4` 只能在 world_v4 extraction/gate/adapter 路径使用，不能替代 `HistoricalReviewHub`。
4. `entities/entity_states/relation_edges/CanonEvent` 不能在新设计文档中被称为 source of truth。
5. `SubWorld` 不能被文档描述成城市、客栈、遗迹入口等局部地点。
6. `Skill` 不能绕过 BookStateReviewGate / DecisionEvent。

验收：

```bash
pytest tests/test_architecture_boundaries.py tests/test_design_status_docs.py -q
```

### P3：重命名兼容层，但保留 alias

第一阶段只改内部命名，不删除旧 import。

建议：

```text
forwin/reviewer_v4/        -> forwin/world_v4_review_gate/
forwin/world_model_v4/     -> forwin/world_v4_compat/
forwin/world_model/        -> forwin/legacy_world_model/ 或保留原名但强标 LEGACY
```

过渡方式：

```python
# forwin/reviewer_v4/__init__.py
from forwin.world_v4_review_gate import *  # deprecated alias
```

```python
# forwin/world_model_v4/__init__.py
from forwin.world_v4_compat import *  # deprecated alias
```

验收：

- 老测试继续通过。
- 新代码 import 只允许新包名。
- 旧包名只出现在 alias、迁移测试、legacy API 中。

### P4：把 BookState 直达路径作为下一阶段目标

目标：减少 `WorldChangeExtractor -> V4ReviewGate -> WorldModelCompilerV4 -> BookStateDeltaAdapter` 这条桥接链的长期依赖。

新增或调整：

```text
forwin/extractor/book_state_graph_delta.py
forwin/book_state/review_gate_ext.py
forwin/book_state/extraction_contract.py
```

运行目标：

```text
WriterOutput / chapter body
  -> BookState GraphDelta candidate extractor
  -> BookStateReviewGate
  -> BookStateCompiler
  -> projection refresh
```

保留：

```text
world_v4_compat adapter path for legacy projects and v4 debug endpoints
```

验收：

- 新项目 accepted chapter 不再必须经过 `WorldModelCompilerV4`。
- `BookStateDeltaAdapter` 只用于 legacy/v4 compatibility tests。
- `world_compile_runs_v4` 不再是新项目 canon 成功的必要证据。

### P5：兼容层只读化

满足以下条件后执行：

```text
1. 新项目 canon commit 全部走 BookState direct path。
2. Obsidian / LLM KB rebuild 全部从 BookState 读取。
3. V4 debug API 可由 BookState projection 替代。
4. legacy project import/backfill 有 report。
5. 线上无 active generation。
```

动作：

- `world_model_v4` rows 标记 read-only compatibility。
- `world_model_v4/compiler.py` 不再作为新项目 writer path 的 compiler。
- 删除或隐藏 UI 中误导性的 “V4 world model = canon” 文案。
- 只保留 legacy import/export/debug read paths。

### P6：清理文档中的旧待办语气

逐文档动作：

```text
V2_9_2.md
  - 增加顶部红线：Genesis/Writer/Review baseline only; SubWorld/world_model 以 V4.5/V4_final/map_scheme_c 覆盖。

V2_9_3_skill_runtime.md
  - 明确 prompt-layer only；项目级 policy/strictness 是当前 residual；script/tool/API/UI 是 future。

V3_8.md
  - 保留 backend observability；把 UI/dashboard/SLO 明确移入 future backlog。

V4.6_knowledge_system.md
  - 将流程中 “V4 ReviewGate -> WorldModelCompilerV4 -> BookStateDeltaAdapter” 标注为 current bridge / compatibility path，不作为最终 direct path。

V4.7 / V4.8
  - 保留 active；把未完成项列成 personality 内容/assignment/reviewer backlog，不与 world_model 清理混合。

superpowers plans
  - 标记为 historical implementation plan，不再作为当前目标架构依据。
```

验收：

- 搜索 `source of truth` 只指向 BookState / DB canon。
- 搜索 `world_model_v4` 不出现“最终 canon source”口径。
- 搜索 `SubWorld` 不出现局部地点新建语义。

### P7：执行架构解耦计划中的低风险拆分

优先拆当前最影响可维护性的三个点：

1. `GenesisHandoffService`
   - 从 API handler 移出 `start-writing` materialize arcs/chapter/map/task enqueue。
2. `BandPlanService`
   - 从 `ArcEnvelopeManager` 移出 band window、experience overlay、world contract persistence。
3. `ContextProvider` chain
   - 从 `ContextAssembler` 拆出 Genesis / BookState / Map / Personality providers。

依据：解耦计划已指出 ArcEnvelopeManager、BookGenesisService、ContextAssembler、WritingOrchestrator 等职责过宽。fileciteturn48file0

验收：

```bash
pytest tests/test_book_genesis_flow.py tests/test_map_world_integration.py tests/test_world_v4_orchestrator_gate.py tests/test_book_state_repository_projection_compiler.py -q
```

### P8：更新维护日志与回归命令

每次执行以上计划时，更新：

```text
Design-docs/maintenance_log.md
Design-docs/V4.5.1_markstone.md
Design-docs/CURRENT_ARCHITECTURE.md
```

最低回归建议：

```bash
pytest tests/test_book_state_legacy_import.py tests/test_map_world_integration.py tests/test_book_genesis_flow.py tests/test_world_v4_orchestrator_gate.py tests/test_api_split_modules.py tests/test_v45_markstone_docs.py -q
pytest tests/test_book_state_repository_projection_compiler.py tests/test_knowledge_system_v46.py tests/test_personality_runtime.py tests/test_personality_assignment.py -q
pytest -q --ignore=tests/browser --ignore=tests/test_mcp_server.py
```

---

## 6. 删除 / 保留判定标准

### 可以立即做的清理

- 文档层：给旧设计加 status header。
- 代码层：加 README/docstring/architecture tests。
- 命名层：引入新包名 alias，不删除旧包。
- 产品层：UI 文案不再称 `world_model_v4` 为最终 canon。

### 不应立即删除的内容

```text
reviewer_v4
world_model_v4
world_model
entities/entity_states/relation_edges
CanonEvent
legacy provisional rows
BookStateDeltaAdapter
```

原因：它们仍在兼容投影、迁移、审计、debug、旧项目 import、测试路径中使用。

### 删除前必须满足

```text
1. 新项目 BookState direct path 完整。
2. legacy import/backfill 可重复运行且报告稳定。
3. 所有 API/UI/debug/export 可从 BookState 投影得到同等信息。
4. 架构守护测试证明旧包没有被新主线引用。
5. maintenance_log 记录迁移影响与回滚路径。
```

---

## 7. 一句话执行原则

```text
不要再把 reviewer_v4 / world_model_v4 当“新版主系统”扩展；
把它们标为 world_v4 compatibility bridge，
把 BookState 作为唯一 canon，
把 map/personality/knowledge 都挂到 BookState，
用文档状态、包 README、架构测试和渐进重命名消除冗余歧义。
```
