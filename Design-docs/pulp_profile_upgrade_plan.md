# Pulp Profile 升级计划

更新时间：2026-05-18
状态：implementation-ready
适用版本：当前 master / V4.5.1 后端

把 ForWin 从"精品长篇引擎"扩展出一个"下沉市场量产档"。本文档是 Codex 实施指令，按 Phase 1 → 6 顺序执行。

## 0. 目标与非目标

### 目标

- 引入 `quality_profile = pulp | standard | premium` 元开关
- `pulp` 档：单章 LLM 调用降到 2-3 次，绕过 cognition/map/narrative 提取与 LLM-driven reviewer，跑 hard floor gate
- 跑 30 章压测验证：单章 token 成本不随章节数线性上升、hard floor 拦截率可观测、reward gap 不超 2 章
- 不破坏现有 `standard` / 现有项目行为；旧项目默认仍是 standard

### 非目标

- 本轮不改 BookState 核心 schema
- 不删除任何现有 reviewer/checker，只加旁路开关
- 不实现完整 188 条爽点库（占 schema 扩展和 8 条 seed）
- 不上 World Studio UI 变更
- 不动 LLM fallback / retry / pause / continue 流程

## 1. 已确认的代码事实（Codex 实施前先读）

不读完这一节就动手会重复造轮子或踩错位。

### 1.1 真正的单章流水线

**不**在 `forwin/orchestrator_loop_core/acceptance.py`——那只是人工 approve 入口。

主路径在 `forwin/orchestrator_loop_core/project_chapters.py` 的 `_run_project_chapters()`，关键步骤行号：

| 步骤 | 位置 | 备注 |
|---|---|---|
| `assembling_context` | L130 `retrieval_broker.build_chapter_context` | 走 `ChapterContextAssembler`（`forwin/context/assembler_core/assembler.py`） |
| pre-write audit | L133 `_audit_current_plan_before_write` | 受 `future_constraints_enabled` 影响 |
| writer | L201 `_write_chapter_with_attention_fallback` | scene/single mode 分流 |
| review + repair | L243 `_review_and_maybe_rewrite` | 调 `HistoricalReviewHub.review()`；重试受 `review_fail_max_rewrites` 控制 |
| operation_mode 分支 | L270-L339 | checkpoint/copilot/blackbox |
| `should_apply_canon` | L341 | pass / blackbox+warn / force_accept |
| `review_interval_chapters` | L369 | 周期 review checkpoint |
| `_apply_canon_candidate` | L403 | 调 BookState gate + canon_quality gate |
| memory upsert | L483 | retrieval_broker.memory_index |
| `_run_phase3_pass` | L512 | phase3 |
| `_audit_future_plans_after_acceptance` | L517 | 受 `future_constraints_enabled` |
| `_compile_world_model_after_acceptance` | L528 | BookState extraction + compile + legacy v4 projection |
| `_record_generation_audit_checkpoint_if_due` | L547 | 受 `generation_audit_interval_chapters` |
| `_create_auto_band_checkpoint` | L563 | **受 `governance.auto_band_checkpoint`（per-project derive，不是 config 直读）** |

### 1.2 Review Hub 内置调用

`forwin/reviewer/hub.py::HistoricalReviewHub.review()` 已经内置：

| 子 reviewer | 行号 | 现有开关 | LLM 成本 |
|---|---|---|---|
| `continuity_checker.check` | L80 | — | deterministic |
| `lint_collector.collect` | L88 | hub 构造参数 `lint_review_enabled` | deterministic |
| `personality_reviewer.collect` | L91 | 无 | deterministic（可选 LLM） |
| `analyze_writer_output_quality` | L110 | **无独立开关，传 `llm_client=None` 走 deterministic-only** | LLM if enabled |
| `experience_reviewer.review` | L141 | hub 构造参数 `experience_review_enabled` + `llm_enabled` | LLM |
| `governance_reviewer.review` | L154 | 无 | deterministic |
| `map_movement_reviewer.review` | L166 | 无 | deterministic |
| `personality_reviewer.review` | L181 | 无 | deterministic（可选 LLM） |

