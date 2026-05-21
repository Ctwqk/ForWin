# Legacy Removal Spec — Drop Old-Project Compatibility

## Goal

把所有 legacy 兼容层从 codebase 删干净。前提:**放弃旧项目向后兼容**——不再支持没有 `location_id`、没有 canonical character_id、用 `legacy_relaxed` governance、`creation_status="legacy"` 的项目运行。

完成后:
- `LEGACY_COMPATIBILITY_REGISTRY` 应减到 0 个 `must_migrate_if_used` 条目
- 下次 60 章 pilot 的 `legacy_compat.total_events` 应该 ≈ 0(只剩可能的 instrumentation 假阳性)
- 删 ~2000-3000 行历史包袱代码

## Audit baseline(2026-05-21 pilot run on `09e38c798dc44286869705478c1c735e`)

```
engine_live_chapters: 60/60        ✅ engine 真的 driving
severe_mismatch_chapters: []       ✅ 0 mismatch
legacy_safety_net_chapters: []     ✅ legacy safety net 没被触发
total_legacy_compat_events: 3647   ← 这次要消灭
```

按 feature 拆:

| feature | events | 类别 | 处理 |
|---|---|---|---|
| `subworld.legacy_entity_id_bridge` | 3543 | A. 新项目也在写 | Phase 2 |
| `projection.legacy_world_model_projection` | 60 | B. flag 控制 | Phase 3 |
| `characters.create_legacy_entity_default_true` | 26 | A. 同上 | Phase 2 |
| `subworld.create_legacy_entity` | 18 | A. 同上 | Phase 2 |
| `dead_code.*` × 4 | 0 / 0 callers | 死代码 | Phase 1 |
| `governance.legacy_relaxed_fallback` | 0 / 1 caller / 0 projects | C. 老项目残留 | Phase 1 |
| `book_state.state.location_fallback` | 0 / 1 caller | C. 同上 | Phase 4 |
| `book_state.state.location_patch_warning` | 0 / 1 caller | C. 同上 | Phase 4 |
| `migration.legacy_book_state_import` | 0 / 1 caller | 不在乎旧项目 → 不再需要 | Phase 4 |
| `api.legacy_checkpoint_status` | instrumentation 缺失 | 待修 | Phase 5 |
| `project.creation_status_legacy` | instrumentation 错位 | 待修 | Phase 5 |

## Scope

### 包含

- 完整删除 legacy_entity_id 系统(writer + reader + ORM + DB column)
- 完整删除 `world_v4_compat` 模块
- 完整删除 `state.location` fallback 链
- 完整删除 `legacy_relaxed` governance mode
- 完整删除 `creation_status="legacy"` 默认值
- 完整删除 `LegacyBookStateImporter` 及其 API
- 完整删除 4 项纯死代码
- Audit registry 自身的清理
- DB migration 把上述删除字段从 schema 移除

### 不包含

- 引擎 cutover 安全网(`ReviewOutcomeRouter` / `RepairPolicy.decide` / `ObligationScopeRouter` / `_prepare_deferred_acceptance_if_needed`)——按 `review-engine-cutover-spec.md` Promotion C,等 Phase 1 cutover ≥30 天稳定后单独删
- 老项目数据迁移脚本——明确不做(放弃旧项目)
- UI 资产里 legacy 项目筛选(`app_library.js:720` 那种)——保留,不影响 backend

## Phase 1 — 立刻删的纯死代码 / 零 caller(单 PR)

### Files to delete entirely

- `forwin/orchestrator/repair_coordinator.py`(0 callers,~395 行)
- `forwin/book_state/legacy_import.py`(0 callers,`LegacyBookStateImporter` 没人 instantiate)

### Files to partially clean

- `forwin/reviewer/repair_loop_detector.py`
  - 删 `class RepairLoopDetector` 和它的 `detect()`
  - **保留**:`class RepairAttemptRecord`、`attempt_record_from_history_item()`、`__all__` 对应条目(`canon_quality/chapter_review_form/operator_report.py:7` 还在用)
- `forwin/reviewer/repair_scope_router.py`
  - 删 `def route_review_repair_scopes()`
  - **保留**:`RepairScopeKind`、`RoutedSignal`、`route_signal_kind`(`canon_quality/gate.py:58-65`、`operator_report.py:8`、`reviewer/repair_handlers/subworld.py:14` 还在用)
