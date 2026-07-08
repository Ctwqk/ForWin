# ForWin Post-100 Chapter Repair Reset Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development
> (recommended) or superpowers:executing-plans to implement this plan
> task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace the post-100-chapter hotfix loop with a destructive
contract reset: entity registration instead of alias regex patches, readability
blocking instead of silent reader corruption, connected fatal producers,
explicit embedding degradation, real pulp BookState extraction, hand-authored
trope templates, and a no-hotfix 100-chapter validation run.

**Architecture:** Admission becomes registration-backed and durable through
`Entity` / `EntityAlias`; review receives deterministic readability and fatal
producer signals; infra and extraction failures fail closed instead of silently
degrading. Old generation data is not migrated or repaired.

**Tech Stack:** Python 3.12/3.13, SQLAlchemy models and Alembic migrations,
pytest, Qdrant retrieval, ForWin DecisionEvent observability, ForWin MCP tools
for runtime project/task/chapter truth.

**Spec:** `docs/superpowers/specs/2026-07-07-forwin-post-100-chapter-repair-design.md`

## Global Constraints

- Do not preserve compatibility with old projects, old tasks, or old generated
  data.
- Do not write to production project, task, chapter, or canon tables to clean
  old data.
- Use old project data only as frozen fixture input.
- Do not add book-specific name rules to make the validation book pass.
- During the final 100-chapter validation run, code hotfix count must be zero.
- If code must change during validation, mark the run failed, deploy the generic
  fix, create a new project, and restart validation.

---

# ForWin 100 章实测暴露问题修理计划

日期：2026-07-07
状态：implementation-ready
证据基线：`docs/operations/100-chapter-test-log-2026-07-06.md`、项目
`d920ac31663743df850d9c5cc3d2df3f`（100/100 章已接受）、07-06 10:33 至
07-07 13:15 期间的 ~28 个热修 commit。

---

## 0. 授权范围（重要，先读）

- **不需要兼容旧项目 / 旧任务 / 旧生成数据。** 之后的测试生成一律新开项目，
  不基于任何已有生成。
- 因此本计划**明确允许**：删除机制、改行为语义、改 gate 阻断集合、
  删除书本专名规则及其测试。**明确不做**：旧项目 canon 清洗、数据迁移、
  兼容开关、对 `d920ac31663743df850d9c5cc3d2df3f` 的修复。
- 旧项目数据只作为回归 fixture 的**输入样本**使用（冻结拷贝进 tests/fixtures），
  不在生产库上做任何回写。

## 1. 问题 → 任务映射

| # | 暴露的问题 | 证据 | 任务 |
|---|---|---|---|
| 1 | 实体准入靠"白名单拼贴 + 摘要正则捞名"，每个新称谓变体都要热修一条窄规则；100 章消耗 ~20 个 alias 补丁 | 07-06/07 的 `Normalize/Admit/Filter` commit 链；ch6/8/9/10/61/67/77/97 停机 | Task 1 |
| 2 | 字符串替换式 autofix 把人名泛化成"馆员"，污染 canon 摘要后经 context 自我繁殖；第 13 章主角与死者同章同称谓、trait 标签泄漏正文，全 gate 绿灯通过 | ch13 正文（repair=0、residual=[]、normal 接受）；ch2 起摘要污染 | Task 1（根因）+ Task 2（兜底） |
| 3 | 没有任何 gate 检查"读者可读性"：同称谓多指代、内部 key 泄漏、主角名消失、标题编号错乱（ch28 标题"第10章"）、空摘要（ch30） | ch13/ch28/ch30 | Task 2 |
| 4 | `pulp_fatal`/`serial_fatal` 的 12 个扩展 fatal 信号**没有任何 producer**；原 fatal 集合中 `character_dead_alive` 等同样无发射点。gate 在监听没人发的信号 | 全库 grep：仅 `gate.py` 与 `review_auto_retry.py` 引用 | Task 3 |
| 5 | embedding gateway 失败时**静默回退 hash**（64 维伪随机），长程召回失效且无法从运行时确认 | `forwin/retrieval/memory_index.py` fallback 分支只打 warning | Task 4 |
| 6 | pulp "light extraction" 只写每章 title+summary 一个节点，不提取角色/状态/物品/势力；pulp 档状态失忆实质未修 | `forwin/extractor/book_state_graph_delta.py::_light_structured_fallback_delta` | Task 5 |
| 7 | trope 库 "≥50 条" 靠 `_BUILTIN_VARIANTS` 程序化生成凑数，手写只有 9 条 | `forwin/protocol/trope_library.py::expand_trope_template_payload` | Task 6 |
| 8 | "100 章跑通"是人肉热修陪跑出来的，没有"无热修长跑"这一验收概念 | 100 章日志全篇 | Task 7 |