→ **关掉 `experience_review_enabled` + 让 `llm_enabled=False` 已经能省掉 hub 内最贵的 2 个 LLM 调用**。`map_movement / personality / governance` 默认 deterministic，但需要新增 enabled 开关让 pulp 完全 short-circuit。

### 1.3 Canon Quality Gate

`forwin/canon_quality/gate.py`：

- `GateMode = Literal["off", "shadow", "strict"]` — **没有 `fatal_only`，要新增需扩 Literal 和 `evaluate_canon_admission` 分支**
- `forwin/orchestrator_loop_core/quality_gates.py::_apply_canon_quality_gate` (L323) 调 `analyze_writer_output_quality` (再跑一次 canon_quality，**会调 LLM 如果 `self.llm_client` 不 None**)

→ pulp 模式同时需要：(a) gate mode = `off` 或新增 `fatal_only`；(b) `_apply_canon_quality_gate` 内传 `llm_client=None` 给 `analyze_writer_output_quality`，避免重复 LLM。

### 1.4 Trope Selector 已存在

`forwin/experience/band_scheduler.py::BandExperienceScheduler.derive_band_delight_schedule`：

- L51-L58 `template_for(category, fallback_index)` — 当前是 round-robin，**这就是 selector 入口**
- L60-L77 blueprint 派生（power/social/mystery/justice/emotion），cost_weight、reward_gap 控制、recent dedup **都没有**
- 输出 `BandRewardItem.template_id` 进入 `BandDelightSchedule.scheduled_rewards`

`forwin/experience/chapter_planner.py::ChapterExperiencePlanner.derive_chapter_experience_plan`：

- L113 `selected_template_ids=[item.template_id for item in chapter_rewards]` — 把当前章命中的 template_id 收集到 `ChapterExperiencePlan`

`forwin/writer/prompt_core/sections.py:189-190`：

```python
if plan.selected_template_ids:
    lines.append(f"  · 选用模板：{'、'.join(plan.selected_template_ids)}")
```

→ writer prompt **只列 template_id**，未注入完整 trope 文本。Phase 5 的工作是：(a) `template_for` 扩出 cost/dedup/reward_gap 逻辑；(b) sections.py 把 ID 解析为完整 4 段文本注入。

### 1.5 Context Assembler

`forwin/context/assembler_core/assembler.py::ChapterContextAssembler`：

- 走 `providers → gates → _build_pack` 模式
- gates 是 plug-in（`_default_gates()`）
- recency truncation 应作为新增 gate，而不是改主流程

### 1.6 Config 已有的细粒度开关

第 1 节列出的全部开关都已经在 `forwin/config.py::_ConfigFields` (L510-L598)，且 `_env_values()` (L320+) 已经从环境变量映射好。**pulp profile 是批量覆盖默认值，不是新增 17 个 flag**。

### 1.7 BookState Extraction & Compile

- 入口：`forwin/orchestrator_loop_core/world_projection.py::_compile_world_model_after_acceptance`
- 提取：`forwin/extractor/book_state_graph_delta.py::BookStateGraphDeltaExtractor`
- 老 v4 projection 已被 `world_v4_compat_write_enabled` 配置（默认 False）
- cognition / narrative 提取无独立开关，需新增 `book_state_layers` 控制

### 1.8 需要新增的真实开关

只有这 4 个：

```python
quality_profile: Literal["pulp", "standard", "premium"] = "standard"
book_state_layers: list[str] = ["world", "map", "cognition", "narrative"]
hard_floor_gate_enabled: bool = False
context_recency_window_chapters: int = 0  # 0 表示不截断

# 加在 hub 构造参数：
map_movement_review_enabled: bool = True
personality_review_enabled: bool = True
canon_quality_review_in_hub_enabled: bool = True  # hub 内的 analyze_writer_output_quality 调用
```

