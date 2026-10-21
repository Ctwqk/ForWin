# 世界引擎大修 P2：章节增量抽取、翻译、世界审查门 v6 与 review 栈调整 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 用一次结构化的章节增量抽取（`chapter_delta_extraction`）取代现有的三次抽取，再由确定性的翻译器把抽取结果变成 GraphDelta，经世界审查门 v6 把关后进入修复循环和 canon；同时把 review 栈、叙事义务和计划核验改为读取结构化结果，删除正则兜底、关键词判定、map_movement 时长词表，以及倒计时和羁押两本平行账本。

**Architecture:**
- 抽取分三层：
  - `chapter_delta_schema.py`：数据契约，每个条目带 `evidence` 原文引用；
  - `chapter_delta_extractor.py`：LLM 调用、证据校验和带错误反馈的重试；
  - `translator.py`：确定性地翻译成 `source_type="chapter_extraction"` 的 GraphDelta，并带上章节故事时间。
- 抽取在每个候选正文进入 draft review 之前执行，挂在所有调用 `_plan_writer_output_entities` 的位置；结果存进 `WriterOutput.chapter_delta`，按正文 sha256 失效。canon 准备复用它，正文变了就重新抽取。
- 世界审查门 v6 仍是 `BookStateReviewGate`。新检查只针对 `chapter_extraction` delta，读取翻译器写进 delta metadata 的移动记录、知识标签和闪回标记。同一套检查在 draft review 里作为 `world_consistency` 信号（error 进入 must_fix），在 canon 准备时再运行一次。
- 旧路径分两步删除：
  - Task 5 切换 canon 与实体准入，删除 writer contract 和正则兜底；
  - Task 6 把其余消费者迁移到 `chapter_delta`，删除 writer 的三次抽取和 WriterOutput 上的旧结构字段。
- 数据库新增前向迁移 `0004_drop_parallel_ledgers`，删除 `countdown_ledgers` 与 `character_state_transitions`。

**Tech Stack:** Python 3.13（`.venv`）、SQLAlchemy、Alembic、PostgreSQL（测试库 `127.0.0.1:55432`）、pydantic、pytest。

**Spec:** `docs/designs/2026-10-20-world-engine-overhaul.md`（重点读 §1.3、§1.7、§5.2–5.4、§7、§11 中的 P2，以及附录 A、B）。P1 计划 `docs/superpowers/plans/2026-10-20-world-engine-p1.md` 中的“与设计的取舍”继续有效。

## 与设计的取舍（实施者照此执行）

1. **`task_contract_json` 存储保留到 P4。** 它还被 band 计划补丁、未来计划审计和 HTTP 接口写入。P2 只删除关键词子串判定，改由 `plan_fulfillment` 核验计划目标；存储随 P4 的 band 规划器一起删除。
2. **未来约束的关键词判定（角色可用性、秘密保留、关系保持、地点存在、规则保持）直接删除，P2 不提供替代。** 设计中的替代物 protection 约束在 P4 随卷节拍表出现，冲突审查在 P3 实现。在此之前，死亡、移动、物品、揭示由世界审查门 v6 覆盖。
3. **§5.4 的 protection 检查推迟到 P3/P4**，因为 P2 还没有 `review_constraint` 节点的生产者。
4. **P2 的揭示时间表取自 Genesis world_seed 的 `reveal_candidates`**：某个秘密最早可以向读者揭示的章节，是对应卷的 `chapter_start`。没有时间表的秘密，向读者推进时记 warning。`reveal_directives` 在 P4 随 band 计划到来。
5. **闪回用条目级的 `flashback: bool` 表示**，加上 `time.evidence`；不另建 `flashback_spans` 列表。闪回条目只能产生事实和知识事件，不产生 state 补丁。
6. **物品持有者只认 item 节点的 `state.holder_id`**（角色或地点节点 id）。角色的 `item_ids` 不再写入。
7. **移动记录写在章节 delta 的 `metadata["movements"]` 里**：既是 §1.7 要求的移动记录，也是审查门的输入。
8. **抽取只调用一次。** `extract.split_calls` 开关不实现，等验收数据表明需要拆分时再加。
9. **抽取输入是从 BookState as-of N−1 构建的最小实体表和地点表**，不是 P4 的 `ChapterWorldView`。writer 的 `<<FORWIN_LOCATION>>` 标记维持原样（P4 再改）。
10. **chapter review form 继续作为 LLM 信号**：它的行改由 BookState 构建，回答不再写入任何账本。
11. 抽取的 stage_key 是 `chapter_delta_extraction`，即设计 §8.1 中的角色 `extract.chapter_delta`。

## Global Constraints

- 运行代码只处理结构；故事内容只来自 Genesis、world_seed、BookState 或 LLM 输出；题材相关词只能来自本书的 lexicon。新增运行代码不得引入 CJK 词表（模块级集合、`re` 模式、`in` 成员判断、函数内字面量迭代）。提示词文本放在 `forwin/writer/prompt_core/**` 这类已登记为 `prompt_text` 的位置，条目数变化时同步 `tests/overfit_allowlist.toml` 的 `max_literals`。
- 已合入的迁移不得修改。`0004_drop_parallel_ledgers` 在 P2 合入前允许本期任务追加修改；revision id ≤ 32 字符；新 ORM 模型必须能在 SQLite 上 `create_all`。
- `BookStateCompiler(` 只能出现在 `forwin/canon/admission.py`；所有 canon 写入经 `CanonAdmissionService`。
- 故事时间是自纪元起的整数分钟。地图边的 `travel_time` 单位是小时，换算成分钟时乘以 60。时长只能用本书日历的单位或 lexicon 的别名，换算统一用 `forwin/book_state/story_clock.duration_to_minutes`。
- 每个结构补丁（不含 create）都必须带 `old_value` 与 `field_path`，否则历史重写无法回卷（`repository.py:941-944`）。
- 新枚举值一律用英文 token。抽取输出中的引用只有以下几种：实体表里的 BookState 节点 id、地点表里的地图节点 id、`reader`、`new:<名称>`（新实体），以及 `fact:<id>`。
- 分支：从 `claude/world-engine-p1` 当前 HEAD 新建 `claude/world-engine-p2`。本机没有 git 身份，每次提交都用 `git -c user.name=CASPER -c user.email=magi1@CASPERs-Mac-mini.local commit ...`，提交信息末尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`；提交信息中不得出现模型名。
- 测试命令：`.venv/bin/python -m pytest -q`。基线在建分支时测得并记入 ledger。每个任务结束时全量测试必须全部通过。
- 不调用真实 LLM，不连接生产库。生产操作只在 Task 17 中执行，每一步经用户确认，并且必须在 P0、P1 都部署之后。

## Review Focus

1. 抽取给出的 `evidence` 改写了原文措辞（不只是空白不同）→ 该条目被丢弃，并以 `evidence_not_in_body` 出现在 draft review 的 warning 中；被丢弃的条目超过一半时，抽取带错误反馈重试一次。由 Task 2 的 `test_paraphrased_evidence_is_dropped_reported_and_retried` 覆盖。
2. 抽取给出的章节时长单位不在本书日历和别名中（例如日历用“日”，抽取写了“天”）→ 带错误重试一次；仍然无效时本章时长按 0 计，记 warning `elapsed_unknown`，不报错退出。由 Task 2 的 `test_unknown_elapsed_unit_retries_then_degrades_to_zero` 覆盖。
3. 角色走了一条只有别人知道的隐藏路线 → 移动检查用移动者自己的认知：有认知覆盖层时只用它已知的路线，没有覆盖层时才用客观路线。走未知路线记 error `movement_route_unknown_to_mover`。由 Task 4 的 `test_movement_uses_movers_known_routes` 覆盖。
4. 修复循环产生的新正文（本地改写、自动修复、整章重写）不得复用旧抽取 → 正文 sha256 与 `chapter_delta.body_sha256` 不一致时重新抽取。由 Task 5 的 `test_local_rewrite_triggers_reextraction` 覆盖。
5. 运营者在 UI 中人工接受一份正文 → canon 准备核对 `chapter_delta.body_sha256`：不一致时重新抽取，LLM 不可用时阻断，绝不用过期的抽取入 canon。由 Task 5 的 `test_human_accept_with_stale_extraction_reextracts_or_blocks` 覆盖。

---

### Task 1: 抽取契约：数据模型、证据校验与抽取输入

**Files:**
- Create: `forwin/book_state/extraction/chapter_delta_schema.py`、`forwin/book_state/extraction/evidence.py`、`forwin/book_state/extraction/chapter_input.py`
- Modify: `forwin/book_state/extraction/__init__.py`、`forwin/protocol/writer.py`（新增字段）、`tests/test_architecture_boundaries.py:1102-1114`（extraction 包的文件清单）
- Test: `tests/test_chapter_delta_contract.py`（新建）

**Interfaces:**
- Produces（`chapter_delta_schema.py`；pydantic，`extra="forbid"`，列表默认空，每个条目都有 `evidence: str`）：

```text
Duration{amount: float, unit: str}
ChapterTime{elapsed: Duration | None, evidence: str}
NewEntity{name, kind: "character"|"faction"|"organization"|"group"|"item"|"location"|"other", aliases: [str]}
EntityChange{ref: str = "", new_entity: NewEntity | None, field: str, new_value: Any, evidence, flashback: bool = False}
RelationChange{source_ref, target_ref, edge_type, op: "create"|"end"|"set", field: str = "", new_value: Any = None, appellation: str = "", evidence}
NewFact{proposition, truth_value: bool, sensitivity: "public"|"hidden"|"secret"}
KnowledgeEventItem{observer_ref, fact_ref: str = "", new_fact: NewFact | None, state, channel, source_ref: str = "",
                   touches_secret_ref: str = "", contradicts_ref: str = "", evidence, flashback: bool = False}
