# Pulp Profile Upgrade Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement the full Phase 1-6 pulp profile upgrade so ForWin has a low-cost `quality_profile="pulp"` path with deterministic floor checks, context truncation, trope injection, and pressure-test reporting.

**Architecture:** Pulp is a derived configuration profile, not a second orchestrator. Existing runtime components receive normal config fields: reviewer enabled switches, canon gate mode, BookState layers, context recency window, trope cost ceiling, and hard floor enablement. The main chapter loop stays intact except for one hard floor decision point between review and operation-mode handling.

**Tech Stack:** Python 3, Pydantic, SQLAlchemy, pytest, existing ForWin runtime container, orchestrator loop, canon-quality gate, context assembler, BookState extractor, experience scheduler, writer prompt builder, and observability records.

---

## File Structure

Create:

- `forwin/checker/hard_floor.py`: deterministic hard floor checks and result model.
- `forwin/checker/hard_floor_dict.py`: model-artifact and hook keyword dictionaries for hard floor.
- `forwin/context/gates/__init__.py`: context gate package export surface.
- `forwin/context/gates/recency_truncate.py`: recency truncation gate.
- `forwin/protocol/trope_md_loader.py`: markdown trope library parser.
- `scripts/pulp_pressure_test.py`: pressure-test CLI and report writer.
- `tests/test_quality_profile.py`: profile derivation and explicit env tracking.
- `tests/test_hard_floor.py`: hard floor unit checks.
- `tests/test_pulp_pipeline_bypass.py`: reviewer, gate, and BookState bypass checks.
- `tests/test_context_recency_truncation.py`: recency gate checks.
- `tests/test_trope_schema_compat.py`: seed JSON compatibility.
- `tests/test_trope_md_loader.py`: markdown loader checks.
- `tests/test_trope_selector.py`: selector cost and dedup checks.
- `tests/test_trope_prompt_injection.py`: writer prompt injection checks.
- `tests/test_pulp_pressure_test.py`: pressure report calculations.

Modify:

- `forwin/config.py`: explicit env tracking, quality profile fields, and derived overrides.
- `forwin/runtime/container.py`: pass new config fields into hub, context assembler, and experience services where needed.
- `forwin/reviewer/hub.py`: reviewer component enabled switches.
- `forwin/canon_quality/gate.py`: `fatal_only` gate mode.
- `forwin/orchestrator_loop_core/quality_gates.py`: deterministic canon-quality analysis for `off` and `fatal_only`.
- `forwin/orchestrator_loop_core/project_chapters.py`: hard floor gate insertion.
- `forwin/orchestrator_loop_core/world_projection.py`: pass BookState layers into extractor.
- `forwin/extractor/book_state_graph_delta.py`: layer filtering.
- `forwin/context/assembler_core/assembler.py`: accept injected recency gate through constructor or factory wiring.
- `forwin/experience/band_scheduler.py`: cost ceiling and template dedup.
- `forwin/orchestrator/phase24.py`: pass profile-aware cost ceiling when deriving band schedule.
- `forwin/planning/band_plan_service.py`: pass profile-aware cost ceiling for service-driven band planning.
- `forwin/protocol/trope_library.py`: schema extension and markdown override loading.
- `forwin/writer/prompt_core/sections.py`: four-part trope prompt injection.
- `Design-docs/CURRENT_ARCHITECTURE.md`: add Quality Profile section.
- `Design-docs/DESIGN_STATUS.md`: register pulp upgrade as active current work.
- `README.md`: document `FORWIN_QUALITY_PROFILE` and `FORWIN_TROPE_TEMPLATE_PATH`.

Track source documents during implementation:

- `Design-docs/pulp_profile_upgrade_plan.md`
- `Design-docs/trope_library_pulp_v1.md`

## Task 1: Quality Profile Config And Explicit Env Tracking

**Files:**
- Modify: `forwin/config.py`
- Test: `tests/test_quality_profile.py`

- [ ] **Step 1: Write failing profile tests**

Create `tests/test_quality_profile.py` with this structure:

```python
from __future__ import annotations

from pathlib import Path

from forwin.config import Config


PROFILE_ENV_KEYS = {
    "FORWIN_QUALITY_PROFILE",
    "WRITER_MODE",
    "OPERATION_MODE",
    "FORWIN_CANON_QUALITY_GATE",
    "EXPERIENCE_REVIEW_ENABLED",
    "LINT_REVIEW_ENABLED",
    "REVIEW_FAIL_MAX_REWRITES",
    "FORWIN_BOOK_STATE_LAYERS",
    "FORWIN_HARD_FLOOR_GATE_ENABLED",
    "FORWIN_CONTEXT_RECENCY_WINDOW_CHAPTERS",
    "FORWIN_MAP_MOVEMENT_REVIEW_ENABLED",
    "FORWIN_PERSONALITY_REVIEW_ENABLED",
    "FORWIN_CANON_QUALITY_REVIEW_IN_HUB_ENABLED",
}


def config_from_env(monkeypatch, tmp_path: Path, values: dict[str, str]) -> Config:
    env_file = tmp_path / "forwin.env"
    env_file.write_text("", encoding="utf-8")
    monkeypatch.setenv("FORWIN_ENV_FILE", str(env_file))
    for key in PROFILE_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)
    for key, value in values.items():
        monkeypatch.setenv(key, value)
    return Config.from_env()


def test_pulp_profile_derives_low_cost_defaults(monkeypatch, tmp_path):
    config = config_from_env(monkeypatch, tmp_path, {"FORWIN_QUALITY_PROFILE": "pulp"})

    assert config.quality_profile == "pulp"
    assert config.writer_mode == "single"
    assert config.operation_mode == "blackbox"
    assert config.review_interval_chapters == 0
    assert config.experience_review_enabled is False
    assert config.canon_quality_gate == "fatal_only"
    assert config.review_fail_max_rewrites == 0
    assert config.book_state_layers == ["world"]
    assert config.hard_floor_gate_enabled is True
    assert config.context_recency_window_chapters == 50
    assert config.map_movement_review_enabled is False
    assert config.personality_review_enabled is False
    assert config.canon_quality_review_in_hub_enabled is False


def test_explicit_env_wins_over_pulp_profile(monkeypatch, tmp_path):
    config = config_from_env(
        monkeypatch,
        tmp_path,
        {
            "FORWIN_QUALITY_PROFILE": "pulp",
            "WRITER_MODE": "scene",
            "FORWIN_CANON_QUALITY_GATE": "strict",
            "FORWIN_HARD_FLOOR_GATE_ENABLED": "false",
        },
    )

    assert config.writer_mode == "scene"
    assert config.canon_quality_gate == "strict"
    assert config.hard_floor_gate_enabled is False


def test_standard_profile_keeps_existing_defaults(monkeypatch, tmp_path):
    config = config_from_env(monkeypatch, tmp_path, {"FORWIN_QUALITY_PROFILE": "standard"})

    assert config.quality_profile == "standard"
    assert config.writer_mode == "scene"
    assert config.canon_quality_gate == "strict"
    assert config.book_state_layers == ["world", "map", "cognition", "narrative"]
    assert config.hard_floor_gate_enabled is False
    assert config.context_recency_window_chapters == 0


def test_premium_profile_is_currently_identity(monkeypatch, tmp_path):
    config = config_from_env(monkeypatch, tmp_path, {"FORWIN_QUALITY_PROFILE": "premium"})

    assert config.quality_profile == "premium"
    assert config.writer_mode == "scene"
    assert config.hard_floor_gate_enabled is False
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_quality_profile.py -q
```

Expected: failures mention missing `quality_profile`, `book_state_layers`, or related fields.

- [ ] **Step 3: Add profile fields to `_ConfigFields`**

In `forwin/config.py`, add the new fields near the existing writer and review config fields:

```python
    quality_profile: Literal["pulp", "standard", "premium"] = "standard"
    book_state_layers: list[str] = ["world", "map", "cognition", "narrative"]
    hard_floor_gate_enabled: bool = False
    context_recency_window_chapters: int = 0
    map_movement_review_enabled: bool = True
    personality_review_enabled: bool = True
    canon_quality_review_in_hub_enabled: bool = True
```

- [ ] **Step 4: Add explicit-key helpers**

Change `_env_values()` to build `explicit_keys`. Use local wrappers so the existing env parsing helpers stay small:

