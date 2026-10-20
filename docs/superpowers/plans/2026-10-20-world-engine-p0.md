# 世界引擎大修 P0：停写清库、防过拟合护栏与纯删除 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** 落地防过拟合护栏，删除所有不需要替代物就能删掉的样例剧本内容、死代码和死表，补齐清库工具，然后在用户确认后停写、清库、部署。

**Architecture:**
- 护栏是一个 AST 扫描器加一份 TOML allowlist。现存命中全部分类登记；计划在后续阶段删除的，登记为带 `remove_in` 的 `pending_removal`。
- 删除按子系统分任务进行，每个任务结束时全量测试通过。
- 数据库只新增一个前向迁移 `0002_drop_dead_tables`。
- 清库是一个独立脚本，最后一个任务按运行手册执行。

**Tech Stack:** Python 3.13（`.venv`）、SQLAlchemy、Alembic、PostgreSQL（测试库 `127.0.0.1:55432`）、pytest、`ast`、`tomllib`、qdrant-client、minio。

**Spec:** `docs/designs/2026-10-20-world-engine-overhaul.md`（重点读 §7、§10、§11 与附录 A、B）

## Global Constraints

- 运行代码只处理结构；所有故事内容来自 Genesis、world_seed、BookState 或 LLM 输出；题材相关词只能来自本书的 lexicon。（设计 §7.1）
- 已合入的迁移**不得再修改**，0001 同样不得原地修改。新 revision id 不超过 32 个字符（`_stamp_test_schema` 使用 `VARCHAR(32)`）。
- 清库保留全局的 publisher 扩展与浏览器会话表（`publisher_extension_clients`、`publisher_connection_states`、`publisher_extension_platform_states`、`publisher_browser_sessions`、`publisher_browser_session_entries`）。
- 清库和每次部署在执行前都需要用户确认。
- 分支：`claude/world-engine-overhaul`。本机没有配置 git 身份，每次提交都用 `git -c user.name=CASPER -c user.email=magi1@CASPERs-Mac-mini.local commit ...`，提交信息末尾加 `Co-Authored-By: Claude Opus 5.5 <noreply@anthropic.com>`。
- 测试命令：`.venv/bin/python -m pytest -q`。基线为 2362 passed、1 skipped（约 4.5 分钟）。每个任务结束时全量测试必须全部通过，数量允许因删测试而减少。
- 不调用真实 LLM，不连接生产库；生产操作只在 Task 13 中、经用户确认后执行。

## Review Focus

1. 清库时仍有生成任务或上传任务在跑 → 应拒绝执行，并列出阻塞的任务。由 Task 1 的 `test_purge_refuses_when_generation_task_active` 和 `test_purge_refuses_when_upload_job_active` 覆盖。
2. 清库时 Qdrant 或 MinIO 不可达 → 数据库清理结果照常报告，外部存储错误列入 `errors`，退出码为 2，不得报告成“干净成功”。由 Task 1 的 `test_purge_reports_external_store_errors` 覆盖。
3. 记忆库集合因向量维度不同被静默改名为 `chapter_memories_384d` → 清库要删除所有维度后缀变体。由 Task 1 的 `test_purge_drops_dimension_suffixed_qdrant_collections` 覆盖。
4. 项目里已有 Genesis 未来卷（只有卷、没有章节计划）时调用 extend-generation → 新卷必须从最后一卷的 `chapter_end + 1` 开始，章节号不得重叠。由 Task 6 的 `test_extend_generation_starts_after_last_genesis_arc` 覆盖。
5. 项目 genre 不在 pulp 题材目录里（例如“星际”）→ 使用中性 profile，不得从正文猜题材。由 Task 9b 的 `test_unmatched_genre_uses_neutral_pulp_profile` 覆盖。

---

### Task 1: 修正项目删除的漏删，并新增清库脚本

**Files:**
- Modify: `forwin/http/project_support.py:48-87`（`_delete_project`）
- Create: `scripts/purge_all_projects.py`
- Test: `tests/test_bulk_delete_api.py`（新增 1 个用例）、`tests/test_purge_all_projects.py`（新建）

**Interfaces:**
- Consumes：`forwin.generation.task_repository.TERMINAL_GENERATION_STATUSES`、`forwin.http.runtime.UPLOAD_TERMINAL_STATUSES`、`forwin.publisher_runtime.covers.PublisherCoverService.cleanup_orphaned_files() -> list[str]`、`forwin.llm_kb.store.DEFAULT_LLM_KB_ROOT`、`forwin.obsidian.exporter.DEFAULT_VAULT_ROOT`、`InfrastructureConfig` 中的 `artifact_root`、`artifact_backend`、`minio_*`、`qdrant_url`、`qdrant_collection`、`llm_kb_qdrant_collection`、`publisher_cover_dir`（实现前核对确切字段名）。
- Produces：
  - `_delete_project(session, project_id) -> None`：签名不变。在通用的按 `project_id` 删除循环之前，依次删除 `publisher_upload_receipts` → `publisher_upload_attempts` 与 `publisher_operator_actions`（按该项目的 upload job id）→ `publisher_milestones`（按该项目的 work binding id）→ `outbox_events`（`aggregate_type == "project" and aggregate_id == project_id`）。
  - `scripts/purge_all_projects.py`：
    - `@dataclass(frozen=True) class MinioTarget: endpoint: str; access_key: str; secret_key: str; bucket: str; prefix: str; secure: bool`
    - `@dataclass(frozen=True) class PurgeTargets: artifact_root: Path; minio: MinioTarget | None; qdrant_url: str; qdrant_collection_names: tuple[str, ...]; local_project_roots: tuple[Path, ...]; publisher_cover_dir: Path`
    - `@dataclass(frozen=True) class PurgeReport: dry_run: bool; project_ids: tuple[str, ...]; deleted_projects: int; removed_paths: tuple[str, ...]; removed_minio_objects: int; dropped_qdrant_collections: tuple[str, ...]; errors: tuple[str, ...]`
    - `class PurgeBlocked(RuntimeError)`
    - `targets_from_config(config: InfrastructureConfig) -> PurgeTargets`
    - `purge_all_projects(*, session_factory, targets: PurgeTargets, dry_run: bool, qdrant_client=None, minio_client=None, cover_service=None) -> PurgeReport`
    - `main(argv: list[str] | None = None) -> int`：默认 dry-run；只有带 `--yes-delete-all-projects` 才真正删除。返回 0 = 成功，2 = 有外部存储错误，3 = 被阻塞。

- [ ] **Step 1: 写失败测试（项目删除）**

在 `tests/test_bulk_delete_api.py` 新增 `test_delete_project_removes_publisher_child_rows_and_project_outbox_events`，沿用文件里现有的 `setUp` 和 harness。
- 准备数据：项目 A 有 `PublisherUploadJob`、`PublisherUploadAttempt`、`PublisherUploadReceipt`（同时引用 job 和 attempt）、`PublisherOperatorAction`、`PublisherWorkBinding`、`PublisherMilestone`，以及 `OutboxEvent(aggregate_type="project", aggregate_id=A)`；项目 B 有一条 `OutboxEvent(aggregate_type="project", aggregate_id=B)`。
- 删除 A 之后，断言 A 的上述各表行数都为 0，B 的 outbox 行仍然存在。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_bulk_delete_api.py::BulkDeleteApiTest::test_delete_project_removes_publisher_child_rows_and_project_outbox_events`（类名以文件中的实际名称为准）
Expected: FAIL（外键 IntegrityError，或 outbox 行残留）

- [ ] **Step 3: 在 `_delete_project` 中按 Interfaces 的顺序补齐删除**

- [ ] **Step 4: 写清库脚本的失败测试**

新建 `tests/test_purge_all_projects.py`，用 `tests/postgres.py::postgres_test_url` 建库，外部存储用 `tmp_path` 下的目录和假的 qdrant / minio / cover 客户端。用例如下：
- `test_purge_refuses_when_generation_task_active`：存在 `GenerationTask(status="running")` → 抛出 `PurgeBlocked`，消息包含该 task id；数据不变。
- `test_purge_refuses_when_upload_job_active`：存在非终态的 `PublisherUploadJob` → 抛出 `PurgeBlocked`。
- `test_purge_dry_run_changes_nothing`：两个项目加若干本地文件；`dry_run=True` 时报告列出两个 project id 和将要删除的路径，但数据库行数和文件都不变。
- `test_purge_deletes_projects_and_local_stores_but_keeps_global_publisher_tables`：
  - 删除前：两个项目；`artifact_root/projects/<id>/x.txt`、`artifact_root/audit_bundles/projects/<id>/b.json`、`<llm_kb_root>/<id>/a.md`、`<vault_root>/<id>/n.md`；一行 `PublisherExtensionClient`。
  - 删除后：`projects` 表为空；`artifact_root/projects` 已清空；`audit_bundles` 保留；`llm_kb` 与 vault 下的项目目录被删除；`PublisherExtensionClient` 仍有 1 行；`report.deleted_projects == 2`。
- `test_purge_drops_dimension_suffixed_qdrant_collections`：假客户端的集合列表为 `["chapter_memories", "chapter_memories_384d", "llm_kb_vectors", "skills"]`，配置的集合名为 `chapter_memories` 和 `llm_kb_vectors` → 删除前三个，保留 `skills`。
- `test_purge_reports_external_store_errors`：假 qdrant 客户端的 `delete_collection` 抛异常 → 数据库照常清理，`report.errors` 非空；`main` 返回 2。
- `test_purge_removes_minio_project_objects`：假 minio 客户端列出 `artifacts/projects/a/1.txt`、`artifacts/projects/b/2.txt`、`artifacts/other/3.txt` → 只删除前两个，`report.removed_minio_objects == 2`。
- `test_main_defaults_to_dry_run`：不带 `--yes-delete-all-projects` 调用 `main([])` → 不删除任何东西，返回 0。

- [ ] **Step 5: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_purge_all_projects.py`
Expected: FAIL（`ModuleNotFoundError: scripts.purge_all_projects`）