Movement{entity_ref, from_ref, to_ref, depart_offset: Duration | None, arrive_offset: Duration | None,
         travel_mode_id: str = "", evidence}
NewSite{name, near_ref: str = "", region_ref: str = "", kind: str = "site", evidence}
ItemTransfer{item_ref: str = "", new_item: NewEntity | None, from_ref, to_ref, evidence}
EventItem{summary, participant_refs: [str], location_ref: str = "", significance: "minor"|"moderate"|"major",
          visibility: "public"|"secret", evidence, flashback: bool = False}
ThreadBeat{thread_ref: str = "", new_thread_title: str = "", beat_type: "setup"|"advance"|"complication"|"resolution", evidence}
IntentInteraction{intent_ref, effect: "disrupted"|"advanced"|"fulfilled", evidence}
PlanFulfillment{target_id, status: "done"|"partial"|"missing", evidence: str = ""}
EntityMention{name, kind, is_named: bool = True, is_on_stage: bool = True, evidence}
ChapterDeltaExtraction{schema_version: Literal["v1"] = "v1", body_sha256: str, time: ChapterTime,
  entity_changes, relation_changes, knowledge_events, movements, new_sites, item_transfers, events,
  thread_beats, intent_interactions, plan_fulfillment, entity_mentions}
ExtractionIssue{severity: "error"|"warning", code: str, path: str, message: str}
```

  - `knowledge.state` / `channel` 使用 `forwin/book_state/knowledge.py` 的 `KNOWLEDGE_STATES` / `KNOWLEDGE_CHANNELS`；`edge_type` 必须属于 `WORLD_EDGE_TYPES`。违反由 pydantic 校验拒绝。
  - `evidence.py`：
    - `normalize_for_evidence(text: str) -> str`：把所有空白折叠为空，再做 NFKC 规范化。
    - `evidence_in_body(evidence: str, body: str) -> bool`：evidence 非空，且其规范化结果是正文规范化结果的子串。
    - `validate_evidence(extraction: ChapterDeltaExtraction, body: str) -> tuple[ChapterDeltaExtraction, list[ExtractionIssue]]`：丢弃不在正文中的条目，每丢一条记一个 warning `evidence_not_in_body`（path 形如 `movements[2]`）。例外：`plan_fulfillment` 中 status 为 `missing` 的条目可以没有 evidence；`time.evidence` 为空时只把 `time.elapsed` 置为 None，并记 `elapsed_without_evidence`。
    - `dropped_ratio(issues, extraction_before) -> float`
  - `chapter_input.py`：
    - `@dataclass(frozen=True) class ChapterExtractionInput`，字段：`project_id, chapter_number, entities: tuple[EntityRow, ...], locations: tuple[LocationRow, ...], calendar_units: tuple[str, ...], time_aliases: tuple[str, ...], fulfillment_targets: tuple[FulfillmentTarget, ...], intents: tuple[IntentRow, ...], secrets: tuple[SecretRow, ...], travel_modes: tuple[str, ...]`。
    - 行类型：`EntityRow(id, kind, name, aliases)`、`LocationRow(id, name, region_id, subworld_id)`、`FulfillmentTarget(target_id, description)`、`IntentRow(id, owner_id, summary, status)`、`SecretRow(fact_ref, proposition, sensitivity)`。
    - `build_chapter_extraction_input(session, *, project_id, chapter_number) -> ChapterExtractionInput`，数据来源：
      - BookState as-of N−1 中处于激活状态的 character / faction / organization / group / item / location 节点；
      - 运行时地图节点；
      - `StoryCalendarRepository` 与 `BookLexiconRepository`；
      - ChapterPlan 的 goals，target_id 为 `goal:<序号从1起>`；
      - 本章 `obligations_to_resolve` 中的叙事义务，target_id 为 `obligation:<id>`；
      - 本章体验计划的 `planned_reward_tags`，target_id 为 `reward:<tag>`；
      - 状态为 planned 或 due 的 intent 节点；
      - sensitivity 为 hidden 或 secret 的事实。
  - `WriterOutput.chapter_delta: ChapterDeltaExtraction | None = None`（新字段，计入准入指纹）。

- [ ] **Step 1: 写失败测试（`tests/test_chapter_delta_contract.py`）**

- `test_schema_rejects_unknown_enum_tokens`：非法的 knowledge state、edge_type、beat_type、significance 各自引发 `ValidationError`。
- `test_evidence_normalization_ignores_whitespace_only`：正文 “甲走进\n  城门。”，evidence “甲走进城门” → 命中；改写成 “甲进了城门” → 不命中。
- `test_validate_evidence_drops_and_reports`：两条 movement，一条证据有效、一条无效 → 保留 1 条；issues 中有 `evidence_not_in_body`，path 为 `movements[1]`；status 为 missing 且 evidence 为空的 plan_fulfillment 被保留。
- `test_build_input_collects_entities_locations_targets_secrets`（postgres，用 Task 15 P1 的 fixture 或测试内种子）：实体表含种子角色；地点表含地图节点；targets 含 `goal:1` 与 `reward:<tag>`；secrets 含 secret 事实；`calendar_units` 等于日历单位名。
- `test_writer_output_chapter_delta_round_trips_and_changes_fingerprint`：给 WriterOutput 设置 `chapter_delta` 后，`writer_output_admission_fingerprint` 发生变化。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_contract.py`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 按 Interfaces 实现；更新 extraction 包的文件清单测试**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_contract.py tests/test_architecture_boundaries.py tests/test_entity_admission.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(extraction): add the chapter delta contract, evidence validation and extraction input`

---

### Task 2: 抽取调用：提示词、`ChapterDeltaExtractor` 与路由

**Files:**
- Create: `forwin/writer/prompt_core/chapter_delta.py`、`forwin/book_state/extraction/chapter_delta_extractor.py`
- Modify: `forwin/writer/llm/routing.py:207-270`（`_llm_task_route`）、`forwin/llm/router.py:45-50,447-473`（codex 主路由）、`forwin/book_state/story_clock.py`（加固）、`forwin/book_state/lexicon.py`（加固）
- Test: `tests/test_chapter_delta_extractor.py`（新建）、`tests/test_llm_client_retry.py`（路由用例）、`tests/test_story_clock.py`、`tests/test_book_lexicon.py`

**Interfaces:**
- Consumes：Task 1 的全部契约。
- Produces：
  - `build_chapter_delta_messages(*, extraction_input: ChapterExtractionInput, chapter_title: str, chapter_body: str, chapter_summary: str, feedback: Sequence[str] = ()) -> list[dict]`：
    - system 段要求只输出 JSON，并说明全部字段和引用规则（引用只能取自给出的表，新实体用 `new:<名称>`，每条都附正文原文引用，时长单位只能取自 `calendar_units` 与 `time_aliases`，秘密清单只用于给条目打 `touches_secret_ref` / `contradicts_ref` 标签，不得当作正文事实）；
    - user 段依次为：实体表、地点表、日历单位与别名、出行方式 id、计划目标（target_id 与描述）、相关意图、秘密清单（含真相文本）、本章正文、本章摘要，以及 `feedback`（上一轮的错误）。
  - `@dataclass(frozen=True) class ChapterDeltaExtractionResult: extraction: ChapterDeltaExtraction | None; issues: tuple[ExtractionIssue, ...]; attempts: int; prompt_trace: dict[str, Any]`
  - `class ChapterDeltaExtractor: __init__(self, llm_client, *, max_attempts: int = 3)`；`extract(self, *, extraction_input, chapter_title, chapter_body, chapter_summary, calendar: StoryCalendar | None, lexicon: BookLexicon) -> ChapterDeltaExtractionResult`：
    - 用 `stage_key="chapter_delta_extraction"`、`task_family="writer"`、`response_format={"type": "json_object"}`、`output_schema=ChapterDeltaExtraction.model_json_schema()` 调用；
    - 解析或 schema 校验失败 → 带错误反馈重试；
    - 证据校验后被丢弃的比例 > 0.5 → 带反馈重试一次；
    - `time.elapsed` 的单位无法换算（捕获 `UnknownTimeUnit`、`StoryClockError`）→ 带反馈重试一次，仍失败则把 `elapsed` 置为 None 并记 warning `elapsed_unknown`；
    - 所有尝试都失败 → `extraction=None` 加上 error `extraction_failed`；
    - 结果的 `body_sha256 = sha256(body)`；
    - 每次尝试都记录进 `prompt_trace`（与 writer 的 `_build_prompt_trace` 格式一致，`template_id="extraction:chapter_delta"`）。
  - `elapsed_minutes(extraction: ChapterDeltaExtraction, calendar: StoryCalendar | None, lexicon: BookLexicon) -> int | None`
  - 路由：`_llm_task_route` 对含 `chapter_delta` 的 stage_key 返回 `"canon_extraction"`；`CODEX_PRIMARY_WRITER_STAGES` 加入 `chapter_delta_extraction`。
  - 加固（P1 延后项）：
    - `duration_to_minutes` 对 nan、inf、负数、以及乘积超出 `2**53` 抛 `StoryClockError`；`format_story_time` 对负分钟抛 `StoryClockError`；
    - `lexicon_from_payload` 拒绝 inf 倍率，以及空白的名称和 id（`LexiconError`）。

- [ ] **Step 1: 写失败测试（`tests/test_chapter_delta_extractor.py`；桩 LLM 按调用顺序返回预设 JSON）**

- `test_valid_extraction_passes_through_with_body_hash`
- `test_schema_error_retries_with_feedback`：第一次返回缺字段的 JSON → 第二次的 messages 中包含错误信息；结果有效，`attempts == 2`。
- `test_paraphrased_evidence_is_dropped_reported_and_retried`：第一次 4 条中有 3 条证据无效 → 带反馈重试；第二次有效 → issues 只含第二次被丢弃的条目。
- `test_unknown_elapsed_unit_retries_then_degrades_to_zero`：两次都给出单位“天”（日历只有“日”）→ `elapsed is None`，issues 中有 `elapsed_unknown`，结果非 None。
- `test_all_attempts_fail_returns_extraction_failed`
- `test_prompt_carries_tables_and_secret_list_but_marks_it_label_only`：messages 中含实体 id、地点 id、`goal:1`，以及秘密真相文本，并含“只用于打标签”的说明。
- 路由：`test_chapter_delta_extraction_routes_to_canon_extraction`（`tests/test_llm_client_retry.py`）。
- 加固：`test_duration_rejects_non_finite_and_overflow`、`test_lexicon_rejects_inf_and_blank_names`。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_extractor.py tests/test_story_clock.py tests/test_book_lexicon.py tests/test_llm_client_retry.py -k "chapter_delta or non_finite or inf_and_blank or routes"`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现**