优先级：Task 1、2 是 P0（不修则任何新长跑都会复现污染与打地鼠）；
Task 3、4 是 P1；Task 5 是 pulp 上线前置；Task 6 是 P2；Task 7 是总验收。

---

## Task 1: 实体准入改为注册流，废除字符串替换式 autofix（P0）

### 根因

当前 admission 判定是 `名字 ∈ 白名单拼贴`，白名单在
`StateRepository.get_allowed_entity_names`（`forwin/state/repo.py:737`）里由
roster + entry targets + 计划文本正则 + `_recent_accepted_summary_character_names`
的摘要正则拼出来。writer 每造一个新称谓变体（`猎锚者X（远程声音）`、
`灰鹞网络遭X从内部击穿`），就需要往
`forwin/checker/reference_classifier.py`（已 424 行）加一条窄规则并部署。
判定失败后的 autofix（`_apply_subworld_admission_autofix`）用字符串替换把
人名改成角色称谓，制造了"馆员"污染。

### 目标形态

未知命名实体不再是"违规"，而是触发一次**显式注册决策**：

```text
writer_output 中的命名引用
  → 与 Entity/EntityAlias 表匹配（已注册 → 直接通过）
  → 未匹配 → EntityRegistrar 一次廉价 LLM 分类调用，输出每个名字的：
      register_character  （新角色：canonical_name + aliases + role_hint + gender)
      register_alias      （既有实体的新称谓：绑定 entity_id）
      background_generic  （背景泛指，不入实体表，不阻断）
      plan_conflict       （与计划/canon 矛盾 → 唯一进 needs_review 的出口）
  → register* 结果写 Entity + EntityAlias（表已存在：
      forwin/models/entity.py 的 Entity.aliases_json 与 EntityAlias，
      含 uq_entity_alias_project_alias 唯一约束，无需迁移）
```

admission 检查从"名字在不在名单"变成"名字有没有注册记录"。别名归一化由
EntityAlias 表承担，不再靠正则。

### 改动

- 新增 `forwin/naming/entity_registrar.py`：分类调用（廉价模型，固定 JSON
  schema，max_tokens ≤ 600）、Entity/EntityAlias 写入、DecisionEvent 记录
  （`ENTITY_REGISTERED` / `ENTITY_ALIAS_REGISTERED` / `ENTITY_PLAN_CONFLICT`）。
  LLM 不可用时的降级行为是 `plan_conflict`（进 needs_review），**不是**静默放行。
- `forwin/orchestrator_loop_core/review_autofix.py`：在 `_review_current_output`
  之前接入 registrar；**删除**以下函数及全部调用点：
  - `_apply_subworld_admission_autofix`
  - `_generic_subworld_reference`
  - `_subworld_role_titles`
  - `_looks_like_genericizable_unknown_reference`
  - `_placeholder_role_replacement`
  保留 `_apply_canon_name_drift_autofix`（有 canon 对照的错名矫正），但其替换
  必须记 DecisionEvent，且 Task 2 的可读性 gate 作为兜底。
- `forwin/state/repo.py::get_allowed_entity_names`：删除摘要正则捞名路径
  （`_recent_accepted_summary_character_names`、`_extract_summary_character_names`
  的调用），allowed set = Entity/EntityAlias 表 + chapter entry targets。
- `forwin/checker/reference_classifier.py`：删除 2026-06-15 之后累积的
  书本专名规则（猎锚者/灰鹞/网络中介人/远程信号/远程声音/编号别名等窗口正则）。
  保留纯通用的字面判定（如 `looks_like_technical_identifier`）。
- `forwin/review_engine/rules/repair_v2.py`：删除 `"subworld"` 作为独立
  repair scope 的死路（`_SCOPE_TO_OUTCOME` / `MAX_ATTEMPTS_PER_SCOPE` 中移除）；
  admission 类问题不再进 repair loop，由 registrar 在 review 前消化，
  仅 `plan_conflict` 走 needs_review。
- `forwin/planning/band_plan_service.py`：`_ENTRY_TARGET_PATTERNS` 中
  硬编码职业名（馆员/审计员/调度员/接线员）的正则删除，entry target 推断
  改为只信 schedule 显式 targets + registrar。

### 测试（red-green）

- 将 100 章项目第 6/8/9/10 章的 writer_output 冻结为
  `tests/fixtures/subworld_replay/`，新测试证明：**不加任何新正则**，
  registrar 把 `猎锚者X` 系列归并为同一实体的 alias、`灰鹞`/`陈昭宁`
  正确注册，四章全部通过 admission。
- 07-06/07 期间新增的 ~20 个 normalize 回归测试：改写为 registrar 行为
  断言或删除（这些测试断言的是被删除的机制）。