- [ ] **Step 6: 实现 `scripts/purge_all_projects.py`**

- 结构沿用 `scripts/reembed_memory_index.py`：`InfrastructureConfig.from_env()` → `get_engine` → `require_v5_schema` → `get_session_factory`，在 `try/finally` 里 `engine.dispose()`。
- 执行顺序：
  1. 阻塞检查：任何不在终态集合中的 `GenerationTask` 或 `PublisherUploadJob` → 抛出 `PurgeBlocked`。
  2. 逐个项目调用 `_delete_project`，每个项目单独提交一次。
  3. 删除 `artifact_root/projects/`、`local_project_roots` 下的每个子目录。
  4. 调用 `cover_service.cleanup_orphaned_files()`。
  5. qdrant：删除名称等于配置名、或匹配 `^<配置名>_\d+d$` 的集合。
  6. minio：删除 `{prefix}/projects/` 前缀下的对象。
- 第 3–6 步中的异常要捕获，写入 `errors`，不中断后续步骤。
- 不调用项目生命周期中的审计包导出（备份由 Task 13 的运行手册负责）。

- [ ] **Step 7: 运行两组测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_purge_all_projects.py tests/test_bulk_delete_api.py tests/test_project_operation_guards.py`
Expected: PASS

- [ ] **Step 8: 全量测试并提交**

Run: `.venv/bin/python -m pytest -q` → 全部通过。
Commit: `git add forwin/http/project_support.py scripts/purge_all_projects.py tests/test_bulk_delete_api.py tests/test_purge_all_projects.py && git commit -m "feat(ops): purge all projects and fix project delete ordering"`（加上 Global Constraints 中的身份参数和 trailer）

---

### Task 2: 防过拟合护栏（扫描器、测试与 allowlist）

**Files:**
- Create: `tests/overfit_guard.py`（扫描器库）、`tests/overfit_allowlist.toml`、`tests/test_no_story_content_in_runtime.py`
- Modify: 无（`tests/test_no_story_specific_hardcoding.py` 保留，它的 allowlist 在后续任务中逐步收缩）

**Interfaces:**
- Produces（后续任务都会修改 allowlist）：
  - `@dataclass(frozen=True) class Hit: check: str; path: str; symbol: str; line: int; literal: str`
  - `check` 的取值：`story_id_literal`、`chapter_number_literal`、`premise_sniff`、`cjk_collection`、`cjk_regex`、`cjk_membership`、`cjk_inline_iteration`、`fixture_term`
  - `scan_runtime(repo_root: Path) -> list[Hit]`：扫描 `forwin/**/*.py`，排除 `forwin/migrations/versions/` 和 `__pycache__`。
  - `scan_source(text: str, path: str) -> list[Hit]`：单文件入口，`scan_runtime` 逐文件调用它；不包含 `fixture_term` 检查（该检查需要仓库级的词表）。
  - `load_allowlist(path: Path) -> Allowlist`
  - `Allowlist.unallowed(hits) -> list[Hit]`、`Allowlist.stale_entries(hits) -> list[Entry]`、`Allowlist.over_budget(hits) -> list[tuple[Entry, int]]`
  - `PHASE_ORDER = ("P0", "P1", "P2", "P3", "P4")`
- allowlist 格式（`tests/overfit_allowlist.toml`）：

```toml
[meta]
completed_phase = ""          # Task 12 改为 "P0"；之后每期结束时推进

[story_id_literal]
allowed_values = ["main", "fact_type", "fact_id", "thread_id", "world_line_id", "fact_refs",
  "fact_conflict", "thread_closed_too_early", "thread_keep_open", "thread_already_closed",
  "thread_drift", "thread_time_extraction", "fact_nodes", "gap_between_public_and_actual"]

[canary]
seed_terms = [ ... ]          # 见 Step 3
harvest_keys = ["name", "display_name", "entity_name", "canonical_name"]
ignore = [ ... ]              # 收集到的通用词，例如 "主角"

[[entry]]
path = "forwin/naming/culture_name_generator.py"
symbol = "CULTURES"           # 可省略；省略即对该文件的 checks 生效
checks = ["cjk_collection"]
category = "catalog"          # language_lexicon | genre_lexicon | catalog | prompt_text | numeric_policy | eval_fixture | pending_removal
justification = "按 Genesis 文化 profile 选择的命名语料目录"
max_literals = 620            # 当前命中数向上取整；增长需要复审
# remove_in = "P2"            # category == "pending_removal" 时必填，取值 P0..P4
```

- [ ] **Step 1: 写扫描器的单元测试（基于内存源码片段，不依赖仓库现状）**

在 `tests/test_no_story_content_in_runtime.py` 中写一组 `test_scanner_detects_*`，每个用例用 `scan_source(text: str, path: str) -> list[Hit]`（`scan_runtime` 的内部单文件入口，同样由本任务产出）断言：
- `story_id`：`x = "line_colony_defense"` 命中；`f"fact_pulp_light_ch{n}"` 命中；`d["fact_type"]` 不命中；`foo(world_line_id="line_a")` 命中；`"father_sieged" in intent.must_not_reveal` 命中；`"main"` 不命中。
- `chapter`：`plan.chapter_number == 23` 命中；`chapter_number > 1` 不命中；`foo(chapter_hint=22)` 命中；`target_total_chapters >= 150` 不命中。
- `premise`：`"母星" in all_text` 命中；`"x" in body` 不命中。
- `cjk_collection`：模块级 `WORDS = ("一炷香", "时辰")` 命中 2 个字面量；类体中的字典命中；`set("赵钱孙")` 命中；函数内的日志字符串不命中。
- `cjk_regex`：`re.compile(r"被(关押|羁押)")` 命中。
- `cjk_membership`：`"救父" in text` 命中；`x in ("被捕", "羁押")` 命中。
- `cjk_inline_iteration`：函数内 `for w in ("档案", "后门"):` 命中；`any(w in t for w in ("刻痕",))` 命中。
- `symbol`：模块级常量的 symbol 是常量名；函数内命中的 symbol 是函数的 qualname（如 `Foo.bar`）。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_no_story_content_in_runtime.py -k scanner`
Expected: FAIL（`ModuleNotFoundError: tests.overfit_guard`）

- [ ] **Step 3: 实现 `tests/overfit_guard.py`**

各项检查的精确定义：
- CJK = `[㐀-鿿]`。
- `story_id_literal`：
  - 字符串常量 fullmatch `^(line|gap|world_line|hidden_line|fact|thread|intent|agenda)_[a-z0-9_]+$`，或 f-string 的首个常量片段匹配该前缀加 `_`。
  - 豁免：字典键、下标、`.get/.pop/getattr/setattr/hasattr` 的第一个参数、`allowed_values` 中的值。
  - 另外检查：`*_id=` 关键字传入不在 `allowed_values` 中的非空字面量；`"<字面量>" in <任意表达式>.must_not_reveal`。
