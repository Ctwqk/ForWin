# 世界引擎大修 P1：账本 v6、world_seed 阶段与开写种子 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 把 BookState 补成 v6 账本（故事时间、全字段时间版本化、新节点类型、知识事件、narrative 快照、统一的读者承诺），用新的 Genesis `world_seed` 阶段取代 `bootstrap`，并在开写交接时把初始世界编译成第 0 章的 GraphDelta 写入 BookState。

**Architecture:**
- 数据库只新增一个前向迁移 `0003_book_state_v6`，P1 的全部表和列都放在里面（P1 合入 master 之前，本期任务可以继续追加修改它）。同时新增列漂移测试：在真实升级后的库上逐表对照 ORM。
- StoryClock 与 lexicon 是两个纯领域模块，各有一个仓储类。故事时间是自纪元起的整数分钟。
- 时间版本化沿用 `world_node_states` 表：任何写 `world_nodes` 行的操作都同时追加一条完整版本行（name / aliases / profile / is_active / state），按章节读取时用版本行；narrative 层进入 world snapshot。
- world_seed 分两层：`forwin/genesis/world_seed.py`（规范化、校验、fallback，全是纯函数）和 `forwin/genesis/handoff/world_seed_committer.py`（编译成 GraphDelta，经 `BookStateReviewGate` 与新入口 `CanonAdmissionService.commit_world_seed` 写入）。
- 开写交接顺序改为：卷 → 地图 → 世界种子 → 卷 1 章节计划。章节计划仍走现有的 `launch_arc_N` LLM 路径，P4 才换成节拍表与 band 规划。

**Tech Stack:** Python 3.13（`.venv`）、SQLAlchemy、Alembic、PostgreSQL（测试库 `127.0.0.1:55432`）、pydantic、pytest；前端为 `forwin/ui_assets/home/*.js` 原生 JS。

**Spec:** `docs/designs/2026-10-20-world-engine-overhaul.md`（重点读 §1、§2、§7.3、§10、附录 C）

## 与设计的取舍（实施者照此执行，不要按设计原文改回）

1. **位置字段沿用 `state.location_id`**（值为数据库地图节点 id），不改名为设计中的 `location_ref`。现有约 70 处读写点（writer contract、map_context、审查门移动检查）会在 P2 / P4 整体替换，此时改名只会制造双字段。新节点类型中的地点字段也叫 `location_id`。world_seed 里引用 Genesis 地图节点的字段仍叫 `location_ref`（值为 Genesis 节点 id），由提交器换算。
2. **章节级起止时间不在 P1 建列。** P1 只给 `graph_deltas` 加时间列，种子 delta 写入开篇时间；章节跨度由 P2 的章节增量抽取写入，存放位置由 P2 决定。“本章开始时间 ≥ 上一章结束时间”与闪回规则属于 P2 的审查门 v6；地图边 `travel_time` 的分钟换算随 P2 的移动检查一起实现。
3. **`finalization._seed_state`、`RunControlStage.run`、`SubWorldManager.apply_initial_arc_plan` 推迟到 P4**，与 ArcDirector 一起删除（附录 B 把它们列为同一组；它们只被 llm_eval 和测试调用，生产路径不经过）。`sub_world_roster_items` 与 `plan_band_activation` 同样留到 P4，由 band 计划的 `onstage_refs` 取代。P1 只删除第 0 章的 subworld 角色生成路径。
4. **Genesis 阶段键沿用裸键 `world_seed`**（LLM stage_key、PromptTrace、URL、skill 都用它）。设计 §8.1 的角色名 `genesis.world_seed` 即指这个键；路由已由 `task_family="genesis"` 覆盖。
5. **读者承诺统一为 narrative `promise` 节点**：删除 `book_reader_promises` 表和提案中的 `metadata.reader_promise_patches`（设计 §1.4、§2.3、附录 B）。
6. **`reveal_candidates` 只保存在冻结的 Genesis pack 中**，供 P4 的卷节拍表读取，不写入 BookState。
7. **world_seed 必须为 story_engine 中的每个 core_cast / opposition / faction 提供一个 actor 条目**（设计 §2.3）；fallback 按 story_engine 的 id 生成 actor 空壳，不编造内容。

## Global Constraints

- 运行代码只处理结构；所有故事内容来自 Genesis、world_seed、BookState 或 LLM 输出；题材相关词只能来自本书的 lexicon。（设计 §7.1）
- 新增运行代码不得引入 CJK 词表（模块级集合、`re` 模式、`in` 成员判断、函数内字面量迭代）。提示词文本只能放在已登记为 `prompt_text` 的位置（`forwin/genesis/messages.py`、`forwin/genesis/constants.py`、`forwin_skills/**`），条目数变化时同步调整 `tests/overfit_allowlist.toml` 中对应条目的 `max_literals`。新增的枚举值一律用英文 token。
- 已合入的迁移不得再修改。`0003_book_state_v6` 在 P1 合入 master 前允许本期任务追加修改。revision id ≤ 32 字符。
- 新 ORM 模型必须能在 SQLite 上 `create_all`（约 10 个测试用 `sqlite:///:memory:` 建全量表），不得使用 PG 专有类型；故事时间用 `BigInteger`；JSON 一律存 `Text`，列名以 `_json` 结尾。
- 新模型模块不得命名为 `forwin/models/{event,thread,timeline}.py`；生产代码不得出现 `legacy` 字样（`tests/test_architecture_boundaries.py:122-131`）。
- `BookStateCompiler(` 只能出现在 `forwin/canon/admission.py`（`tests/test_v5_canon_architecture.py:18-27`）；所有 canon 写入都经 `CanonAdmissionService`。
- 故事时间 = 自纪元起的整数分钟。代码只内置“分钟”这一个单位名，其余单位名只能来自本书日历或 lexicon。
- 分支：从 `claude/world-engine-overhaul` 当前 HEAD 新建 `claude/world-engine-p1`。本机没有配置 git 身份，每次提交都用 `git -c user.name=CASPER -c user.email=magi1@CASPERs-Mac-mini.local commit ...`，提交信息末尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 测试命令：`.venv/bin/python -m pytest -q`。基线为 2412 passed、1 skipped（约 4.5 分钟，2026-10-20 测得）。每个任务结束时全量测试必须全部通过，数量允许因删测试而减少。
- 不调用真实 LLM，不连接生产库。生产操作只在 Task 17 中执行，每一步经用户确认，并且必须在 P0 的 Task 13（停写、清库、部署 0002）完成之后。

## Review Focus

1. world_seed 中的时长单位不在本书日历和 lexicon 别名里（例如日历单位是“日”，LLM 写了“天”）→ 校验给出 `unknown_time_unit`（带路径），阶段不能加锁；不得静默换算或丢弃。由 Task 9 的 `test_validate_reports_unknown_time_unit` 与 Task 10 的 `test_world_seed_lock_refused_while_validation_errors` 覆盖。
2. world_seed 生成之后，用户又修改了 story_engine（删掉一个角色）→ 加锁时按当前 pack 重新校验，报 `unknown_actor_ref`，加锁和开写都被拒绝。由 Task 10 的 `test_world_seed_lock_revalidates_against_current_story_engine` 覆盖。
3. world_seed 引用的 Genesis 地图节点在地图生成后没有对应的 `map_nodes` 行（按 `metadata.source_node_id` 查不到）→ 种子提交以 `unresolved_location_ref` 失败，整个交接回滚并记录失败事件；不得把角色放在空位置。由 Task 11 的 `test_commit_rejects_unresolved_location_ref` 与 Task 12 的 `test_world_seed_failure_rolls_back_handoff_and_records_failure_event` 覆盖。
4. 写到第 N 章之后，人格补全或 HTTP 人格编辑这类直接写入者更新了角色 profile → 按最新章节加载的运行时能看到新值，不会被更早的版本行覆盖。由 Task 4 的 `test_direct_writer_update_is_visible_as_of_latest_chapter` 覆盖。
5. world_seed 的输出远大于其它 Genesis 阶段 → 调用必须使用 `world_seed_max_tokens`，不得被全局的 1600 上限截断成解析失败后退回空壳。由 Task 10 的 `test_world_seed_generation_uses_world_seed_token_budget` 覆盖。

---

### Task 1: 四层永远全开（删除 `canon.book_state_layers`）

**Files:**
- Modify: `forwin/runtime/policy.py:61-65,94,109`、`forwin/canon/preparation.py:88-91`、`forwin/book_state/extraction/graph_delta.py:25-37,50-56,89`
- Test: `tests/test_runtime_policy.py:21,40`、`tests/browser/fixtures.py:1069`

**Interfaces:**
- Produces：
  - `CanonPolicy` 只剩 `hard_floor: bool = True`、`quality_gate: Literal["strict", "pulp_fatal"] = "strict"`。
  - `BookStateGraphDeltaExtractor.__init__` 不再接受 `layers`；删除 `DEFAULT_BOOK_STATE_LAYERS`、`_normalize_book_state_layers`。pulp_light 兜底去掉 `"world" in self.layers` 条件（两个 profile 原来都含 world，行为不变）。
  - `planning.use_llm_simulation` 不动（P4 随 WorldSimulator 一起删）。

- [ ] **Step 1: 写失败测试**

在 `tests/test_runtime_policy.py` 新增 `test_canon_policy_has_no_layer_switch`：`"book_state_layers" not in CanonPolicy.model_fields`，并且 `RuntimePolicy.for_profile("pulp")`、`for_profile("standard")` 都能构造。删除 :21、:40 两处对 `book_state_layers` 的断言；`tests/browser/fixtures.py:1069` 删除该键。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_runtime_policy.py`
Expected: FAIL（字段仍存在）

- [ ] **Step 3: 按 Interfaces 修改生产代码**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_runtime_policy.py tests/test_pulp_pipeline_bypass.py tests/test_book_state_writer_contract.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(policy): keep all four BookState layers on`

---

### Task 2: 迁移 `0003_book_state_v6` 与 ORM（P1 的全部新增表与列）

**Files:**
- Create: `forwin/models/book_frame.py`、`forwin/migrations/versions/0003_book_state_v6.py`
- Modify: `forwin/models/__init__.py`（注册并导出新类）、`forwin/models/book_state.py`（`WorldNodeStateRow` :44-60、`GraphDeltaRow` :177-201、`CognitionOverlayRow` :229-253、`WorldSnapshotRow` :278-303）、`forwin/models/project.py:36-61`（`ArcPlanVersion`）
- Test: `tests/test_v5_live_migration.py:16,71-88`、`tests/test_v5_recovery_schema.py:455-528`、`tests/test_architecture_boundaries.py:341-348`

**Interfaces:**
- Produces（列定义；本期新增的 NOT NULL 列在 ORM 与迁移中都带 `server_default`，以便对已有行执行 `add_column`）：