其余靠 `apply_quality_profile()` 切换现有 17 个 flag。

## 2. Phase 1：`quality_profile` 元开关 + governance 派生

### 2.1 explicit-key tracking

`Config.from_env` 现状是所有字段都通过 `_env_*` getter 取值，**无法区分用户显式设置 vs 走 default**。pulp profile 需要"只覆盖未显式设置的字段"，否则用户 ENV 会被静默覆盖。

实现方案：

- 修改 `forwin/config.py::_env_values()`，让它返回 `(values: dict, explicit_keys: set[str])`
- 每个 `_env_*(env, key, default)` helper 检测 `key in env`，命中时把 ORM 字段名加入 `explicit_keys`
- `Config.from_env()` 改为：

```python
@classmethod
def from_env(cls) -> "Config":
    values, explicit_keys = _env_values()
    config = cls(**values)
    return apply_quality_profile(config, explicit_keys=explicit_keys)
```

### 2.2 `apply_quality_profile`

新增模块级函数：

```python
PULP_OVERRIDES: dict[str, Any] = {
    "writer_mode": "single",
    "operation_mode": "blackbox",
    "review_interval_chapters": 0,
    "experience_review_enabled": False,
    "lint_review_enabled": True,
    "canon_quality_gate": "off",  # 或 Phase 3 完成后切到 "fatal_only"
    "freeze_failed_candidates": False,
    "review_fail_max_rewrites": 0,
    "auto_band_checkpoint": False,
    "manual_checkpoints_enabled": False,
    "future_constraints_enabled": False,
    "generation_audit_interval_chapters": 0,
    "generation_audit_pause_enabled": False,
    "world_v4_compat_write_enabled": False,
    "phase4_use_llm": False,
    "reviewer_quality_mode": "deterministic",
    "planning_audit_mode": "off",
    "plan_patch_validation_mode": "off",
    "final_gate_mode": "off",
    "band_checkpoint_mode": "off",
    "min_chapter_chars": 1800,
    "target_chapter_chars": 2400,
    "max_chapter_chars": 3000,
    # 新增字段：
    "book_state_layers": ["world"],
    "hard_floor_gate_enabled": True,
    "context_recency_window_chapters": 50,
    "map_movement_review_enabled": False,
    "personality_review_enabled": False,
    "canon_quality_review_in_hub_enabled": False,
}

PREMIUM_OVERRIDES: dict[str, Any] = {
    # 留作占位，第一轮可空，等后续再填
}

def apply_quality_profile(config: Config, *, explicit_keys: set[str]) -> Config:
    profile = config.quality_profile
    if profile == "pulp":
        overrides = PULP_OVERRIDES
    elif profile == "premium":
        overrides = PREMIUM_OVERRIDES
    else:
        return config
    update = {
        key: value
        for key, value in overrides.items()
        if key not in explicit_keys
    }
    return config.model_copy(update=update)
```

### 2.3 governance 派生联动

`WritingOrchestrator._project_governance(project)` 会从 project / config 派生 `governance` 对象（含 `auto_band_checkpoint / future_constraints_enabled / band_warn_action`）。需要确认派生函数读 config 的字段，apply_quality_profile 改了 config 后自动跟上。**Codex 实施时核验：grep `_project_governance` 找到派生位置，确保读的是 `self.config.auto_band_checkpoint`，否则手动调整。**

### 2.4 测试

- `tests/test_quality_profile.py`
  - `from_env({FORWIN_QUALITY_PROFILE: "pulp"})` 后断言 `writer_mode == "single"`、`book_state_layers == ["world"]`、`hard_floor_gate_enabled is True`
  - `from_env({FORWIN_QUALITY_PROFILE: "pulp", WRITER_MODE: "scene"})` 后 `writer_mode == "scene"`（用户显式覆盖优先）
  - `quality_profile == "standard"` 时无变化