- [ ] **Step 4: 运行，确认通过**

Run: 同 Step 2（去掉 `-k`），再加上 `tests/test_codex_bridge.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(extraction): add the single-call chapter delta extractor`

---

### Task 3: `ChapterDeltaTranslator`：抽取结果 → GraphDelta

**Files:**
- Create: `forwin/book_state/extraction/translator.py`
- Modify: `forwin/book_state/runtime.py`（edge 重新激活）、`forwin/book_state/repository.py`（如需要，补 map 节点或边的读取辅助函数）
- Test: `tests/test_chapter_delta_translator.py`（新建）

**Interfaces:**
- Consumes：Task 1 的契约；P1 的 `knowledge_event_to_patch`、`seed_ref_id` 风格的稳定 id、`duration_to_minutes`、`format_story_time`、`EntityAdmissionPlan`。
- Produces：
  - `@dataclass(frozen=True) class TranslationResult: delta: GraphDelta | None; issues: tuple[ExtractionIssue, ...]`
  - `class ChapterDeltaTranslator: __init__(self, session)`；`translate(self, *, project_id: str, chapter_number: int, extraction: ChapterDeltaExtraction, admission_plan: EntityAdmissionPlan, chapter_summary: str, review_verdict_id: str = "") -> TranslationResult`
  - 生成的 delta：
    - id 为 `f"chapter-delta:{project_id}:{chapter_number}:{extraction.body_sha256[:16]}"`；
    - `source_type="chapter_extraction"`、`delta_type="world_state"`、`operation="apply_chapter_extraction"`、`target_type="chapter"`；
    - `story_time_start` = `BookStateQuery.current_story_minute(as_of_chapter=N-1)`，没有时取日历的 `opening_minute`，再没有取 0；`story_time_end` = start + `elapsed_minutes`（None 时按 0）；`story_time` = `format_story_time(calendar, end)`，没有日历时为空串；
    - `metadata` 含 `extraction_path="chapter_extraction"`、`movements`、`knowledge_tags`、`flashback_refs`、`plan_fulfillment`、`intent_interactions`、`time_elapsed_minutes`。
  - 映射规则（读取 as-of N−1 的运行时，所有非 create 补丁都带 `old_value` 与 `field_path`）：

| 抽取条目 | 补丁 |
| --- | --- |
| `new:<名称>` 的引用 | 用准入计划解析：`register_character` 用 `entity_id`，`register_alias` 用目标 id；非角色的新实体用稳定 id `f"{kind}-{sha256(project\0kind\0name)[:20]}"` 创建（NodePatch create）；`background_generic` 的引用被忽略；其余 → issue `unresolved_ref` |
| `entity_change` | 字段必须在 `WORLD_NODE_FIELDS` 的 state 或 profile 白名单内（否则 issue `unknown_field`），生成 `NodePatch(op="set", field_path="state.<f>"或"profile.<f>")`；`flashback=True` 时不生成补丁，记入 `flashback_refs` |
| `relation_change` | `create` → `EdgePatch(create)`，族由 edge_type 唯一确定，appellation 写入 metadata；`end` → `deactivate`（`field_path="is_active"`, `old_value=True`）；`set` → `field_path` 与 `old_value` 都必填；对已结束的同 id 边再执行 `create` → 重新激活（`is_active=True`，清除 `ended_at_chapter`，runtime 支持该操作） |
| `knowledge_event` | `new_fact` 先生成 `FactPatch(create)`；再生成 `knowledge_event_to_patch(KnowledgeEvent(..., at=start))`；`reader` 观察者用 `ObserverType.READER`；`touches_secret_ref`、`contradicts_ref`、channel、source 写入 `metadata["knowledge_tags"]` |
| `movement` | `state.location_id` 的 set 补丁（`old_value` 取当前值）；`metadata["movements"]` 追加 `{entity_id, from_id, to_id, depart_minute, arrive_minute, travel_mode_id, evidence}`；`to_ref` 为 `new:<名称>` 时引用同章的 `new_site` |
| `new_site` | `MapPatch(map_node, create)`（id 为稳定 id，`subworld_id` / `region_id` 继承 `near_ref` 节点或 `region_ref`），加一条到 `near_ref` 的 `MapPatch(map_edge, create)`（edge_type `path`，`travel_time` 为 0.25 小时），再加 `NodePatch(site_state, create)`（`profile.map_node_id`）；`near_ref` 与 `region_ref` 都缺失 → issue `new_site_unanchored` |
| 路线被发现（移动走了观察者此前未知的边） | `CognitionPatch(field_path="visible_refs", op="append", new_value="map_edge:<id>")`，正反两个 id 都写 |
| `item_transfer` | item 节点的 `state.holder_id` set（old 为当前值）；`new_item` 先创建 |
| `event` | 生成 event NodePatch 与 `FactPatch(fact_type="event")`，与旧 writer contract 的形状相同；`visibility="secret"` 时 fact 的 sensitivity 为 secret |
| `thread_beat` | 生成 `plot_thread` 的 NarrativePatch（create 或 append beats；resolution 时把 status 置为 resolved） |
| `intent_interaction` | intent 节点 `state.status` set：disrupted → `disrupted`，fulfilled → `resolved`，advanced 不改状态；同时记入 metadata |

  - issues 中有任何 error（`unresolved_ref`、`unknown_field`、`new_site_unanchored`、`edge_type_invalid`、`missing_old_value`）→ 返回 `delta=None`。