| 表 | 变更 |
| --- | --- |
| `story_calendars`（新，`StoryCalendarRow`） | `id` String PK（`default=new_id`）、`project_id` FK `projects.id` NOT NULL、`epoch_label` Text default ""、`opening_minute` BigInteger default 0、`units_json` Text default "[]"、`created_at`、`updated_at`；`Index("ux_story_calendars_project", "project_id", unique=True)` |
| `book_lexicons`（新，`BookLexiconRow`） | `id`、`project_id`、`lexicon_json` Text default "{}"、`created_at`、`updated_at`；`Index("ux_book_lexicons_project", "project_id", unique=True)` |
| `graph_deltas` | `story_time_start` BigInteger NULL、`story_time_end` BigInteger NULL |
| `world_node_states` | `name` Text NOT NULL ""、`aliases_json` Text NOT NULL "[]"、`profile_json` Text NOT NULL "{}"、`is_active` Boolean NOT NULL true（`server_default=sa.true()`） |
| `cognition_overlays` | `knowledge_by_ref_json` Text NOT NULL "{}" |
| `world_snapshots` | `narrative_node_index_json` Text NOT NULL "{}"、`narrative_edge_index_json` Text NOT NULL "{}" |
| `arc_plan_versions` | `title`、`goal`、`stakes`、`payoff_direction`：Text NOT NULL "" |

- 迁移：`revision = "0003_book_state_v6"`、`down_revision = "0002_drop_dead_tables"`。`upgrade()` 用 `op.create_table` / `op.create_index` / `op.add_column`；`downgrade()` 逆序删除。Task 7 会在同一文件追加删除 `book_reader_promises` 的步骤。

- [ ] **Step 1: 写失败测试**

- 在 `tests/test_v5_live_migration.py`：
  - `EXPECTED_REVISION = "0003_book_state_v6"`。
  - 新增 `test_upgraded_columns_match_orm_metadata`：在 `postgres_empty_test_url` 上 `alembic upgrade head`，对 `Base.metadata.sorted_tables` 中的每张表，断言 `{列名: nullable}`（取自 `sqlalchemy.inspect(engine).get_columns`）与 ORM 的 `{c.name: c.nullable}` 完全相等。失败信息列出每张表多出与缺少的列。
- `tests/test_v5_recovery_schema.py:517-528` 与 `tests/test_architecture_boundaries.py:341-348` 的迁移文件列表加入 `0003_book_state_v6.py`。
- `tests/test_v5_recovery_schema.py:455-514` 的 baseline 对照只比较 0001 建立的部分：在测试内新增常量 `COLUMNS_ADDED_AFTER_BASELINE`（本表的新增列，按表分组）和 `TABLES_ADDED_AFTER_BASELINE = {"story_calendars", "book_lexicons"}`，从 ORM 侧剔除后再比较。列漂移改由上面的新测试在真实升级后的库上把关。
- 新增 `test_book_frame_models_are_registered`：`hasattr(forwin.models, "StoryCalendarRow")`、`hasattr(forwin.models, "BookLexiconRow")`。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_v5_live_migration.py tests/test_v5_recovery_schema.py tests/test_architecture_boundaries.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 写 ORM 与迁移**

- [ ] **Step 4: 运行，确认通过**

Run: 同 Step 2
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(db): add 0003 book state v6 schema`

---

### Task 3: StoryClock 与 lexicon 领域模块

**Files:**
- Create: `forwin/book_state/story_clock.py`、`forwin/book_state/lexicon.py`、`forwin/book_state/book_frame_repository.py`
- Modify: `forwin/protocol/book_state.py:433-455`（`GraphDelta`）、`forwin/book_state/repository.py:497-530`（`append_graph_delta`）与 `_graph_delta_from_row`（约 :673）、`forwin/book_state/query.py`
- Test: `tests/test_story_clock.py`、`tests/test_book_lexicon.py`（均新建）、`tests/test_book_state_repository_projection_compiler.py`（新增 1 个用例）

**Interfaces:**
- Produces：
  - `story_clock.py`：
    - `@dataclass(frozen=True) class CalendarUnit: name: str; minutes: int`
    - `@dataclass(frozen=True) class StoryCalendar: epoch_label: str; opening_minute: int; units: tuple[CalendarUnit, ...]`
    - `class StoryClockError(ValueError)`、`class UnknownTimeUnit(StoryClockError)`（属性 `unit_name: str`）
    - `calendar_from_payload(payload: Mapping[str, Any]) -> StoryCalendar`：`opening_minute` 为 ≥0 的 int；每个单位的 `minutes` 为 >0 的 int；单位名非空且互不重复；违反即抛 `StoryClockError`。
    - `calendar_to_payload(calendar: StoryCalendar) -> dict[str, Any]`
    - `format_story_time(calendar: StoryCalendar, minutes: int) -> str`：单位按 `minutes` 从大到小贪心分解，数量为 0 的单位省略；余下的分钟写作 `f"{r}分钟"`；全为 0 时写 `"0" + 最小单位名`（没有单位时写 `"0分钟"`）；结果为 `f"{epoch_label} {各段拼接}".strip()`。
    - `unit_minutes(calendar: StoryCalendar, lexicon: BookLexicon, unit_name: str) -> int`：先查日历单位，再查 lexicon 的时间别名，都没有则抛 `UnknownTimeUnit`。
    - `duration_to_minutes(calendar, lexicon, *, amount: float, unit_name: str) -> int`：`round(amount * unit_minutes(...))`；`amount < 0` 抛 `StoryClockError`。
  - `lexicon.py`：
    - `TimeUnitAlias(name: str, minutes: int)`、`RankLadder(id: str, name: str, ranks: tuple[str, ...])`、`Currency(id: str, name: str)`、`TravelMode(id: str, name: str, speed_multiplier: float, allowed_edge_types: tuple[str, ...])`，均为 frozen dataclass。
    - `@dataclass(frozen=True) class BookLexicon: time_unit_aliases: tuple[TimeUnitAlias, ...] = (); rank_ladders: tuple[RankLadder, ...] = (); currencies: tuple[Currency, ...] = (); appellations: tuple[str, ...] = (); generic_character_refs: tuple[str, ...] = (); travel_modes: tuple[TravelMode, ...] = (); news_speed_multiplier: float = 1.0`
    - `EMPTY_LEXICON = BookLexicon()`
    - `class LexiconError(ValueError)`
    - `lexicon_from_payload(payload: Mapping[str, Any]) -> BookLexicon`：别名 `minutes > 0`；`speed_multiplier > 0`；`allowed_edge_types` 非空且 ⊆ `{t.value for t in MapEdgeType}`；各列表内 id 不重复；`news_speed_multiplier > 0`。违反即抛 `LexiconError`。
    - `lexicon_to_payload(lexicon: BookLexicon) -> dict[str, Any]`
  - `book_frame_repository.py`：
    - `StoryCalendarRepository(session)`：`get(project_id) -> StoryCalendar | None`、`save(project_id, calendar) -> None`（按 project 唯一 upsert）。
    - `BookLexiconRepository(session)`：`get(project_id) -> BookLexicon`（没有行时返回 `EMPTY_LEXICON`）、`save(project_id, lexicon) -> None`。
  - `GraphDelta` 新增 `story_time_start: int | None = None`、`story_time_end: int | None = None`，经 `append_graph_delta` 与 `_graph_delta_from_row` 往返。
  - `BookStateQuery.current_story_minute(project_id: str, *, as_of_chapter: int) -> int | None`：`chapter_number ≤ as_of_chapter` 且 `story_time_end` 非空的 delta 中，按 `(chapter_number desc, created_at desc, id desc)` 取第一条的 `story_time_end`。

- [ ] **Step 1: 写失败测试**

- `tests/test_story_clock.py`（日历：纪元“开元”，单位 日=1440、时辰=120）：
  - `test_format_story_time_decomposes_units`：`format_story_time(cal, 3*1440 + 2*120 + 15) == "开元 3日2时辰15分钟"`；`format_story_time(cal, 0) == "开元 0时辰"`；没有单位的日历 `format_story_time(StoryCalendar("开元", 0, ()), 95) == "开元 95分钟"`。
  - `test_duration_uses_calendar_then_lexicon_alias`：lexicon 别名 一炷香=30 → `duration_to_minutes(cal, lex, amount=2, unit_name="时辰") == 240`；`amount=1, unit_name="一炷香"` → 30；`unit_name="天"` 抛 `UnknownTimeUnit`，且 `exc.unit_name == "天"`。
  - `test_calendar_rejects_bad_units`：`minutes` 为 0、单位名重复、`opening_minute` 为负，各自抛 `StoryClockError`。
- `tests/test_book_lexicon.py`：
  - `test_lexicon_round_trip`：`lexicon_from_payload(lexicon_to_payload(x)) == x`。
  - `test_lexicon_rejects_unknown_edge_type_and_zero_speed`：`allowed_edge_types=["teleport"]` 抛 `LexiconError`；`speed_multiplier=0` 抛 `LexiconError`。
  - `test_repositories_upsert_per_project`（postgres）：同一项目 `save` 两次只有 1 行；`BookLexiconRepository.get` 对没有行的项目返回 `EMPTY_LEXICON`。
- `tests/test_book_state_repository_projection_compiler.py` 新增 `test_graph_delta_story_minutes_round_trip_and_current_minute`：在第 0 章和第 2 章分别写入 `story_time_end` 为 100 和 400 的 delta → `current_story_minute(as_of_chapter=1) == 100`，`as_of_chapter=2` 时为 400。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_story_clock.py tests/test_book_lexicon.py tests/test_book_state_repository_projection_compiler.py -k "story or lexicon or minute"`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 按 Interfaces 实现**

- [ ] **Step 4: 运行，确认通过**

Run: 同 Step 2，再加上 `tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(book_state): add story clock, book lexicon and numeric delta story time`

---

### Task 4: 全字段时间版本化与 narrative 快照

**Files:**
- Modify:
  - `forwin/book_state/runtime.py:81-118`（`apply_node_patch`）与 `apply_edge_patch`
  - `forwin/book_state/repository.py`：`create_world_node`（:88-113）、`append_world_node_state`（:119 起）、`_graph_delta_from_row` 中的 fact patch（约 :673）、`load_base_world_graph` 与 `_latest_state_index`（:1149-1231）、`load_narrative_graph`（:1314-1337）、world snapshot 的读写（`persist_world_snapshot` :1390 及对应的 row 映射函数）、:1143 的内部调用
  - `forwin/book_state/projection.py:33-102,142-253`
  - `forwin/book_state/compiler.py:259-358`
  - `forwin/protocol/book_state.py:520-541`（`WorldSnapshot`）
  - 直接写入者：`forwin/characters/creation.py:142,150`、`forwin/personality/enrichment.py:128`、`forwin/map/service.py:309`、`forwin/http/adapters/api_book_state_routes.py:553,680,825`、`forwin/generation/pipeline_core/finalization.py:271-299`。实现前用 `rg -n "create_world_node\(|append_world_node_state\(" forwin` 复核完整列表。
- Test: `tests/test_book_state_versioning.py`（新建）