### 2.5 验收

- 单元测试通过
- `pytest` 全套不退化（pulp profile 不应影响默认路径）

## 3. Phase 2：Hard Floor Gate

### 3.1 位置

放在 `_run_project_chapters` 的 `_review_and_maybe_rewrite` 之后、`_apply_canon_candidate` 之前——即 `project_chapters.py` 约 L255-L270 之间。具体插入点：在 `residual_review_issues = self._review_issue_payloads(verdict)` 后、operation_mode 分支前。

### 3.2 8 条检查

实现位于 `forwin/checker/hard_floor.py`（新建）：

1. **chapter_length**：`len(writer_output.body) >= config.min_chapter_chars`
2. **no_garbage**：
   - 非空
   - 无连续 ≥ 12 字符的非 CJK / 非常用标点块（疑似乱码）
   - 不含模型自述标记：`[INST]`、`<think>`、` ```json`、`assistant:`、`<|im_start|>` 等（词典放 `forwin/checker/hard_floor_dict.py`）
3. **protagonist_name_stable**：复用 `forwin.canon_names.find_canon_name_violations` —— 已经存在且 deterministic
4. **at_least_one_event**：`writer_output.events or writer_output.state_changes or writer_output.thread_beats` 至少一项非空（注意 `WriterOutput` 是 candidate list，不是已写入的）
5. **ending_hook**：最后 200 字符匹配钩子词典（疑问句 / 危险标识词 / 收益标识词 / 反转标识词）；词典放 `hard_floor_dict.py`
6. **must_not_reveal**：`context_pack.must_not_reveal` 做子串匹配；命中返回 fail，附带命中条目
7. **no_dead_alive / no_teleport / no_closed_thread**：复用 `forwin/checker/rules.py::DEAD_STATUS_KEYWORDS` 与 `governance` 现有 thread state 查询；teleport 在 `book_state_layers` 含 `"map"` 时调 `forwin/book_state/map_graph.py::distance_between_world_nodes`，否则跳过此子检查
8. **reward_gap**：从 `repo.list_recent_chapter_plans(project_id, count=N+1)` 查最近章节的 `ChapterExperiencePlan.planned_reward_tags`，全空则 fail（N 默认从 config 读，pulp 默认 2）

### 3.3 API

```python
from pydantic import BaseModel

class HardFloorResult(BaseModel):
    passed: bool
    fail_reasons: list[str]
    warning_reasons: list[str]
    checks: dict[str, bool]

def run_hard_floor(
    *,
    writer_output: WriterOutput,
    context_pack: ChapterContextPack,
    repo,
    project_id: str,
    chapter_number: int,
    config: Config,
) -> HardFloorResult: ...
```

`reward_gap` 默认是 warning（不阻断，因为可能正常 setup 章），但 `chapter_length / no_garbage / must_not_reveal` 必 fail。

### 3.4 接入 orchestrator

在 `project_chapters.py` 插入：

```python
if self.config.hard_floor_gate_enabled:
    hard_floor = run_hard_floor(
        writer_output=writer_output,
        context_pack=context,
        repo=repo,
        project_id=project_id,
        chapter_number=chapter_num,
        config=self.config,
    )
    if not hard_floor.passed:
        updater.mark_chapter_status(
            project_id, chapter_num, "failed",
            residual_review_issues=[
                {"reviewer": "hard_floor", "reason": r}
                for r in hard_floor.fail_reasons
            ],
        )
        self._record_decision_event(
            updater=updater, project_id=project_id, chapter_number=chapter_num,
            event_family="evaluation_verdict",
            event_type=DecisionEventType.HARD_GATE_HIT,
            scope="chapter",
            summary=f"hard floor fail: {', '.join(hard_floor.fail_reasons)}",
        )
        session.commit()
        failed_chapters.append(chapter_num)
        continue