- `forwin/canon_quality/rule_profile.py:86-101`
  - 删 `use_legacy_fallback: bool = False` 参数
  - 删 `if use_legacy_fallback:` 死分支
- `forwin/orchestrator_loop_core/governance.py`
  - 删 `_project_governance` 里 `if str(governance.progression_mode or "") == "legacy_relaxed":` 的 instrumentation 整段(governance 阶段 + audit 钩子)
- `forwin/api_runtime.py:245`
  - `progression_mode=progression_mode or "legacy_relaxed"` → 改为 `progression_mode=progression_mode or "serial_canon_band_guard"`(0/15 项目用 legacy_relaxed,且默认值应该是当前主路径)
- `forwin/governance.py` 的 `ProgressionMode` 枚举里删 `legacy_relaxed`(如果有)
- `forwin/governance.py` 的 `normalize_project_governance` 里删 `treat_empty_as_legacy` 参数 + 相关分支(15 个 caller 全改成不传)

### API schema cleanup

- `forwin/api_schema/world.py:115` `class BookStateLegacyImportResponse` —— 删
- `forwin/api_schema/world.py:131` 字段 `legacy_entity_id: str = ""` —— Phase 2 处理(还在用 schema 暴露给 client)

### Registry cleanup

`forwin/review_engine/audit.py`:
- 删 `dead_code.*` × 4(已经确认死了,留着只是噪音)
- 删 `governance.legacy_relaxed_fallback`
- 删 `migration.legacy_book_state_import`(Phase 1 删了 importer 之后)

### Acceptance

- `pytest` 全过
- `docker exec forwin python scripts/audit_review_engine_cutover.py --project-id <pilot> --include-legacy-compat`,新事件数应该 ≤ 上次
- `git grep -rn ChapterRepairCoordinator\|LegacyBookStateImporter\|legacy_relaxed forwin/` 应该只剩 migration 文件 / 历史 commit message

### Risk

低。所有目标 audit 已证明 0 usage。

## Phase 2 — 删 `legacy_entity_id` 整个子系统

这是最大头(3543 + 26 + 18 = 3587 events,98% 的 legacy traffic)。

### Step 2.1 — 翻转 default,停止新写入

- `forwin/characters/models.py:34`
  ```python
  create_legacy_entity: bool = True   # 改成 False
  ```
- `forwin/characters/creation.py:77-88`
  - 删 `if request.create_legacy_entity and not legacy_entity_id:` 整段
  - `legacy_entity_id` 参数继续接受外部传入(为下个 step 平滑),但内部不再自己生成
- `forwin/state/updater.py:493`
  - `create_legacy_entity=entity is None` → 改成 `create_legacy_entity=False`,然后下游 path 整段就走不到了,顺手删

### Step 2.2 — 删 reader fallback 链

按调用图从浅到深删:

- `forwin/context/assembler_core/personality_integrity.py:69-72`
  ```python
  legacy_entity_id = str(character.get("legacy_entity_id") or "").strip()
  if not (
      character_id in allowed_ids
      or legacy_entity_id in allowed_ids   # 删这条 OR
      ...
  )
  ```
- `forwin/context/assembler_core/book_state_overlay.py:73`
  - 删 `"legacy_entity_id": str(metadata.get("legacy_entity_id") or ""),` 不再注入 context
- `forwin/characters/registry.py:33-57`
  - 删 `normalized_legacy_id` 变量 + `if normalized_legacy_id:` 整段 resolution 分支
  - 删 method signature 里 `legacy_entity_id: str = ""` 参数
- `forwin/characters/identity.py`
  - 删 `legacy_entity_id` 参数(line 35, 87, 135)
  - 删 `legacy_entity_id` WHERE clause(line 49, 144-145)
  - 删 写入(line 107, 117)
  - `CharacterIdentityMapRow` 上的 `legacy_entity_id` 列保留到 Step 2.4 DB migration
- `forwin/characters/creation.py:225, 288, 308`
  - 删 fallback 读取 metadata 里的 legacy_entity_id

### Step 2.3 — 删 subworld bridge

- `forwin/subworld_manager.py:163-176, 456-468, 470-484`
  - 删 3 处 `_record_subworld_legacy_compatibility(... compat_feature="subworld.legacy_entity_id_bridge", ...)`
  - 删 `if legacy_entity_id:` resolve 分支(metadata 里没字段后这条永远 false)
  - 删 `compat_feature="subworld.create_legacy_entity"` 的 record 调用 + 它附近的 entity 创建 fallback
  - 删 `_record_subworld_legacy_compatibility` 函数本身