- [ ] **Step 1: 写失败测试（`tests/test_chapter_delta_translator.py`，postgres；以 P1 的种子工具构造第 0 章世界）**

- `test_story_time_continues_from_previous_chapter`：上一章结束于 1000 分钟，本章 elapsed 为 2 日 → start = 1000，end = 1000 + 2880，label 由日历格式化。
- `test_movement_sets_location_and_records_movement_metadata`
- `test_new_site_creates_map_node_edge_and_site_state`
- `test_relation_create_end_and_reactivate`：create → 下一章 end → 再下一章 create 同 id → 运行时为激活状态，`ended_at_chapter` 为 None。
- `test_knowledge_events_compile_with_tags_and_reader_observer`
- `test_flashback_entity_change_emits_no_state_patch`
- `test_item_transfer_updates_holder_with_old_value`
- `test_new_character_ref_resolves_through_admission_plan`，以及 `test_unresolved_new_ref_blocks_translation`
- `test_every_non_create_patch_has_field_path_and_old_value`（遍历生成 delta 的全部补丁）
- `test_route_discovery_appends_visible_refs`

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_translator.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_translator.py tests/test_book_state_versioning.py tests/test_book_state_knowledge.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(extraction): translate chapter extractions into graph deltas`

---

### Task 4: 世界审查门 v6

**Files:**
- Modify: `forwin/book_state/reviewer.py`（新增检查；删除 `_writer_hidden_truth_issues` :93-126；把 `_movement_issues` :201-230 改写为 v6）、`forwin/runtime/policy.py`（`ReviewPolicy.travel_tolerance`）
- Create: `forwin/book_state/reveal_schedule.py`
- Test: `tests/test_world_gate_v6.py`（新建）、`tests/test_book_state_final.py:108`、`tests/test_book_state_writer_contract.py:715`（如断言了旧的移动错误码则改写）

**Interfaces:**
- Consumes：Task 3 delta 的 metadata（`movements`、`knowledge_tags`、`flashback_refs`）。
- Produces：
  - `ReviewPolicy.travel_tolerance: float = Field(default=0.2, ge=0.0, le=1.0)`
  - `BookStateReviewGate.review(changes, *, base_chapter: int | None = None, travel_tolerance: float = 0.2) -> BookStateReviewVerdict`。v6 检查只作用于 `source_type == "chapter_extraction"` 的 delta：

| code | severity | 规则 |
| --- | --- | --- |
| `time_offset_out_of_range` | error | 移动的 depart / arrive 分钟不在 [start, end] 之内，或 arrive < depart |
| `movement_unreachable` | error | 两地之间无路径 |
| `movement_route_unknown_to_mover` | error | 移动者有认知覆盖层，但只能经由它不知道的边到达（`shortest_path(observer=(type, id))` 不可达而客观路径可达）；同一 delta 中移动者对该边的发现补丁算作已知 |
| `movement_too_fast` | error | 声称耗时 < 最短耗时 × (1 − tolerance) |
| `movement_fast_within_tolerance` | warning | 最短耗时 × (1 − tolerance) ≤ 声称耗时 < 最短耗时 |
| `restrained_entity_moved` | error | 移动者 as-of N−1 的 `restraint.state` 不为 free，且同一 delta 中此前没有把它置为 free 的 entity_change |
| `dead_entity_acts` | error | as-of N−1 的 `alive_status == "dead"`（或同章更早被置为 dead）的实体出现在非闪回的 event 参与者、移动者、channel 为 told 的知识来源、或观察者中 |
| `item_holder_mismatch` | error | `item_transfer` 的 from 不等于 item 当前的 `state.holder_id`（同一 delta 内按顺序推进） |
| `told_by_unaware_source` | error | channel 为 told，但 source 对该事实的 `knowledge_state` 不属于 {knows, believes} |
| `unexplained_secret_knowledge` | warning | 观察者通过 channel `rumor` 对 hidden / secret 事实达到 `knows` |
| `premature_reveal` | error | reader 对 hidden / secret 事实推进（或标签 `touches_secret_ref` 指向它），且本章 < 该事实的最早揭示章节 |
| `unscheduled_reveal` | warning | 同上，但该事实没有揭示时间表 |
| `contradicts_hidden_fact` | error | `contradicts_ref` 指向 hidden / secret 事实 |

  - 最短耗时（分钟）= 路径 travel_time（小时）× 60 ÷ 速度倍率。速度倍率取移动者 `travel_mode_ids` 中、`allowed_edge_types` 覆盖路径上全部边类型的出行方式里最大的 `speed_multiplier`；没有合格的出行方式时取 1.0。声称耗时 = arrive − depart，两者缺一时为本章时长。
  - `reveal_schedule.py`：`earliest_reveal_chapters(session, project_id) -> dict[str, int]`，键为 fact 节点 id。来源是激活中的 Genesis revision 的 `world_seed.reveal_candidates`，用 `seed_ref_id(project, "fact", fact_id)` 映射到节点 id，再取对应卷的 `chapter_start`。
  - 删除 `_writer_hidden_truth_issues`（由揭示检查取代）。
  - 调用方传入 `travel_tolerance`：`canon/preparation.py`（取项目策略中的值）；`world_seed_committer` 与 `proposal_review` 用默认值。

- [ ] **Step 1: 写失败测试（`tests/test_world_gate_v6.py`，postgres；手工构造 chapter_extraction delta）**

每条规则至少一个触发用例和一个不触发用例，另外包括：
- `test_movement_uses_movers_known_routes`（Review Focus 3）
- `test_travel_mode_speed_requires_allowed_edge_types`：出行方式倍率为 4，但不允许路径上的 `mountain_pass` → 按 1.0 计算 → `movement_too_fast`；允许时通过。
- `test_flashback_dead_character_is_allowed`
- `test_premature_reveal_uses_world_seed_schedule`（两卷，秘密排在第 2 卷）
- `test_non_extraction_deltas_skip_v6_checks`（种子与 world edit 不受影响）

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_world_gate_v6.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_world_gate_v6.py tests/test_book_state_final.py tests/test_book_state_canon_gate.py tests/test_world_seed_committer.py tests/test_knowledge_system_v46.py tests/test_runtime_policy.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(book_state): add world review gate v6 checks for chapter extractions`

---

### Task 5: 切换：每个候选正文都抽取，实体准入与 canon 改走新路径，删除 writer contract 与正则兜底

**Files:**
- Modify:
  - `forwin/generation/pipeline_core/review_autofix.py:340-359`（`_plan_writer_output_entities` → 先确保抽取，再做准入）
  - `forwin/review/repair/service.py`（全部 6 处调用点经由上面的函数，无需改动调用点，但需核实）
  - `forwin/naming/entity_registrar.py:998-1038`（名字来源改为 `chapter_delta`）
  - `forwin/canon/preparation.py:75-146`（`BookStateCanonPreparer` 改用翻译器与审查门 v6）
  - `forwin/generation/pipeline_core/acceptance.py:44-102`（人工接受路径）
  - `forwin/canon/preparation.py`（`CanonPreparationContext` 提供抽取器）