```

### 3.5 测试

`tests/test_hard_floor.py`：8 个 fixture 各 fail 一条；1 个全 pass fixture。

### 3.6 验收

- 单元测试覆盖 8 条所有 fail 路径
- 与 standard pipeline 并跑不退化（standard 默认 `hard_floor_gate_enabled=False`）

## 4. Phase 3：Pulp 模式 bypass 短路点

### 4.1 改 hub 构造参数

`forwin/reviewer/hub.py::HistoricalReviewHub.__init__` 增加 3 个开关：

```python
def __init__(
    self,
    *,
    experience_review_enabled: bool = True,
    lint_review_enabled: bool = True,
    map_movement_review_enabled: bool = True,
    personality_review_enabled: bool = True,
    canon_quality_review_in_hub_enabled: bool = True,
    ...
)
```

在 `review()` 内对应位置加 guard：

- L110 `analyze_writer_output_quality` 调用包在 `if self.canon_quality_review_in_hub_enabled:` 内；否则 `deterministic_quality_report = {}`
- L141 `experience_reviewer.review` 已有 `experience_review_enabled` 控制（构造时传入 disabled 的 reviewer），确认运行时还能再 guard
- L166 `map_movement_reviewer.review` 包在 `if self.map_movement_review_enabled:` 内；否则 `map_movement = ReviewVerdict(verdict="pass", issues=[])`
- L181 `personality_reviewer.review` 同上

### 4.2 改 hub 注入位置

`forwin/runtime/container.py` 或 `factories.py` 构造 hub 时把 config 新字段透传：

```python
hub = HistoricalReviewHub(
    experience_review_enabled=config.experience_review_enabled,
    lint_review_enabled=config.lint_review_enabled,
    map_movement_review_enabled=config.map_movement_review_enabled,
    personality_review_enabled=config.personality_review_enabled,
    canon_quality_review_in_hub_enabled=config.canon_quality_review_in_hub_enabled,
    llm_client=llm_client if config.reviewer_quality_mode != "deterministic" else None,
    llm_enabled=config.reviewer_quality_mode != "deterministic",
    ...
)
```

### 4.3 `canon_quality_gate` 扩 `fatal_only` 模式（可选优化）

最简方案：pulp 用 `canon_quality_gate = "off"`，gate 完全跳过 commit block。

进阶方案（推荐）：扩 `forwin/canon_quality/gate.py`：

```python
GateMode = Literal["off", "shadow", "fatal_only", "strict"]
```

`evaluate_canon_admission` 增加 `fatal_only` 分支：只把以下 `signal_kind` 当 blocking，其余降为 warning：

- `character_dead_alive`
- `character_teleport`
- `closed_thread_reopened`
- `final_dangling`
- `final_denied`
- `countdown_inconsistent`

`_apply_canon_quality_gate` (L341) 调 `analyze_writer_output_quality` 时也按 mode 传 `llm_client=None`（pulp 即 fatal_only / off）。

### 4.4 BookState extraction layers

`forwin/extractor/book_state_graph_delta.py::BookStateGraphDeltaExtractor` 增加 `layers: set[str]` 参数。提取时：

- `"world" not in layers` → 完全不提取（pulp 模式至少保留 world，所以这个分支理论上不会触发）
- `"map" not in layers` → 跳过 `MapPatch` 生成
- `"cognition" not in layers` → 跳过 `CognitionPatch` 生成
- `"narrative" not in layers` → 跳过 narrative_node/narrative_edge patch

`world_projection.py::_compile_world_model_after_acceptance` 构造 extractor 时传 `layers=set(config.book_state_layers)`。

### 4.5 phase3 / future audit / band checkpoint

- `_run_phase3_pass` 在 `forwin/orchestrator/phase3.py`——pulp 用 `phase4_use_llm = False` 已经能让它走 deterministic 分支；若仍想 short-circuit 加 `phase3_enabled` 开关（备选）
- `_audit_future_plans_after_acceptance` 受 `future_constraints_enabled` 控制——已经存在，pulp 设 False
- `_create_auto_band_checkpoint` 受 `governance.auto_band_checkpoint`——Phase 1 的 governance 派生联动覆盖

### 4.6 测试

`tests/test_pulp_pipeline_bypass.py`：

- mock 单章流水线，断言 pulp 模式下：
  - `map_movement_reviewer.review` 调用次数 == 0
  - `experience_reviewer.review` 调用次数 == 0
  - `personality_reviewer.review` 调用次数 == 0
  - `analyze_writer_output_quality` 调用次数 ≤ 1（gate 跳过，hub 跳过，可能只在 `_apply_canon_quality_gate` 里走一次 deterministic）
  - `BookStateGraphDeltaExtractor` 不输出 MapPatch / CognitionPatch / narrative patch

### 4.7 验收

- pulp 模式单章 LLM 调用计数 ≤ 3
- standard 模式与原有行为完全一致

## 5. Phase 4：Context Recency Gate

### 5.1 位置

新增 gate（不是改 assembler 主流程）：`forwin/context/gates/recency_truncate.py`：

```python
class RecencyTruncateGate:
    name = "recency_truncate"

    def __init__(self, window_chapters: int = 0, max_entities: int = 0):
        self.window = max(0, int(window_chapters))
        self.max_entities = max(0, int(max_entities))

    def validate(self, request, draft) -> list:
        if self.window <= 0:
            return []
        current = int(request.chapter_plan.chapter_number or 0)
        cutoff = current - self.window
        for key in ("summaries", "recent_state_changes", "recent_thread_beats", "recent_events"):
            items = draft.data.get(key) or []
            draft.data[key] = [
                item for item in items
                if int(getattr(item, "chapter_number", 0) or 0) >= cutoff
            ]
        if self.max_entities:
            entities = draft.data.get("entities") or []
            draft.data["entities"] = self._rank_and_take(entities, cutoff)
        return []

    def _rank_and_take(self, entities, cutoff):
        # 按 last_seen_chapter desc, importance desc 排序，取前 max_entities
        ...