### Step 2.4 — DB schema migration

写一个 alembic migration(版本号自定,例如 `0028_drop_legacy_entity_id.py`):

```python
def upgrade():
    op.drop_column("character_identity_map", "legacy_entity_id")
    op.drop_index("idx_character_identity_legacy", table_name="character_identity_map")
    # 注意:`models/book_state.py:78` 是 metadata JSON 里的字段,不是独立列,
    # 不需要 DDL,只要保证写入路径已经删掉(Step 2.1)。
    # 历史项目 metadata 里残留的 legacy_entity_id 字段:不在乎旧项目,留着不读即可。
```

`forwin/models/character_identity.py`(或类似)删 `legacy_entity_id` mapped_column。

### Step 2.5 — API schema cleanup

- `forwin/api_schema/world.py:131`
  - 删 `legacy_entity_id: str = ""` 字段
- `forwin/characters/models.py:15, 44, 54`
  - 删 model 里 `legacy_entity_id: str = ""` 字段(CharacterCreationRequest / CharacterCreationResult)
  - 删 `LegacyCharacterImportRequest = CharacterCreationRequest` 别名

### Step 2.6 — Audit registry cleanup

`forwin/review_engine/audit.py` LEGACY_COMPATIBILITY_REGISTRY 删:
- `subworld.legacy_entity_id_bridge`
- `subworld.create_legacy_entity`
- `characters.create_legacy_entity_default_true`

### Acceptance

- `pytest` 全过(预期 personality_integrity 测试可能要更新——只用 canonical id 不再 fallback)
- 跑新 60 章 pilot,期望:
  - `subworld.legacy_entity_id_bridge` events: **0**
  - `subworld.create_legacy_entity` events: **0**
  - `characters.create_legacy_entity_default_true` events: **0**
  - 所有 chapter 正常 accept,personality_integrity 不报错

### Risk

- personality_integrity / character resolution 走 `legacy_entity_id` 路径的旧测试 fixture 会挂——更新 fixture。
- 中风险。**别跟 Phase 3 / 4 合 PR**,单独可回滚。

## Phase 3 — 删 `world_v4_compat`

### Step 3.1 — 关 flag 验证一次

容器 env 把 `FORWIN_WORLD_V4_COMPAT_WRITE` 改成 false(`config.py:579, 696` 已经 default false,你 pilot 容器里大概率显式开了)。

跑 60 章 pilot,期望:
- `projection.legacy_world_model_projection` events: **0**
- 所有 chapter accept 正常,canon gate 不变红

### Step 3.2 — 真删代码

确认 Step 3.1 没问题后:

- 删整个 `forwin/world_v4_compat/` 目录
- `forwin/orchestrator/loop.py:7-13`
  - 删 `from forwin.world_v4_compat.compiler import WorldModelCompiler as WorldModelCompilerV4`
  - 删它的 documentation 字符串里 `WorldModelCompilerV4` 等行
- `forwin/orchestrator_loop_core/common.py:104`
  - 删 `from forwin.world_v4_compat.compiler import ...`
- `forwin/orchestrator_loop_core/world_projection.py`
  - 删 `_apply_world_v4_gate` 函数里 `if self.config.world_v4_compat_write_enabled and gate_verdict is not None:` 那整段 legacy 投影写入(看仔细,**不要**删 `BookStateDirectCommitService` 的 review/compile 调用——那是新路径)
  - 删 instrumentation 调用 `compat_feature="projection.legacy_world_model_projection"`
- `forwin/orchestrator_loop_core/service.py:24`
  - `_apply_world_v4_gate` import 保留(函数名不变,只是内部 legacy 段删了)
- `forwin/orchestrator_loop_core/quality_gates.py:1038`
  - `self._apply_world_v4_gate(...)` 调用保留(函数还在,只是内部不再写 v4 projection)
- `forwin/config.py:431-434, 579, 696-697`
  - 删 `world_v4_compat_write_enabled` config 字段
  - 删 `enable_world_v4_debug_api` config 字段(如果只为 v4 服务)
- `forwin/api_route_registry.py:412`
  - 删 `enable_world_v4_debug_api` 路由(或整段 debug API)

### Step 3.3 — DB schema migration