```python
def _env_values() -> tuple[dict[str, object], set[str]]:
    env = _resolved_env()
    explicit_keys: set[str] = set()

    def mark(field: str, *env_keys: str) -> None:
        if any(key in env for key in env_keys):
            explicit_keys.add(field)

    def tracked_str(field: str, key: str, default: str = "") -> str:
        mark(field, key)
        return _env_str(env, key, default)

    def tracked_bool(field: str, key: str, default: bool = False) -> bool:
        mark(field, key)
        return _env_bool(env, key, default)

    def tracked_int(field: str, key: str, default: int = 0) -> int:
        mark(field, key)
        return _env_int(env, key, default)

    def tracked_csv(field: str, key: str) -> list[str]:
        mark(field, key)
        return _env_csv(env, key)
```

Replace the profile-sensitive entries in the returned values dict with tracked helper calls. Keep non-profile fields on existing helpers unless they need explicit protection by pulp overrides.

- [ ] **Step 5: Add profile override maps**

Place these module-level constants above `_ConfigFields`:

```python
PULP_OVERRIDES: dict[str, object] = {
    "writer_mode": "single",
    "operation_mode": "blackbox",
    "review_interval_chapters": 0,
    "experience_review_enabled": False,
    "lint_review_enabled": True,
    "canon_quality_gate": "fatal_only",
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
    "book_state_layers": ["world"],
    "hard_floor_gate_enabled": True,
    "context_recency_window_chapters": 50,
    "map_movement_review_enabled": False,
    "personality_review_enabled": False,
    "canon_quality_review_in_hub_enabled": False,
}

PREMIUM_OVERRIDES: dict[str, object] = {}
```

- [ ] **Step 6: Implement `apply_quality_profile()` and update `from_env()`**

Add:

```python
def apply_quality_profile(config: "Config", *, explicit_keys: set[str]) -> "Config":
    profile = str(getattr(config, "quality_profile", "standard") or "standard").strip().lower()
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

Change `Config.from_env()`:

```python
    @classmethod
    def from_env(cls) -> "Config":
        values, explicit_keys = _env_values()
        config = cls(**values)
        return apply_quality_profile(config, explicit_keys=explicit_keys)
```

- [ ] **Step 7: Add env parsing for new fields**

Add values in `_env_values()`:

```python
        "quality_profile": tracked_str("quality_profile", "FORWIN_QUALITY_PROFILE", "standard"),
        "book_state_layers": tracked_csv("book_state_layers", "FORWIN_BOOK_STATE_LAYERS") or ["world", "map", "cognition", "narrative"],
        "hard_floor_gate_enabled": tracked_bool("hard_floor_gate_enabled", "FORWIN_HARD_FLOOR_GATE_ENABLED", False),
        "context_recency_window_chapters": tracked_int("context_recency_window_chapters", "FORWIN_CONTEXT_RECENCY_WINDOW_CHAPTERS", 0),
        "map_movement_review_enabled": tracked_bool("map_movement_review_enabled", "FORWIN_MAP_MOVEMENT_REVIEW_ENABLED", True),
        "personality_review_enabled": tracked_bool("personality_review_enabled", "FORWIN_PERSONALITY_REVIEW_ENABLED", True),
        "canon_quality_review_in_hub_enabled": tracked_bool("canon_quality_review_in_hub_enabled", "FORWIN_CANON_QUALITY_REVIEW_IN_HUB_ENABLED", True),
```

Use tracked helpers for every key in `PULP_OVERRIDES` that is env-configurable.

- [ ] **Step 8: Run tests**

Run:

```bash
pytest tests/test_quality_profile.py -q
```

Expected: all tests pass.

- [ ] **Step 9: Commit**

```bash
git add forwin/config.py tests/test_quality_profile.py
git commit -m "feat: add pulp quality profile config"
```

## Task 2: Hard Floor Checker

**Files:**
- Create: `forwin/checker/hard_floor_dict.py`
- Create: `forwin/checker/hard_floor.py`
- Test: `tests/test_hard_floor.py`

- [ ] **Step 1: Write failing hard floor unit tests**

Create `tests/test_hard_floor.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from forwin.checker.hard_floor import run_hard_floor
from forwin.config import Config
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.writer import WriterOutput
from forwin.protocol.state_change import EventCandidate


def writer(body: str, **updates) -> WriterOutput:
    data = {
        "chapter_number": 1,
        "title": "第一章",
        "body": body,
        "char_count": len(body),
        "end_of_chapter_summary": "本章发生了一件事。",
        "new_events": [EventCandidate(event_type="scene_event", description="角色A完成行动")],
    }
    data.update(updates)
    return WriterOutput(**data)


def context(**updates) -> ChapterContextPack:
    data = {
        "project_id": "project-1",
        "project_title": "测试项目",
        "premise": "测试前提",
        "genre": "玄幻",
        "setting_summary": "测试设定",
        "chapter_number": 1,
        "must_not_reveal": [],
    }
    data.update(updates)
    return ChapterContextPack(**data)


def config() -> Config:
    return Config(min_chapter_chars=20, hard_floor_gate_enabled=True)