- Delete: `forwin/book_state/writer_contract.py`、`forwin/book_state/extraction/graph_delta.py`（连同 `BookStateGraphDeltaExtractor`、正则兜底 `_light_state_extraction_delta` / `_unique_light_names`），以及 `forwin/book_state/repository.py` 中 `metadata.writer_location` / `metadata.writer_state.*` 的回卷特例（:932-940、:1076-1090）
- Modify: `tests/overfit_allowlist.toml`（删除 graph_delta 的 2 个 P2 条目）、`tests/test_no_story_content_in_runtime.py:50-53`、`tests/test_architecture_boundaries.py:1102-1114`
- Test: `tests/test_chapter_delta_pipeline.py`（新建），并改写 `tests/test_book_state_writer_contract.py`（删除或迁移到翻译器用例）、`tests/test_pulp_pipeline_bypass.py:448,485,508`、`tests/test_book_state_canon_gate.py`、`tests/test_entity_admission.py`、`tests/test_reference_classifier_scope.py:219,268`

**Interfaces:**
- Consumes：Task 1–4。
- Produces：
  - `ensure_chapter_delta(session, *, llm_client, project_id: str, chapter_number: int, writer_output: WriterOutput) -> tuple[WriterOutput, ChapterDeltaExtractionResult | None]`（放在 `forwin/book_state/extraction/chapter_delta_extractor.py`）：
    - `writer_output.chapter_delta` 存在且 `body_sha256 == sha256(body)` 时，原样返回并带 `None`；
    - 否则构建输入并抽取，返回 `writer_output.model_copy(update={"chapter_delta": result.extraction})` 和结果；
    - 抽取的 PromptTrace 由调用方保存（`trace_scope="writer"`，`stage_key="chapter_delta_extraction"`）；
    - 抽取 issues 写入 `writer_output.generation_meta["chapter_delta_issues"]`（不计入指纹）。
  - `_plan_writer_output_entities` 先调用 `ensure_chapter_delta`，再做准入。抽取失败（`extraction is None`）时照常返回 output，`chapter_delta` 为 None，由 draft review 和 canon 阻断（见下）。
  - 实体准入的名字来自 `chapter_delta`：
    - `entity_mentions` 中具名、在场的角色；
    - `new_entity.kind == "character"` 的条目；
    - 所有 `new:<名称>` 形式的 event 参与者、移动者、知识观察者；
    - 旧结构字段不再读取。
  - `BookStateCanonPreparer.prepare(...)`：
    - `ensure_chapter_delta`，正文变了就重新抽取；`context.llm_client` 不可用且需要重新抽取时阻断，`gate_id="chapter_delta_extraction"`、`blocked_path="chapter-delta-extraction-blocked"`；
    - `ChapterDeltaTranslator.translate`，有 issue 时阻断，`gate_id="chapter_delta_translation"`；
    - 构造 `ApprovedGraphDeltaSet(approved_by=["chapter_extraction"])`；
    - `BookStateReviewGate.review(..., base_chapter=N-1, travel_tolerance=policy.review.travel_tolerance)`。
    - 原有的 `book_state_review` 阻断路径保持不变。
  - 人工接受路径：从 artifact 重建 WriterOutput 后，同样经过 `ensure_chapter_delta`。
  - writer 的三次抽取在本任务保留（Task 6 删除），这样中间态下其余消费者仍然可用。

- [ ] **Step 1: 写失败测试（`tests/test_chapter_delta_pipeline.py`；桩 LLM 同时桩出 writer 与 `chapter_delta_extraction`）**

- `test_every_candidate_gets_an_extraction_before_review`：一次 draft review 中，桩抽取器被调用一次，WriterOutput 带有 `chapter_delta` 且 `body_sha256` 匹配。
- `test_local_rewrite_triggers_reextraction`（Review Focus 4）：修复循环中的本地改写改变正文后，抽取器再次被调用，新 `body_sha256` 与新正文一致。
- `test_unchanged_body_reuses_extraction`
- `test_admission_reads_new_refs_from_chapter_delta`：`new:陈七` 作为 event 参与者 → 准入计划中有对应决定；旧字段为空也能正常工作。
- `test_canon_commits_translated_delta`：完整一章 → `graph_deltas` 中有 `source_type="chapter_extraction"` 的行，start / end 正确。
- `test_extraction_failure_blocks_canon_without_regex_fallback`
- `test_human_accept_with_stale_extraction_reextracts_or_blocks`（Review Focus 5）：正文与 `body_sha256` 不一致 → 有 LLM 时重新抽取后提交，无 LLM 时阻断，并记录 `chapter-delta-extraction-blocked`。
- 删除正则兜底的三个用例（`test_pulp_pipeline_bypass.py:448,485,508`），换成 `test_empty_extraction_blocks_canon`。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_pipeline.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现，删除旧模块，迁移相关测试**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_pipeline.py tests/test_book_state_canon_gate.py tests/test_entity_admission.py tests/test_canon_atomic_transaction.py tests/test_canon_commit_plan.py tests/test_canon_repair_stage.py tests/test_candidate_to_canon_flow_v5.py tests/test_generation_worker_canon_recovery.py tests/test_pulp_pipeline_bypass.py tests/test_reference_classifier_scope.py tests/test_architecture_boundaries.py tests/test_v5_canon_architecture.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(extraction): extract every candidate and commit chapters through the translator and gate v6`

---

### Task 6: 消费者迁移到 `chapter_delta`；删除 writer 三次抽取与旧结构字段

**Files:**
- Modify:
  - `forwin/writer/chapter_writer.py`（删除 `_extract_structured`、`_extract_structured_part`、窗口与合并逻辑、payoff 引文恢复、`_complete_scene_fallback_structured_extraction`，以及 generation_meta 中的 structured_extraction 键）
  - `forwin/writer/prompt_core/extraction.py`（删除）、`forwin/writer/prompt_core/__init__.py`
  - `forwin/protocol/writer.py`、`forwin/protocol/state_change.py`（删除 `state_changes`、`new_events`、`delivered_payoffs`、`thread_beats`、`time_advance`、`lore_candidates`、`timeline_hints`、`writer_notes`、`entity_mentions`、`scene_continuation` 字段，以及仅被这些字段使用的模型）
  - `forwin/checker/rules.py`（死亡检查改读 `alive_status`，删除 `DEAD_STATUS_KEYWORDS`；state / event / thread 检查改读 `chapter_delta`）
  - `forwin/checker/hard_floor.py:63-99`、`forwin/checker/pulp_beat.py:629+`（事件来自 `chapter_delta.events`；已兑现 payoff 来自 status 为 done、target_id 为 `reward:` 开头且带证据的 `plan_fulfillment`）
  - `forwin/review/webnovel.py`、`forwin/review/llm_webnovel.py`（证据与时间改读 `chapter_delta`）
  - `forwin/candidate_drafts.py:184-222`（三列 JSON 改由 `chapter_delta` 填充：`entity_changes` / `events` / `thread_beats`）
  - `forwin/generation/pipeline_core/writer_attention.py:143-152`（计数）
  - `forwin/generation/pipeline_core/chapter_execution_support.py:150-181`（删除 `defer_structured_extraction_if_needed` 及其调用点 `project_chapters.py:693`）
  - `forwin/llm_eval/validators.py`、`forwin/llm_eval/cases.py`、`tests/fixtures/llm_eval/cases.json`
  - `tests/overfit_allowlist.toml`（删除 `DEAD_STATUS_KEYWORDS` 条目）
- Test: 迁移所有构造旧字段的测试（`test_writer_split_pipeline.py`、`test_chapter_writer_extraction_windows.py`（删除）、`test_canon_repair_stage.py`、`test_map_world_integration.py`、`test_repair_preserved_title.py`、`test_review_final_body.py`、`test_reviewer_split.py`、`test_skill_runtime.py`、`test_canon_quality_reviewer_payload.py`、`test_subworld_control.py`、`test_hard_floor.py`、`test_pulp_beat_verifier.py`、`test_llm_eval_core.py`、`test_llm_eval_runner.py` 等；以 `rg -n "state_changes|new_events|thread_beats|time_advance|delivered_payoffs|entity_mentions|lore_candidates" tests forwin` 复核）；新建 `tests/test_chapter_delta_consumers.py`

**Interfaces:**
- Consumes：`WriterOutput.chapter_delta`（Task 1、5）。
- Produces：
  - `ContinuityChecker._check_dead_characters`：as-of N−1 中 `alive_status == "dead"` 的角色（名字加别名）出现在非闪回的 `chapter_delta.events[].participant_refs`（按 id 匹配）或 `movements[].entity_ref` 中 → `dead_character_active`（error）。不再看 `state.status` 文本，也不再限于主角和反派角色。
  - 辅助函数 `delivered_payoffs_from_fulfillment(chapter_delta) -> tuple[DeliveredPayoffCandidate, ...]`（放在 `forwin/checker/pulp_beat.py`，`DeliveredPayoffCandidate` 模型保留在该模块内部）。
  - writer 产出的 WriterOutput 只含正文、标题、摘要、scene_outputs、generation_meta、prompt_revision_hash 等，以及（经 Task 5 补上的）`chapter_delta`。

- [ ] **Step 1: 写失败测试（`tests/test_chapter_delta_consumers.py`）**

- `test_dead_character_check_reads_alive_status_not_status_text`：`state.status = "已死"` 但 `alive_status = "alive"` → 不报；`alive_status = "dead"` 且出现在 event 参与者中 → 报 error。
- `test_hard_floor_counts_events_from_chapter_delta`
- `test_pulp_payoff_from_plan_fulfillment_reward_targets`
- `test_candidate_columns_filled_from_chapter_delta`
- `test_writer_no_longer_runs_structured_extraction`：scene 模式的桩 LLM 只收到 scene 相关的 stage_key，没有 `state_event_extraction`、`thread_time_extraction`、`lore_timeline_notes_extraction`。
- `test_writer_output_has_no_legacy_structured_fields`：`WriterOutput.model_fields` 中不再有上述旧字段。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_consumers.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改生产代码并迁移测试**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_consumers.py tests/test_writer_split_pipeline.py tests/test_hard_floor.py tests/test_pulp_beat_verifier.py tests/test_reviewer_split.py tests/test_canon_repair_stage.py tests/test_subworld_control.py tests/test_llm_eval_core.py tests/test_llm_eval_runner.py tests/test_no_story_content_in_runtime.py tests/test_prompt_revision.py`
Expected: PASS（prompt 快照若因删除抽取提示词而变化，按 P0 Task 8 Step 4 的方法重新生成）

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor: read chapter facts from the chapter delta and drop the three-pass writer extraction`

---

### Task 7: 计划核验改用 `plan_fulfillment`，删除关键词判定

**Files:**
- Modify: `forwin/review/plan_reviewer.py`、`forwin/review/plan_checks.py`（删除 `_task_is_satisfied`、`evaluate_constraint_issues`、`_constraint_triggered`、`evaluate_resource_closure_risk`、`evaluate_next_band_task_compatibility` 中的关键词部分，以及 `evaluate_director_imbalance` 中的标记词）、`forwin/generation/pipeline_core/audit_control.py:16-23,694-980`（band checkpoint 不再调用关键词函数）、`tests/overfit_allowlist.toml`（删除 `constraint_keywords.py` 的 9 个条目）
- Delete: `forwin/review/constraint_keywords.py`
- Test: `tests/test_plan_checks.py`、`tests/test_plan_constraint_keywords.py`（删除）、`tests/test_plan_fulfillment_review.py`（新建）、`tests/test_repair_verification.py`、`tests/review_decision/test_review_outcome_engine_only.py`

**Interfaces:**
- Produces：
  - `PlanContractReviewer.issues(*, context, writer_output)`：
    - 对每个 `goal:<n>` 目标：抽取中缺少该目标，或 status 为 `missing` → error `plan_goal_missing`（`issue_type="plan_task_fulfillment"`，suggested_fix 写明目标描述）；`partial` → warning `plan_goal_partial`。
    - `obligation:` 与 `reward:` 目标不在这里判（分别由 Task 8 和 hard floor 处理）。
    - `chapter_delta` 为 None → error `plan_fulfillment_unavailable`。
  - band checkpoint 中依赖关键词的风险项全部删除；保留结构化的 `evaluate_band_obligation_contract`。

- [ ] **Step 1: 写失败测试（`tests/test_plan_fulfillment_review.py`）**

- `test_missing_goal_is_error_partial_is_warning`
- `test_goal_absent_from_extraction_counts_as_missing`
- `test_no_extraction_is_error`
- `test_keyword_module_is_gone`：`importlib.util.find_spec("forwin.review.constraint_keywords") is None`。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_plan_fulfillment_review.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_plan_fulfillment_review.py tests/test_plan_checks.py tests/test_repair_verification.py tests/review_decision tests/test_post_canon_maintenance.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(review): judge plan goals by plan fulfillment and drop keyword checks`