- `chapter_number_literal`：`Compare` 的某个操作数是 Name 或 Attribute，名字 fullmatch `chapter(_number|_start|_end|_hint)?|current_chapter|target_chapter|deadline_chapter|latest_chapter`，另一个操作数是 ≥2 的 int 常量；以及 `chapter_hint=<int 常量>`。
- `premise_sniff`：`Compare(left=字符串常量, ops=[In|NotIn])`，右侧的 Name 或 Attribute 名字匹配 `.*(premise|all_text|synopsis|setting|title|genre).*`。
- `cjk_collection`：模块级或类体中的 Assign / AnnAssign，其值（含嵌套）里 List / Tuple / Set / Dict 的含 CJK 字符串常量（键和值都算），以及 `set("…")` / `frozenset("…")`。每个常量算一个 literal。
- `cjk_regex`：`re.compile/search/match/fullmatch/findall/finditer/sub/split` 的第一个参数含 CJK（包括 f-string 的常量片段）。
- `cjk_membership`：`In/NotIn` 比较中，左侧是含 CJK 的常量，或右侧是含 CJK 常量的字面量集合。
- `cjk_inline_iteration`：函数体内，含 CJK 常量的字面量集合作为 `for` 或推导式的迭代对象，或作为 `any()` / `all()` 的参数。
- `fixture_term`：
  - 词表 = `seed_terms` 加上从 `tests/fixtures/**/*.json` 中 `harvest_keys` 对应的值里收集到的 2–6 字 CJK 串，再减去 `ignore`。
  - 任何 `forwin/**/*.py` 的文本中出现这些词即命中（symbol 为 `<file>`）。
- allowlist 匹配规则：`path` 相同、`check` 在 `checks` 中，并且 `symbol` 为空或相等；同一条目的命中数不得超过 `max_literals`。

`seed_terms` 取设计附录 A 两套剧本的特征词，并合入 `tests/test_no_story_specific_hardcoding.py` 中的 `BANNED_CURRENT_BOOK_TERMS` 与 `BANNED_CURRENT_BOOK_MECHANISM_TERMS`：母星、殖民、救父、父亲被围、father_sieged、homeworld、colony、回声账、恢复后门、家族档案、记忆重置、记忆熔铸、档案署、钟塔、民间记忆馆、地下检修线、追踪器、磁力铐、羁押室、馆员、审计员、巡检员、工号在线、刻痕、插孔、memory_reset、archive_cleanup、terminal_audit_window、core_access_window、public_countdown、失业档案修复师、地下算法师、三十万、陆明、韩青。

- [ ] **Step 4: 写针对仓库的护栏测试**

在同一个测试文件中加入：
- `test_runtime_hits_are_all_allowlisted`：`unallowed == []`。失败信息逐行列出 `path:line [check] symbol literal`，并提示“登记到 tests/overfit_allowlist.toml 并写明 category / justification；剧本内容不得登记，只能删除”。
- `test_allowlist_has_no_stale_entries`：`stale_entries == []`。
- `test_allowlist_entries_stay_within_budget`：`over_budget == []`。
- `test_pending_removal_entries_name_a_future_phase`：每个 `pending_removal` 条目都有 `remove_in ∈ PHASE_ORDER`，并且排在 `meta.completed_phase` 之后。
- `test_allowlist_categories_are_known`：`category` 必须在枚举之内。

- [ ] **Step 5: 生成 allowlist 初稿并分类**

运行一次 `scan_runtime`，把命中按 `(path, symbol, check)` 聚合，写出 `tests/overfit_allowlist.toml`。分类决定见下表（未列出的命中按本表原则逐个判断：发给 LLM 的说明文字 → `prompt_text`；题材中立的语言词 → `language_lexicon`；按显式 genre 选择的语料 → `catalog`；具体故事的内容 → 只能登记为 `pending_removal`）：

| 路径（及符号） | category | remove_in |
| --- | --- | --- |
| `planning/world_contract_service.py` | pending_removal | P0（Task 5） |
| `book_state/extraction/{delta_extractor,evidence_rules,cognitive,reveal,world_delta,reader_cognition,gate}.py` | pending_removal | P0（Task 3） |
| `application/projects/common.py`（扩展节拍相关符号）、`planning/contracts.py:is_derived_goal_control_instruction` 中“不得回退成几天”“分钟级倒计时不得回退” | pending_removal | P0（Task 6） |
| `genesis/fallbacks.py`、`genesis/helpers.py:_FALLBACK_PERSON_ROLE_PREFIXES`、`map/genesis_adapter.py` 中的“主舞台总图” | pending_removal | P0（Task 7） |
| `subworld_manager.py:_NAME_SURNAMES/_NAME_GIVEN`、`director/arc_director.py` 的默认世界字符串、`simulation/world.py` 中 `chapter_number >= 3` | pending_removal | P0（Task 8） |
| `canon_quality/placeholder.py:BLOCKED_INTERNAL_STATE_KEYS` 及角色词、`canon_quality/readability.py` 中的剧本称谓、`context/assembler_core/canon_quality_context.py` 中的 memory_reset 分支、`generation/pipeline_core/repair_patches.py:_countdown_repair_rule_anchors` | pending_removal | P0（Task 9a） |
| `checker/pulp_beat.py` 中的剧本标记词与 `inference_words`、`checker/reference_classifier.py` 中的 `_GENRE_ROLE_SUFFIXES/_ORGANIZATION_KEYWORDS/_PARENTHETICAL_CANDIDATE_KEYWORDS`、`canon_names.py` 中的“母亲”模式与剧本名词 | pending_removal | P0（Task 9b） |
| `planning/future_plan_audit/helpers.py` 中的 memory_reset 分支、倒计时别名表、追踪器措辞；`future_plan_audit/patches.py:203` | pending_removal | P0（Task 9c） |
| `llm_eval/cases.py` | pending_removal | P0（Task 11） |
| `map/genesis_adapter.py` 的路线与地形词、`map/generator.py` 的地形关键词、`characters/normalization.py`、`personality/enrichment.py` 的称谓 | pending_removal | P1 |
| `future_plan_audit/helpers.py` 与 `custody.py` 中的羁押标记、`canon_quality_context.py:_RECENT_CANON_CUSTODY_*`、`canon_names.extract_candidate_character_names`、`review/map_movement.py` 的时长表、`book_state/extraction/graph_delta.py` 的正则兜底、`checker/rules.py:DEAD_STATUS_KEYWORDS`、`review/constraint_keywords.py`、`canon_quality/obligation_verifier.py` 的标记词 | pending_removal | P2 |
| `subworld_manager.py:_window_overlaps` 的章节字面量、`simulation/world.py:_KEYWORD_RULES` | pending_removal | P4 |
| `naming/culture_name_generator.py`、`world_templates.py`、`protocol/trope_library.py`、`publisher_runtime/platform_catalogs.py`、`checker/pulp_beat.py:PULP_BEAT_PROFILES/PULP_GENRE_TRACKS` | catalog | — |
| `writer/prompt_core/**`、`genesis/messages.py` 以及其它只用于拼接 LLM prompt 的模块 | prompt_text | — |
| `checker/hard_floor_dict.py`、`canon_names.py` 的姓氏与停用字、readability 的代词、数字解析、`review/publisher_compliance.py`、`review/webnovel.py` 的钩子与爽点标记、`context/assembler_core/canon_quality_context.py:_looks_like_final_chapter_label` | language_lexicon | — |

- [ ] **Step 6: 运行护栏测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 7: 全量测试并提交**

Commit: `git add tests/overfit_guard.py tests/overfit_allowlist.toml tests/test_no_story_content_in_runtime.py && git commit -m "test: guard runtime code against story content"`

---

### Task 3: 删除 v4 抽取路径

**Files:**
- Delete: `forwin/book_state/extraction/{delta_extractor,gate,cognitive,reveal,world_delta,reader_cognition,evidence_rules,types}.py`、`forwin/book_state/adapter.py`、`forwin/protocol/world_v4.py`
- Modify:
  - `forwin/book_state/extraction/graph_delta.py:7,15-16,22-140,160-284`
  - `forwin/book_state/extraction/contract.py:5,8-9,16,35-36`
  - `forwin/book_state/extraction/__init__.py:8-17`（保留模块 docstring，`test_architecture_boundaries.py:46` 会断言它）
  - `forwin/book_state/__init__.py:5,20`
  - `forwin/canon/preparation.py:29,89-92,103`
  - `forwin/protocol/writer.py:12-18,67-74`
  - `forwin/protocol/review.py:19,28-34,43,87-90,163-166`
  - `forwin/review/draft_service.py:914`
  - `forwin/generation/pipeline_core/review_autofix.py:213-235`（删除 `world_model_conflict`、`cognition_conflict`、`unpaid_promise_debt` 和 `target_scope == "world_model"` 这几支）
  - `forwin/book_state/compiler.py:360,408-444`（删除 `_persist_reader_experience_side_effects` 及其调用）
  - `forwin/api_schema/world.py:9-67,300-304`、`forwin/api_schema/__init__.py:55-59,256-260`