**Interfaces:**
- Produces：
  - `BookStateRepository.create_world_node(node: WorldNode, *, version_chapter: int, version_state: dict[str, Any] | None = None, as_of_story_time: str = "", source_delta_id: str = "") -> WorldNodeRow`：
    - 照旧 upsert `world_nodes` 行（它只是“最新值”投影）；
    - 同时追加一条 `world_node_states` 版本行，写入 node 的 name / aliases / profile / is_active，以及 state。
    - `version_state is None` 时，state 复制该节点 `≤ version_chapter` 的最新版本行的 state（没有就用 `{}`）。
    - `version_chapter` 为必填参数，这样漏改的调用方会直接报错。
  - 删除 `append_world_node_state`。编译器改为每个 delta 对每个被修改的节点只调用一次 `create_world_node(node, version_chapter=delta.chapter_number, version_state=runtime.world.get_state(node.id), source_delta_id=delta.id)`，不再区分 `create` / `state.*`。
  - 直接写入者的 `version_chapter`：
    - 新建节点 → `node.created_at_chapter`；
    - 修改已有节点（人格补全、HTTP 人格编辑）→ `repo.latest_available_chapter(project_id)`。
  - `load_base_world_graph` 改用 `_latest_version_index(project_id, as_of_chapter) -> dict[str, NodeVersion]`（`NodeVersion` 为模块内 frozen dataclass：name、aliases、profile、is_active、state），用它覆盖节点的 name / aliases / profile / is_active 和 state。快照中非空的 state index 仍然覆盖 state（行为不变）。
  - `ObjectiveWorldGraph.apply_node_patch`：非 `state.*` 路径的补丁不得改动 `states_by_node_id`，即删除 :117-118。
  - `ObjectiveWorldGraph.apply_edge_patch(patch, *, chapter_number: int)`：`deactivate` 同时设置 `is_active=False`、`ended_at_chapter=chapter_number`。`apply_delta` 传入 `graph_delta.chapter_number`。读取 as-of 第 k 章时，若 `ended_at_chapter > k`，把边视为 `is_active=True`。
  - fact patch 的 `field_path` 在 `_graph_delta_from_row` 中保留。
  - `WorldSnapshot` 新增 `narrative_node_index: dict[str, NarrativeNode]`、`narrative_edge_index: dict[str, NarrativeEdge]`。`persist_snapshots` 从 `runtime.narrative` 填入，并写 `metadata["narrative_indexed"] = True`。
  - `load_runtime_as_of`：world snapshot 的 `metadata.narrative_indexed` 为真时，narrative 图直接取自快照，并令 `narrative_after = world_after`；否则沿用现有行为（读行 + 从 −1 重放）。
  - `active_promise_ids` = narrative 中 `node_type == "promise"` 且 `status in {"open", "active"}` 的节点 id（不再统计 `reader_promise` world 节点）。

- [ ] **Step 1: 写失败测试（`tests/test_book_state_versioning.py`，postgres，通过 `CanonAdmissionService` 或编译器测试 harness 写入）**

- `test_non_state_node_patch_keeps_runtime_state`：节点 state 为 `{status, health, location_id}`，应用 `metadata.writer_location` 的 set 补丁之后，state 不变。
- `test_fact_patch_field_path_survives_reload`：写入一个带 `field_path="state.status"` 的 fact patch，重新加载后 `field_path` 仍为 `"state.status"`。
- `test_profile_name_alias_changes_are_versioned_by_chapter`：
  - 第 0 章创建角色，name 为“甲”，`profile.narrative_role="a"`；
  - 第 2 章把 name 改为“乙”、修改 `profile.narrative_role`、追加 alias；
  - as-of 1 加载 → “甲”与 `"a"`，aliases 为空；as-of 2 → 新值；`world_nodes` 行为最新值。
- `test_direct_writer_update_is_visible_as_of_latest_chapter`：经编译器在第 0、1 章写入后，用 `create_world_node(updated, version_chapter=repo.latest_available_chapter(pid))` 更新 `profile.personality_loadout` → as-of 1 的运行时可见新值，as-of 0 仍是旧值。
- `test_deactivated_edge_is_active_for_earlier_chapters`：第 0 章建边，第 2 章 deactivate → as-of 1 时边处于激活状态，as-of 2 时不处于激活状态。
- `test_narrative_loads_from_snapshot_without_full_replay`：第 0 章创建 narrative 节点，第 1 章用非 create 补丁把 status 改为 `"resolved"`。as-of 2 加载：
  - status 为 `"resolved"`；
  - 用 spy 包住 `BookStateRepository.list_graph_deltas`，断言调用时 `after_chapter == 1`。
- `test_active_promise_ids_count_narrative_promise_nodes`：写入一个 `promise` 节点和一个 `reader_promise` world 节点 → 快照的 `active_promise_ids` 只含前者。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_book_state_versioning.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现，更新全部调用方**

- [ ] **Step 4: 运行相关测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_book_state_versioning.py tests/test_book_state_repository_projection_compiler.py tests/test_canon_atomic_transaction.py tests/test_knowledge_system_v46.py tests/test_character_creation_helper.py tests/test_character_relationship_enrichment.py tests/test_map_world_integration.py tests/test_book_state_runtime.py tests/test_book_state_final.py`
Expected: PASS（历史重写按 `source_delta_id` 删除版本行的逻辑不变，`test_canon_atomic_transaction.py` 应原样通过）

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(book_state): version node identity per chapter and snapshot the narrative layer`

---

### Task 5: Schema v6：议程、意图、时限、角色规范字段与行动者属性

**Files:**
- Modify: `forwin/protocol/book_state.py:14-40`（`WorldNodeType`）、`:60-75`（`WORLD_EDGE_TYPES_BY_FAMILY`）、`forwin/book_state/schema.py`（`WORLD_NODE_FIELDS`、`validate_world_node` :479、`validate_graph_delta` :511-589）
- Test: `tests/test_book_state_schema_v6.py`（新建）

**Interfaces:**
- Produces：
  - `WorldNodeType` 新增 `AGENDA = "agenda"`、`INTENT = "intent"`、`CLOCK = "clock"`。
  - `WORLD_EDGE_TYPES_BY_FAMILY["social"]` 新增 `"rival_of"`（Task 13 的关系技能与 world_seed 关系会用到）。
  - `WORLD_NODE_FIELDS` 新增：

| 类型 | profile | state |
| --- | --- | --- |
| `agenda` | owner_ref、goal、motivation、horizon | status、priority、means_refs、constraints、secrecy_refs |
| `intent` | owner_ref、agenda_ref、action_type、summary、target_refs、location_id、preconditions、expected_effects、impact_level、requires_onstage | status、scheduled_at、slip_count |
| `clock` | label、owner_ref、scope、on_expire | due_at、status |

  - `character` 的 profile 增加 `fear`；state 增加 `alive_status`、`restraint`、`travel_mode_ids`。
  - `ACTOR_NODE_TYPES = ("character", "faction", "organization", "group")`；这四类的 state 增加 `ACTOR_STATE_FIELDS = {"actor_tier", "next_turn_at", "resources"}`。organization 与 group 改为显式条目：通用字段加上行动者字段。
  - `rule` 的 profile 增加 `hardness`、`statement`、`checkable`。
  - 模块级枚举常量（英文 token）：
    - `AGENDA_STATUSES = ("active", "achieved", "abandoned", "failed")`
    - `INTENT_STATUSES = ("planned", "due", "resolved", "disrupted", "vetoed", "held")`
    - `INTENT_ACTION_TYPES = ("move", "confront", "negotiate", "ally", "betray", "acquire", "transfer", "investigate", "spread_info", "deceive", "build", "recruit", "hide", "train", "decree", "other")`
    - `IMPACT_LEVELS = ("minor", "moderate", "major", "critical")`
    - `CLOCK_SCOPES = ("public", "secret")`、`CLOCK_STATUSES = ("running", "expired", "resolved")`
    - `ALIVE_STATUSES = ("alive", "dead", "unknown")`、`RESTRAINT_STATES = ("free", "restrained", "imprisoned")`
    - `ACTOR_TIERS = ("major", "minor", "background")`、`RULE_HARDNESS = ("hard", "soft")`
  - 值校验 `_v6_value_issues(node_type: str, profile: Mapping, state: Mapping, *, target: str) -> list[BookStateSchemaIssue]`（severity 全部为 `"error"`）：
    - 上述枚举字段的取值必须在枚举之内；
    - `priority` 为 1–5 的 int；`scheduled_at`、`due_at`、`next_turn_at`、`slip_count` 为 ≥0 的 int；
    - `restraint` 是含 `state`（属于 `RESTRAINT_STATES`）、`by_ref`（str）、`since`（int 或 None）的 dict；
    - `requires_onstage` 为 bool；`checkable` 为 None，或含非空 str `kind` 的 dict。
  - 创建时的必填字段（`code="missing_required_field"`）：agenda 需要 owner_ref、goal；intent 需要 owner_ref、action_type、scheduled_at；clock 需要 label、due_at。
  - `validate_world_node` 和 `validate_graph_delta`（create 补丁与针对这些字段的 set 补丁）都调用 `_v6_value_issues`。对这三种新类型及上述新字段，即使 `strict=False` 也报 error。

- [ ] **Step 1: 写失败测试（`tests/test_book_state_schema_v6.py`）**

- `test_agenda_intent_clock_nodes_validate`：三种合法节点的 `validate_world_node(...).ok` 为真。
- `test_v6_enum_and_range_errors`：分别构造 `intent.action_type="teleport"`、`agenda.state.priority=9`、`clock.state.due_at=-1`、`character.state.alive_status="zombie"`、`restraint={"state": "chained"}`、`rule.profile.hardness="medium"` → 各自产生 `severity="error"` 的问题。
- `test_v6_required_fields_on_create`：缺少 owner_ref 的 agenda 创建补丁 → `validate_graph_delta` 报 `missing_required_field`。
- `test_set_patch_on_intent_status_is_validated`：针对 `state.status` 设置为 `"done"` 的 set 补丁 → error。
- `test_rival_of_is_a_social_edge`：`WorldEdge(edge_type="rival_of", edge_family="social", ...)` 能构造。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_book_state_schema_v6.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_book_state_schema_v6.py tests/test_book_state_final.py tests/test_book_state_writer_contract.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(book_state): add agenda, intent and clock nodes with v6 field validation`

---

### Task 6: 认知层：知识事件

**Files:**
- Create: `forwin/book_state/knowledge.py`
- Modify: `forwin/protocol/book_state.py:481-497`（`CognitionOverlay`）、`forwin/book_state/cognition.py`、`forwin/book_state/projection.py:131-137`、`forwin/book_state/repository.py` 中 cognition overlay 的读写映射（`knowledge_by_ref_json`）、`forwin/book_state/schema.py`（cognition 补丁校验）、`forwin/book_state/compiler.py:198-225`（cognition 当前值）
- Test: `tests/test_book_state_knowledge.py`（新建）

**Interfaces:**
- Produces：
  - `knowledge.py`：
    - `KNOWLEDGE_STATES = ("heard", "suspects", "believes", "knows", "disbelieves", "misled")`
    - `KNOWLEDGE_CHANNELS = ("witnessed", "told", "rumor", "document", "inferred", "seeded")`
    - `HIDDEN_SENSITIVITY = frozenset({"hidden", "secret", "must_not_reveal"})`
    - `@dataclass(frozen=True) class KnowledgeEvent: observer_type: str; observer_id: str; fact_ref: str; state: str; channel: str; source_ref: str = ""; at: int | None = None`
    - `knowledge_evidence_ref(event: KnowledgeEvent) -> str` → `f"knowledge:{channel}:{source_ref or '-'}:{at if at is not None else '-'}"`
    - `knowledge_event_to_patch(event: KnowledgeEvent) -> CognitionPatch`：`op="merge"`、`field_path="knowledge_by_ref"`、`new_value={fact_ref: state}`、`evidence_refs=[knowledge_evidence_ref(event)]`。state 或 channel 不在枚举内、`fact_ref` 不以 `"fact:"` 开头，或 `observer_type` 不属于 `ObserverType`，都抛 `ValueError`。
  - `CognitionOverlay.knowledge_by_ref: dict[str, str] = {}`，经 `cognition_overlays.knowledge_by_ref_json` 持久化。
  - `CognitionView`：
    - `knowledge_by_ref` 属性；`apply_cognition_patch` 支持 `knowledge_by_ref` 的 merge 与 set / replace。
    - `knowledge_state(fact: FactNode) -> str`：显式记录优先；否则 `fact.sensitivity_level in HIDDEN_SENSITIVITY` → `"unknown"`，其余 → `"knows"`（设计 §1.6 的默认可见规则）。
    - `false_beliefs(facts_by_id: Mapping[str, FactNode]) -> list[str]`：对 `truth_value == "false"` 的 fact 处于 believes 或 knows 的 `fact:<id>` 引用，加上 `field_overrides` 的全部键，排序后返回。
  - `projection.apply_delta_to_runtime`：`new_value` 为 dict 时，evidence 按 dict 的每个键分别登记，不再用 `str(new_value)` 作键。
  - `validate_graph_delta`：`field_path == "knowledge_by_ref"` 的 cognition 补丁，键必须以 `fact:` 开头、值必须属于 `KNOWLEDGE_STATES`，否则报 error。
  - 编译器的 compare-and-set：`knowledge_by_ref` 补丁带 `old_value` 时，与 `{ref: 当前状态}` 比较。
  - 观察者 `reader` 用 `ObserverType.READER`、`observer_id="reader"`。

- [ ] **Step 1: 写失败测试（`tests/test_book_state_knowledge.py`）**

- `test_knowledge_event_to_patch_shape`：`KnowledgeEvent("character", "c1", "fact:f1", "believes", "told", "c2", 600)` → patch 的 `field_path`、`new_value`、`evidence_refs == ["knowledge:told:c2:600"]`。
- `test_knowledge_event_rejects_unknown_state_channel_and_ref`：三种非法输入各自抛 `ValueError`。
- `test_knowledge_state_defaults_follow_sensitivity`：没有显式记录时，公开 fact → `"knows"`，`sensitivity_level="secret"` → `"unknown"`；显式 `"misled"` 优先。
- `test_false_beliefs_detects_believed_false_fact_and_overrides`。
- `test_knowledge_events_compile_and_replay`（postgres）：第 0 章编译两个观察者（含 `reader`）的知识事件 → 重新加载后 `knowledge_by_ref` 与 `evidence_by_ref` 都正确，并且 evidence 不重复（覆盖已有的 “cognition evidence not double-replayed” 场景）。
- `test_invalid_knowledge_patch_is_schema_error`：值为 `"maybe"` → `validate_graph_delta` 报 error。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_book_state_knowledge.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_book_state_knowledge.py tests/test_map_cognition_path.py tests/test_book_state_repository_projection_compiler.py tests/test_knowledge_system_v46.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(book_state): write cognition through knowledge events`