---

### Task 8: 叙事义务改用 `plan_fulfillment` 核验

**Files:**
- Modify: `forwin/canon_quality/obligation_verifier.py`（删除 `_verify_by_type`、`_CAUSAL_MARKERS`、`_matched_payoff_markers`、`_PAYOFF_STOP_WORDS`；`verify` 改为读取 `plan_fulfillment`）、`forwin/generation/pipeline_core/obligation_resolution.py:11-33`、`forwin/generation/pipeline_core/quality_gates.py:357`、`forwin/generation/pipeline_core/world_projection.py:124`、`tests/overfit_allowlist.toml`（删除 2 个 P2 条目，以及 `_PAYOFF_STOP_WORDS` / `_matched_payoff_markers` 的 language_lexicon 条目）
- Test: `tests/test_obligation_resolution_verifier.py`、`tests/test_final_gate_obligation_clearance.py`、`tests/test_canon_repair_stage.py:482`

**Interfaces:**
- Produces：
  - `ObligationResolutionVerifier.verify(*, obligation, plan_fulfillment: Sequence[PlanFulfillment]) -> ObligationResolutionCandidate`：target `obligation:<id>` 的 status 为 done 且有 evidence → resolved（`matched_markers` 改放 evidence）；partial 或 missing → unresolved。
  - `verify_due_obligations_for_draft(*, obligations, chapter_number, chapter_delta: ChapterDeltaExtraction | None, evidence_ref="", verifier=None)`
  - `verify_active_obligations_after_acceptance(*, session, project_id, chapter_number, verifier=None)`：从本章已提交的 `chapter_extraction` delta 的 `metadata["plan_fulfillment"]` 读取核验结果。

- [ ] **Step 1: 改写测试**

- 原有的标记词用例改为 plan_fulfillment 的 done / partial / missing 三种情形。
- 新增 `test_after_acceptance_reads_committed_delta_fulfillment`（postgres）。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_obligation_resolution_verifier.py tests/test_final_gate_obligation_clearance.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改**

- [ ] **Step 4: 运行，确认通过**

Run: 同 Step 2，再加上 `tests/test_canon_repair_stage.py tests/test_narrative_obligation_ledger.py tests/test_obligation_context_injection.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(obligations): verify obligations from plan fulfillment instead of marker words`

---

### Task 9: draft review 的 `world_consistency` 信号；删除 MapMovementReviewer

**Files:**
- Create: `forwin/review/world_consistency.py`
- Modify:
  - `forwin/review/draft_service.py`（新信号接入 :94-364；must_fix 缺口修复 :293-332）
  - `forwin/runtime/policy.py:86,102`（两个 profile 的 signals：standard 用 `world_consistency` 替换 `map_movement`，pulp 新增 `world_consistency`）
  - `forwin/runtime/container.py:386-401`
  - `forwin/review/webnovel.py:12,46-76,256`（删除 `include_map_movement`）
  - `tests/overfit_allowlist.toml`（删除 map_movement 的条目）
- Delete: `forwin/review/map_movement.py`
- Test: `tests/test_world_consistency_review.py`（新建）、`tests/test_reviewer_split.py:206-245`、`tests/test_map_world_integration.py:306-553`（迁移为 world_consistency 用例或删除）、`tests/test_runtime_policy.py`、`tests/test_pulp_pipeline_bypass.py:98,125`、`tests/test_publisher_compliance_reviewer.py:99,121`、`tests/test_canon_name_anchors.py:228`