- Test：删除 `tests/test_book_state_extraction_{delta,delta_deep,gate,reviewers}.py`、`tests/test_world_v4_protocol.py`、`tests/test_world_v4_writer_protocol.py`；修改下面列出的混合文件。

**Interfaces:**
- Produces：
  - `BookStateGraphDeltaExtractor.extract(request) -> BookStateExtractionResult`：先跑 `WriterContractDeltaBuilder`，`graph_deltas` 为空时再走 pulp_light 兜底；直接构造 `ApprovedGraphDeltaSet(project_id, chapter_number, graph_deltas, approved_by=["book_state_direct_extractor"], review_verdict_id=request.review_verdict_id or f"book_state_direct_extract_{project_id}_{chapter_number}", forced_accept_reason=request.forced_accept_reason)`。
  - `BookStateExtractionRequest` 删除 `chapter_intent`；`BookStateExtractionResult` 删除 `extracted_changes`、`gate_verdict`。
  - `_filter_graph_delta_layers` 及其辅助函数删除（删 v4 之后已无生产调用方）。`self.layers` 保留，用于 `"world" in layers` 判断和构造器校验。
  - `RepairScope` 删除 `"world_model"`；`RepairInstruction` 删除 `required_{delta,belief,hint,payoff}_patch`；`ReviewVerdict` 删除 `approved_delta_refs`、`rejected_delta_refs`、`compiler_gate_status`、`extracted_actuals`。
  - `llm/router.py` 与 `writer/llm/routing.py` 中的 `"world_model"` 是 LLM 调用族，不是修复 scope，**保留**；`review/llm_webnovel.py:408,416` 的证据来源标签也**保留**。

- [ ] **Step 1: 写失败测试**

在 `tests/test_architecture_boundaries.py` 中，把 `test_book_state_extraction_has_one_current_owner`（:1004-1032）改写为：
- `forwin/book_state/extraction/` 下只剩 `__init__.py`、`contract.py`、`graph_delta.py`；
- `forwin/book_state/adapter.py`、`forwin/protocol/world_v4.py` 不存在；
- 生产代码中不出现 `BookStateExtractionGate`、`BookStateExtractionDeltaExtractor`、`BookStateDeltaAdapter`。

新增 `test_writer_output_has_no_v4_fields`：`WriterOutput.model_fields` 与上述 8 个字段名的交集为空。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py -k "extraction_has_one_current_owner or writer_output_has_no_v4_fields"`
Expected: FAIL

- [ ] **Step 3: 按 Files 与 Interfaces 删除并修改生产代码**

- [ ] **Step 4: 修改混合测试文件**

- `tests/test_book_state_final.py`：删除 `test_v4_adapter_create_book_state_rows_without_import`（:143）和 `test_v4_adapter_scopes_canonical_rows_across_projects`（:179），以及 :7、:30 的导入。
- `tests/test_knowledge_system_v46.py`：删除 `test_reader_experience_syncs_into_book_state_native_tables`（:177）和 :18、:40 的 v4 导入；保留 :841、:954 两个 reader promise 用例。
- `tests/test_pulp_pipeline_bypass.py`：删除 `_install_empty_world_delta_path`（:661-687）及其在 :690、:731、:758 的调用，删除 :28、:31 的导入；删除测试 `_filter_graph_delta_layers` 的用例（:462、:525、:553、:624、:637、:805）。
- `tests/test_world_v4_orchestrator_gate.py`：删除 `_setup_project` 中保存 homeworld `ChapterWorldDeltaIntent` 的部分（:72-86）；:155 和 :235 两个用例里的 `WriterOutput` 加上 `time_advance` 与一条 `state_changes`，让 writer contract 产出 delta，断言保持原意；将文件重命名为 `tests/test_book_state_canon_gate.py`。
- `tests/test_repair_preserved_title.py:99`：去掉 `must_preserve_facts=` 参数。
- `tests/test_repair_verification.py:117`：去掉 `extracted_actuals`。
- `Design-docs/CURRENT_ARCHITECTURE.md:102` 与 `Design-docs/DESIGN_STATUS.md:109`：删掉 “deterministic gate” 的说法。保留前缀 “`forwin.book_state.extraction`：BookState candidate extraction”（`tests/test_design_status_docs.py:32` 会断言）。

- [ ] **Step 5: 从 `tests/overfit_allowlist.toml` 删除第 3 步删掉的那些文件对应的条目**

- [ ] **Step 6: 运行相关测试和护栏测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_book_state_final.py tests/test_knowledge_system_v46.py tests/test_pulp_pipeline_bypass.py tests/test_book_state_canon_gate.py tests/test_book_state_writer_contract.py tests/test_no_story_content_in_runtime.py tests/test_design_status_docs.py`
Expected: PASS

- [ ] **Step 7: 全量测试并提交**

Commit message: `refactor(book_state): delete fixture-driven v4 extraction path`

---

### Task 4: 迁移 `0002_drop_dead_tables`

**Files:**
- Create: `forwin/migrations/versions/0002_drop_dead_tables.py`
- Modify:
  - 删除 ORM 类：`forwin/models/phase.py`（`WorldProjectionDeltaRow`）、`forwin/models/canon_quality.py`（`StoryObligationRow`、`RevealRegistryEntryRow`、`ArtifactCollectionLedgerRow`）、`forwin/models/book_state.py`（`BookReaderExperienceDeltaRow`）
  - `forwin/models/__init__.py`（相应导出）
  - `forwin/book_state/repository.py`：删除 `upsert_reader_experience_delta`（:328-358）、`list_reader_experience_deltas`（:378-397），以及 invalidate / rewind 中涉及 experience delta 的代码（:866-883、:950-956、:995-1001、:1625）
  - `forwin/canon_quality/repository.py`：删除仅为这些表服务的方法（:355、:379 附近）
  - `forwin/http/adapters/api_book_state_routes.py:215-220`：reader-promises 接口不再返回 experience deltas
- Test: `tests/test_architecture_boundaries.py:249-321`、`tests/test_architecture_boundaries.py:281`、`tests/test_v5_recovery_schema.py:517-528`、`tests/test_v5_live_migration.py:14`、`tests/test_book_state_repository_projection_compiler.py:13,123,157`

**Interfaces:**
- Produces：revision `"0002_drop_dead_tables"`，`down_revision = "0001_v5_recovery"`。
  - `upgrade()`：先 `op.drop_index` 再 `op.drop_table`，删除 `world_projection_deltas`、`story_obligations`、`reveal_registry_entries`、`artifact_collection_ledgers`、`book_reader_experience_deltas`。
  - `downgrade()`：按 0001 中的定义原样重建（0001 的 :425-455、:526-568、~:2026-2060、~:2093-2128、~:2357-2384）。
  - `book_reader_promises` **保留**：它还有 proposal 写入路径。

- [ ] **Step 1: 写失败测试**

在 `tests/test_architecture_boundaries.py` 新增 `test_dead_tables_are_dropped_by_0002`：
- 5 个表名都不在 `Base.metadata.tables` 中；
- 5 个 ORM 类名都不出现在 `forwin/` 的源码中；
- `0002_drop_dead_tables.py` 的 `upgrade()` 源码里包含 `op.drop_table("<表名>")`。

把以下三处改为接受两个 revision：
- `test_architecture_boundaries.py:281` 断言 `versions == ["0001_v5_baseline.py", "0002_drop_dead_tables.py"]`；
- `test_v5_recovery_schema.py:525` 同上；
- `test_v5_live_migration.py:14` 的 `EXPECTED_REVISION = "0002_drop_dead_tables"`。

`test_architecture_boundaries.py:249-272` 中针对 0001 文本的死表断言保持不变（0001 不可改）。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_v5_recovery_schema.py tests/test_v5_live_migration.py`
Expected: FAIL

- [ ] **Step 3: 写迁移，按 Files 删除 ORM 类、仓储方法与路由字段**

- [ ] **Step 4: 修改 `tests/test_book_state_repository_projection_compiler.py`**

删除 :123 的 `..._retires_reader_experience_by_delta_provenance`，以及 :13、:157 对已删类与方法的引用。

- [ ] **Step 5: 运行，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_architecture_boundaries.py tests/test_v5_recovery_schema.py tests/test_v5_live_migration.py tests/test_book_state_repository_projection_compiler.py tests/test_knowledge_system_v46.py`
Expected: PASS（`test_v5_live_migration` 在空库上完成 upgrade → downgrade → upgrade）