---

### Task 7: 读者承诺统一为 narrative `promise` 节点

**Files:**
- Modify:
  - `forwin/models/book_state.py:150-174`（删除 `BookReaderPromiseRow`）、`forwin/models/__init__.py`
  - `forwin/migrations/versions/0003_book_state_v6.py`：`upgrade()` 末尾删除 `book_reader_promises` 的索引和表；`downgrade()` 按 0001 原样重建（参照 0002 的写法）
  - `forwin/book_state/compiler.py:227-257,360-404,472-476`（删除 reader promise 的 compare-and-set 与 upsert）
  - `forwin/book_state/repository.py:291-296,326` 起（删除 `list_reader_promises` 与 `list_reader_promises_native`）
  - `forwin/protocol/book_state.py:36`（删除 `WorldNodeType.READER_PROMISE`）、`forwin/book_state/schema.py`（从通用类型列表中删除 `reader_promise`）
  - `forwin/book_state/narrative.py`、`forwin/book_state/query.py`
  - `forwin/proposals/structured_patch.py:48,103-137,622-660`、`forwin/http/adapters/api_book_state_routes.py:205`、`forwin/llm_kb/compiler.py:227`
- Test: `tests/test_knowledge_system_v46.py`（reader promise 相关用例）、`tests/test_book_state_repository_projection_compiler.py`（“unversioned promise metadata” 用例）、`tests/test_book_state_reader_promises.py`（新建）

**Interfaces:**
- Produces：
  - `narrative.py`：
    - `promise_node_payload(promise: ReaderPromise) -> dict[str, Any]` → `{"node_type": "promise", "title": summary, "status": status, "payload": 其余字段（promise_type、expected_payoff_window、maximum_safe_delay、current_debt_level、reward_tags、linked_threads、linked_knowledge_gaps、source_refs、created_at_chapter）}`
    - `reader_promise_from_node(node: NarrativeNode) -> ReaderPromise`（`promise_id = node.id`）
  - `BookStateQuery.reader_promises(project_id: str, *, as_of_chapter: int | None = None) -> list[ReaderPromise]`：取自 as-of 运行时的 narrative `promise` 节点；`as_of_chapter=None` 表示最新章节。
  - `structured_patch`：
    - `_reader_promise_patches` 改为 `_promise_narrative_patches(...) -> list[NarrativePatch]`。
    - 新承诺 → `NarrativePatch(target_ref=f"promise:{promise_id}", op="create", new_value=promise_node_payload(...))`。
    - 字段修改 → `op="set"`，`field_path` 为 `"status"`、`"title"` 或 `"payload.<字段>"`，`old_value` 取当前值（保留原来的 CAS 语义，由编译器的 `_narrative_current` 校验）。
    - 补丁放进 `delta.narrative_patches`；删除 `metadata["reader_promise_patches"]`。
  - `ReaderPromise` 模型保留，作为视图类型。

- [ ] **Step 1: 写失败测试并改写旧测试**

- `tests/test_book_state_reader_promises.py`：
  - `test_proposal_creates_promise_node`：结构化提案新增承诺 → 编译后 `reader_promises()` 返回 1 条，`promise_id` 等于节点 id；快照的 `active_promise_ids` 包含它。
  - `test_promise_update_cas_mismatch_blocks`：针对承诺 status 的 set 补丁，`old_value` 与当前值不符 → 编译被阻断（`blocked_reasons` 非空）。
  - `test_promise_status_is_versioned`：第 1 章 open，第 3 章改为 paid_off → `reader_promises(as_of_chapter=2)` 为 open，as-of 3 为 paid_off。
- `tests/test_knowledge_system_v46.py` 中断言 `book_reader_promises` 行或 `reader_promise_patches` 的用例，改为断言 narrative 节点或 `reader_promises()` 的结果。
- `tests/test_book_state_repository_projection_compiler.py` 的 “unversioned promise metadata” 用例：删除，或改为 narrative 承诺的历史回滚语义（按用例原意二选一，并在提交信息中说明）。
- `tests/test_v5_recovery_schema.py` 的 baseline 对照：`book_reader_promises` 仍由 0001 创建，但 ORM 中已没有它。按该测试处理 0002 所删 5 张表的同一方式，把它登记为“基线之后删除的表”。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_book_state_reader_promises.py tests/test_knowledge_system_v46.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现，并修改迁移**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_book_state_reader_promises.py tests/test_knowledge_system_v46.py tests/test_book_state_repository_projection_compiler.py tests/test_v5_live_migration.py tests/test_v5_recovery_schema.py tests/test_architecture_boundaries.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(book_state): store reader promises as narrative promise nodes`

---

### Task 8: Genesis story_engine 结构化

**Files:**
- Modify:
  - `forwin/genesis/normalize.py:365-571`（`_normalize_story_engine_payload`）
  - `forwin/genesis/messages.py:75-91`、`forwin/genesis/constants.py:132-137`
  - `forwin/context/providers/genesis_provider.py:38-39`
  - `forwin/ui_assets/home/app_state.js:53-57,614-736`、`forwin/ui_assets/home/app_genesis.js:358-361`
- Test: `tests/test_book_genesis_flow.py`（新增用例，并调整 story_engine 规范化相关断言）、`tests/test_genesis_story_engine_structure.py`（新建）

**Interfaces:**
- Produces（规范化后的 story_engine）：
  - `core_cast[]` 新增 `id`，默认 `f"cast-{n}"`；`opposition[]` 新增 `id`，默认 `f"opp-{n}"`；`factions[]` 保留 `faction-{n}`。三者的 id 在整个 story_engine 中去重，冲突时追加 `-2`、`-3`。
  - `core_cast[].secret: str` 改为 `core_cast[].secrets: list[str]`。兼容旧输入：非空的 `secret` 字符串转为 `[secret]`；输出中不再有 `secret` 键。
  - `relationship_axes: list[{"source": str, "target": str, "edge_type": str, "note": str, "visibility": "public" | "secret"}]`：
    - source / target 先按 id 匹配，再按名字匹配 cast / opposition / faction，匹配不到时为 `""`；
    - `edge_type` 必须属于 `WORLD_EDGE_TYPES_BY_FAMILY` 的某个族，否则为 `""`；
    - `visibility` 默认 `"public"`；
    - 字符串条目转为 `{"source": "", "target": "", "edge_type": "", "note": <原文>, "visibility": "public"}`。
  - `long_arcs: list[{"id": f"long-arc-{n}", "summary": str, "owner_refs": list[str]}]`：字符串条目转为只有 summary 的条目；owner_refs 按上面的规则解析，解析不到的丢弃。
  - `reader_promises` 保持 `list[str]`。
  - `genesis_provider`：`genesis_story_engine_summary` 用各条 `long_arcs[].summary` 以 `"；"` 拼接。
  - UI：
    - `relationship_axes`、`long_arcs` 从 `kind: 'list'` 的表单字段改为 `GENESIS_STAGE_ITEM_TARGETS.story_engine` 中的条目集合，模板分别为 `{source: '', target: '', edge_type: '', note: '', visibility: 'public'}` 与 `{id: 'long-arc-new', summary: '', owner_refs: []}`；
    - `core_cast` 模板的 `secret` 改为 `secrets: []`，对应表单字段为 `kind: 'list'`；
    - `app_genesis.js` 引用 core_cast / opposition 时优先使用 `item.id`。

- [ ] **Step 1: 写失败测试（`tests/test_genesis_story_engine_structure.py`）**

- `test_story_engine_normalizer_assigns_ids`：两个没有 id 的 core_cast、一个 opposition → id 分别为 `cast-1`、`cast-2`、`opp-1`；重复 id 被重新编号。
- `test_relationship_axes_are_structured_and_resolved_by_name`：`{"source": <cast 名字>, "target": "faction-1", "edge_type": "rival_of"}` → source 解析为 `cast-1`；`edge_type="frenemy"` → `""`；字符串条目 → 只有 note。
- `test_secret_string_upgrades_to_secrets_list`。
- `test_long_arcs_are_structured`，以及 `test_genesis_story_summary_joins_long_arc_summaries`（调用 `GenesisContextProvider`）。
- 确认 `tests/test_no_story_specific_hardcoding.py:165` 的 fallback 断言不受影响（fallback 的各列表仍为空）。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_story_engine_structure.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改规范化、提示词、上下文与 UI**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_story_engine_structure.py tests/test_book_genesis_flow.py tests/test_no_story_specific_hardcoding.py tests/test_no_story_content_in_runtime.py tests/test_api_pages_rendering.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(genesis): give story engine entities ids and structured relations`

---

### Task 9: world_seed 领域模块（规范化、校验、fallback）

**Files:**
- Create: `forwin/genesis/world_seed.py`
- Test: `tests/test_genesis_world_seed.py`（新建）