- 断言 `_apply_subworld_admission_autofix` 等符号不再存在
  （architecture boundary 测试）。

### 验收

- 全库 grep 无正文字符串泛化替换路径。
- subworld/alias 类问题从"需要代码热修"变为"registrar 决策 + 事件记录"。

---

## Task 2: 读者可读性 gate（P0）

### 根因

现有 gate 全部面向"信号消失"（canon 一致性、placeholder、style），没有一层
回答"这一章读者能不能读懂"。第 13 章的四类事故全部绿灯通过。

### 改动

新增 `forwin/canon_quality/readability.py` 确定性 analyzer，产出以下
signal（全部进入 fatal 阻断集合，所有 gate 档位一致阻断）：

| signal_type | 判定 | severity |
|---|---|---|
| `appellation_referent_conflict` | 同一称谓（实体表 role 称谓 + 常见职务词）在同章内与冲突性别代词（他/她）邻接共现，或同称谓被实体表解析到 ≥2 个实体 | error |
| `internal_key_leakage_v2` | 正文含 `trait-[a-z-]+`、`<<FORWIN_`、experience plan 内部字段名。**先查明现有 `internal_state_key_leakage` 为何漏掉 `trait-` 前缀并修其根因**，新 pattern 并入 | error |
| `protagonist_name_missing` | 主角真名（Genesis brief / premise 抽取，落到 Entity 表 protagonist 标记）在章内出现 0 次 | error |
| `protagonist_name_diluted` | 主角真名出现次数 / (真名 + 泛称谓指代) < 0.3 | warning |
| `chapter_title_mismatch` | 标题匹配 `第(\d+)章` 且 N ≠ chapter_number | error |
| `chapter_summary_empty` | 接受时 end_of_chapter_summary 为空 | error |

接入点：

- `forwin/reviewer/hub.py`：作为 hub 内新 collector（deterministic，无 LLM）。
- `forwin/canon_quality/gate.py`：六个类型加入 `_FATAL_ONLY_SIGNAL_TYPES`
  （从而自动进入所有 fatal profile 与 strict）。
- `forwin/models/entity.py` / Genesis bootstrap：Entity 增加 protagonist
  标记的落库路径（`kind="character"` + importance 或新布尔列均可；无兼容
  约束，选最简单的 alembic 迁移）。

### 测试（red-green）

- 第 13 章 writer_output 冻结为 `tests/fixtures/readability/ch13_polluted.json`，
  断言 `appellation_referent_conflict`、`internal_key_leakage_v2`、
  `protagonist_name_missing` 全部触发且 gate block。
- ch28（标题"第10章"）、ch30（空摘要）各一条 fixture。
- 干净章节（如 ch1）fixture 断言零误报。

### 验收

- 第 13 章样本在新 gate 下不可能被接受。
- 误报率：对旧项目 100 章正文批量跑 analyzer（只读），人工核对告警清单，
  确认无大面积误报后再合入阻断。

---

## Task 3: fatal 信号 producer 接线 + "聋 gate"防回归（P1）

### 根因

`_EXPANDED_FATAL_SIGNAL_TYPES` 的 12 个类型全库无发射点；原集合中
`character_dead_alive`、`character_teleport`、`closed_thread_reopened` 等
也无 producer。当前唯一活跃的 fatal producer 是 `form_*` 家族。

### 改动

- 新增 `forwin/canon_quality/producer_registry.py`：
  `SIGNAL_PRODUCERS: dict[str, str]`，声明每个 fatal signal_type 由哪个模块
  产出。**architecture 测试**：`_FATAL_ONLY_SIGNAL_TYPES ∪ _EXPANDED_FATAL_SIGNAL_TYPES`
  中每个类型必须在 registry 有登记，否则测试失败——从机制上杜绝再次出现
  "gate 监听没人发的信号"。
- 便宜的确定性 producer（适配器模式）：
  - `ContinuityChecker._check_dead_characters` 的 error →
    `dead_character_resurrection` 信号（`forwin/checker/rules.py` 已有检测，
    加 issue→CanonQualitySignal 适配层即可）。
  - `_check_thread_status` 的已关线程复开 → `closed_thread_reopened`。
  - BookState map 路径不可达 + 人物位移 → `impossible_location_teleport`
    （用 `forwin/map/pathfinding.py` 已有能力）。
- LLM producer：`forwin/canon_quality/chapter_review_form/form_schema.py`
  的 issue 分类枚举扩充 `level_rollback`、`duplicate_artifact_resource`、
  `faction_relation_reversal`、`protagonist_resource_debt_mismatch`，
  prompt 指示 LLM 按此归类；经 `_issue_signal_type` 已有映射自动进 gate。