**Interfaces:**
- Produces：
  - `class WorldConsistencyReviewer: name = "world_consistency"`；`review(self, *, session, project_id, chapter_number, writer_output, travel_tolerance) -> ReviewVerdict`：
    - 用 writer_output 中准入计划的 `ChapterDeltaTranslator.translate`；
    - 翻译 issue → error（`issue_type="continuity"`，`reviewer="world_consistency"`）；
    - 再运行 `BookStateReviewGate.review(base_chapter=N-1, ...)`，审查门 issue 按其 severity 转为 `ContinuityIssue`；
    - `generation_meta["chapter_delta_issues"]` → warning；
    - `chapter_delta is None` → error `chapter_delta_missing`。
  - `DraftReviewService.__init__(..., world_consistency_review_enabled: bool = True, world_consistency_reviewer=None)`。
  - verdict 合并时加入它。只要存在任何 writer 可修复的 error，`repair_instruction` 就不得为 None：在 :306 的触发条件中加入 world_consistency，或为它单独建 `_world_repair_instruction`（scope `"draft"`，failure_type `"continuity"`）。
  - `RuntimePolicy` 的 signals 默认值：standard 为 `("experience", "lint", "world_consistency", "personality", "canon_quality", "publisher")`，pulp 为 `("lint", "world_consistency", "publisher")`。

- [ ] **Step 1: 写失败测试（`tests/test_world_consistency_review.py`）**

- `test_world_gate_error_fails_draft_with_must_fix`：一个 `movement_too_fast` → verdict 为 fail，must_fix 中含该描述，`repair_instruction` 不为 None。
- `test_world_warning_only_is_warn_not_fail`
- `test_extraction_issues_surface_as_warnings`
- `test_missing_chapter_delta_is_error`
- `test_map_movement_module_is_gone`
- `test_policy_defaults_include_world_consistency`

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_world_consistency_review.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改并迁移测试**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_world_consistency_review.py tests/test_reviewer_split.py tests/test_runtime_policy.py tests/test_pulp_pipeline_bypass.py tests/test_publisher_compliance_reviewer.py tests/test_canon_name_anchors.py tests/test_full_decoupling_regression.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(review): add the world consistency signal and fold map movement into the world gate`

---

### Task 10: 删除 canon_quality 的倒计时与角色状态平行账本

**Files:**
- Create: `forwin/migrations/versions/0004_drop_parallel_ledgers.py`
- Modify:
  - `forwin/models/canon_quality.py:72-117`（删除两个 ORM 类）、`forwin/models/__init__.py`
  - `forwin/canon_quality/repository.py:173-297`、`forwin/canon_quality/cache.py:143-153`、`forwin/canon_quality/service.py:45-81,261-304`
  - `forwin/canon_quality/chapter_review_form/{form_builder,canon_projector,service,replay,pruning,form_schema}.py`（form 的行改由 BookState 构建）
  - `forwin/canon_quality/signals.py:94-123`、`forwin/canon_quality/types.py:26-27`
  - `forwin/book_state/query_interface.py:109-165`（删除 countdown，invariants 不再来自倒计时）
  - `forwin/context/assembler_core/canon_quality_context.py:24-275,469-550`（删除 countdown / custody 段与三个标记词表）、`forwin/context/assembler_core/__init__.py`
  - `forwin/writer/prompt_core/constraints.py:178-263`（删除倒计时与角色状态段）
  - `forwin/canon_names.py`（删除 `extract_candidate_character_names`）
  - `tests/overfit_allowlist.toml`（删除 5 个 P2 条目）
- Test: `tests/test_canon_quality_repository.py`、`tests/test_bookstate_query_interface.py`、`tests/test_countdown_live_state_source.py`（删除）、`tests/test_chapter_review_form_api_flow.py`、`tests/test_canon_quality_cache.py`、`tests/test_chapter_review_form_dry_run.py`、`tests/test_canon_quality_service.py`、`tests/test_chapter_review_form_architecture_boundaries.py`、`tests/test_writer_prompt_contract.py`、`tests/test_prompt_regression_samples.py`、`tests/test_v5_live_migration.py`、`tests/test_v5_recovery_schema.py`、`tests/test_architecture_boundaries.py`、`tests/test_chapter_review_form_from_book_state.py`（新建）

**Interfaces:**
- Produces：
  - 迁移：`revision = "0004_drop_parallel_ledgers"`、`down_revision = "0003_book_state_v6"`；`upgrade()` 删除两张表及其索引；`downgrade()` 按 0001 原样重建。迁移文件列表测试加入 0004；`EXPECTED_REVISION` 改为 0004。
  - `build_form(...)`：
    - `characters` 行来自 BookState as-of N−1 的角色：`prior_life_state` 取 `alive_status`，`prior_custody_state` 取 `restraint.state`，再加上 `location_id`；
    - `countdowns` 行改为 clock 节点：`label`，剩余分钟 = `due_at` − 当前故事分钟；
    - 字段名保持不变，LLM 表单的 schema 不变。
  - `canon_projector` 不再产出 transitions 和 countdown entries。`ProjectionResult` 去掉这两个字段，`ChapterReviewFormResult` 同步。
  - `canon_quality_context` 不再产出 `countdown_constraints`、`character_state_constraints`，以及由倒计时派生的 `invariant_constraints`。

- [ ] **Step 1: 写失败测试**

- `tests/test_chapter_review_form_from_book_state.py`：
  - `test_form_character_rows_come_from_book_state`
  - `test_form_countdown_rows_come_from_clock_nodes`
- `test_parallel_ledger_tables_are_gone`：ORM 元数据中不再有这两张表；迁移升级后的库中也没有。
- 删除依赖账本的旧用例，或把它们改写到 BookState。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_review_form_from_book_state.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改，写迁移，迁移测试**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_review_form_from_book_state.py tests/test_canon_quality_service.py tests/test_canon_quality_cache.py tests/test_chapter_review_form_api_flow.py tests/test_chapter_review_form_dry_run.py tests/test_bookstate_query_interface.py tests/test_writer_prompt_contract.py tests/test_prompt_regression_samples.py tests/test_v5_live_migration.py tests/test_v5_recovery_schema.py tests/test_architecture_boundaries.py tests/test_no_story_content_in_runtime.py`
Expected: PASS（prompt 快照按 P0 Task 8 Step 4 的方法重新生成，并核对 diff 只涉及删除的段落）

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(canon_quality): drop the countdown and character state parallel ledgers`

---

### Task 11: 删除未来计划审计中的倒计时与羁押逻辑

**Files:**
- Delete: `forwin/planning/future_plan_audit/countdown.py`、`custody.py`、`countdown_drift_pre_audit.py`
- Modify:
  - `forwin/planning/future_plan_audit/auditor.py:15-37,76-111`
  - `forwin/planning/future_plan_audit/helpers.py`（删除 :15-62、:65-81、:102-130 与 :182-552 中的倒计时和羁押部分）
  - `forwin/planning/future_plan_audit/patches.py:26-31,69-134,173-208`
  - `forwin/planning/future_plan_audit/obligations.py:12-16,60-75,198-266`
  - `forwin/planning/future_plan_audit/ledger_state_drift_pre_audit.py:29-30,38-73,106-141,169-190`
  - `forwin/planning/plan_patch_validator.py:40-45`（删除 `canon_plan_staleness` / `future_plan_audit` 补丁类型）
  - `tests/overfit_allowlist.toml`（删除 helpers 的 14 个条目）
- Test: `tests/test_future_plan_auditor.py`（删除倒计时与羁押用例；:824 改为基于义务）、`tests/test_future_plan_audit_persistence.py`、`tests/test_plan_patch_validator.py:79,131`、`tests/test_plan_patcher_loop_closure.py`

**Interfaces:**
- Produces：`FuturePlanAuditor` 保留不变量绑定、义务与信号的写前补丁、band 与章节的义务绑定，以及 `form_invariant_drift`；`audit_and_apply` 的签名与 order-controls 的载荷结构不变。

- [ ] **Step 1: 改写测试**

- 删除倒计时与羁押用例。
- `test_audit_and_apply_clears_blocker_*` 改为以义务驱动。
- 新增 `test_future_audit_has_no_countdown_or_custody_mixins`：`FuturePlanAuditor.__mro__` 中不再有对应的 mixin。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_future_plan_auditor.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 删除并修改**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_future_plan_auditor.py tests/test_future_plan_audit_persistence.py tests/test_plan_patch_validator.py tests/test_plan_patcher_loop_closure.py tests/test_post_canon_maintenance.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(planning): drop countdown and custody audits from the future plan auditor`

---

### Task 12: Genesis 地图结构化（P1 延后项）