- [ ] **Step 6: 全量测试并提交**

Commit message: `feat(db): drop dead tables in 0002 migration`

---

### Task 5: 去掉世界合同里的样例剧本，只保留通用路径

**Files:**
- Modify: `forwin/planning/world_contract_service.py:44-183`
- Test: `tests/test_world_contract_service.py:11`、`tests/test_planning_world_contracts.py:137`

**Interfaces:**
- Produces：`WorldContractPlanningService.ensure_for_arc_band(...)` 签名不变，输出固定为下面的通用形式，其余字段全部取模型默认的空值：
  - Arc：`ArcWorldContract(contract_id=f"arc_world_contract_{arc.id}", project_id, arc_id, arc_number=arc.arc_number, title=arc.arc_synopsis, primary_world_line_ids=["primary_visible_line"], arc_exit_objective_state=arc.arc_synopsis, arc_exit_reader_state="aware")`
  - Band：`BandWorldContract(contract_id=f"band_world_contract_{arc.id}_{window.chapter_start}_{window.chapter_end}", project_id, arc_id, band_id=window.band_id, chapter_start, chapter_end, foreground_world_line_ids=["primary_visible_line"], band_exit_reader_state="aware", band_exit_hidden_line_state="")`
  - Chapter：对 `window.active_band` 中的每个 plan，`ChapterWorldDeltaIntent(intent_id=f"chapter_{n}_world_intent", project_id, chapter_plan_id=plan.id, chapter_number=n, visible_delta_intents=_load_goals_json(plan.goals_json)[:1])`

- [ ] **Step 1: 改写测试**

把 `tests/test_world_contract_service.py::test_world_contract_service_preserves_arc_band_and_chapter_intent_semantics` 改名为 `test_world_contract_service_emits_generic_contracts_for_any_premise`。
- 用两个 premise 跑同一套断言：一个是含“母星”“殖民”的 premise，另一个是普通 premise。
- 两者输出完全一致，都符合 Interfaces 所列的形式；`hidden_world_line_ids == []`，`must_not_reveal == []`。

删除 `tests/test_planning_world_contracts.py::test_phase24_persists_homeworld_crisis_contracts_for_arc_plan`。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_world_contract_service.py`
Expected: FAIL（含“母星”的 premise 仍会产出 `line_homeworld_siege`）

- [ ] **Step 3: 删除 premise 嗅探（:44-54）和所有 homeworld 分支**

- [ ] **Step 4: 从 allowlist 删除 `world_contract_service.py` 的条目，运行测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_world_contract_service.py tests/test_planning_world_contracts.py tests/test_band_plan_service.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(planning): remove homeworld fixture from world contracts`

---

### Task 6: 续写只追加一卷，由既有的卷激活流程规划章节

**Files:**
- Modify:
  - `forwin/application/projects/common.py:215-331`：删除 `_EXTENSION_BEATS`、`_extension_continuity_guard` 的默认文本、`_extension_chapter_blueprint`
  - `forwin/application/projects/generation.py:214-326`
  - `forwin/genesis/handoff/chapter_materializer.py:24-60`
  - `forwin/planning/contracts.py:78-99`
- Test: `tests/test_project_operation_guards.py:194,279,351`、`tests/test_mcp_server.py:775`、`tests/test_genesis_arc_materialization.py`（新建，或放进现有的 materializer 测试文件）

**Interfaces:**
- Produces：
  - `extend_project_generation(...)`：签名和返回值不变。
    - 不再创建任何 `ChapterPlan`。
    - 只创建一条 `ArcPlanVersion(status="planned")`：
      - `arc_number = max(arc_number) + 1`
      - `chapter_start = max(所有 ArcPlanVersion.chapter_end, 所有 ChapterPlan.chapter_number) + 1`
      - `chapter_end = chapter_start + additional_chapters - 1`
      - `planned_target_size = additional_chapters`；`planned_soft_min` / `planned_soft_max` 沿用现在的 ×0.85 / ×1.20
      - `arc_synopsis = _extension_arc_synopsis(req, start, end)`：`arc_title` 或 `"续写弧线：第{s}-{e}章"`，加上 `arc_synopsis`，再加上用户显式给出的 `continuity_guard`。没有给出时不加任何默认文本。
    - `project.target_total_chapters` 照常增加。
  - `materialize_arc_chapter_plans(...)`：Genesis blueprint 中没有该 `arc_number` 时，不再抛出 `ValueError`，而是由 `ArcPlanVersion` 合成 `arc_payload`：`{"arc_number", "title": "", "arc_synopsis", "goal": "", "stakes": "", "payoff_direction": "", "chapter_start", "chapter_end", "chapter_count", "target_size"}`。之后照常调用 `_plan_arc_chapters`。
  - `is_derived_goal_control_instruction`：删除 “不得回退成几天”“分钟级倒计时不得回退” 两个标记，其它标记保留。

- [ ] **Step 1: 写失败测试**

- `test_extend_generation_appends_one_planned_arc_without_chapter_plans`（替换 :194 的用例）：已有 24 章并全部 accepted，`additional_chapters=12` → 新增 `arc_number=2`、范围 25–36、状态 planned；不新增任何 `ChapterPlan`；`target_total_chapters == 36`；`continue_workset` 的 `reason == "future_arc_materialization_required"`。
- `test_extend_generation_starts_after_last_genesis_arc`：Genesis 卷 1 为 1–10（active，有计划），卷 2 为 11–20（planned，无计划）→ extend 5 章，得到卷 3，范围 21–25。
- `test_extend_generation_has_no_default_story_text`：不传 `continuity_guard` 时，新卷的 `arc_synopsis` 只包含 `"续写弧线：第"` 和章节范围；extend 不产生任何 `ChapterPlan` 行。
- `test_materializer_synthesizes_payload_for_arc_missing_from_blueprint`：blueprint 只有卷 1；卷 2 由 extend 产生。使用桩 LLM 调用 `materialize_arc_chapter_plans` → 生成卷 2 范围内的章节计划，且桩 LLM 收到的 arc payload 中带有该卷的 `arc_synopsis`。
- 修改 :279 的用例（已有 planned 工作集时追加）：断言改为新增一卷，`ChapterPlan` 不增加。
- 修改 `tests/test_mcp_server.py::test_extend_generation_via_mcp_appends_future_plans`：断言改为新增一卷、计划数不变。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_project_operation_guards.py -k extend tests/test_mcp_server.py -k extend tests/test_genesis_arc_materialization.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改生产代码**