- 清理：`_EXPANDED_FATAL_SIGNAL_TYPES` 中同义重复的别名
  （`dead_character_resurrection` / `already_dead_character_resurrected` /
  `character_resurrection` 三选一），无兼容约束，直接收敛为单一命名。

### 测试

- 每个 fatal signal_type：一个合成输入产生该信号且 gate `commit_allowed=False`
  的集成测试。
- producer registry architecture 测试（见上）。

---

## Task 4: embedding gateway 去静默（P1）

### 改动

- `forwin/retrieval/memory_index.py`：gateway/remote 构建失败时——
  - 记 DecisionEvent `EMBEDDING_BACKEND_DEGRADED`（project 无关，scope=system，
    落 observability）+ `logger.error`；
  - 新 config `embedding_required: bool`（生产 compose 默认 `true`）：
    required 时直接抛异常拒绝启动，禁止 hash 兜底进入生产。
- `/health`（`forwin/api_system_routes.py`）返回当前 embedder kind 与 dims，
  让 `check_codex_operator_ready.py` 能断言 `kind=gateway, dims=384`。
- `scripts/reembed_memory_index.py`：结束时校验目标 collection 向量维度
  与 config 一致，不一致返回非零退出码。

### 测试

- gateway 不可达 + required=true → 启动抛 RuntimeError。
- required=false → 降级事件被记录且 health 上报 `kind=hash`。

---

## Task 5: pulp 轻量提取真实化（P1，pulp 上线前置）

### 根因

`_light_structured_fallback_delta` 只造 title+summary 节点，角色/状态/物品/
势力全部缺失；pulp 档实体表与 BookState 仍然不增长。

### 改动

- 删除 `_light_structured_fallback_delta` 的摘要节点路径。
- `forwin/writer/chapter_writer.py`：single 模式接受路径新增一次廉价 LLM
  调用 `light_state_extraction`（固定 schema：
  `characters[{name,status,location}] / possessions[] / factions[] / key_events[]`，
  max_tokens ≤ 800），产出走既有
  `BookStateGraphDeltaExtractor` world 层管道；新实体喂给 Task 1 的 registrar。
- 提取调用失败 → 该章 needs_review（不静默、不降级为摘要节点）。

### 测试 / 验收

- pulp 30 章压测：Entity 行数与 BookState world 节点数单调增长；
  第 N 章新角色出现在第 N+1 章 allowed set 与 writer context。
- 单章 LLM 调用数 ≤ 3（写作 1 + 提取 1 + 预留 1）。

---

## Task 6: trope 库真实内容（P2）

- 删除 `forwin/protocol/trope_library.py` 中 `_BUILTIN_VARIANTS` 与
  `expand_trope_template_payload` 的程序化凑数分支。
- `Design-docs/trope_library_pulp_v1.md` 手写补齐至 ≥50 条完整四段式
  （建议配额：power 12 / social 10 / justice 10 / mystery 9 / emotion 9；
  覆盖男频升级流与女频关系流，标注 genre_fit 与 market_tier）。
- `tests/test_trope_md_loader.py` 的 `>= 50` 断言保留，但增加断言：
  每条模板的四段正文均非模板化占位文本（禁止 `_generated_template_payload`
  特征字段出现）。

---

## Task 7: 全量验证 + 无热修实机验收

- [ ] Task 1-6 focused 测试全绿；`.venv/bin/pytest tests -q` 全量通过。
- [ ] 提交、推送、经 150 sync 路径部署；`check_codex_operator_ready.py` 通过，
  `/health` 确认 embedder `gateway/384`。
- [ ] **新建全新 100 章项目**（禁止复用任何旧项目），
  `project_start_writing(run_until_chapter=100)`。
- [ ] 运行纪律：**运行期间禁止代码热修。** 遇到阻断只允许通过 UI/MCP 的
  官方动作恢复（retry / register entity / create obligation / accept soft）。
  如果必须改代码才能推进，本次 run 判定失败：修完、部署、**重开新项目**再跑。

### 验收指标

| 指标 | 目标 |
|---|---|
| 运行期间代码热修次数 | **0** |
| needs_review 停机次数 | ≤ 5，且全部经 UI/MCP 一键恢复 |
| 可读性抽查（每 10 章抽 1 章跑 readability analyzer + 人工快读） | 称谓多指代 0；trait/内部 key 泄漏 0；标题编号错误 0；空摘要 0 |
| 主角真名出现于正文的章节占比 | ≥ 95% |
| 实体表增长 | 新命名角色全部有 Entity/EntityAlias 记录，无正则热修 |
| 完成状态 | `project_get`: 100/100 accepted，无 pending review，无 active task |

任何一项不达标 → 记录 stop reason 分布，回到对应 Task 修复，重开新项目重跑。
不允许用"对这本书加规则"的方式让指标通过。