```

### 5.2 注入

`ChapterContextAssembler._default_gates()` 末尾追加 `RecencyTruncateGate(window=config.context_recency_window_chapters, max_entities=...)`。Pulp 模式默认 window=50。

### 5.3 测试

`tests/test_context_recency_truncation.py`：

- 构造 200 章 fixture，window=50
- 跑第 100/150/200 章，断言 `prompt_message_chars(pack)` 持平 ±10%
- 断言最近 3 章出现的角色仍在 pack 内

### 5.4 验收

- 200 章模拟 pack char_count 稳定
- 单元测试通过

## 6. Phase 5：Trope Selector 扩展 + Schema 扩展 + MD Loader + Writer 注入

### 6.1 Schema 扩展

`forwin/protocol/trope_library.py::TropeTemplate` 增加（默认值确保兼容现有 JSON seed）：

```python
subcategory: str = ""
market_tier: Literal["sinking", "mainstream", "premium"] = "mainstream"
cost_weight: int = 2
genre_fit: list[str] = Field(default_factory=list)
pressure_shape: str = ""
protagonist_action: str = ""
visible_payoff: str = ""
audience_reaction: str = ""
next_hook_shape: str = ""
anti_patterns: list[str] = Field(default_factory=list)
review_signals: list[str] = Field(default_factory=list)
desire_setup: str = ""
resistance: str = ""
payoff: str = ""
aftermath: str = ""
```

### 6.2 MD Loader

新增 `forwin/protocol/trope_md_loader.py::load_trope_templates_from_md(path) -> list[TropeTemplate]`，解析规则见 `trope_library_pulp_v1.md` 的 "Schema 字段说明" 节。

`forwin/protocol/trope_library.py::load_trope_template_library()` 修改：

```python
override_path = os.environ.get("FORWIN_TROPE_TEMPLATE_PATH", "").strip()
if override_path:
    path = Path(override_path)
    if path.suffix.lower() == ".md":
        return tuple(load_trope_templates_from_md(path))
    return load_trope_template_file(path, require_full=True)