**Interfaces:**
- Consumes：Task 3 的 `calendar_from_payload`、`lexicon_from_payload`、`unit_minutes`、`StoryClockError`、`LexiconError`、`UnknownTimeUnit`；Task 5 的枚举常量；Task 6 的 `KNOWLEDGE_STATES`；Task 8 的 story_engine id。
- Produces：
  - 规范化后的 world_seed 结构（数据契约，也是 Task 10 提示词的输出要求）：

```text
calendar: {epoch_label: str, opening_minute: int >= 0, units: [{name: str, minutes: int > 0}]}
lexicon: {time_unit_aliases: [{name, minutes}], rank_ladders: [{id, name, ranks: [str]}],
          currencies: [{id, name}], appellations: [str], generic_character_refs: [str],
          travel_modes: [{id, name, speed_multiplier: float, allowed_edge_types: [MapEdgeType]}],
          news_speed_multiplier: float}
actors: [{ref, actor_tier, location_ref, travel_mode_ids: [str], resources: [str],
          agendas: [{id, goal, motivation, priority: 1..5, horizon, means_refs: [ref],
                     constraints: [str], secrecy_refs: [ref]}],
          intents: [{id, agenda_id, action_type, summary, target_refs: [ref], location_ref,
                     scheduled_in: {amount: number, unit: str}, requires_onstage: bool,
                     impact_level}]}]
relations: [{source: ref, target: ref, edge_type, note, visibility: public|secret, appellation: str}]
facts: [{id, proposition, truth_value: bool, sensitivity: public|hidden|secret,
         related_refs: [ref], nobody_knows: bool}]
knowledge: [{observer_ref: ref, fact_id, state: KNOWLEDGE_STATES, channel: "seeded"}]
rules: [{id, statement, hardness: hard|soft, checkable: {kind: str, ...} | null}]
clocks: [{id, label, due_in: {amount, unit}, owner_ref: ref | "", scope: public|secret, on_expire: str}]
reader_promises: [{id, summary, promise_type, expected_payoff_window}]
reveal_candidates: [{fact_id, arc_number: int, note}]
```

  其中 `ref` 是 story_engine 的 id（cast / opp / faction），`location_ref` 是 Genesis `map_atlas.nodes[].id`。`means_refs`、`secrecy_refs`、`target_refs`、`related_refs` 可以是 actor ref，也可以是 fact id。

  - `WORLD_SEED_KEYS: tuple[str, ...]`（上面 10 个键，按此顺序）
  - `@dataclass(frozen=True) class WorldSeedIssue: severity: str; code: str; path: str; message: str`
  - `story_engine_actor_refs(story_engine: Mapping) -> dict[str, str]`：ref → `"character"`（cast / opposition）或 `"faction"`。
  - `normalize_world_seed(payload: Mapping, *, story_engine: Mapping, map_atlas: Mapping) -> dict[str, Any]`：只做形状整理（补齐缺失的键，丢弃非 dict 条目，把数字、布尔、字符串强制转为契约类型，按原样保留未知引用以便校验报告），不判断引用是否有效。
  - `validate_world_seed(seed: Mapping, *, story_engine: Mapping, map_atlas: Mapping, arc_numbers: Collection[int]) -> list[WorldSeedIssue]`。问题码如下（无特别说明的 severity 均为 `"error"`）：
    - 日历与词表：`invalid_calendar`、`invalid_lexicon`（包装 `StoryClockError` / `LexiconError` 的消息）、`unknown_time_unit`（`scheduled_in` / `due_in` 的 unit 不在日历单位与别名中）。
    - actor：`unknown_actor_ref`、`missing_actor`（story_engine 中的实体没有 actor 条目）、`duplicate_ref`、`invalid_actor_tier`、`unknown_location_ref`、`major_actor_without_location`（character 类、tier 为 major、`location_ref` 为空）、`minor_actor_without_location`（**warning**）、`major_actor_without_agenda`、`unknown_travel_mode`。
    - 议程与意图：`invalid_priority`、`invalid_action_type`、`invalid_impact_level`、`unknown_agenda_ref`（intent 的 `agenda_id` 不在本 actor 的议程中）、`unknown_ref`（`means_refs` / `secrecy_refs` / `target_refs` / `related_refs` / `owner_ref` 无法解析）。
    - 关系：`invalid_edge_type`、`unknown_relation_ref`。
    - 事实与知识：`secret_without_knower`（sensitivity 为 hidden / secret，没有任何 knows / believes / suspects 的知识条目，且 `nobody_knows` 为假）、`unknown_fact_ref`、`unknown_observer_ref`、`invalid_knowledge_state`、`invalid_channel`（必须为 `"seeded"`）。
    - 规则、时限与揭示：`invalid_hardness`、`invalid_checkable`、`invalid_clock_scope`、`invalid_arc_number`。
    - `path` 用点号与下标表示，例如 `actors[2].intents[0].scheduled_in.unit`。
  - `fallback_world_seed(story_engine: Mapping) -> dict[str, Any]`：
    - calendar `{"epoch_label": "", "opening_minute": 0, "units": []}`；lexicon 全空，`news_speed_multiplier` 为 1.0；
    - 每个 story_engine 实体一个 actor 空壳：`{"ref", "actor_tier": "minor", "location_ref": <cast 的 current_base 或 home_location，或 faction 的 base_location，原样照抄，可能为空>, "travel_mode_ids": [], "resources": [], "agendas": [], "intents": []}`；
    - 其余列表为空。
    - 不得产生任何文本内容。

- [ ] **Step 1: 写失败测试（`tests/test_genesis_world_seed.py`；测试内构造一个最小 story_engine（cast-1、opp-1、faction-1）、map_atlas（node-1、node-2）和合法 seed，人名用“甲”“乙”这类占位）**

- `test_valid_seed_has_no_issues`
- `test_validate_reports_unknown_time_unit`：日历单位为“日”，intent 的 `scheduled_in.unit="天"` → 恰有一条 `unknown_time_unit`，`path == "actors[0].intents[0].scheduled_in.unit"`。
- `test_missing_and_unknown_actor_refs`：删掉 opp-1 的 actor 条目 → `missing_actor`；新增 `ref="cast-9"` → `unknown_actor_ref`。
- `test_major_actor_rules`：major 且没有 agenda → `major_actor_without_agenda`；major character 没有地点 → error；minor 没有地点 → 只有 warning。
- `test_secret_without_knower`：secret fact 没有知识条目 → error；设 `nobody_knows=True` 后 → 无问题。
- `test_enum_and_reference_errors`：action_type、edge_type、knowledge state、channel（`"told"`）、hardness、clock scope、arc_number 各取一个非法值 → 各自报对应的问题码。
- `test_normalize_coerces_shapes_without_judging_refs`：缺少 `rules` 键、`priority` 为字符串 `"3"`、`truth_value` 为字符串 `"false"` → 规范化后结构完整，`priority == 3`，`truth_value is False`，未知 ref 原样保留。
- `test_fallback_is_structural_shell`：actor 条目与 story_engine 实体一一对应且都为 minor；序列化结果中除 story_engine 的 id 与地图节点 id 之外没有任何字符串内容；`validate_world_seed(fallback, ...)` 没有 error。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_world_seed.py`
Expected: FAIL（`ModuleNotFoundError`）

- [ ] **Step 3: 实现 `forwin/genesis/world_seed.py`**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_world_seed.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(genesis): add world seed normalization, validation and structural fallback`

---

### Task 10: 用 world_seed 阶段替换 bootstrap（阶段机制与各入口）

**Files:**
- Modify（Genesis 内部）：
  - `forwin/genesis/constants.py:10-25,65-72,90-147`
  - `forwin/genesis/helpers.py:235-238`（`_initial_pack`）、`forwin/genesis/names_paths.py:207-244`（新增 `GenesisStageInvalidError`；`_initial_pack_dummy_merge` :221）
  - `forwin/genesis/llm.py:39,50-110,139,206-238,281-389`
  - `forwin/genesis/messages.py:105-120`
  - `forwin/genesis/planning.py:14-48`
  - `forwin/genesis/fallbacks.py:154-159`（删除 `_fallback_bootstrap`）
  - `forwin/genesis/workspace/service.py:114-220,222-321,323-437,439-498`
  - `forwin/genesis/service.py`（`BookGenesisService.__init__`）
- Modify（入口）：
  - `forwin/runtime/container.py:681-688`
  - `forwin/writer/llm/routing.py:258`
  - `forwin/api_schema/genesis.py:9-24,58-65`、`forwin/http/request_support.py:159-171`
  - `forwin/application/read_models/genesis.py:27-34,74,101-105,133,143`
  - `forwin/application/projects/genesis.py`（lock 处理，把 `GenesisStageInvalidError` 映射为 409）
  - `forwin/mcp/models.py:7-32`、`forwin/mcp/client.py:687-689,893-907`
  - `forwin/ui_assets/home/app_state.js:23-31,61-72`、`forwin/ui_assets/home/app_genesis.js:2-11`
  - `forwin_skills/genesis/bootstrap/SKILL.md` → 改名为 `forwin_skills/genesis/world-seed/SKILL.md`
  - `tests/overfit_allowlist.toml`（`constants.py` 三个条目的 `max_literals`）
- Test：
  - 改写 `bootstrap` 相关断言：`tests/test_genesis_handoff_service.py:108-121`、`tests/test_book_genesis_flow.py:347-361,560-573,710-724,1788`、`tests/test_mcp_server.py:517-530`、`tests/browser/fixtures.py:14-21,1525-1560`、`tests/browser/test_genesis_workspace.py:10-17`、`tests/browser/test_mock_book_creation_generation_regression.py:16-23`、`tests/test_skill_runtime.py:131-170`、`tests/test_llm_router.py`（若断言了 bootstrap 路由）
  - 新建：`tests/test_genesis_world_seed_stage.py`

**Interfaces:**
- Consumes：Task 9 的 `normalize_world_seed`、`validate_world_seed`、`fallback_world_seed`、`WorldSeedIssue`。
- Produces：
  - `GENESIS_STAGE_ORDER = ("brief", "world", "map", "story_engine", "book_blueprint", "world_seed")`；`_STAGE_TO_SECTION["world_seed"] = "world_seed"`；pack 顶层键 `world_seed`（替代 `execution_bootstrap`，初始值为 `{}`）。
  - 提示词：标签、系统提示词和硬约束中的 world_seed 条目，按 Task 9 的数据契约描述输出。user 段落依次为：阶段、输出要求、阶段硬约束、已锁定阶段上下文、项目 genre（直接读 `project.genre`）、WorldBible、地图节点表（`id`、name、kind、所属 submap）、story_engine（cast / opposition / factions 及其 id、relationship_axes、secrets）、卷列表（arc_number 与 synopsis）、当前 world_seed、参考骨架（fallback）。
  - `_refine_support_context` 的 world_seed 分支返回 story_engine、地图节点表和卷列表，不返回整个 pack。
  - 生成、refine 和 `patch_pack` 修改 world_seed 之后都要：先 `normalize_world_seed`，再写入 `stage_states["world_seed"]["validation_issues"] = [asdict(i) for i in validate_world_seed(...)]`。
  - `lock_stage(stage_key="world_seed")`：按当前 pack 重新校验，存在 error 时抛 `GenesisStageInvalidError`（消息列出前 5 条问题），HTTP 返回 409，MCP 透传错误。
  - `BookGenesisService.__init__(..., max_tokens: int = 1600, world_seed_max_tokens: int = 6000)`；`_call_json_with_trace_impl` 对 `stage_key.split(":")[0] == "world_seed"` 的调用使用 `world_seed_max_tokens` 作为上限。container 传入 `world_seed_max_tokens=min(config.max_tokens, 6000)`。
  - `BookGenesisPack.world_seed: dict[str, Any] = {}`、`BookGenesisPatchRequest.world_seed: dict[str, Any] | None = None`；删除 `execution_bootstrap`。`BookGenesisStageState.validation_issues: list[dict[str, Any]] = []`；MCP `StageStateView` 同样新增该字段。
  - `read_models/genesis.py` 删除自己的 `_GENESIS_STAGE_ORDER`，改为导入 `GENESIS_STAGE_ORDER`。MCP 包不导入 `forwin.*`，所以保留自己的 `StageKey` / `STAGE_KEY_ORDER`，由测试保证它们一致。
  - UI：
    - 阶段顺序与字段映射中，`bootstrap` 改为 `world_seed`，标签为“初始世界”；
    - 删除 bootstrap 表单；
    - world_seed 不提供逐字段表单，阶段面板只读展示各列表的条目数和 `validation_issues`，编辑通过已有的“按指令 refine”完成。
  - Skill：`name: genesis.world_seed`、`stage_keys: [world_seed]`。正文只写题材中立的生成要求（行动者有议程、秘密有知情者、时长单位来自本书日历等）。