- [ ] **Step 4: 删除对应的 allowlist 条目；把 `forwin/application/projects/common.py` 从 `tests/test_no_story_specific_hardcoding.py` 的 `ALLOWED_PRODUCTION_MECHANISM_FILES` 中移除；运行测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_project_operation_guards.py tests/test_mcp_server.py tests/test_genesis_arc_materialization.py tests/test_project_schema_long_run.py tests/test_no_story_content_in_runtime.py tests/test_no_story_specific_hardcoding.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(generation): extend by appending an arc planned by arc activation`

---

### Task 7: Genesis fallback 只产出结构空壳，并标记 degraded

**Files:**
- Modify:
  - `forwin/genesis/fallbacks.py:40-637`
  - `forwin/genesis/helpers.py:311-362`
  - `forwin/genesis/workspace/service.py:148-188,217-448`
  - `forwin/api_schema/genesis.py:9-14`
  - `forwin/map/genesis_adapter.py:95-111`
- Test: `tests/test_book_genesis_flow.py:105,154,1112,1228`、`tests/test_no_story_specific_hardcoding.py:164`

**Interfaces:**
- Produces：
  - 删除 `_FALLBACK_SAMPLE_TERMS`、`_reject_sample_terms`、`_fallback_generated_names`、`_fallback_named_entity_seed`、`_fallback_location_kind`，以及 `helpers._FALLBACK_PERSON_ROLE_PREFIXES`。
  - `_fallback_culture_profiles(project, pack) -> []`
  - `_fallback_world_bible(project, pack)` → `{"overview": project.setting_summary or "", "axioms": [], "history_slice": "", "naming_style": "", "forbidden_zones": [], "culture_profiles": []}`
  - `_fallback_map(pack)` → 一个 submap、一个 region、一个 node，id 分别为 `subworld-1`、`region-1`、`node-1`；名称和描述为空（由 normalizer 填占位）；`edges = []`。
  - `_fallback_story_engine(pack)` → `{"core_cast": [], "factions": [], "opposition": [], "relationship_axes": [], "reader_promises": [], "long_arcs": []}`
  - `_fallback_blueprint(project, pack)`：保留 `allocate_arc_chapter_sizes` 的分卷尺寸；`title`、`arc_synopsis`、`goal`、`stakes`、`payoff_direction` 都为空串。`_story_focus_from_blueprint_fallback` 对空串返回 `""`。
  - `pack["stage_states"][stage]["degraded"]`：
    - 生成或修订走 fallback（trace 中 `output_summary.mode == "fallback"`）时置为 `True`；
    - 后续一次非 fallback 的生成或修订，或者 `patch_pack` 修改了该阶段，置为 `False`。
    - 加锁不受影响。
  - `BookGenesisStageState` 增加 `degraded: bool = False`。
  - `map/genesis_adapter.py` 中注入的默认 submap 名称从“主舞台总图”改为“未命名小世界”（与 `normalize.py:249` 的占位一致）。

- [ ] **Step 1: 写失败测试**

- `test_fallback_stages_are_structural_shells`：以 `test_no_story_specific_hardcoding.py:164` 中的项目为输入，分别调用 `_fallback_world`、`_fallback_map`、`_fallback_story_engine`、`_fallback_blueprint`：
  - 序列化结果中不含 premise 中的任何人名或地名（“宁望舒”“星门”）；
  - 不含 seed 黑名单中的词；
  - story_engine 各列表都为空；
  - blueprint 的 arcs 数量与尺寸等于 `allocate_arc_chapter_sizes` 的结果，且 synopsis 为空。

  这个用例替换 `test_no_story_specific_hardcoding.py:164` 的旧用例（旧用例断言保留了“宁望舒”，与新语义相反）。
- `test_fallback_generation_marks_stage_degraded_and_successful_refine_clears_it`：桩 LLM 先返回非法 JSON（触发 fallback）→ `stage_states["world"]["degraded"] is True`；之后用桩 LLM 返回合法 JSON 做 refine → 变为 `False`。
- 删除 `tests/test_book_genesis_flow.py` 中 :105 和 :1228 两个断言编造内容的用例。改写 :154：blueprint fallback 的 synopsis 为空，分卷尺寸保留。改写 :1112：fallback 的 id 链接（`subworld-1` / `region-1` / `node-1`）存在，名称为 normalizer 的占位。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_book_genesis_flow.py tests/test_no_story_specific_hardcoding.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改生产代码**

- [ ] **Step 4: 删除对应的 allowlist 条目，运行测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_book_genesis_flow.py tests/test_no_story_specific_hardcoding.py tests/test_no_story_content_in_runtime.py tests/test_map_world_integration.py tests/test_map_generation.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(genesis): fallback yields structural shells marked degraded`

---

### Task 8: 删除 prompt 示例、默认世界、名字池与章节阈值

**Files:**
- Modify:
  - `forwin/writer/prompt_core/sections.py:74-82`
  - `forwin/writer/prompt_core/constraints.py:255`
  - `forwin/director/arc_director.py:74-77,191-201`
  - `forwin/simulation/world.py:738-740`（`chapter_number >= 3` 那一段）
  - `forwin/subworld_manager.py:35-40,686-700,918-923`
- Test: `tests/test_writer_prompt_contract.py:256`、`tests/test_prompt_revision.py:22-31`、`tests/fixtures/prompt_regression/snapshots/*.prompt.txt`、`tests/test_subworld_control.py:1381,1445,1533`

**Interfaces:**
- Produces：
  - `sections.py`：命名规则那一句删掉 “例如“系统巡检员”” 这个例子；倒计时约束改为 `"倒计时约束：不同倒计时各自独立，不得混用、合并或互相替换。"`，不带任何例子。
  - `constraints.py:255` 改为 `"可以写TA仍受伤势、行动范围等限制。"`
  - `arc_director.py`：默认的 `initial_time` 为 `{"label": "", "description": ""}`；`_plan_core` 的 fallback 中 synopsis、setting、initial_time 都为空串。
  - `simulation/world.py`：删除 `chapter_number >= 3` 时追加 shift 并升级压力的规则。
  - `subworld_manager.py`：删除 `_NAME_SURNAMES`、`_NAME_GIVEN`、`fallback_slot_name`。`plan_band_activation` 遇到 `display_name` 为空的 roster item 时直接跳过：不激活，不生成 entry target，保持 `planned_slot` 状态。

- [ ] **Step 1: 写失败测试**