写 migration `0029_drop_world_v4_tables.py`:
```python
def upgrade():
    op.drop_table("world_v4_schema_v1")
    op.drop_table("world_v4_compile_audit_v1")
```

- `forwin/models/base.py:47-48` 删表注册

### Step 3.4 — Audit registry cleanup

删 `projection.legacy_world_model_projection`。

### Acceptance

- Step 3.1 验证通过
- 删代码后 `pytest` 全过
- 再跑 60 章,`projection.legacy_world_model_projection` events: 0

### Risk

- 中。`world_v4_compat` 的 review/extract 路径可能跟 `BookStateDirectCommitService` 共享代码——拆 `_apply_world_v4_gate` 要小心,只删 v4 写入分支,不删 BookState 走的部分。
- **务必**先 Step 3.1 跑通再删代码。

## Phase 4 — 删 `state.location` fallback + `LegacyBookStateImporter`

放弃旧项目 → 这一组全部可删。

### Step 4.1 — `state.location` fallback

- `forwin/book_state/runtime.py:269`
  - 删 `legacy_location = state.get("location", "") ...` 整段 fallback,只保留 `state.get("location_id")`
- `forwin/map/service.py:296`
  - 同样删
- `forwin/context/assembler_core/book_state_overlay.py:150-152`
  - `state.get("location_id") or state.get("location")` → 改为 `state.get("location_id")`
- `forwin/context/assembler_core/map_context.py:232`
  - 同样
- `forwin/world_model/conflict_detector.py:56`
  - 删 `location = str(state.get("location", "") or "").strip()`(或换 `location_id`)
- `forwin/orchestrator/phase4.py:721`
  - 同样
- `forwin/book_state/reviewer.py:134`
  - 删 `is_legacy_location = patch.field_path == "state.location"` 变量 + 相关 instrumentation
- `forwin/book_state/reviewer.py:158`
  - `patch.field_path in {"state.location_id", "state.location"}` → 改为 `patch.field_path == "state.location_id"`
- `forwin/extractor/` 里如果还有产出 `state.location` patch 的逻辑,改成 `state.location_id`

### Step 4.2 — 删 importer

Phase 1 已经删了 `forwin/book_state/legacy_import.py`。Phase 4 顺手:
- `forwin/book_state/__init__.py` 删 `LegacyBookStateImporter` 的 re-export
- 找 API 层调用 importer 的路由(如果有),删
- 删 `api_schema/world.py:115 BookStateLegacyImportResponse`(Phase 1 已删)

### Step 4.3 — Audit registry cleanup

删 `book_state.state.location_fallback`、`book_state.state.location_patch_warning`、`migration.legacy_book_state_import`。

### Acceptance

- `pytest` 全过
- 60 章 pilot 这 3 项 events: 0
- **明确放弃旧项目 — 老项目数据库里若仍有 `state.location` 字段,该项目 runtime 会缺 location 信息**。Pilot run 必须是新项目。

### Risk

- 中。删 fallback 后任何老项目尝试运行都会 location lookup 失败。已确认"不在乎"。

## Phase 5 — 删 `creation_status="legacy"` + 修剩余 instrumentation

### Step 5.1 — `creation_status` 默认值

- `forwin/models/project.py:21`
  - `creation_status: Mapped[str] = mapped_column(String, default="legacy")` → 改 default 为 `"complete"` 或新建项目的真实状态(查 book_genesis_core 看新项目应该是什么)
- 真 fallback 分支:
  - `forwin/api_core/runtime.py:340` `if str(getattr(project, "creation_status", "") or "").strip() == "legacy":`
    - 看这分支做什么。如果是给老项目做的兜底处理,删整段
- `forwin/context/assembler_core/personality_integrity.py:44`
  - `return str(getattr(project, "creation_status", "") or "legacy") != "legacy"` → 改为 `return True`(新项目都过)或基于真实 status 判断
- `forwin/book_genesis_core/workflow.py:428`、`forwin/genesis_workspace/service.py:442`、`forwin/mcp/client.py:506`、`forwin/mcp/models.py:113`、`forwin/api_schema/project.py:101, 169` 里 `or "legacy"` 的兜底默认值:全改成新项目应该有的真实状态(`"complete"` / `"in_progress"` 等,根据语义)

### Step 5.2 — 修 instrumentation 或彻底删