def test_short_chapter_fails():
    result = run_hard_floor(
        writer_output=writer("太短"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "chapter_length" in result.fail_reasons


def test_model_artifact_fails():
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见线索。assistant: 模型分析。章末问题出现。"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "no_garbage" in result.fail_reasons


def test_must_not_reveal_fails_on_direct_match():
    result = run_hard_floor(
        writer_output=writer("角色A终于发现父亲被围的真相，众人沉默。"),
        context_pack=context(must_not_reveal=["父亲被围"]),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "must_not_reveal" in result.fail_reasons


def test_missing_event_fails():
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见线索。章末新的脚步声逼近。", new_events=[], state_changes=[], thread_beats=[]),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is False
    assert "at_least_one_event" in result.fail_reasons


def test_ending_hook_is_warning_only():
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见线索。他把证据交给同伴，众人决定继续调查。"),
        context_pack=context(),
        repo=SimpleNamespace(get_chapter_experience_plan=lambda *args: None),
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert "ending_hook" in result.warning_reasons
    assert "ending_hook" not in result.fail_reasons


def test_clean_chapter_passes():
    result = run_hard_floor(
        writer_output=writer("角色A推开门，看见线索。他当场拿出证据，反派失去资格。门外忽然传来第二封密令？"),
        context_pack=context(),
        repo=None,
        project_id="project-1",
        chapter_number=1,
        config=config(),
    )

    assert result.passed is True
    assert result.fail_reasons == []
```

- [ ] **Step 2: Run tests and verify they fail**

Run:

```bash
pytest tests/test_hard_floor.py -q
```

Expected: import error for `forwin.checker.hard_floor`.

- [ ] **Step 3: Add hard floor dictionaries**

Create `forwin/checker/hard_floor_dict.py`:

```python
MODEL_ARTIFACT_MARKERS = (
    "[INST]",
    "\x3cthink\x3e",
    "\x3c/think\x3e",
    "```json",
    "assistant:",
    "\x3c|im_start|\x3e",
)

ENDING_HOOK_MARKERS = (
    "？",
    "?",
    "危险",
    "忽然",
    "密令",
    "反转",
    "代价",
    "线索",
    "盯上",
    "逼近",
)
```

- [ ] **Step 4: Implement `HardFloorResult` and core checks**

Create `forwin/checker/hard_floor.py`:

```python
from __future__ import annotations

import re
from typing import Any

from pydantic import BaseModel, Field

from forwin.config import Config
from forwin.protocol.context import ChapterContextPack
from forwin.protocol.writer import WriterOutput

from .hard_floor_dict import ENDING_HOOK_MARKERS, MODEL_ARTIFACT_MARKERS


_GARBAGE_BLOCK_RE = re.compile(r"[^\u4e00-\u9fff，。！？；：、“”‘’（）《》\sA-Za-z0-9_-]{12,}")


class HardFloorResult(BaseModel):
    passed: bool
    fail_reasons: list[str] = Field(default_factory=list)
    warning_reasons: list[str] = Field(default_factory=list)
    checks: dict[str, bool] = Field(default_factory=dict)
    metadata: dict[str, Any] = Field(default_factory=dict)


def run_hard_floor(
    *,
    writer_output: WriterOutput,
    context_pack: ChapterContextPack,
    repo,
    project_id: str,
    chapter_number: int,
    config: Config,
) -> HardFloorResult:
    fail_reasons: list[str] = []
    warning_reasons: list[str] = []
    checks: dict[str, bool] = {}
    body = str(writer_output.body or "")

    checks["chapter_length"] = len(body) >= int(config.min_chapter_chars or 0)
    if not checks["chapter_length"]:
        fail_reasons.append("chapter_length")

    checks["no_garbage"] = _no_garbage(body)
    if not checks["no_garbage"]:
        fail_reasons.append("no_garbage")

    checks["at_least_one_event"] = bool(
        writer_output.new_events or writer_output.state_changes or writer_output.thread_beats
    )
    if not checks["at_least_one_event"]:
        fail_reasons.append("at_least_one_event")

    hidden_hits = _must_not_reveal_hits(body, context_pack.must_not_reveal)
    checks["must_not_reveal"] = not hidden_hits
    if hidden_hits:
        fail_reasons.append("must_not_reveal")

    checks["ending_hook"] = _has_ending_hook(body)
    if not checks["ending_hook"]:
        warning_reasons.append("ending_hook")

    return HardFloorResult(
        passed=not fail_reasons,
        fail_reasons=fail_reasons,
        warning_reasons=warning_reasons,
        checks=checks,
        metadata={
            "project_id": project_id,
            "chapter_number": int(chapter_number or 0),
            "must_not_reveal_hits": hidden_hits,
        },
    )


def _no_garbage(body: str) -> bool:
    if not body.strip():
        return False
    lowered = body.lower()
    if any(marker.lower() in lowered for marker in MODEL_ARTIFACT_MARKERS):
        return False
    return _GARBAGE_BLOCK_RE.search(body) is None


def _must_not_reveal_hits(body: str, items: list[str]) -> list[str]:
    return [item for item in items if item and item in body]


def _has_ending_hook(body: str) -> bool:
    tail = body[-200:]
    return any(marker in tail for marker in ENDING_HOOK_MARKERS)
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_hard_floor.py -q
```

Expected: all hard floor unit tests pass.

- [ ] **Step 6: Commit**

```bash
git add forwin/checker/hard_floor.py forwin/checker/hard_floor_dict.py tests/test_hard_floor.py
git commit -m "feat: add pulp hard floor checker"
```

## Task 3: Hard Floor Orchestrator Integration

**Files:**
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Test: `tests/test_hard_floor.py`

- [ ] **Step 1: Add integration test for disabled standard behavior**

Append to `tests/test_hard_floor.py` a focused helper test that verifies the new checker does not run when disabled:

```python
def test_disabled_config_is_visible_to_callers():
    disabled = Config(hard_floor_gate_enabled=False)

    assert disabled.hard_floor_gate_enabled is False
```

This keeps the orchestrator behavior simple to verify in later integration tests while Task 3 changes the loop.

- [ ] **Step 2: Import hard floor in project loop**

In `forwin/orchestrator_loop_core/project_chapters.py`, add:

```python
from forwin.checker.hard_floor import run_hard_floor
```

- [ ] **Step 3: Insert gate after review payload extraction**

After `residual_review_issues = self._review_issue_payloads(verdict)` and `canon_risk_level = self._review_canon_risk(verdict)`, insert:

```python
            if bool(getattr(self.config, "hard_floor_gate_enabled", False)):
                hard_floor = run_hard_floor(
                    writer_output=writer_output,
                    context_pack=context,
                    repo=repo,
                    project_id=project_id,
                    chapter_number=chapter_num,
                    config=self.config,
                )
                if not hard_floor.passed:
                    hard_floor_issues = [
                        {
                            "reviewer": "hard_floor",
                            "rule_name": reason,
                            "severity": "error",
                            "message": f"hard floor failed: {reason}",
                        }
                        for reason in hard_floor.fail_reasons
                    ]
                    updater.mark_chapter_status(
                        project_id,
                        chapter_num,
                        "failed",
                        repair_attempt_count=repair_attempt_count,
                        residual_review_issues=[*residual_review_issues, *hard_floor_issues],
                        canon_risk_level="high",
                    )
                    self._record_decision_event(
                        updater=updater,
                        project_id=project_id,
                        chapter_number=chapter_num,
                        event_family="evaluation_verdict",
                        event_type=DecisionEventType.HARD_GATE_HIT,
                        scope="chapter",
                        summary=f"第{chapter_num}章 hard floor 未通过。",
                        reason=";".join(hard_floor.fail_reasons),
                        payload=hard_floor.model_dump(mode="json"),
                    )
                    session.commit()
                    failed_chapters.append(chapter_num)
                    continue
```

- [ ] **Step 4: Run focused tests**

Run:

```bash
pytest tests/test_hard_floor.py -q
```

Expected: all tests pass.

- [ ] **Step 5: Commit**

```bash
git add forwin/orchestrator_loop_core/project_chapters.py tests/test_hard_floor.py
git commit -m "feat: enforce hard floor in chapter loop"
```

## Task 4: Reviewer Hub Bypass Switches

**Files:**
- Modify: `forwin/reviewer/hub.py`
- Modify: `forwin/runtime/container.py`
- Test: `tests/test_pulp_pipeline_bypass.py`

- [ ] **Step 1: Write failing hub bypass tests**

Create `tests/test_pulp_pipeline_bypass.py`:

```python
from __future__ import annotations

from forwin.protocol.context import ChapterContextPack
from forwin.protocol.review import ReviewVerdict
from forwin.protocol.writer import WriterOutput
from forwin.reviewer.hub import HistoricalReviewHub


class DummyChecker:
    def check(self, project_id, writer_output):
        return ReviewVerdict(verdict="pass", issues=[])


class RecordingReviewer:
    def __init__(self):
        self.calls = 0

    def review(self, *args, **kwargs):
        self.calls += 1
        return ReviewVerdict(verdict="pass", issues=[])


def context() -> ChapterContextPack:
    return ChapterContextPack(
        project_id="project-1",
        project_title="测试",
        premise="测试",
        genre="玄幻",
        setting_summary="测试",
        chapter_number=1,
    )


def writer() -> WriterOutput:
    return WriterOutput(
        chapter_number=1,
        title="第一章",
        body="角色A推开门，看见线索。门外忽然传来密令？",
        char_count=24,
        end_of_chapter_summary="角色A发现线索。",
    )


def test_disabled_reviewers_are_not_called():
    experience = RecordingReviewer()
    map_movement = RecordingReviewer()
    personality = RecordingReviewer()
    hub = HistoricalReviewHub(
        experience_reviewer=experience,
        map_movement_reviewer=map_movement,
        personality_reviewer=personality,
        experience_review_enabled=False,
        map_movement_review_enabled=False,
        personality_review_enabled=False,
        canon_quality_review_in_hub_enabled=False,
    )

    verdict = hub.review(
        project_id="project-1",
        repo=None,
        context=context(),
        writer_output=writer(),
        continuity_checker=DummyChecker(),
    )

    assert verdict.verdict == "pass"
    assert experience.calls == 0
    assert map_movement.calls == 0
    assert personality.calls == 0
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_pulp_pipeline_bypass.py::test_disabled_reviewers_are_not_called -q
```

Expected: constructor rejects new keyword arguments.

- [ ] **Step 3: Add hub constructor flags**

In `forwin/reviewer/hub.py`, extend `__init__` and store flags:

```python
        map_movement_review_enabled: bool = True,
        personality_review_enabled: bool = True,
        canon_quality_review_in_hub_enabled: bool = True,
```

Set:

```python
        self.map_movement_review_enabled = bool(map_movement_review_enabled)
        self.personality_review_enabled = bool(personality_review_enabled)
        self.canon_quality_review_in_hub_enabled = bool(canon_quality_review_in_hub_enabled)
```

- [ ] **Step 4: Guard hub-local canon quality**

Change:

```python
        if session is not None:
```

to:

```python
        if session is not None and self.canon_quality_review_in_hub_enabled:
```

- [ ] **Step 5: Guard map movement review**

Wrap `map_movement_reviewer.review()`:

```python
        map_movement = ReviewVerdict(verdict="pass", issues=[])
        if self.map_movement_review_enabled:
            with self.observability.span(
                obs_context,
                "review.map_movement",
                span_kind="reviewer",
                component="reviewer",
            ) as span:
                map_movement = self._call_with_compatible_kwargs(
                    self.map_movement_reviewer.review,
                    review_context,
                    writer_output,
                )
                span.metric("issue_count", len(getattr(map_movement, "issues", []) or []))
```

- [ ] **Step 6: Guard personality review**

Change the personality review condition:

```python
        if self.personality_review_enabled and callable(personality_review_call) and not callable(personality_collect):
```

Keep personality `collect` deterministic lint signals for now unless later tests prove it can trigger LLM.

- [ ] **Step 7: Pass config from runtime container**

In `forwin/runtime/container.py`, update hub construction:

```python
        review_hub = HistoricalReviewHub(
            experience_review_enabled=config.experience_review_enabled,
            lint_review_enabled=config.lint_review_enabled,
            map_movement_review_enabled=config.map_movement_review_enabled,
            personality_review_enabled=config.personality_review_enabled,
            canon_quality_review_in_hub_enabled=config.canon_quality_review_in_hub_enabled,
            llm_client=llm_client if llm_available and config.reviewer_quality_mode != "deterministic" else None,
            llm_enabled=llm_available and config.reviewer_quality_mode != "deterministic",
            observability=observability,
            chapter_review_form_mode=config.chapter_review_form_mode,
        )
```

- [ ] **Step 8: Run tests**

Run:

```bash
pytest tests/test_pulp_pipeline_bypass.py -q
pytest tests/test_reviewer_split.py tests/test_reviewer_personality_consistency.py -q
```

Expected: all listed tests pass.

- [ ] **Step 9: Commit**

```bash
git add forwin/reviewer/hub.py forwin/runtime/container.py tests/test_pulp_pipeline_bypass.py
git commit -m "feat: add pulp reviewer bypass switches"
```

## Task 5: Canon Gate `fatal_only` And Deterministic Analysis

**Files:**
- Modify: `forwin/canon_quality/gate.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Test: `tests/test_pulp_pipeline_bypass.py`

- [ ] **Step 1: Add gate mode tests**

Append to `tests/test_pulp_pipeline_bypass.py`:

```python
from forwin.canon_quality.gate import evaluate_canon_admission, normalize_gate_mode
from forwin.canon_quality.signals import CanonQualitySignal


def canon_signal(signal_type: str, severity: str = "error") -> CanonQualitySignal:
    return CanonQualitySignal(
        signal_id=f"sig-{signal_type}",
        project_id="project-1",
        chapter_number=1,
        signal_type=signal_type,
        severity=severity,
        description="测试信号",
        evidence_refs=["body:证据"],
    )


def test_fatal_only_mode_is_normalized():
    assert normalize_gate_mode("fatal_only") == "fatal_only"


def test_fatal_only_blocks_fatal_signal():
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        signals=[canon_signal("countdown_non_monotonic")],
        mode="fatal_only",
    )

    assert result.commit_allowed is False
    assert result.verdict == "fail"
    assert result.deterministic_issue_refs == ["sig-countdown_non_monotonic"]


def test_fatal_only_does_not_block_warning_signal():
    result = evaluate_canon_admission(
        project_id="project-1",
        chapter_number=1,
        signals=[canon_signal("style_repetition", severity="warning")],
        mode="fatal_only",
    )

    assert result.commit_allowed is True
    assert result.verdict == "warn"
```

- [ ] **Step 2: Run tests and verify failure**

Run:

```bash
pytest tests/test_pulp_pipeline_bypass.py::test_fatal_only_mode_is_normalized -q
```

Expected: normalization returns strict or default instead of `fatal_only`.

- [ ] **Step 3: Extend `GateMode`**

In `forwin/canon_quality/gate.py`:

```python
GateMode = Literal["off", "shadow", "fatal_only", "strict"]
```

Update `normalize_gate_mode()`:

```python
    if normalized in {"off", "shadow", "fatal_only", "strict"}:
        return normalized  # type: ignore[return-value]
```

- [ ] **Step 4: Add fatal signal helper**

Add near the top of `gate.py`:

```python
_FATAL_ONLY_SIGNAL_TYPES = {
    "character_dead_alive",
    "character_teleport",
    "closed_thread_reopened",
    "final_dangling",
    "final_denied",
    "countdown_inconsistent",
    "countdown_non_monotonic",
    "terminal_state_active_conflict",
    "form_countdown_inconsistency",
    "form_final_chapter_unresolved",
}


def _fatal_only_blocking(signals: list[CanonQualitySignal]) -> list[CanonQualitySignal]:
    return [
        signal
        for signal in signals
        if signal.status == "open"
        and signal.severity == "error"
        and str(signal.signal_type) in _FATAL_ONLY_SIGNAL_TYPES
        and bool(signal.evidence_refs)
    ]
```

- [ ] **Step 5: Add fatal_only branch**

In `evaluate_canon_admission()`, compute:

```python
    fatal_blocking = _fatal_only_blocking(quality_signals)
```

Add branch between `shadow` and `strict`:

```python
    elif resolved_mode == "fatal_only":
        commit_allowed = (
            not fatal_blocking
            and not form_blocking_refs
            and not review_failed
            and open_terminal_obligation_count <= 0
            and not obligation_reasons
        )
        admission_mode = (
            "blocked"
            if not commit_allowed
            else ("with_obligation" if active_obligations else "clean")
        )
        verdict = "fail" if not commit_allowed else (
            "warn" if warnings or active_obligations or str(review_verdict) == "warn" else "pass"
        )
        blocking = fatal_blocking
        deterministic_refs = [signal.signal_id for signal in fatal_blocking]
        summary = (
            f"canon quality gate fatal_only: commit_allowed={commit_allowed}, "
            f"fatal_blocking={len(fatal_blocking)}, warnings={len(warnings)}, "
            f"open_obligations={open_terminal_obligation_count}, "
            f"narrative_obligations={len(active_obligations)}"
        )
```

- [ ] **Step 6: Force deterministic analyzer for off and fatal_only**

In `forwin/orchestrator_loop_core/quality_gates.py`, before `analyze_writer_output_quality()`:

```python
    gate_mode = str(getattr(self.config, "canon_quality_gate", "strict") or "strict")
    gate_llm_client = None if gate_mode in {"off", "fatal_only"} else self.llm_client
```

Then pass:

```python
        llm_client=gate_llm_client,
```

- [ ] **Step 7: Run tests**

Run:

```bash
pytest tests/test_pulp_pipeline_bypass.py -q
pytest tests/test_canon_quality_service.py tests/test_temporal_semantics.py -q
```

Expected: tests pass.

- [ ] **Step 8: Commit**

```bash
git add forwin/canon_quality/gate.py forwin/orchestrator_loop_core/quality_gates.py tests/test_pulp_pipeline_bypass.py
git commit -m "feat: add fatal-only canon gate mode"
```

## Task 6: BookState Extraction Layers

**Files:**
- Modify: `forwin/extractor/book_state_graph_delta.py`
- Modify: `forwin/orchestrator_loop_core/world_projection.py`
- Test: `tests/test_pulp_pipeline_bypass.py`

- [ ] **Step 1: Add extractor layer test**

Append to `tests/test_pulp_pipeline_bypass.py`:

```python
from forwin.extractor.book_state_graph_delta import _filter_graph_delta_layers
from forwin.protocol.book_state import GraphDelta, GraphDeltaType, MapPatch, CognitionPatch, NarrativePatch


def test_world_only_layer_filter_removes_non_world_patches():
    delta = GraphDelta(
        id="delta-1",
        project_id="project-1",
        chapter_number=1,
        delta_type=GraphDeltaType.WORLD_STATE,
        summary="测试 delta",
        map_patches=[MapPatch(target_type="location", target_id="loc-a", op="set", field_path="x", new_value="y")],
        cognition_patches=[CognitionPatch(observer_type="character", observer_id="a", op="set", field_path="belief", new_value="b")],
        narrative_patches=[NarrativePatch(target_ref="thread:a", op="set", field_path="status", new_value="active")],
    )

    filtered = _filter_graph_delta_layers([delta], {"world"})

    assert filtered[0].map_patches == []
    assert filtered[0].cognition_patches == []
    assert filtered[0].narrative_patches == []
    assert filtered[0].metadata["requested_book_state_layers"] == ["world"]
    assert filtered[0].metadata["filtered_patch_counts"]["map"] == 1
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_pulp_pipeline_bypass.py::test_world_only_layer_filter_removes_non_world_patches -q
```

Expected: import error for `_filter_graph_delta_layers`.

- [ ] **Step 3: Add layer-aware extractor constructor**

In `forwin/extractor/book_state_graph_delta.py`:

```python
class BookStateGraphDeltaExtractor:
    def __init__(self, *, layers: set[str] | None = None) -> None:
        self.layers = set(layers or {"world", "map", "cognition", "narrative"})
```

- [ ] **Step 4: Add filter helper**

Add:

```python
def _filter_graph_delta_layers(graph_deltas, layers: set[str]):
    requested = sorted(str(layer) for layer in layers)
    filtered = []
    for delta in graph_deltas:
        update = {}
        counts = {
            "map": len(delta.map_patches),
            "cognition": len(delta.cognition_patches),
            "narrative": len(delta.narrative_patches),
        }
        if "map" not in layers:
            update["map_patches"] = []
        if "cognition" not in layers:
            update["cognition_patches"] = []
        if "narrative" not in layers:
            update["narrative_patches"] = []
        update["metadata"] = {
            **dict(delta.metadata),
            "requested_book_state_layers": requested,
            "filtered_patch_counts": {
                key: value
                for key, value in counts.items()
                if key not in layers and value
            },
        }
        filtered.append(delta.model_copy(update=update))
    return filtered
```

- [ ] **Step 5: Apply filter after adapter output**

After the local `graph_deltas` list is built from adapter output, add:

```python
        graph_deltas = _filter_graph_delta_layers(graph_deltas, self.layers)
```

- [ ] **Step 6: Pass config layers from world projection**

In `forwin/orchestrator_loop_core/world_projection.py`, find `BookStateGraphDeltaExtractor()` and change to:

```python
    extractor = BookStateGraphDeltaExtractor(
        layers=set(getattr(self.config, "book_state_layers", ["world", "map", "cognition", "narrative"]) or [])
    )
```

Use the local variable in the existing extraction call.

- [ ] **Step 7: Run tests**

Run:

```bash
pytest tests/test_pulp_pipeline_bypass.py::test_world_only_layer_filter_removes_non_world_patches -q
pytest tests/test_map_world_integration.py -q
```

Expected: tests pass.

- [ ] **Step 8: Commit**

```bash
git add forwin/extractor/book_state_graph_delta.py forwin/orchestrator_loop_core/world_projection.py tests/test_pulp_pipeline_bypass.py
git commit -m "feat: support pulp bookstate extraction layers"
```

## Task 7: Context Recency Gate

**Files:**
- Create: `forwin/context/gates/__init__.py`
- Create: `forwin/context/gates/recency_truncate.py`
- Modify: `forwin/context/assembler_core/assembler.py`
- Modify: `forwin/runtime/container.py`
- Test: `tests/test_context_recency_truncation.py`

- [ ] **Step 1: Write failing recency gate tests**

Create `tests/test_context_recency_truncation.py`:

```python
from __future__ import annotations

from types import SimpleNamespace

from forwin.context.gates.recency_truncate import RecencyTruncateGate


class Draft:
    def __init__(self, data):
        self.data = data


def request(chapter_number: int):
    return SimpleNamespace(chapter_plan=SimpleNamespace(chapter_number=chapter_number))


def item(chapter_number: int, label: str):
    return SimpleNamespace(chapter_number=chapter_number, label=label)


def test_window_zero_is_noop():
    draft = Draft({"summaries": [item(1, "old")]})

    issues = RecencyTruncateGate(window_chapters=0).validate(request(100), draft)

    assert issues == []
    assert [entry.label for entry in draft.data["summaries"]] == ["old"]


def test_trims_items_older_than_window():
    draft = Draft({"summaries": [item(40, "old"), item(75, "recent")]})

    RecencyTruncateGate(window_chapters=50).validate(request(100), draft)

    assert [entry.label for entry in draft.data["summaries"]] == ["recent"]


def test_entity_ranking_keeps_recent_and_important_items():
    draft = Draft(
        {
            "entities": [
                {"id": "old", "last_seen_chapter": 10, "importance": 1},
                {"id": "important", "last_seen_chapter": 30, "importance": 99},
                {"id": "recent", "last_seen_chapter": 98, "importance": 1},
            ]
        }
    )

    RecencyTruncateGate(window_chapters=50, max_entities=2).validate(request(100), draft)

    assert [entry["id"] for entry in draft.data["entities"]] == ["recent", "important"]
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_context_recency_truncation.py -q
```

Expected: import error for `forwin.context.gates`.

- [ ] **Step 3: Add gate package**

Create `forwin/context/gates/__init__.py`:

```python
from .recency_truncate import RecencyTruncateGate

__all__ = ["RecencyTruncateGate"]
```

- [ ] **Step 4: Implement recency gate**

Create `forwin/context/gates/recency_truncate.py`:

```python
from __future__ import annotations

from typing import Any


class RecencyTruncateGate:
    name = "recency_truncate"

    def __init__(self, window_chapters: int = 0, max_entities: int = 0):
        self.window = max(0, int(window_chapters or 0))
        self.max_entities = max(0, int(max_entities or 0))

    def validate(self, request, draft) -> list:
        if self.window <= 0:
            return []
        current = int(getattr(getattr(request, "chapter_plan", None), "chapter_number", 0) or 0)
        cutoff = current - self.window
        for key in ("summaries", "recent_state_changes", "recent_thread_beats", "recent_events"):
            draft.data[key] = [
                item for item in (draft.data.get(key) or [])
                if _chapter_number(item) >= cutoff
            ]
        if self.max_entities:
            draft.data["entities"] = self._rank_entities(draft.data.get("entities") or [], cutoff)
        return []

    def _rank_entities(self, entities: list[Any], cutoff: int) -> list[Any]:
        ranked = sorted(
            entities,
            key=lambda item: (
                _last_seen(item) < cutoff,
                -_last_seen(item),
                -_importance(item),
                _entity_id(item),
            ),
        )
        return ranked[: self.max_entities]


def _chapter_number(item: Any) -> int:
    if isinstance(item, dict):
        return int(item.get("chapter_number") or item.get("last_seen_chapter") or 0)
    return int(getattr(item, "chapter_number", 0) or getattr(item, "last_seen_chapter", 0) or 0)


def _last_seen(item: Any) -> int:
    if isinstance(item, dict):
        return int(item.get("last_seen_chapter") or item.get("chapter_number") or 0)
    return int(getattr(item, "last_seen_chapter", 0) or getattr(item, "chapter_number", 0) or 0)


def _importance(item: Any) -> int:
    if isinstance(item, dict):
        return int(item.get("importance") or 0)
    return int(getattr(item, "importance", 0) or 0)


def _entity_id(item: Any) -> str:
    if isinstance(item, dict):
        return str(item.get("id") or item.get("name") or "")
    return str(getattr(item, "id", "") or getattr(item, "name", "") or "")
```

- [ ] **Step 5: Add config-aware assembler construction**

In `forwin/context/assembler_core/assembler.py`, keep the constructor compatible. No global config import:

```python
    def __init__(self, *, providers: list | None = None, gates: list | None = None, observability=None) -> None:
        self.providers = providers or self._default_providers()
        self.gates = gates if gates is not None else self._default_gates()
        self.observability = observability or NullObservability()
```

In `forwin/runtime/container.py`, import `RecencyTruncateGate` and construct:

```python
            context_assembler=ChapterContextAssembler(
                gates=[
                    *ChapterContextAssembler._default_gates(),
                    RecencyTruncateGate(
                        window_chapters=config.context_recency_window_chapters,
                        max_entities=config.retrieval_max_entities,
                    ),
                ],
                observability=observability,
            ),
```

- [ ] **Step 6: Run tests**

Run:

```bash
pytest tests/test_context_recency_truncation.py tests/test_context_provider_chain.py -q
```

Expected: tests pass.

- [ ] **Step 7: Commit**

```bash
git add forwin/context/gates forwin/context/assembler_core/assembler.py forwin/runtime/container.py tests/test_context_recency_truncation.py
git commit -m "feat: add context recency truncation gate"
```

## Task 8: Trope Schema And Markdown Loader

**Files:**
- Modify: `forwin/protocol/trope_library.py`
- Create: `forwin/protocol/trope_md_loader.py`
- Test: `tests/test_trope_schema_compat.py`
- Test: `tests/test_trope_md_loader.py`
- Track: `Design-docs/trope_library_pulp_v1.md`

- [ ] **Step 1: Write schema compatibility test**

Create `tests/test_trope_schema_compat.py`:

```python
from __future__ import annotations

from forwin.protocol.trope_library import TROPE_TEMPLATE_LIBRARY, TropeTemplate


def test_existing_seed_templates_get_new_defaults():
    template = TROPE_TEMPLATE_LIBRARY[0]

    assert isinstance(template, TropeTemplate)
    assert template.market_tier == "mainstream"
    assert template.cost_weight == 2
    assert template.desire_setup == ""
    assert template.anti_patterns == []
```

- [ ] **Step 2: Write markdown loader test**

Create `tests/test_trope_md_loader.py`:

```python
from __future__ import annotations

from pathlib import Path

from forwin.protocol.trope_md_loader import load_trope_templates_from_md


def test_loads_pulp_markdown_library():
    templates = load_trope_templates_from_md(Path("Design-docs/trope_library_pulp_v1.md"))
    by_id = {template.template_id: template for template in templates}

    assert "power-level-up" in by_id
    assert len(templates) >= 8
    assert by_id["power-level-up"].category == "power"
    assert by_id["power-level-up"].subcategory == "升级"
    assert by_id["power-level-up"].market_tier == "sinking"
    assert by_id["power-level-up"].cost_weight == 1
    assert "写出主角当前的具体限制" in by_id["power-level-up"].desire_setup
    assert "反派或环境" in by_id["power-level-up"].resistance
    assert "具体变化" in by_id["power-level-up"].payoff
    assert "三层反应" in by_id["power-level-up"].aftermath
    assert by_id["power-level-up"].anti_patterns
    assert by_id["power-level-up"].review_signals
```

- [ ] **Step 3: Run tests and verify failure**

Run:

```bash
pytest tests/test_trope_schema_compat.py tests/test_trope_md_loader.py -q
```

Expected: schema defaults are missing and loader import fails.

- [ ] **Step 4: Extend `TropeTemplate`**

In `forwin/protocol/trope_library.py`, extend the model:

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

Add `Literal` import if the file does not already import it.

- [ ] **Step 5: Implement markdown loader**

Create `forwin/protocol/trope_md_loader.py` with:

```python
from __future__ import annotations

import re
from pathlib import Path

from forwin.protocol.trope_library import TropeTemplate


_TITLE_RE = re.compile(r"^##\s+([A-Za-z0-9_-]+)\s+·\s+(.+)$")
_PROP_RE = re.compile(r"^-\s+\*\*([^*]+)\*\*:\s*(.*)$")


def load_trope_templates_from_md(path: str | Path) -> list[TropeTemplate]:
    text = Path(path).read_text(encoding="utf-8")
    sections = _template_sections(text)
    templates = [_parse_template_section(title, body) for title, body in sections]
    return templates


def _template_sections(text: str) -> list[tuple[str, str]]:
    lines = text.splitlines()
    sections: list[tuple[str, list[str]]] = []
    current_title = ""
    current_lines: list[str] = []
    for line in lines:
        if line.startswith("## "):
            match = _TITLE_RE.match(line)
            if match:
                if current_title:
                    sections.append((current_title, current_lines))
                current_title = line
                current_lines = []
                continue
        if current_title:
            current_lines.append(line)
    if current_title:
        sections.append((current_title, current_lines))
    return [(title, "\n".join(body)) for title, body in sections]


def _parse_template_section(title: str, body: str) -> TropeTemplate:
    title_match = _TITLE_RE.match(title)
    if title_match is None:
        raise ValueError(f"invalid trope title: {title}")
    payload: dict[str, object] = {
        "template_id": title_match.group(1).strip(),
        "display_name": title_match.group(2).strip(),
    }
    current_heading = ""
    buckets: dict[str, list[str]] = {}
    for raw_line in body.splitlines():
        line = raw_line.rstrip()
        prop_match = _PROP_RE.match(line)
        if prop_match and not current_heading:
            key = prop_match.group(1).strip()
            value = prop_match.group(2).strip()
            payload[key] = _parse_property(key, value)
            continue
        if line.startswith("### "):
            current_heading = line.removeprefix("### ").strip()
            buckets[current_heading] = []
            continue
        if current_heading:
            buckets[current_heading].append(line)
    payload["desire_setup"] = _section_text(buckets, "欲望建立")
    payload["resistance"] = _section_text(buckets, "阻力加压")
    payload["payoff"] = _section_text(buckets, "爽点兑现")
    payload["aftermath"] = _section_text(buckets, "余波钩子")
    payload["anti_patterns"] = _bullet_list(buckets, "anti_patterns")
    payload["review_signals"] = _bullet_list(buckets, "review_signals")
    return TropeTemplate.model_validate(payload)


def _parse_property(key: str, value: str) -> object:
    if key == "cost_weight":
        return int(value or 2)
    if key in {"best_window", "genre_fit"}:
        return [item.strip() for item in value.split(",") if item.strip()]
    return value


def _section_text(buckets: dict[str, list[str]], key: str) -> str:
    return "\n".join(line for line in buckets.get(key, []) if line.strip()).strip()


def _bullet_list(buckets: dict[str, list[str]], key: str) -> list[str]:
    result: list[str] = []
    for line in buckets.get(key, []):
        stripped = line.strip()
        if stripped.startswith("- "):
            result.append(stripped.removeprefix("- ").strip())
    return result
```

- [ ] **Step 6: Support markdown override path**

In `forwin/protocol/trope_library.py`, update `load_trope_template_library()`:

```python
    if override_path:
        path = Path(override_path)
        if path.suffix.lower() == ".md":
            from forwin.protocol.trope_md_loader import load_trope_templates_from_md

            return load_trope_templates_from_md(path)
        return load_trope_template_file(path, require_full=True)
```

Remove silent fallback for override failures so broken configured libraries fail visibly.

- [ ] **Step 7: Run tests**

Run:

```bash
pytest tests/test_trope_schema_compat.py tests/test_trope_md_loader.py -q
```

Expected: tests pass.

- [ ] **Step 8: Commit**

```bash
git add forwin/protocol/trope_library.py forwin/protocol/trope_md_loader.py tests/test_trope_schema_compat.py tests/test_trope_md_loader.py Design-docs/trope_library_pulp_v1.md
git commit -m "feat: load pulp trope markdown library"
```

## Task 9: Trope Selector Cost And Dedup

**Files:**
- Modify: `forwin/experience/band_scheduler.py`
- Modify: `forwin/orchestrator/phase24.py`
- Modify: `forwin/planning/band_plan_service.py`
- Test: `tests/test_trope_selector.py`

- [ ] **Step 1: Write failing selector tests**

Create `tests/test_trope_selector.py`:

```python
from __future__ import annotations

from forwin.experience.band_scheduler import BandExperienceScheduler
from forwin.experience.service import AudienceCalibrationProfile
from forwin.experience.types import ArcExperienceBundle
from forwin.models.project import ChapterPlan
from forwin.planning.arc_structure_service import ArcStructureDraftData
from forwin.protocol.experience import ArcPayoffMap, ReaderPromise


def test_cost_ceiling_prefers_low_cost_templates():
    schedule = BandExperienceScheduler().derive_band_delight_schedule(
        band_id="band-1",
        chapter_start=1,
        chapter_end=3,
        structure=ArcStructureDraftData(
            phase_layout=[],
            key_beats=["问题一", "问题二", "问题三"],
            thread_priorities=[],
            hotspot_candidates=[],
            compression_candidates=[],
        ),
        arc_experience=ArcExperienceBundle(
            reader_promise=ReaderPromise(core_pleasures=["升级"]),
            arc_payoff_map=ArcPayoffMap(),
        ),
        active_band=[ChapterPlan(project_id="project-1", chapter_number=1, one_line="开局")],
        calibration=AudienceCalibrationProfile(),
        cost_ceiling=1,
    )

    assert schedule.scheduled_rewards
    assert all(item.template_id for item in schedule.scheduled_rewards)
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_trope_selector.py -q
```

Expected: `derive_band_delight_schedule()` rejects `cost_ceiling`.

- [ ] **Step 3: Add scheduler argument and deterministic selection**

In `forwin/experience/band_scheduler.py`, add `cost_ceiling: int = 3` to the method signature and update `template_for()`:

```python
        used_template_ids: set[str] = set()

        def template_for(category: str, fallback_index: int) -> str:
            macro = macro_by_category.get(category)
            if macro is not None and macro.template_id and macro.template_id not in used_template_ids:
                used_template_ids.add(macro.template_id)
                return macro.template_id
            template_candidates = [
                template
                for template in trope_templates_by_category(category)
                if int(getattr(template, "cost_weight", 2) or 2) <= int(cost_ceiling or 3)
                and template.template_id not in used_template_ids
            ]
            if not template_candidates:
                template_candidates = [
                    template
                    for template in trope_templates_by_category(category)
                    if template.template_id not in used_template_ids
                ]
            if not template_candidates:
                template_candidates = list(TROPE_TEMPLATE_LIBRARY)
            template_candidates.sort(key=lambda item: (int(getattr(item, "cost_weight", 2) or 2), item.template_id))
            selected = template_candidates[fallback_index % len(template_candidates)].template_id
            used_template_ids.add(selected)
            return selected
```

- [ ] **Step 4: Add config-aware cost ceiling in call sites**

In `forwin/orchestrator/phase24.py`, extend `_derive_band_delight_schedule()` to accept `cost_ceiling: int = 3` and pass it through. Where it is called from services that have config, pass `2` for pulp and `3` otherwise.

In `forwin/planning/band_plan_service.py`, add optional constructor arg `trope_cost_ceiling: int = 3`, store it, and pass it to scheduler:

```python
        schedule = self.scheduler.derive_band_delight_schedule(
            band_id=window.band_id,
            chapter_start=window.chapter_start,
            chapter_end=window.chapter_end,
            structure=request.structure,
            arc_experience=request.arc_experience,
            active_band=window.active_band,
            calibration=calibration,
            cost_ceiling=self.trope_cost_ceiling,
        )
```

In `forwin/runtime/container.py`, construct `BandPlanService` with:

```python
            trope_cost_ceiling=2 if config.quality_profile == "pulp" else 3,
```

- [ ] **Step 5: Run tests**

Run:

```bash
pytest tests/test_trope_selector.py tests/test_experience_planning_service.py tests/test_audience_feedback_alignment.py -q
```

Expected: tests pass.

- [ ] **Step 6: Commit**

```bash
git add forwin/experience/band_scheduler.py forwin/orchestrator/phase24.py forwin/planning/band_plan_service.py forwin/runtime/container.py tests/test_trope_selector.py
git commit -m "feat: prefer low-cost pulp tropes"
```

## Task 10: Writer Prompt Trope Injection

**Files:**
- Modify: `forwin/writer/prompt_core/sections.py`
- Test: `tests/test_trope_prompt_injection.py`

- [ ] **Step 1: Write failing prompt test**

Create `tests/test_trope_prompt_injection.py`:

```python
from __future__ import annotations

from forwin.protocol.experience import ChapterExperiencePlan
from forwin.protocol.context import ChapterContextPack
from forwin.protocol import trope_library
from forwin.protocol.trope_library import TropeTemplate
from forwin.writer.prompt_core.sections import _experience_section


def test_selected_template_injects_four_part_instruction(monkeypatch):
    monkeypatch.setattr(
        trope_library,
        "trope_template_index",
        lambda: {
            "power-level-up": TropeTemplate(
                template_id="power-level-up",
                display_name="升级",
                category="power",
                desire_setup="写出限制",
                resistance="写出阻力",
                payoff="写出兑现",
                aftermath="写出钩子",
                anti_patterns=["禁止空泛震惊"],
            )
        },
    )
    context = ChapterContextPack(
        project_id="project-1",
        project_title="测试",
        premise="测试",
        genre="玄幻",
        setting_summary="测试",
        chapter_number=1,
        chapter_experience_plan=ChapterExperiencePlan(
            planned_reward_tags=["power"],
            selected_template_ids=["power-level-up"],
        ),
    )

    text = _experience_section(context)

    assert "本章爽点指令" in text
    assert "欲望建立" in text
    assert "阻力加压" in text
    assert "爽点兑现" in text
    assert "余波钩子" in text
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_trope_prompt_injection.py -q
```

Expected: prompt contains only selected template IDs.

- [ ] **Step 3: Add helper to render selected templates**

In `forwin/writer/prompt_core/sections.py`, add near the experience section helpers:

```python
def _selected_trope_instruction_lines(template_ids: list[str]) -> list[str]:
    if not template_ids:
        return []
    from forwin.protocol.trope_library import trope_template_index

    library = trope_template_index()
    lines = ["  · 本章爽点指令（按段落执行）:"]
    for template_id in template_ids[:2]:
        template = library.get(template_id)
        if template is None:
            continue
        lines.append(f"    [{template.display_name or template.template_id}]")
        if template.desire_setup:
            lines.append(f"    1. 欲望建立：{_clip_trope_text(template.desire_setup)}")
        if template.resistance:
            lines.append(f"    2. 阻力加压：{_clip_trope_text(template.resistance)}")
        if template.payoff:
            lines.append(f"    3. 爽点兑现：{_clip_trope_text(template.payoff)}")
        if template.aftermath:
            lines.append(f"    4. 余波钩子：{_clip_trope_text(template.aftermath)}")
        if template.anti_patterns:
            lines.append(f"    禁止：{'；'.join(template.anti_patterns[:3])}")
    return lines if len(lines) > 1 else []


def _clip_trope_text(text: str, limit: int = 260) -> str:
    clean = " ".join(str(text or "").split())
    if len(clean) <= limit:
        return clean
    return clean[:limit].rstrip() + "。"
```

- [ ] **Step 4: Replace selected template ID line**

Change:

```python
        if plan.selected_template_ids:
            lines.append(f"  · 选用模板：{'、'.join(plan.selected_template_ids)}")
```

to:

```python
        if plan.selected_template_ids:
            lines.extend(_selected_trope_instruction_lines(list(plan.selected_template_ids)))
```

- [ ] **Step 5: Run tests**

Run:

```bash
FORWIN_TROPE_TEMPLATE_PATH=Design-docs/trope_library_pulp_v1.md pytest tests/test_trope_prompt_injection.py tests/test_trope_md_loader.py -q
```

Expected: tests pass.

- [ ] **Step 6: Commit**

```bash
git add forwin/writer/prompt_core/sections.py tests/test_trope_prompt_injection.py
git commit -m "feat: inject trope instructions into writer prompt"
```

## Task 11: Pressure Test Script

**Files:**
- Create: `scripts/pulp_pressure_test.py`
- Test: `tests/test_pulp_pressure_test.py`

- [ ] **Step 1: Write report calculation tests**

Create `tests/test_pulp_pressure_test.py`:

```python
from __future__ import annotations

from scripts.pulp_pressure_test import (
    ChapterMetric,
    compute_summary,
    reward_gap_since_last,
)


def test_reward_gap_since_last_counts_missing_rewards():
    rows = [
        ChapterMetric(chapter_number=1, reward_beats_in_plan=1),
        ChapterMetric(chapter_number=2, reward_beats_in_plan=0),
        ChapterMetric(chapter_number=3, reward_beats_in_plan=0),
        ChapterMetric(chapter_number=4, reward_beats_in_plan=1),
    ]

    assert [reward_gap_since_last(rows[: index + 1]) for index in range(len(rows))] == [0, 1, 2, 0]


def test_compute_summary_distinguishes_null_from_zero():
    rows = [
        ChapterMetric(chapter_number=1, llm_call_count=2, prompt_char_count=1000, wall_time_seconds=10),
        ChapterMetric(chapter_number=2, llm_call_count=0, prompt_char_count=None, wall_time_seconds=20),
    ]

    summary = compute_summary(rows)

    assert summary["average_llm_call_count"] == 1
    assert "prompt_char_count" in summary["missing_metric_sources"]
    assert summary["average_wall_time_seconds"] == 15
```

- [ ] **Step 2: Run test and verify failure**

Run:

```bash
pytest tests/test_pulp_pressure_test.py -q
```

Expected: import error for `scripts.pulp_pressure_test`.

- [ ] **Step 3: Implement script models and calculations**

Create `scripts/pulp_pressure_test.py`:

```python
from __future__ import annotations

import argparse
import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from statistics import mean


@dataclass
class ChapterMetric:
    chapter_number: int
    wall_time_seconds: float | None = None
    llm_call_count: int | None = None
    output_token_count: int | None = None
    prompt_char_count: int | None = None
    context_pack_char_count: int | None = None
    hard_floor_passed: bool | None = None
    hard_floor_fail_reasons: list[str] | None = None
    reward_beats_in_plan: int | None = None
    reward_gap_since_last: int | None = None
    selected_trope_ids: list[str] | None = None
    ending_hook_detected: bool | None = None
    chapter_length: int | None = None
    bookstate_compile_succeeded: bool | None = None
    rewrite_count: int | None = None
    verdict: str | None = None


def reward_gap_since_last(rows: list[ChapterMetric]) -> int | None:
    if not rows:
        return None
    current = rows[-1]
    if int(current.reward_beats_in_plan or 0) > 0:
        return 0
    gap = 0
    for row in reversed(rows[:-1]):
        gap += 1
        if int(row.reward_beats_in_plan or 0) > 0:
            return gap
    return gap


def compute_summary(rows: list[ChapterMetric]) -> dict[str, object]:
    missing = sorted(
        field
        for field in ChapterMetric.__dataclass_fields__
        if any(getattr(row, field) is None for row in rows)
    )
    llm_values = [row.llm_call_count for row in rows if row.llm_call_count is not None]
    wall_values = [row.wall_time_seconds for row in rows if row.wall_time_seconds is not None]
    return {
        "chapter_count": len(rows),
        "average_llm_call_count": mean(llm_values) if llm_values else None,
        "average_wall_time_seconds": mean(wall_values) if wall_values else None,
        "missing_metric_sources": missing,
    }


def write_reports(rows: list[ChapterMetric], output: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    metrics_path = output / "metrics.csv"
    with metrics_path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=list(ChapterMetric.__dataclass_fields__))
        writer.writeheader()
        for row in rows:
            payload = asdict(row)
            payload["hard_floor_fail_reasons"] = json.dumps(payload["hard_floor_fail_reasons"] or [], ensure_ascii=False)
            payload["selected_trope_ids"] = json.dumps(payload["selected_trope_ids"] or [], ensure_ascii=False)
            writer.writerow(payload)
    summary = compute_summary(rows)
    (output / "summary.json").write_text(json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "README.md").write_text(_readme(summary), encoding="utf-8")


def _readme(summary: dict[str, object]) -> str:
    lines = ["# Pulp Pressure Test Report", ""]
    for key, value in summary.items():
        lines.append(f"- `{key}`: {value}")
    return "\n".join(lines) + "\n"


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--project-id", required=True)
    parser.add_argument("--chapters", type=int, default=30)
    parser.add_argument("--output", required=True)
    args = parser.parse_args()
    rows = [ChapterMetric(chapter_number=index) for index in range(1, args.chapters + 1)]
    write_reports(rows, Path(args.output))


if __name__ == "__main__":
    main()
```

- [ ] **Step 4: Run tests**

Run:

```bash
pytest tests/test_pulp_pressure_test.py -q
```

Expected: tests pass.

- [ ] **Step 5: Run script smoke**

Run:

```bash
python scripts/pulp_pressure_test.py --project-id smoke-project --chapters 2 --output reports/pulp_test_smoke
```

Expected: `reports/pulp_test_smoke/metrics.csv`, `summary.json`, and `README.md` are created.

- [ ] **Step 6: Commit**

```bash
git add scripts/pulp_pressure_test.py tests/test_pulp_pressure_test.py
git commit -m "feat: add pulp pressure report script"
```

## Task 12: Documentation And Source Docs

**Files:**
- Modify: `Design-docs/CURRENT_ARCHITECTURE.md`
- Modify: `Design-docs/DESIGN_STATUS.md`
- Modify: `README.md`
- Track: `Design-docs/pulp_profile_upgrade_plan.md`
- Track: `Design-docs/trope_library_pulp_v1.md`

- [ ] **Step 1: Update architecture doc**

Add a `Quality Profile` section to `Design-docs/CURRENT_ARCHITECTURE.md`:

```markdown
## Quality Profile

ForWin supports `quality_profile=standard|pulp|premium`.

`standard` is the default and preserves the existing long-form quality path.
`pulp` derives a low-cost runtime profile from config: single-call writer mode, deterministic review, fatal-only canon admission, hard floor checks, world-only BookState extraction, context recency truncation, and low-cost trope selection.
`premium` is reserved for future defaults and currently behaves like standard unless explicit config fields override it.
```

- [ ] **Step 2: Update design status**

Add an entry in `Design-docs/DESIGN_STATUS.md` marking the pulp profile upgrade as active current implementation work and linking:

```markdown
- `Design-docs/pulp_profile_upgrade_plan.md` — active-current; implementation tracked by `docs/superpowers/specs/2026-05-18-pulp-profile-upgrade-design.md` and `docs/superpowers/plans/2026-05-19-pulp-profile-upgrade.md`.
```

- [ ] **Step 3: Update README configuration section**

Add:

```markdown
- `FORWIN_QUALITY_PROFILE`: `standard`, `pulp`, or `premium`. Defaults to `standard`. `pulp` applies low-cost defaults unless the same field is explicitly configured by env.
- `FORWIN_TROPE_TEMPLATE_PATH`: optional JSON or markdown trope template library path. Markdown libraries use the section format in `Design-docs/trope_library_pulp_v1.md`.
```

- [ ] **Step 4: Run doc hygiene**

Run:

```bash
git diff --check
```

Expected: no output.

- [ ] **Step 5: Commit**

```bash
git add Design-docs/CURRENT_ARCHITECTURE.md Design-docs/DESIGN_STATUS.md README.md Design-docs/pulp_profile_upgrade_plan.md Design-docs/trope_library_pulp_v1.md
git commit -m "docs: document pulp quality profile"
```

## Task 13: Integration Verification

**Files:**
- No new files.
- Verify all files touched by Tasks 1-12.

- [ ] **Step 1: Run focused test suite**

Run:

```bash
pytest tests/test_quality_profile.py -q
pytest tests/test_hard_floor.py -q
pytest tests/test_pulp_pipeline_bypass.py -q
pytest tests/test_context_recency_truncation.py -q
pytest tests/test_trope_schema_compat.py tests/test_trope_md_loader.py -q
pytest tests/test_trope_selector.py tests/test_trope_prompt_injection.py -q
pytest tests/test_pulp_pressure_test.py -q
```

Expected: every command passes.

- [ ] **Step 2: Run related regression tests**

Run:

```bash
pytest tests/test_canon_quality_service.py tests/test_temporal_semantics.py -q
pytest tests/test_context_provider_chain.py tests/test_experience_planning_service.py -q
pytest tests/test_reviewer_split.py tests/test_reviewer_personality_consistency.py -q
pytest tests/test_map_world_integration.py -q
```

Expected: every command passes.

- [ ] **Step 3: Run full test suite**

Run:

```bash
pytest -q
```

Expected: passes. If a pre-existing unrelated failure appears, capture the exact failing test and rerun it on `origin/master` before calling it unrelated.

- [ ] **Step 4: Verify profile config manually**

Run:

```bash
FORWIN_QUALITY_PROFILE=pulp python - <<'PY'
from forwin.config import Config
config = Config.from_env()
print(config.quality_profile)
print(config.writer_mode)
print(config.canon_quality_gate)
print(config.book_state_layers)
print(config.hard_floor_gate_enabled)
PY
```

Expected output:

```text
pulp
single
fatal_only
['world']
True
```

- [ ] **Step 5: Verify trope markdown loading manually**

Run:

```bash
FORWIN_TROPE_TEMPLATE_PATH=Design-docs/trope_library_pulp_v1.md python - <<'PY'
from forwin.protocol.trope_library import load_trope_template_library
templates = load_trope_template_library()
print(len(templates))
print(templates[0].template_id)
print(bool(templates[0].desire_setup))
PY
```

Expected: first line is at least `8`, third line is `True`.

- [ ] **Step 6: Verify pressure report smoke**

Run:

```bash
python scripts/pulp_pressure_test.py --project-id smoke-project --chapters 2 --output reports/pulp_test_smoke
test -f reports/pulp_test_smoke/metrics.csv
test -f reports/pulp_test_smoke/summary.json
test -f reports/pulp_test_smoke/README.md
```

Expected: all commands pass.

- [ ] **Step 7: Commit verification follow-up only if files changed**

If verification required fixes, commit them:

```bash
git add forwin tests scripts Design-docs README.md
git commit -m "fix: stabilize pulp profile integration"
```

If no files changed, do not create a commit.