- 修改 `tests/test_writer_prompt_contract.py:256`：断言新句子出现，且“追踪器”“系统权限”都不出现。
- 新增 `test_story_basics_section_has_no_example_roles_or_countdowns`：渲染出的 story basics 段落不含“系统巡检员”“剩余4小时”“剩余7天”。
- 新增 `test_band_activation_skips_unnamed_planned_slots`：一个有名字的 slot 和一个空名字的 slot → 只为有名字的生成 entry target；空名字的 slot 状态仍为 `planned_slot`。同时更新 `tests/test_subworld_control.py` 中 :1381、:1445、:1533 依赖编造名字的断言。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_writer_prompt_contract.py tests/test_subworld_control.py -k "slot or story_basics or character_state"`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改生产代码**

- [ ] **Step 4: 重新生成 prompt 快照**

用一段一次性脚本：从 `tests.test_prompt_revision` 导入 `_build_messages`、`_snapshot_text`、`prompt_revision_hash` 和 `FIXTURE_PATH`，为每个 case 重写 `tests/fixtures/prompt_regression/snapshots/<case_id>.prompt.txt`，并打印新的哈希，填回 `EXPECTED_PROMPT_REVISIONS`。用 `git diff tests/fixtures/prompt_regression` 确认改动只涉及 Step 3 删改的句子。

- [ ] **Step 5: 删除对应的 allowlist 条目，运行测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_writer_prompt_contract.py tests/test_prompt_revision.py tests/test_subworld_control.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 6: 全量测试并提交**

Commit message: `refactor: drop story examples, default world and name pools from runtime`

---

### Task 9a: 清理 canon_quality 与修复补丁中的第二套剧本

**Files:**
- Modify:
  - `forwin/context/assembler_core/canon_quality_context.py:86-93`
  - `forwin/generation/pipeline_core/repair_patches.py:120-166,339-340`
  - `forwin/canon_quality/placeholder.py:9-16,186,196-198`
  - `forwin/canon_quality/readability.py:10-42`
- Test: `tests/test_future_plan_audit_persistence.py:411`、`tests/test_placeholder_leakage_gate.py:39,107,122,140`、`tests/test_readability_gate.py:29`

**Interfaces:**
- Produces：
  - `canon_quality_context`：删除 memory_reset 覆盖 main 倒计时的分支。每个 key 保留最新一条，剩余时间 ≥ 0 的照常传出。
  - `repair_patches`：删除 `_countdown_repair_rule_anchors` 及其在 :339-340 的调用（它的输入已经没有任何产生方）。
  - `placeholder.py`：`BLOCKED_INTERNAL_STATE_KEYS = {"countdown_key"}`；新增 `INTERNAL_IDENTIFIER_IN_PROSE = re.compile(r"(?<![A-Za-z0-9_])[a-z][a-z0-9]*(?:_[a-z0-9]+)+(?![A-Za-z0-9_])")`，在正文含 CJK 的句子中命中即视为内部标识泄漏（问题类型沿用 `internal_state_key:<命中>`）。角色名正则改为 `(队长|操作员|守卫)`；修复提示里删除“旧书摊主”“地下管理员”这两个例子。
  - `readability.py`：`COMMON_APPELLATIONS` 删除 馆员、审计员、调度员、接线员、巡检员、摊主；`PROTAGONIST_GENERIC_REFS` 删除 馆员、审计员。

- [ ] **Step 1: 改写测试**

- 删除 `test_canon_quality_context_drops_stale_generic_main_when_memory_reset_is_active`，新增 `test_canon_quality_context_keeps_latest_entry_per_countdown_key`：输入两个 key，每个 key 各有新旧两条 → 每个 key 只输出最新一条。
- `test_internal_countdown_state_key_leakage_blocks_canon` 的断言改为：正文中出现 `archive_window_close` → 命中 `internal_state_key:archive_window_close`；正文中出现 `countdown_key` → 同样阻断。
- `tests/test_readability_gate.py:29`：调用时显式传入 `appellations={"馆员"}`，断言不变。
- 检查 `tests/test_placeholder_leakage_gate.py` 中 :107、:122、:140 三个用例：不依赖“巡检员”的，保持通过；依赖的，把输入换成“操作员”。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_future_plan_audit_persistence.py tests/test_placeholder_leakage_gate.py tests/test_readability_gate.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改生产代码**

- [ ] **Step 4: 删除对应的 allowlist 条目；把 `repair_patches.py` 从 `ALLOWED_PRODUCTION_MECHANISM_FILES` 中移除；运行测试，确认通过**

Run: 同 Step 2，再加上 `tests/test_no_story_content_in_runtime.py tests/test_no_story_specific_hardcoding.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(canon_quality): remove leaked countdown and role vocabulary`

---

### Task 9b: 清理 checker 与名字锚点中的剧本词

**Files:**
- Modify:
  - `forwin/checker/pulp_beat.py:52,111-113,154-184,264-307,976-991`
  - `forwin/checker/hard_floor.py:81-112`
  - `forwin/checker/reference_classifier.py:106-147`
  - `forwin/canon_names.py:11-94,157-212,284-325`
  - `forwin/checker/rules.py:118-186`
  - `forwin/writer/prompt_core/sections.py:463-473`
- Test: `tests/test_pulp_beat_verifier.py:18,51,67,77,174,184,194`、`tests/test_hard_floor.py:134,173,199,224`、`tests/test_reference_classifier_scope.py:31`、`tests/test_subworld_control.py:608-1076`

**Interfaces:**
- Produces：
  - `pulp_beat.py`：
    - 删除剧本标记词：三十万、赔偿金、权限变更：开放、工号在线、刻痕指向、编号对应、`MYSTERY_PAYOFF_EVIDENCE_WORDS` 中的 工号 / 刻痕 / 回执 / 插孔 / 时间戳、封存操作、复核操作、查验操作、处置权、身份标签、角色标签、承运者、合作方。
    - 删除 `_infer_track` 和各 profile 中的 `inference_words`。
    - `_profile_for(track)`：没有匹配的 track 时返回中性的空 profile（各类证据词集合为空），兑现判断只依赖计划中的奖励标签和结构化证据。
  - `hard_floor.py:112`：来源标签从 `"body_inferred"` 改为 `"none"`。
  - `reference_classifier.py`：删除 `_GENRE_ROLE_SUFFIXES`、`_ORGANIZATION_KEYWORDS`、`_PARENTHETICAL_CANDIDATE_KEYWORDS`，以及由它们产出的 `genre_candidate` 特征；保留形态类正则（技术 id、复合身份、状态标签）。
  - `canon_names.py`：
    - 删除“母亲”锚点抽取模式与 `_mother_name_observation_patterns`。
    - `_NON_NAME_WORDS` 和 `_NON_PERSON_NAME_KEYWORDS` 只保留功能词（代词、连词等），删除故事名词。
    - `extract_canon_name_anchors(text) -> list[CanonNameAnchor]` 删除；替换为 `find_canon_name_violations(anchors: list[CanonNameAnchor], text: str)`，对所有 role 一视同仁。
    - 调用方（`checker/rules.py`、`writer/prompt_core/sections.py:_canon_name_anchor_section`）暂时传入空锚点列表，P1 改为从 BookState 亲属关系边生成锚点。
    - `extract_candidate_character_names` 中的羁押模式保留到 P2（allowlist 中已登记）。

- [ ] **Step 1: 写失败测试并改写旧测试**

- 新增 `test_unmatched_genre_uses_neutral_pulp_profile`：`genre="星际"`、正文里塞满旧都市题材的词 → `track` 为 `None` 或 `"none"`，证据词命中为空，不抛异常。
- `test_verify_pulp_beats_detects_core_payoff`（:18）：显式传 `track="urban"`。
- 依赖剧本标记词的用例（`tests/test_pulp_beat_verifier.py` 的 :51、:67、:77、:174、:184、:194 和 `tests/test_hard_floor.py` 的 :134、:173、:199、:224）：改用计划奖励标签或 `delivered_payoffs` 的结构化证据来表达同一断言；无法表达的删除。
- `tests/test_reference_classifier_scope.py:31`：“基金会代理人”改为不再产出 `genre_candidate`。
- 新增 `test_canon_name_violations_are_role_agnostic`：锚点为 `CanonNameAnchor(role_label="师父", canonical_name="甲乙")`，正文写成“甲乙丙” → 检出漂移。
- `tests/test_subworld_control.py:608-1076` 中的 mother-anchor 与 `canon_name_observation` 用例：删除依赖文本模式抽取的那些；保留并改写为传入显式锚点的漂移与自动修复用例（`test_canon_name_drift_autofix_*`）。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_pulp_beat_verifier.py tests/test_hard_floor.py tests/test_reference_classifier_scope.py tests/test_subworld_control.py -k "pulp or hard_floor or reference or canon_name"`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改生产代码**

- [ ] **Step 4: 删除对应的 allowlist 条目，运行测试，确认通过**

Run: 同 Step 2（去掉 `-k`），再加上 `tests/test_no_story_content_in_runtime.py tests/test_prompt_revision.py`
Expected: PASS（如果 prompt 快照因锚点段落变化而改变，按 Task 8 Step 4 的方法重新生成）

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(checker): select pulp profile by explicit genre and drop story markers`

---

### Task 9c: 去掉未来计划审计中的剧本词

**Files:**
- Modify:
  - `forwin/planning/future_plan_audit/helpers.py:227-260,342-388`
  - `forwin/planning/future_plan_audit/patches.py:203`
- Test: `tests/test_future_plan_auditor.py:36,621`、`tests/test_plan_patch_validator.py:87,139`、`tests/test_canon_quality_repository.py:197,246`

**Interfaces:**
- Produces：
  - 删除 `memory_reset` 分支（:346-351）。
  - 倒计时别名表（:357-388）改为每个 key 只有 `[key, label]`，删除固定的 6 个 key 与 label 列表。
  - `_hard_custody_instruction`、`_rewrite_stale_custody_text`（:227-260）与 `patches.py:203` 中的 `"已脱困但仍受追踪器或系统权限限制"` 统一改为 `"已脱困"`。
  - 羁押标记词表（:16-55）保留到 P2（allowlist 中已登记）。

- [ ] **Step 1: 改写测试**

- `tests/test_future_plan_auditor.py:36`：输入改为 ledger 中的 key 与 label 本身，不再依赖“记忆重置”这个别名。
- `:621`：断言改为 `"已脱困"`，且不含“追踪器”。
- 检查 `tests/test_plan_patch_validator.py`（:87、:139）与 `tests/test_canon_quality_repository.py`（:197、:246）中对上述文本的断言，改为新文本。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_future_plan_auditor.py tests/test_plan_patch_validator.py tests/test_canon_quality_repository.py`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改生产代码**

- [ ] **Step 4: 删除对应的 allowlist 条目，运行测试，确认通过**

Run: 同 Step 2，再加上 `tests/test_no_story_content_in_runtime.py tests/test_writer_prompt_contract.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(planning): drop leaked countdown aliases and custody wording`

---

### Task 10: 创建项目时 genre 必填

**Files:**
- Modify:
  - `forwin/models/project.py:18`
  - `forwin/api_schema/project.py:224`
  - `forwin/application/projects/lifecycle.py:116`
  - `forwin/api_pages_home.py:29`
  - `forwin/ui_assets/home/body.html:244`
  - `forwin/ui_assets/home/app_library.js:274,316,330-337`
  - `forwin/cli.py:276`
  - `forwin/mcp/client.py:196`
  - `forwin/mcp/http.py:172`
  - `forwin/generation/pipeline_core/run_control.py:70`
- Test: `tests/test_project_schema_long_run.py:12,19`、`tests/test_api_pages_rendering.py`、`tests/test_mcp_server.py`

**Interfaces:**
- Produces：
  - `ProjectCreateRequest.genre: str = Field(min_length=1)`，无默认值；POST `/api/projects` 缺少 genre 返回 422。
  - `lifecycle` 删除 `or "玄幻"`。
  - ORM 列去掉 `default`。
  - CLI 参数 `--genre` 设为 `required=True`。
  - MCP `project_create` 的 genre 无默认值，并调整参数顺序，使必填参数在前。
  - `RunControlStage.run` 的 genre 参数无默认值。
  - UI 输入框不预填，改用 placeholder，提交前校验必填（与 title、premise 的校验并列）；`render_home_page()` 去掉 `default_genre` 参数。