- [ ] **Step 1: 写失败测试（`tests/test_genesis_world_seed_stage.py`，桩 LLM）**

- `test_stage_order_ends_with_world_seed_and_copies_match`：
  - `GENESIS_STAGE_ORDER[-1] == "world_seed"`；
  - `forwin.mcp.models.STAGE_KEY_ORDER == GENESIS_STAGE_ORDER`；
  - 从 `app_state.js` 文本中用正则取出的 `GENESIS_STAGE_ORDER` 数组也与之相等；
  - `forwin/` 下不再出现 `execution_bootstrap`。
- `test_world_seed_generation_normalizes_and_records_issues`：桩 LLM 返回一个含 `unknown_time_unit` 的 seed → pack 中的 world_seed 已规范化；`stage_states["world_seed"]["validation_issues"]` 中有该问题码；`degraded` 为假。
- `test_world_seed_generation_uses_world_seed_token_budget`：`BookGenesisService(max_tokens=1600, world_seed_max_tokens=6000)` → 桩客户端收到的首次 `max_tokens == 6000`；其它阶段仍为 1600。
- `test_world_seed_fallback_marks_degraded`：桩 LLM 返回非法 JSON → pack 为 `fallback_world_seed` 的结果，`degraded` 为真。
- `test_world_seed_lock_refused_while_validation_errors`：调用 HTTP 的 lock 路由 → 409；修正后再 lock → 成功，`creation_status` 变为 `genesis_ready`（其余阶段已锁）。
- `test_world_seed_lock_revalidates_against_current_story_engine`：world_seed 合法之后，经 `patch_pack` 删掉 story_engine 中的 cast-2 → lock 返回 409，消息中包含 `missing_actor` 或 `unknown_actor_ref`。
- `test_patch_pack_world_seed_recomputes_issues`：用 `patch_pack` 写入一个未知的 `location_ref` → 阶段状态变为 `edited`、`locked=False`，`validation_issues` 中包含 `unknown_location_ref`。
- 按 Files 改写其余测试中的 `bootstrap`，fixture pack 一律改为带 world_seed。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_world_seed_stage.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改 Genesis 内部、各入口与 UI**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_world_seed_stage.py tests/test_book_genesis_flow.py tests/test_genesis_handoff_service.py tests/test_genesis_workspace_service.py tests/test_genesis_freeze.py tests/test_mcp_server.py tests/test_skill_runtime.py tests/test_llm_router.py tests/test_large_module_boundaries.py tests/test_no_story_content_in_runtime.py tests/test_api_pages_rendering.py`
Expected: PASS。浏览器测试（`tests/browser/`）若本地可运行也一并运行；不能运行时在提交信息中注明。

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(genesis): replace bootstrap with the world_seed stage`

---

### Task 11: `WorldSeedCommitter` 与 `CanonAdmissionService.commit_world_seed`

**Files:**
- Create: `forwin/genesis/handoff/world_seed_committer.py`
- Modify: `forwin/canon/admission.py`（新方法）、`forwin/book_state/compiler.py:264-290`（角色创建时传入 `creation_context`）、`forwin/audit/events.py:27-33`
- Test: `tests/test_world_seed_committer.py`（新建）

**Interfaces:**
- Consumes：Task 3（日历、词表及其仓储，`duration_to_minutes`、`format_story_time`）、Task 4（版本化写入）、Task 5（节点类型与字段）、Task 6（`KnowledgeEvent`、`knowledge_event_to_patch`）、Task 7（`promise_node_payload`）、Task 9（`validate_world_seed`、`story_engine_actor_refs`）。
- Produces：
  - `DecisionEventType.WORLD_SEED_COMMITTED = "world_seed_committed"`、`DecisionEventType.WORLD_SEED_COMMIT_FAILED = "world_seed_commit_failed"`。
  - `class HandoffStageError(ValueError)`（基类，放在 `forwin/genesis/handoff/commands.py`）；`class WorldSeedCommitError(HandoffStageError)`，属性 `issues: tuple[str, ...]`。
  - `seed_ref_id(project_id: str, kind: str, ref: str) -> str` → `f"{kind}-{sha256(f'{project_id}\0{kind}\0{ref}'.encode()).hexdigest()[:20]}"`。kind 取值：character、faction、agenda、intent、clock、rule、fact、edge、promise。用连字符而不是下划线，以免触发护栏的 `story_id_literal` 检查。
  - `map_node_ids_by_genesis_ref(session: Session, project_id: str) -> dict[str, str]`：读取 `MapNodeRow.metadata_json["source_node_id"]`，得到 Genesis 节点 id → 数据库节点 id。
  - `build_world_seed_deltas(*, project_id: str, revision_id: str, seed: Mapping, story_engine: Mapping, map_node_ids: Mapping[str, str], calendar: StoryCalendar, lexicon: BookLexicon) -> list[GraphDelta]`（纯函数）：
    - 返回 3 个第 0 章的 delta：`f"genesis-seed:{project_id}:world"`（`delta_type="world_state"`，含节点、边、事实）、`:cognition`（知识事件）、`:narrative`（承诺）。
    - 每个 delta 都设置 `source_type="genesis_seed"`、`source_id=revision_id`、`story_time=format_story_time(calendar, calendar.opening_minute)`、`story_time_start = story_time_end = calendar.opening_minute`。
    - 引用无法解析时抛 `WorldSeedCommitError`，地图节点查不到时问题码为 `unresolved_location_ref`。
    - 映射规则：

| world_seed | BookState（全部为 create 补丁） |
| --- | --- |
| cast / opposition 的 actor | character 节点：`name`（取自 story_engine）、`summary=role`、`profile={narrative_role: role, culture_tag, fear}`、`state={alive_status: "alive", location_id, restraint: {state: "free", by_ref: "", since: None}, actor_tier, next_turn_at: opening, travel_mode_ids, resources, goal: desire, faction_id: <所属 faction 的种子 id，可为空>}`、`metadata={genesis_ref, source: "genesis_seed"}` |
| faction 的 actor | faction 节点：`name`、`summary=goal`、`profile={narrative_role: role, culture_tag}`、`state={actor_tier, next_turn_at, location_id, goal, resources}`、metadata 同上 |
| agenda | agenda 节点（ref 取 `f"{actor_ref}/{agenda.id}"`）：`name=goal`、`profile={owner_ref, goal, motivation, horizon}`、`state={status: "active", priority, means_refs, constraints, secrecy_refs}` |
| intent | intent 节点：`name=summary`、`profile={owner_ref, agenda_ref, action_type, summary, target_refs, location_id, requires_onstage, impact_level, expected_effects: [], preconditions: []}`、`state={status: "planned", scheduled_at: opening + duration_to_minutes(scheduled_in), slip_count: 0}` |
| relation | `EdgePatch`：族由 `edge_type` 在 `WORLD_EDGE_TYPES_BY_FAMILY` 中唯一确定；`visibility_default` 在 secret 时为 `"hidden"`，否则为 `"visible"`；`metadata={note, appellation, genesis_ref}` |
| fact | `FactPatch`：`truth_value` 为 `"true"` 或 `"false"`、`sensitivity_level=sensitivity`、`related_refs` 解析为节点 id 或 `fact:<id>` |
| knowledge | `knowledge_event_to_patch(KnowledgeEvent(observer_type, observer_id, f"fact:{fact 节点 id}", state, "seeded", at=opening))`。observer_type：character → `character`，faction → `faction`，organization / group → `group` |
| rule | rule 节点：`name=statement`、`profile={statement, hardness, checkable}`、`state={status: "active"}` |
| clock | clock 节点：`name=label`、`profile={label, owner_ref, scope, on_expire}`、`state={due_at: opening + duration_to_minutes(due_in), status: "running"}` |
| reader_promise | `NarrativePatch(target_ref=f"promise:{seed id}", op="create", new_value={"node_type": "promise", "title": summary, "status": "open", "payload": {promise_type, expected_payoff_window, created_at_chapter: 0}})` |

  - `@dataclass(frozen=True) class WorldSeedCommitSummary: skipped: bool; graph_delta_ids: tuple[str, ...]; actor_count: int; agenda_count: int; intent_count: int; clock_count: int; fact_count: int; knowledge_count: int; rule_count: int; promise_count: int`
  - `class WorldSeedCommitter: __init__(self, admission: CanonAdmissionService | None = None)`；`commit(self, *, session: Session, project: Project, revision: BookGenesisRevision, pack: Mapping) -> WorldSeedCommitSummary`：
    1. 用 `validate_world_seed` 重新校验，存在 error 时抛 `WorldSeedCommitError`。
    2. 3 个 delta 的 id 都已存在时，返回 `skipped=True`。
    3. 保存日历与词表。
    4. 解析地图节点 id，调用 `build_world_seed_deltas`。
    5. `BookStateReviewGate(session).review(ApprovedGraphDeltaSet(project_id, chapter_number=0, graph_deltas=..., approved_by=["genesis_seed"]))`，未通过时抛 `WorldSeedCommitError`（issues 取自审查结果）。
    6. 调用 `admission.commit_world_seed(...)`。
    7. 记录 `WORLD_SEED_COMMITTED` 事件（payload 为 summary 的计数）。
  - `CanonAdmissionService.commit_world_seed(self, *, session: Session, project_id: str, revision_id: str, approved_changes: ApprovedGraphDeltaSet) -> BookStateCompileResult`：
    - 以 `FOR UPDATE` 锁定 Project；
    - 要求 `approved_changes.chapter_number == 0`，且每个 delta 的 `source_type == "genesis_seed"`；
    - 项目中已有非 `genesis_seed` 的 graph delta 时抛 `CanonStaleVersion`；
    - 编译时 `compiler_run_id=f"genesis-seed-{revision_id}"`；未提交时抛 `CanonWriteFailure`；幂等时直接返回结果；
    - 入队 outbox `knowledge.projection.refresh_requested`（event id 为 `f"genesis-seed:{project_id}:projection"`）；
    - 不提交事务（与 `commit_world_edit` 相同，由调用方的 session 负责）。
  - 编译器的角色分支：`node.metadata.get("genesis_ref")` 非空时，传入 `creation_context={"genesis_ref_id": <该值>}`。