```

### 6.3 Selector 扩展（**不**新建 trope_selector.py）

修改 `forwin/experience/band_scheduler.py::BandExperienceScheduler.derive_band_delight_schedule`。

`template_for` 改为：

```python
def template_for(
    category: str,
    fallback_index: int,
    *,
    used_template_ids: set[str],
    cost_ceiling: int,
) -> str:
    macro = macro_by_category.get(category)
    if macro is not None and macro.template_id:
        return macro.template_id
    candidates = trope_templates_by_category(category)
    candidates = [
        t for t in candidates
        if t.cost_weight <= cost_ceiling and t.template_id not in used_template_ids
    ]
    if not candidates:
        candidates = trope_templates_by_category(category)
    # 按 cost_weight asc + name 排序，保持 deterministic
    candidates.sort(key=lambda t: (t.cost_weight, t.template_id))
    return candidates[fallback_index % len(candidates)].template_id
```

`derive_band_delight_schedule` 入参增加 `cost_ceiling: int = 3`（pulp 模式由 service 层传 `2`），并维护 `used_template_ids` 跨 band loop。

### 6.4 Writer Prompt 注入

修改 `forwin/writer/prompt_core/sections.py:189-190`：

```python
# 旧:
if plan.selected_template_ids:
    lines.append(f"  · 选用模板：{'、'.join(plan.selected_template_ids)}")

# 新:
if plan.selected_template_ids:
    from forwin.protocol.trope_library import trope_template_index
    library = trope_template_index()
    selected_templates = [library[tid] for tid in plan.selected_template_ids if tid in library]
    if selected_templates:
        lines.append("  · 本章爽点指令（按段落执行）:")
        for template in selected_templates[:2]:  # 最多 2 个
            lines.append(f"    [{template.display_name or template.template_id}]")
            if template.desire_setup:
                lines.append(f"    1. 欲望建立：{template.desire_setup}")
            if template.resistance:
                lines.append(f"    2. 阻力加压：{template.resistance}")
            if template.payoff:
                lines.append(f"    3. 爽点兑现：{template.payoff}")
            if template.aftermath:
                lines.append(f"    4. 余波钩子：{template.aftermath}")
            if template.anti_patterns:
                lines.append(f"    禁止：{'；'.join(template.anti_patterns[:3])}")
```

注意：注入有 prompt budget 风险。最多 2 条 trope，每条约 600 字符 = 1200 字符。`prompt_budget_chars` 现有默认 12000，留足空间。Codex 实施时跑 `forwin/writer/prompt_budget.py::prompt_budget_warning` 验证不超限。

### 6.5 测试

- `tests/test_trope_schema_compat.py`：现有 `trope_templates.seed.json` 加载后所有新字段为默认值，不报错
- `tests/test_trope_md_loader.py`：解析 `Design-docs/trope_library_pulp_v1.md` 8 条
- `tests/test_trope_selector.py`：mock library，验证 cost_ceiling / dedup 生效
- `tests/test_trope_prompt_injection.py`：构造 plan 含 1 个 template_id，断言 prompt 含 4 段文本

### 6.6 验收

- `FORWIN_TROPE_TEMPLATE_PATH=Design-docs/trope_library_pulp_v1.md` 启动 server 不报错
- writer prompt 内含完整 4 段 trope 文本，char_count ≤ `prompt_budget_chars`

## 7. Phase 6：30 章压测脚本与指标

### 7.1 新增脚本

`scripts/pulp_pressure_test.py`：

```bash
python scripts/pulp_pressure_test.py \
  --project-id <id> \
  --chapters 30 \
  --output reports/pulp_test_$(date +%s)/