- [ ] **Step 1: 写失败测试**

- `test_project_create_requires_genre`：`ProjectCreateRequest(title=..., premise=..., target_total_chapters=...)` 抛出 `ValidationError`。
- `test_home_page_does_not_prefill_genre`：`render_home_page()` 的 HTML 中 `book_form_genre` 的 value 为空，且页面中不出现“玄幻”作为默认值。
- `test_mcp_project_create_schema_requires_genre`：MCP 工具 schema 的 required 列表包含 genre。
- 更新 `tests/test_project_schema_long_run.py:12,19`：显式传入 genre。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_project_schema_long_run.py tests/test_api_pages_rendering.py tests/test_mcp_server.py -k "genre or long_run"`
Expected: FAIL

- [ ] **Step 3: 按 Interfaces 修改生产代码**

- [ ] **Step 4: 运行测试，确认通过**

Run: `.venv/bin/python -m pytest -q tests/test_project_schema_long_run.py tests/test_api_pages_rendering.py tests/test_mcp_server.py tests/test_api_split_modules.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `feat(api): require genre when creating a project`

---

### Task 11: 把 llm_eval 的样例剧本移出运行包

**Files:**
- Create: `tests/fixtures/llm_eval/sample_contexts.json`、`tests/fixtures/llm_eval/cases.json`、`tests/llm_eval_fixtures.py`
- Modify: `forwin/llm_eval/cases.py`、`forwin/llm_eval/cli.py:10-40`、`forwin/llm_eval/runner.py:464-530`、`forwin/cli.py:140-147,289-300`
- Test: `tests/test_historical_rewrite_anchors.py:6`、`tests/test_obligation_context_injection.py:4`、`tests/test_llm_eval_core.py`、`tests/test_llm_eval_runner.py`、`tests/test_runtime_container_roles.py:318`

**Interfaces:**
- Produces：
  - `forwin/llm_eval/cases.py` 只保留不含故事内容的加载与构建逻辑：
    - `load_sample_contexts(path: Path) -> list[ChapterContextPack]`
    - `load_eval_cases(path: Path, *, contexts: list[ChapterContextPack], suite: str) -> list[EvalCase]`
    - 原来 `default_eval_cases` 中的 prompt 构建逻辑保留，但文本素材全部来自 JSON。
  - `forwin llm-eval run` 新增必填参数 `--context-file`、`--cases-file`；可选参数 `--mini-premise`、`--mini-genre`，两者都提供时才执行 mini real run，否则记录 `skipped_mini_real`（复用现有的 `_record_skipped_mini_real_for_profile`）。
  - `tests/llm_eval_fixtures.py::sample_context(*, variant: int = 0) -> ChapterContextPack`：读取 `tests/fixtures/llm_eval/sample_contexts.json`。
  - 原 `cases.py` 中的人名、地名、premise 与 prompt 文本原样迁入这两个 JSON 文件。

- [ ] **Step 1: 写失败测试**

- `test_llm_eval_package_has_no_story_fixtures`：`forwin/llm_eval/` 下所有 `.py` 文件不含“林夜”“周澜”“雾港”“罗盘”“潮雾”。
- `test_llm_eval_cli_requires_context_and_cases_files`：缺少 `--context-file` 时，argparse 报错退出（`SystemExit`，code 2）。
- `test_mini_real_run_is_skipped_without_premise`：未提供 `--mini-premise` 时，结果中记录 `skipped_mini_real`，并且不构建 pipeline。
- 把 `tests/test_historical_rewrite_anchors.py:6` 与 `tests/test_obligation_context_injection.py:4` 的导入改为 `from tests.llm_eval_fixtures import sample_context`。

- [ ] **Step 2: 运行，确认失败**

Run: `.venv/bin/python -m pytest -q tests/test_llm_eval_core.py tests/test_llm_eval_runner.py tests/test_historical_rewrite_anchors.py tests/test_obligation_context_injection.py`
Expected: FAIL

- [ ] **Step 3: 迁移数据，按 Interfaces 修改生产代码**

- [ ] **Step 4: 删除对应的 allowlist 条目，运行测试，确认通过**

Run: 同 Step 2，再加上 `tests/test_runtime_container_roles.py tests/test_no_story_content_in_runtime.py`
Expected: PASS

- [ ] **Step 5: 全量测试并提交**

Commit message: `refactor(llm_eval): load eval fixtures from files instead of the runtime package`

---

### Task 12: P0 收尾：推进阶段标记、收缩旧护栏、更新文档

**Files:**
- Modify:
  - `tests/overfit_allowlist.toml`（`meta.completed_phase = "P0"`）
  - `tests/test_no_story_specific_hardcoding.py:68-75`
  - `Design-docs/CURRENT_ARCHITECTURE.md`、`Design-docs/CURRENT_DESIGN.md`、`Design-docs/DESIGN_STATUS.md`（写明 world contracts 的样例已删除、v4 抽取已删除、`0002` 迁移、续写改为追加卷、genre 必填）
- Test: `tests/test_no_story_content_in_runtime.py`、`tests/test_no_story_specific_hardcoding.py`、`tests/test_design_status_docs.py`

- [ ] **Step 1: 设置 `completed_phase = "P0"`，运行护栏测试**

Run: `.venv/bin/python -m pytest -q tests/test_no_story_content_in_runtime.py`
Expected: PASS。若 `test_pending_removal_entries_name_a_future_phase` 失败，说明还有 `remove_in = "P0"` 的条目未清理，回到对应任务处理。

- [ ] **Step 2: 收缩 `ALLOWED_PRODUCTION_MECHANISM_FILES`**

只保留确实仍含机制词的文件（预期只剩 P2 才删的 `planning/future_plan_audit/helpers.py`，以及经核实仍需保留的条目），运行 `tests/test_no_story_specific_hardcoding.py`，确认通过。

- [ ] **Step 3: 更新三份设计文档，运行文档测试**

Run: `.venv/bin/python -m pytest -q tests/test_design_status_docs.py tests/test_architecture_boundaries.py`
Expected: PASS

- [ ] **Step 4: 全量测试并提交**

Run: `.venv/bin/python -m pytest -q` → 全部通过，记录通过数。
Commit message: `docs: close world engine overhaul P0`

---

### Task 13: 运行手册：停写、备份、合入、部署、迁移、清库（每一步都需用户确认）

这一任务不写代码，由主会话逐步执行。每一步执行前先向用户说明并取得确认。

- [ ] **Step 1: 暂停线上生成**

用 forwin MCP 的 `task_list` 找出所有非终态的生成任务，逐个 `task_pause`；MCP 不可用时，改用 `POST /api/tasks/{task_id}/pause`。确认 `task_active_generation_check` 对每个项目都返回无活跃任务。关闭每个项目的每日自动调度。

- [ ] **Step 2: 备份**

在生产环境的应用容器中执行 `scripts/backup_forwin_data.py`，得到 `pg_dump` 加本地 artifacts 的备份；按该脚本的提示单独备份 MinIO 与 Qdrant。参照 magi1 上 `~/forwin-db-backups/` 中 2026-10-19 的记录，确认生产库的位置和容器名。

- [ ] **Step 3: 合入 master 并推送**

在 `master` 上执行 `git merge --no-ff claude/world-engine-overhaul`（带身份参数），然后 `.venv/bin/python -m pytest -q` 全部通过，再 `git push origin master`。

- [ ] **Step 4: 部署**

Run: `ssh infra-150 '/home/taiwei/deploy-github-sync/bin/deploy-github-sync.sh --apply --project forwin'`

- [ ] **Step 5: 执行迁移**

在应用容器中运行 `alembic upgrade head`（执行方式以 2026-10-19 的记录为准）。确认 `alembic_version` 为 `0002_drop_dead_tables`，应用正常启动（`require_v5_schema` 通过）。

- [ ] **Step 6: 清库**

在应用容器中先执行 `python scripts/purge_all_projects.py`（dry-run），把报告给用户核对；用户确认后，执行 `python scripts/purge_all_projects.py --yes-delete-all-projects`。退出码必须为 0；为 2 时，逐条处理 `errors` 中列出的外部存储。

- [ ] **Step 7: 冒烟测试**

- 项目列表为空；
- publisher 扩展客户端仍在列表中；
- 新建一个测试项目时，不填 genre 返回 422；
- MCP 的 `project_list` 正常。