- [ ] **Step 1: 写失败测试（`tests/test_world_seed_committer.py`；seed 内容与 Task 9 测试同构，用占位名字）**

- `test_build_world_seed_deltas_maps_every_section`（纯函数）：三个 delta 的 id、source_type、时间列都正确；各类补丁的数量与 seed 一致；intent 的 `scheduled_at == opening + 2*1440`（seed 中写的是 `{amount: 2, unit: "日"}`）；character 的 `location_id` 为数据库节点 id。
- `test_commit_rejects_unresolved_location_ref`：`map_node_ids` 中缺少 node-2 → 抛 `WorldSeedCommitError`，issues 中包含 `unresolved_location_ref`。
- `test_commit_world_seed_writes_book_state_calendar_and_lexicon`（postgres；先用 `GenesisMapBootstrap` 或直接插入带 `source_node_id` 的 `MapNodeRow`）：
  - as-of 0 的运行时中有 character / faction / agenda / intent / clock / rule 节点，字段符合映射表；
  - 边上有 `appellation`；facts 的 sensitivity 正确；knower 的 `knowledge_by_ref` 正确；narrative 中有 promise 节点；
  - `story_calendars`、`book_lexicons` 各 1 行；
  - 角色都有 `profile.personality_loadout`，`character_identity_map` 中的 genesis ref 正确；
  - `current_story_minute(as_of_chapter=0) == opening`。
- `test_commit_world_seed_is_idempotent`：再提交一次 → `skipped=True`，行数不变。
- `test_commit_world_seed_refuses_when_book_already_has_canon`：先写入一个 `source_type="writer_output"` 的 delta → 抛 `CanonStaleVersion`。
- `test_commit_world_seed_rejects_invalid_seed_without_writes`：seed 存在 `missing_actor` → 抛 `WorldSeedCommitError`，日历、词表和 delta 都没有写入。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_world_seed_committer.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_world_seed_committer.py tests/test_v5_canon_architecture.py tests/test_character_creation_helper.py tests/test_book_state_canon_gate.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(genesis): commit the world seed as chapter-0 BookState`

---

### Task 12: 开写交接：新顺序、卷字段持久化、类型化失败，删除第 0 章 subworld 角色生成

**Files:**
- Modify:
  - `forwin/genesis/handoff/service.py:30-184`、`forwin/genesis/handoff/commands.py:7-24`、`forwin/genesis/handoff/arc_materializer.py:17-54`、`forwin/genesis/handoff/chapter_materializer.py:20-35`、`forwin/genesis/handoff/map_bootstrap.py`
  - `forwin/state/updater.py:117-146`（`create_arc_plan`）
  - `forwin/application/projects/genesis.py:417-438`
  - `forwin/subworld_manager.py:228-294,447-477`、`forwin/planning/arc_envelope.py:257-261`
- Test: `tests/test_genesis_handoff_service.py`、`tests/test_book_genesis_flow.py:291,522,670`、`tests/test_project_operation_guards.py:877`、`tests/test_subworld_control.py:285,1513`、`tests/test_genesis_arc_materialization.py:44`

**Interfaces:**
- Consumes：Task 11 的 `WorldSeedCommitter`、`HandoffStageError`、`WorldSeedCommitError`。
- Produces：
  - `GenesisHandoffService.start_writing` 的步骤顺序：
    1. 前置检查；
    2. `START_WRITING_REQUESTED`；
    3. `materialize_book_arcs`；
    4. 地图 bootstrap；
    5. `WorldSeedCommitter.commit`；
    6. `materialize_arc_chapter_plans`（卷 1）；
    7. setting_summary；
    8. revision 置为 locked，`creation_status` 置为 writing。
  - `class GenesisMapBootstrapError(HandoffStageError)`：地图 bootstrap 内部的失败都包装为该类型。
  - 失败处理统一按异常类型判断（不再看消息文本）：
    - `HandoffStageError` → handoff 内 `session.rollback()`，用新的 `StateUpdater` 写失败事件（地图为 `MAP_GENERATION_FAILED`，种子为 `WORLD_SEED_COMMIT_FAILED`，payload 带 issues），flush 后重新抛出；
    - application 层捕获 `HandoffStageError` 后 `session.commit()` 并返回 409；其它 `ValueError` 照旧 rollback 并返回 409。
  - `StartWritingHandoffResult` 新增 `world_seed_summary: dict[str, Any]`。
  - `create_arc_plan(..., title: str = "", goal: str = "", stakes: str = "", payoff_direction: str = "")`；`materialize_book_arcs` 从 blueprint 写入这四个字段；`_arc_payload_from_row` 改为读取这些列。
  - `SubWorldManager`：
    - 删除 `ensure_initial_registry_for_active_arc`，以及 `arc_envelope.py:257-261` 对它的调用；
    - 删除 `apply_arc_delta` 中 `chapter_number <= 0` 时用 `CharacterCreationHelper` 建角色的分支（:447-477）。所有 core seed 一律成为带 `pending_entity_admission: True` 的 roster item，由实体准入在 canon 时落地。
    - `plan_subworld_delta` 与 `apply_initial_arc_plan` 保留到 P4（见“与设计的取舍”第 3 条）。

- [ ] **Step 1: 写失败测试**

- `test_start_writing_commits_world_seed_after_map_and_before_chapter_plans`：桩出章节规划 LLM，在桩里记录调用时的状态：
  - 此时 BookState 中已有种子角色节点，`map_nodes` 已存在；
  - 交接完成后 `world_seed_summary.actor_count` 与 seed 一致，revision 为 locked。
- `test_world_seed_failure_rolls_back_handoff_and_records_failure_event`：seed 合法且已加锁，但用 monkeypatch 让 `map_node_ids_by_genesis_ref` 返回 `{}`（模拟地图生成丢失了 Genesis 节点）→ HTTP 返回 409；arcs、chapter plans、map 行和 delta 全部回滚；`WORLD_SEED_COMMIT_FAILED` 事件已持久化，payload 中含 `unresolved_location_ref`；`creation_status` 仍为 `genesis_ready`。
- `test_map_failure_is_typed_and_persists_failure_event`：改写现有的 `test_map_bootstrap_failure_rolls_back_handoff_and_records_failure_event`，让失败消息不含 “map” / “地图” / “BookMap” 字样，仍然能持久化 `MAP_GENERATION_FAILED`（修复按消息文本判断的问题）。
- `test_arc_plan_versions_persist_blueprint_goal_stakes_payoff`：卷行的 title / goal / stakes / payoff_direction 等于 blueprint 中的值；`_arc_payload_from_row` 返回这些值。
- `test_handoff_retry_reuses_seed`：同一项目在第二次交接时（模拟重试：先手工提交种子，再运行 handoff）→ `world_seed_summary["skipped"] is True`，不重复写入。
- `tests/test_subworld_control.py`：删除 :1513 的 `test_genesis_subworld_core_character_materializes_at_chapter_zero`；检查 :285 的用例，把第 0 章立即建角色的断言改为 roster item 处于 pending admission 状态。
- 现有交接测试的 fixture pack 都要带合法的 world_seed，并且 story_engine 带 id。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_handoff_service.py tests/test_book_genesis_flow.py tests/test_project_operation_guards.py -k "start_writing or handoff or arc"`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改**

- [ ] **Step 4: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_genesis_handoff_service.py tests/test_book_genesis_flow.py tests/test_project_operation_guards.py tests/test_subworld_control.py tests/test_genesis_arc_materialization.py tests/test_canon_atomic_transaction.py tests/test_mcp_server.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(genesis): seed the world at start-writing and persist arc goals`

---

### Task 13: 词表消费者：泛称、关系技能与称谓锚点

**Files:**
- Modify:
  - `forwin/characters/normalization.py:4-20`、`forwin/characters/creation.py:31,53`
  - `forwin/checker/reference_classifier.py:27-82,156-211`、`forwin/naming/entity_registrar.py:89-160,693-720`、`forwin/book_state/writer_contract.py:359-362`
  - `forwin/personality/enrichment.py:137-147`
  - `forwin/book_state/query.py`、`forwin/checker/rules.py:118-172`、`forwin/protocol/context.py:169-245`、`forwin/context/providers/state_provider.py`、`forwin/writer/prompt_core/builders.py:105,144,203`
  - `tests/overfit_allowlist.toml`
- Test: `tests/test_character_relationship_enrichment.py:60-101`、`tests/test_reference_classifier_scope.py:11,31,53`、`tests/test_placeholder_leakage_gate.py:193-195`、`tests/test_subworld_control.py:77-91,800-823`、`tests/test_canon_name_anchors.py`（新建）

**Interfaces:**
- Consumes：Task 3 的 `BookLexiconRepository`；Task 5 的 `rival_of`；Task 11 写在边上的 `metadata.appellation`。
- Produces：
  - `GENERIC_CHARACTER_TOKENS` 删除 店小二、师兄、师姐、弟子，改为 frozenset。`is_generic_character_name(name: str, *, extra_refs: Iterable[str] = ()) -> bool`。`CharacterCreationHelper` 传入 `BookLexiconRepository(self.session).get(request.project_id).generic_character_refs`。
  - `LANGUAGE_GENERIC_CHARACTER_REFERENCES` 删除同样 4 个词。`classify_reference(name: str, *, extra_generic_refs: frozenset[str] = frozenset())` 与 `looks_like_generic_character_reference(name, *, extra_generic_refs=frozenset())`。
    - `EntityRegistrar._classify` 和 `WriterContractDeltaBuilder.build` 都持有 session，各自加载本书词表后传入。
    - `LLMEntityAdmissionClassifier.classify` 新增 `extra_generic_refs` 参数，由 `EntityRegistrar` 传入（实施前核对调用链，保证每条路径都拿到本书词表）。
  - `RelationshipPersonalityEnricher._skill_for_relation` 只看边类型：`{"mentor_of", "master_of"}` → `"rel-mentor-protector"`；`{"rival_of"}` → `"rel-rival-respect"`；其余 → `""`。
  - `BookStateQuery.canon_name_anchors(project_id: str, *, as_of_chapter: int) -> list[CanonNameAnchor]`：
    - 取 as-of 运行时中处于激活状态的边，类型属于 `{"kin_of", "family_of", "master_of", "mentor_of"}`，且 `metadata.appellation` 非空；
    - 两端都是有名字的 character 节点；
    - 生成 `CanonNameAnchor(role_label=f"{source.name}的{appellation}", canonical_name=target.name)`，按 `(role_label, canonical_name)` 去重并排序。
  - `ContinuityChecker._canon_name_anchors` 改为调用 `self.book_state.canon_name_anchors(...)`；`self.book_state is None` 时返回 `[]`。
  - `ChapterContextPack.canon_name_anchors: list[CanonNameAnchorRef] = []`，其中 `class CanonNameAnchorRef(BaseModel): role_label: str; canonical_name: str` 定义在 `protocol/context.py`。由 `state_provider` 填入；`builders.py` 三处调用改为传入 `anchors=[CanonNameAnchor(a.role_label, a.canonical_name) for a in context.canon_name_anchors]`。
  - allowlist：两个泛称条目改为 `category = "language_lexicon"`，删除 `remove_in`，`max_literals` 设为当前命中数（预计 8 和 48，以实际扫描为准）；删除 `enrichment.py` 的 P1 条目。

- [ ] **Step 1: 写失败测试并改写旧测试**

- `test_book_lexicon_generic_refs_extend_generic_names`：词表 `generic_character_refs=("弟子",)` → 对该项目，`CharacterCreationHelper` 以 `reject_or_group` 策略拒绝名字“弟子”；对没有词表的项目则接受。
- `test_reference_classifier_uses_book_generic_refs`：`classify_reference("店小二")` 不再是 generic；传入 `extra_generic_refs=frozenset({"店小二"})` 后是 generic。
- `tests/test_character_relationship_enrichment.py`：竞争关系用例改用 `edge_type="rival_of"`；新增断言：`edge_type="enemy_of"`、description 中写着“对手” → 不选任何技能。
- `tests/test_canon_name_anchors.py`：
  - `test_anchors_come_from_kinship_edges_with_appellation`：角色“丙”经 `kin_of` 边指向“丁”，`appellation="母亲"` → 锚点为 `("丙的母亲", "丁")`；没有 appellation 的边不产生锚点。
  - `test_continuity_checker_flags_drift_from_book_state_anchor`：正文写“丁戊”→ 检出漂移。
- `tests/test_subworld_control.py:800-823` 的“默认没有锚点”用例改为“没有亲属边时没有锚点”。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_canon_name_anchors.py tests/test_character_relationship_enrichment.py tests/test_reference_classifier_scope.py tests/test_placeholder_leakage_gate.py tests/test_character_creation_helper.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 实现**

- [ ] **Step 4: 运行，确认通过**

Run: 同 Step 2，再加上 `tests/test_subworld_control.py tests/test_entity_admission.py tests/test_book_state_writer_contract.py tests/test_prompt_revision.py tests/test_writer_prompt_contract.py tests/test_no_story_content_in_runtime.py`
Expected: PASS（prompt 快照的 fixture 没有锚点，预期不变；如有变化，按 P0 Task 8 Step 4 的方法重新生成并核对 diff）

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor: read generic references and name anchors from the book lexicon and BookState`