```

环境变量：设置 `FORWIN_QUALITY_PROFILE=pulp` 后再启动 orchestrator。

### 7.2 必须记录的指标（per chapter）

| 指标 | 来源 |
|---|---|
| `chapter_number` | — |
| `wall_time_seconds` | 计时 |
| `llm_call_count` | `writer_output.generation_meta.call_count` + hub.review 内的 LLM 调用计数（observability span） |
| `output_token_count` | LLM response usage |
| `prompt_char_count` | `prompt_message_chars(messages)` 总和 |
| `context_pack_char_count` | `ChapterContextPack.model_dump_json()` 长度 |
| `hard_floor_passed` | bool |
| `hard_floor_fail_reasons` | list |
| `reward_beats_in_plan` | `ChapterExperiencePlan.planned_reward_tags` 长度 |
| `reward_gap_since_last` | 距上次 reward beat 章数 |
| `selected_trope_ids` | `ChapterExperiencePlan.selected_template_ids` |
| `ending_hook_detected` | hard floor check 5 |
| `chapter_length` | `writer_output.char_count` |
| `bookstate_compile_succeeded` | `_compile_world_model_after_acceptance` 返回值 |
| `rewrite_count` | `repo.list_chapter_rewrite_attempts` 长度 |
| `verdict` | `review.verdict` |

### 7.3 汇总指标 + 验收阈值

跑完 30 章必须满足：

- **token 成本 slope**：单章 prompt_char_count 对 chapter_number 做线性回归，slope < 0.02（基本持平）—— 这是 Phase 4 truncation 的核心验收点
- **平均 LLM 调用数 / 章 ≤ 3**
- **hard floor 拦截率 5%-20%**（太低说明 gate 失效；太高说明 writer profile 设错）
- **reward gap p95 ≤ 2**
- **BookState compile 失败 == 0**（允许重试）
- **平均 wall time / 章 ≤ 60s**

### 7.4 输出格式

- `reports/pulp_test_<ts>/metrics.csv` — 逐章
- `reports/pulp_test_<ts>/summary.json` — 汇总
- `reports/pulp_test_<ts>/README.md` — 自动生成人读报告

## 8. 不要碰的部分

- BookState canon commit path 主结构
- LLM retry / fallback 链
- Pause / Cancel / Continue 状态机
- 现有 `standard` profile 行为（quality_profile 默认 "standard"，对现有项目零影响）
- Genesis 流程
- Publisher 工作流
- 现有 `trope_templates.seed.json`（保留，新字段全部默认值兼容）

## 9. 文档更新

实施完成后更新：

- `Design-docs/CURRENT_ARCHITECTURE.md` 增加"Quality Profile"一节
- `Design-docs/DESIGN_STATUS.md` 把本文档登记为 `active-current`
- `README.md` Configuration 段增加 `FORWIN_QUALITY_PROFILE` 与 `FORWIN_TROPE_TEMPLATE_PATH` 说明

## 10. 实施顺序（强约束）

Phase 1 → 2 → 3 → 4 必须按顺序：

- 没有 Phase 1 元开关，后续 phase 没法决定何时启用
- 没有 Phase 2 hard floor，pulp 模式无质量底线
- 没有 Phase 3 短路点，pulp 实际还在跑重型 reviewer
- 没有 Phase 4 截断，30 章压测的 token slope 必然爆

Phase 5、6 可与 Phase 3 并行启动（schema 扩展和压测脚本不依赖短路点逻辑）。

## 11. 一次性提交建议

每 Phase 一个 PR。Phase 5 拆 3 个 PR：
- 5a：TropeTemplate schema 扩展 + MD loader
- 5b：BandExperienceScheduler selector 扩展
- 5c：Writer prompt 注入

爽点内容在配套文档 `trope_library_pulp_v1.md`，本计划 Phase 5a 完成后即可加载。