**Files:**
- Modify: `forwin/genesis/normalize.py:228-363`（map 规范化）、`forwin/genesis/messages.py`（map 阶段提示词）、`forwin/genesis/constants.py`（map 硬约束；allowlist 的 `max_literals` 同步）、`forwin/genesis/world_seed.py`（加锁时的地点校验）、`forwin/map/genesis_adapter.py`（复用节点集合）
- Test: `tests/test_genesis_map_structure.py`（新建）、`tests/test_genesis_world_seed.py`

**Interfaces:**
- Produces：
  - map 规范化：
    - `submaps[].scope` 归一到 `("continent", "realm", "plane", "underground")`，其余为 `continent`；
    - `submaps[].danger_level` 与 `nodes[].danger_level` 为 0–5 的 int（缺失或非法时为 None，不编造）；
    - `edges[]` 规范为 `{id: f"edge-{n}", from_node_id, to_node_id, kind ∈ MapEdgeType, bidirectional: bool, hidden: bool}`，端点必须是已有节点 id，否则丢弃该边；kind 非法时为 `road`。
  - 提示词要求输出上述字段。
  - `validate_world_seed` 的 `unknown_location_ref` 改用 `forwin.map.genesis_adapter.buildable_atlas_node_ids(map_atlas) -> frozenset[str]`（与 adapter 实际建图时使用的节点集合一致）。

- [ ] **Step 1: 写失败测试**

- `test_map_normalizer_structures_edges_scope_and_danger`
- `test_unbuildable_node_is_unknown_location_at_lock`：一个没有名字的节点 → 校验报 `unknown_location_ref`。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_map_structure.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_map_structure.py tests/test_genesis_world_seed.py tests/test_map_genesis_adapter.py tests/test_map_generation.py tests/test_book_genesis_flow.py tests/test_world_seed_multi_genre.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(genesis): structure map edges, scope and danger levels`

---

### Task 13: P1 延后项：开写原子性与小修

**Files:**
- Modify:
  - `forwin/application/projects/genesis.py:420-470`（在 handoff 的 session 内入队：`create_continue_generation_task` 传入 session；handoff 开始时锁定项目行）
  - `forwin/genesis/handoff/service.py:167-178`（失败事件的 `parent_event_id` 在回滚后置为 ""）
  - `forwin/cli.py:55-81`（world_seed 加锁被拒时打印问题列表与下一步，退出码 2）
  - `forwin/genesis/messages.py`（world_seed 的输出要求只在整阶段 refine 时附带）
  - `forwin/genesis/normalize.py`（story_engine 的 `secrets` 为字符串时按旧的 `secret` 处理）
  - `forwin/proposals/structured_patch.py:709-717`（`_proposal_chapter` 与 `proposal_review.proposal_chapter` 读同一字段）
  - `forwin/proposals/proposal_review.py`（world edit 的章节也向上收敛：超过最新 canon 章节时，按最新 canon 章节处理）
  - `forwin/characters/creation.py`（由编译器调用时不再写 `source_delta_id=""` 的额外版本行）
  - 直接写入者的 `version_chapter` 取 `max(latest_available_chapter, node.created_at_chapter)`（`personality/enrichment.py`、`http/adapters/api_book_state_routes.py`）
  - `forwin/checker/pulp_beat.py:67`（删除“弟子”）
- Test: `tests/test_handoff_atomicity.py`（新建）及受影响的现有测试

**Interfaces:**
- Produces：
  - start-writing 的任务入队与 handoff 在同一事务中提交，handoff 失败时不会留下孤儿任务；
  - 两个并发的 start-writing 请求 → 第二个得到 409，而不是 500。

- [ ] **Step 1: 写失败测试**

- `test_failed_handoff_leaves_no_generation_task`
- `test_concurrent_start_writing_second_gets_409`（两个 session 与线程，`lock_timeout`）
- `test_failure_event_has_no_dangling_parent`
- `test_cli_reports_world_seed_lock_refusal`
- `test_future_chapter_world_edit_clamps_down`
- `test_compiler_created_character_has_one_version_row`
- 其余小修各一个最小用例

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_handoff_atomicity.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_handoff_atomicity.py tests/test_genesis_handoff_service.py tests/test_project_operation_guards.py tests/test_book_state_versioning.py tests/test_book_state_reader_promises.py tests/test_genesis_world_seed_stage.py tests/test_genesis_story_engine_structure.py tests/test_pulp_beat_verifier.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `fix: make start-writing atomic and close P1 follow-ups`

---

### Task 14: 多题材端到端（第 1 章）

**Files:**
- Modify: `tests/fixtures/world_seed/{xianxia,interstellar,urban_mystery}.json`（每本新增一份 `chapter_1` 段：正文、`chapter_delta_extraction` 的桩输出）
- Create: `tests/test_chapter_delta_multi_genre.py`

**Interfaces:**
- 每本书第 1 章的桩抽取都至少包含：一次使用本书出行方式的移动、一条关系变化、一个知识事件（含 reader 观察者）、一次物品转移、一个事件、一条计划目标完成、本书日历单位的时长。

- [ ] **Step 1: 写测试（参数化三个 fixture）**

- `test_chapter_one_commits_through_translator_and_gate`：开写之后，桩出 writer 与抽取，跑完一章 → `chapter_extraction` delta 已提交；as-of 1 的运行时中位置、关系、知识、物品持有者都已更新；故事时间 = 开篇 + 时长；没有 `world_consistency` error。
- `test_too_fast_travel_is_caught_per_genre`：把移动耗时改为最短耗时的一半 → draft review fail，must_fix 中含 `movement_too_fast`。
- `test_three_genres_share_chapter_pipeline`：用 spy 包住 `ensure_chapter_delta`、`ChapterDeltaTranslator.translate`、`BookStateReviewGate.review`，三本书的调用序列相同。

- [ ] **Step 2: 运行，确认通过（只增加测试；若失败，回到对应任务修实现，不修改 fixture 去迁就）**

Run: `.venv/bin/python -m pytest -q tests/test_chapter_delta_multi_genre.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 3: 全量测试并提交**

Commit message: `test: run chapter one of three genres through extraction, translation and the world gate`

---

### Task 15: P2 收尾

**Files:**
- Modify: `tests/overfit_allowlist.toml`（`meta.completed_phase = "P2"`；删除 `[canary].ignore` 中的“小时”“时辰”）、`Design-docs/CURRENT_ARCHITECTURE.md`、`Design-docs/CURRENT_DESIGN.md`、`Design-docs/DESIGN_STATUS.md`、`forwin/book_state/README.md`
- Test: `tests/test_no_story_content_in_runtime.py`、`tests/test_design_status_docs.py`、`tests/test_architecture_boundaries.py`

- [ ] **Step 1: 设置 `completed_phase = "P2"`、删除 canary ignore，运行护栏测试**

Run: `.venv/bin/python -m pytest -q tests/test_no_story_content_in_runtime.py`
Expected: PASS。若 `test_pending_removal_entries_name_a_future_phase` 失败，说明还有 `remove_in = "P2"` 的条目，回到对应任务处理。

- [ ] **Step 2: 更新文档**

写明以下内容：
- 抽取、翻译与审查门 v6 的流程；
- `world_consistency` 信号；
- 已删除的模块与表；
- 迁移头 `0004_drop_parallel_ledgers`；
- “与设计的取舍”中推迟到 P3 / P4 的事项。

保留文档测试要求的全部标记。

- [ ] **Step 3: 运行文档与边界测试，然后全量测试并提交**

Commit message: `docs: close world engine overhaul P2`

---

### Task 16: 运行手册（每一步都需用户确认）

前提：P0 与 P1 已部署（`alembic_version` 为 `0003_book_state_v6`）。由主会话执行。

- [ ] **Step 1: 备份。** 在生产应用容器中执行 `scripts/backup_forwin_data.py`。
- [ ] **Step 2: 合入并推送。** 在 `master` 上执行 `git merge --no-ff claude/world-engine-p2`，全量测试通过后推送。
- [ ] **Step 3: 部署。** `ssh infra-150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`
- [ ] **Step 4: 执行迁移。** `alembic upgrade head`，确认版本为 `0004_drop_parallel_ledgers`，应用正常启动。
- [ ] **Step 5: 冒烟测试（不调用 LLM）。** MCP `project_list` 正常；新建一个测试项目，Genesis 阶段列表正确。真实 LLM 的一章验证另行经用户批准。