---

### Task 14: 地图生成去掉题材词

**Files:**
- Modify: `forwin/map/generator.py:81-131,437-523`、`forwin/map/genesis_adapter.py:13,29-92,156-168,214-276`、`forwin/map/protocol.py:58-86`（`SubWorldMapSpec`）、`tests/overfit_allowlist.toml`
- Test: `tests/test_map_generation.py:13,34-35`、`tests/test_map_world_integration.py:52,70,190,235`、`tests/test_map_models.py:118,190,234`、`tests/test_map_genesis_adapter.py`

**Interfaces:**
- Produces：
  - `SubWorldMapSpec.region_kinds: dict[str, str] = {}`（区域名 → Genesis region 的 `kind`），由 adapter 从 regions 行填入。
  - `generator.py`：
    - 删除 `_region_type_for_role`，`region_type = spec.region_kinds.get(role, "region")`；
    - 删除 `_terrain_for_role`，地形取 `_pick(spec.terrain_tags, rng, fallback="mixed")`；
    - `_danger_for_role(spec, rng)` 只用 `danger_profile["base"]` 加随机扰动（删除关键词加成）；
    - `_generated_region_role(index)` 返回 `f"区域{index}"`；
    - `_connection_role_to_edge_type(role)`：role 属于 `MapEdgeType` 的取值时原样返回，否则返回 `"road"`；
    - `_node_type_label`、`_baseline_node_name` 中若有题材词，改为中性写法（以扫描结果和人工核对为准）。
  - `genesis_adapter.py`：
    - 删除 `_DEFAULT_REGION_ROLES`，没有 regions 时用 `[f"区域{i}" for i in range(1, 4)]`；
    - `_subworld_type(submap)` 只看 `scope`（realm、plane、underground，其余为 continent）；
    - `_connection_roles(atlas, submap)` 收集该 submap 相关的 atlas 边中属于 `MapEdgeType` 的 `edge_type` / `kind` 值（去重、保持顺序），没有则为 `["road"]`；
    - `_danger_base(submap, ...)`：`submap.get("danger_level")` 为数值时取该值，否则为 2.0。
  - allowlist：删除这 7 个 P1 条目（generator 3 个、genesis_adapter 4 个）。

- [ ] **Step 1: 写失败测试并改写旧测试**

- `test_generator_has_no_genre_keyword_branches`：两个 spec 只有区域名不同（“宗门山门”与“港口”），其余参数与种子都相同 → 生成结果的 `region_type`、`terrain`、`default_danger_level` 相同。
- `test_connection_roles_come_from_atlas_edge_types`：atlas 中有一条 `kind="portal"` 的边 → `required_connection_roles == ["portal"]`；没有边时为 `["road"]`；生成出的锚点间边类型与之对应。
- `test_region_kind_flows_into_region_type`。
- 改写 `tests/test_map_generation.py:13,34-35`（改用 `["road", "mountain_pass", "portal"]`）和 `tests/test_map_world_integration.py`、`tests/test_map_models.py` 中依赖名字嗅探的断言（例如名为“上古秘境界”但 scope 不是 realm 的情况，期望值改为 continent）。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_map_generation.py tests/test_map_world_integration.py tests/test_map_models.py tests/test_map_genesis_adapter.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改**

- [ ] **Step 4: 删除 allowlist 条目，运行测试，确认通过**

Run: 同 Step 2，再加上 `tests/test_no_story_content_in_runtime.py tests/test_genesis_handoff_service.py tests/test_book_genesis_flow.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(map): derive region types, terrain and routes from Genesis structure`

---

### Task 15: 多题材 fixture 与端到端集成测试

**Files:**
- Create: `tests/fixtures/world_seed/xianxia.json`、`tests/fixtures/world_seed/interstellar.json`、`tests/fixtures/world_seed/urban_mystery.json`、`tests/test_world_seed_multi_genre.py`
- Modify: `tests/overfit_allowlist.toml`（只在必要时修改 `[canary].ignore`）

**Interfaces:**
- 每个 fixture 的结构：`{"project": {"title", "genre", "premise", "target_total_chapters"}, "pack": {"book_brief", "world": {"world_bible", "map_atlas", "story_engine"}, "book_arc_blueprint", "world_seed"}}`。
  - 三本书的题材、日历单位、出行方式、等级体系互不相同（例如：仙侠用时辰与飞剑；星际用标准时与跃迁艇；都市悬疑用小时与地铁）。
  - 每本至少有 3 个 actor（含 1 个 faction）、每个 major actor 至少 1 个议程、2 个 intent、1 个 secret fact 及其知情者、1 个误信、1 个 clock、1 条带 appellation 的亲属边、1 个读者承诺。
- canary：fixture 中 `name` 等键下的 2–6 字 CJK 串会成为金丝雀词。人名、地名必须是 `forwin/` 中不存在的专名。时间单位这类通用词若命中现有词表，在 `[canary].ignore` 中登记，并在提交信息中列出。

- [ ] **Step 1: 写测试（参数化三个 fixture）**

- `test_fixture_world_seed_is_valid`：`validate_world_seed(...)` 没有 error。
- `test_fixture_flows_from_genesis_to_chapter_zero_book_state`（postgres，桩 LLM）：
  - 用 fixture 建项目和 Genesis revision（各阶段已锁、`creation_status="genesis_ready"`）；
  - 调用 `GenesisHandoffService.start_writing`，桩出章节规划 LLM；
  - 断言 as-of 0 运行时中 actor、议程、意图、时限、事实、知识、承诺的数量与 fixture 一致；
  - 误信观察者的 `false_beliefs` 非空；
  - 种子 delta 的 label 等于 `format_story_time(calendar, opening)`；
  - `canon_name_anchors(as_of_chapter=0)` 非空；
  - 日历与词表各 1 行。
- `test_three_genres_share_one_code_path`：用 spy 包住 `build_world_seed_deltas`、`validate_world_seed`、`BookStateReviewGate.review`、`CanonAdmissionService.commit_world_seed` → 每个 fixture 各调用 1 次，三者的调用序列相同。

- [ ] **Step 2: 运行，确认通过（这一任务只增加测试，预期直接通过；若失败，回到对应任务修正实现，而不是修改 fixture 去迁就）**

Run: `.venv/bin/python -m pytest -q tests/test_world_seed_multi_genre.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 3: 全量测试并提交**

Commit message: `test: run three genres through world seed and chapter-zero BookState`

---

### Task 16: P1 收尾：推进阶段标记、更新文档

**Files:**
- Modify: `tests/overfit_allowlist.toml`（`meta.completed_phase = "P1"`）、`Design-docs/CURRENT_ARCHITECTURE.md`、`Design-docs/CURRENT_DESIGN.md`（:87 的阶段列表）、`Design-docs/DESIGN_STATUS.md`、`forwin/book_state/README.md`
- Test: `tests/test_no_story_content_in_runtime.py`、`tests/test_design_status_docs.py`、`tests/test_architecture_boundaries.py`

- [ ] **Step 1: 设置 `completed_phase = "P1"`，运行护栏测试**

Run: `.venv/bin/python -m pytest -q tests/test_no_story_content_in_runtime.py`
Expected: PASS。若 `test_pending_removal_entries_name_a_future_phase` 失败，说明还有 `remove_in = "P1"` 的条目未清理，回到 Task 13 或 14。

- [ ] **Step 2: 更新文档**

写明以下内容：
- 阶段顺序以 world_seed 结尾；
- 开写交接的新顺序；
- 故事时间为整数分钟，以及 `story_calendars` / `book_lexicons`；
- 节点版本化与 narrative 快照；
- 新节点类型；
- 知识事件；
- 读者承诺落为 narrative 节点；
- `0003_book_state_v6`；
- “与设计的取舍”中推迟到 P2 / P4 的事项。

保留 `tests/test_design_status_docs.py` 要求的全部标记。

- [ ] **Step 3: 运行文档与边界测试**

Run: `.venv/bin/python -m pytest -q tests/test_design_status_docs.py tests/test_architecture_boundaries.py`
Expected: PASS

- [ ] **Step 4: 全量测试并提交**

Run: `.venv/bin/python -m pytest -q` → 全部通过，记录通过数。
Commit message: `docs: close world engine overhaul P1`

---

### Task 17: 运行手册：合入、部署、迁移（每一步都需用户确认）

这一任务不写代码，由主会话逐步执行。前提：P0 的 Task 13 已完成（线上已停写、已清库、`alembic_version` 为 `0002_drop_dead_tables`）。每一步执行前先向用户说明，并取得确认。

- [ ] **Step 1: 备份**

在生产应用容器中执行 `scripts/backup_forwin_data.py`（方式与 P0 Task 13 Step 2 相同）。

- [ ] **Step 2: 合入 master 并推送**

在 `master` 上执行 `git merge --no-ff claude/world-engine-p1`，运行 `.venv/bin/python -m pytest -q` 全部通过后，`git push origin master`。

- [ ] **Step 3: 部署**

Run: `ssh infra-150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`

- [ ] **Step 4: 执行迁移**

在应用容器中运行 `alembic upgrade head`，确认 `alembic_version` 为 `0003_book_state_v6`，应用正常启动（`require_v5_schema` 通过）。

- [ ] **Step 5: 冒烟测试（不调用 LLM）**

- MCP `project_list` 正常；
- 新建一个测试项目，`genesis_get` 的阶段列表以 `world_seed` 结尾；
- `book_lexicons`、`story_calendars` 表存在且为空。

完整的 Genesis → 开写验证需要真实 LLM，运行成本须另行经用户批准。