- `api.legacy_checkpoint_status`:
  - 找到 normalize legacy checkpoint status 的真实代码(可能在 `api_core/tasks.py` 或 `api_schema/tasks.py`)
  - 加 `_record_legacy_compatibility_event` 到那个真分支
  - **或者**——既然不在乎旧 API client,直接删 normalize 路径,registry 同步删
- `project.creation_status_legacy`:
  - Step 5.1 之后这条事件自然消失(没有 `creation_status == 'legacy'` 分支了)
  - registry 删掉这项

### Acceptance

- `pytest` 全过
- 60 章 pilot 期望:`api.legacy_checkpoint_status` 和 `project.creation_status_legacy` events: 0
- `LEGACY_COMPATIBILITY_REGISTRY` 应该已经空了 / 只剩几项 archival_only(若有)

## Phase 6 — 终态验证

跑一次 60 章 pilot,期望:

```
engine_live_chapters: 60/60
severe_mismatch_chapters: []
legacy_safety_net_chapters: []
total_legacy_compat_events: 0    ← 终极指标
```

如果 events > 0,看 by_feature,补对应 cleanup。

## Phase 7(独立,远期)— Cutover safety net 删除

按 `review-engine-cutover-spec.md` Promotion C:Phase 1 cutover 持续 30 天 0 severe mismatch 之后,删:
- `forwin/reviewer/outcome.py:ReviewOutcomeRouter`
- `forwin/reviser/policy.py:RepairPolicy.decide()` (attempt-count 分支)
- `forwin/planning/obligation_scope_router.py:ObligationScopeRouter`
- `forwin/orchestrator_loop_core/quality_gates.py:_prepare_deferred_acceptance_if_needed`
- `forwin/review_engine/rules/{repair.py:build_repair_rules, obligation_scope.py, review_outcome.py, final_acceptance.py}` 的 legacy adapter 半边

**不在本 spec 范围**——它是 cutover 的延伸,有自己的窗口。

## 执行顺序与依赖

```
Phase 1 (deletes + governance)   独立,先做,低风险
    ↓
Phase 2 (legacy_entity_id)       独立,大改,单 PR
    ↓
Phase 3 Step 3.1 (关 flag 验证)
    ↓ (确认 events=0)
Phase 3 Step 3.2-3.4 (删代码)    单 PR
    ↓
Phase 4 (location + importer)    单 PR
    ↓
Phase 5 (creation_status + instrumentation) 单 PR
    ↓
Phase 6 验证 60 章 pilot          终态确认

Phase 7 不在本 spec
```

每个 Phase 单 PR,可独立回滚。**Phase 2 和 Phase 3 别合一起**——任何一个炸了都难定位。

## Risk

| Phase | 风险 |
|---|---|
| 1 | 低。死代码 + 0-project mode |
| 2 | 中。改 default + 删 reader fallback,personality_integrity 测试要更新 |
| 3 | 中。`_apply_world_v4_gate` 共享了 BookState 路径,拆要小心 |
| 4 | 中。明确放弃旧项目,任何老项目 runtime 会断 |
| 5 | 低-中。creation_status 默认值要查清新项目应该是什么 |
| 6 | — 纯验证 |

## Verification

每个 Phase 完成的判据:

- `pytest` 全过
- 60 章 pilot 跑通,`engine_live_chapters: 60/60`
- 对应 feature 的 `legacy_compat` events:0
- 没有新增 `severe_mismatch_chapters`

每个 PR merge 前必须跑 pilot 验证,不只 unit test。

## Rollback

- 每个 Phase 独立 PR,纯 git revert 即可回滚
- DB migration 提供 `downgrade()` 路径(虽然不打算用)
- 没有数据破坏性操作——所有删除都是代码 + 未来不再读;历史 metadata 里残留的 `legacy_entity_id` / `state.location` 不会被读,等于"漂浮"在 JSON 里无害

## Open questions

1. Phase 3 Step 3.2 拆 `_apply_world_v4_gate` 时,`BookStateDirectCommitService` review 部分的语义边界——它现在是 v4 路径的一部分还是 v5+ 主路径?(需要 code 读再确认)
2. Phase 5 Step 5.1 的 `creation_status` 新 default 应该是什么——`"complete"`、`"in_progress"`、`"draft"`?(看 `book_genesis_core` 里项目走完 genesis 后 status 写的是什么)
3. Phase 1 删 `LegacyBookStateImporter` 之前,有没有运维流程依赖它做一次性迁移?如果有,留 1-2 个月不删。但你说"不在乎旧项目",可能直接删即可。
