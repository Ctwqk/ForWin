# P2/P3 Long-Run Correctness Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement P2/P3 long-run correctness repairs: lower pulp single-mode writer call cost, track-aware pulp beat verification, materialize cleanup, evidence-aware macro status, and resume-safe arc boundary audits.

**Architecture:** Keep existing public APIs stable where possible. Add deterministic local/deferred behavior in writer and pulp beat modules, then use existing BookState and future-plan audit storage boundaries for macro evidence and boundary audit markers.

**Tech Stack:** Python 3.13, Pydantic, SQLAlchemy ORM, pytest, existing ForWin orchestration and audit modules.

---

## File Structure

- Modify `forwin/writer/chapter_writer.py`
  - Single writer skips synchronous structured extraction and marks extraction as deferred.
- Modify `forwin/orchestrator_loop_core/project_chapters.py`
  - Deferred maintenance route treats `structured_extraction="deferred"` as a maintenance task.
- Modify `forwin/checker/pulp_beat.py`
  - Add track-aware word matrix and optional `track` argument.
- Modify `forwin/book_genesis_core/materialize.py`
  - Delete unreachable legacy bodies after delegation returns.
- Modify `forwin/book_state/macro_status.py`
  - Derive macro status from BookState fact rows first, then explicit chapter evidence, then legacy projection.
- Modify `forwin/planning/future_plan_audit/apply.py`
  - Audit arcs with `chapter_end <= current_chapter` and record successful boundary audit markers in run metadata.
- Test files:
  - `tests/test_writer_split_pipeline.py`
  - `tests/test_chapter_failure_stop_policy.py`
  - `tests/test_pulp_beat_verifier.py`
  - `tests/test_book_genesis_materialize.py`
  - `tests/test_book_state_macro_status.py`
  - `tests/test_future_plan_macro_progression_audit.py`

## Execution Setup

- [ ] **Step 1: Create isolated worktree**

```bash
git status --short
git worktree add .worktrees/p2-p3-longrun-correctness -b codex/p2-p3-longrun-correctness
cd .worktrees/p2-p3-longrun-correctness
```

Expected: clean worktree on `codex/p2-p3-longrun-correctness`.

- [ ] **Step 2: Confirm toolchain**

```bash
/home/kikuhiko/ForWin/.venv/bin/python --version
/home/kikuhiko/ForWin/.venv/bin/pytest --version
```

Expected: both commands succeed.

---

### Task 1: Defer Single Writer Structured Extraction

**Files:**
- Modify: `forwin/writer/chapter_writer.py`
- Modify: `forwin/orchestrator_loop_core/project_chapters.py`
- Test: `tests/test_writer_split_pipeline.py`
- Test: `tests/test_chapter_failure_stop_policy.py`

- [ ] **Step 1: Update failing writer test**

In `tests/test_writer_split_pipeline.py`, update the single writer expectation:

```python
self.assertEqual(output.state_changes, [])
self.assertEqual(output.thread_beats, [])
self.assertIsNone(output.time_advance)
self.assertEqual(output.generation_meta["mode"], "single")
self.assertEqual(output.generation_meta["call_count"], 1)
self.assertEqual(output.generation_meta["structured_extraction"], "deferred")
self.assertEqual(output.generation_meta["structured_extraction_calls"], 0)
self.assertEqual(output.generation_meta["state_event_extraction"], "deferred")
self.assertEqual(output.generation_meta["thread_time_extraction"], "deferred")
self.assertEqual(output.generation_meta["lore_timeline_notes_extraction"], "deferred")
```

Expected failure before implementation: call count is still 4 and structured fields are populated by LLM extraction.

- [ ] **Step 2: Add accepted deferred maintenance test**

In `tests/test_chapter_failure_stop_policy.py`, add a single-chapter accepted run whose `WriterOutput.generation_meta` has `structured_extraction="deferred"`, then assert a `DEFERRED_MAINTENANCE_RECORDED` event with `task_type="structured_extraction"` exists.

- [ ] **Step 3: Run failing tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest \
  tests/test_writer_split_pipeline.py::TestWriterSplitPipeline::test_single_writer_uses_single_draft_plus_structured_extraction \
  tests/test_chapter_failure_stop_policy.py::test_accepted_chapter_records_deferred_structured_extraction \
  -q
```

Expected: FAIL.

- [ ] **Step 4: Implement deferred single extraction**

In `_write_single_chapter`, remove the call to `_extract_structured()`. Merge draft data with:

```python
"_generation_meta": {
    "structured_extraction": "deferred",
    "structured_extraction_calls": 0,
    "state_event_extraction": "deferred",
    "thread_time_extraction": "deferred",
    "lore_timeline_notes_extraction": "deferred",
}
```

Set output meta:

```python
"call_count": 1,
```

In `_defer_structured_extraction_if_needed`, include `status == "deferred"` in the route.

- [ ] **Step 5: Run Task 1 tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest \
  tests/test_writer_split_pipeline.py \
  tests/test_chapter_failure_stop_policy.py \
  -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add forwin/writer/chapter_writer.py forwin/orchestrator_loop_core/project_chapters.py tests/test_writer_split_pipeline.py tests/test_chapter_failure_stop_policy.py
git commit -m "fix: defer single writer structured extraction"
```

---

### Task 2: Add Track-Aware Pulp Beat Verification

**Files:**
- Modify: `forwin/checker/pulp_beat.py`
- Test: `tests/test_pulp_beat_verifier.py`

- [ ] **Step 1: Add failing track tests**

Add tests:

```python
def test_verify_pulp_beats_detects_xuanhuan_payoff_without_urban_words() -> None:
    body = "宗门长老当众威胁逐他出山。林远当场运转灵诀突破境界，擂台全场震动，敌人经脉受创退下，灵石奖励入袋。秘境入口忽然开启。"
    result = verify_pulp_beats(body, track="xuanhuan")
    assert result.pressure_present is True
    assert result.visible_payoff_present is True
    assert result.new_gain_or_status_shift_present is True
    assert result.enemy_or_obstacle_damage_present is True
    assert result.next_hook_present is True


def test_verify_pulp_beats_detects_treasure_medicine_gain_separately() -> None:
    body = "掌柜当众质疑他没眼力。沈青当场施针验出古玉暗纹，围观客人哗然，病人苏醒，假专家脸色大变。鉴定证书到手，后院忽然传来求救声。"
    result = verify_pulp_beats(body, track="treasure_medicine")
    assert result.visible_payoff_present is True
    assert result.new_gain_or_status_shift_present is True
    assert result.visible_payoff_present != result.new_gain_or_status_shift_present or "new_gain_or_status_shift_present" not in result.missing_fields
```

- [ ] **Step 2: Run failing tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_pulp_beat_verifier.py -q
```

Expected: FAIL before matrix support.

- [ ] **Step 3: Implement matrix**

Replace single word constants with:

```python
PULP_BEAT_PROFILES = {
    "urban": PulpBeatProfile(...),
    "xuanhuan": PulpBeatProfile(...),
    "rural": PulpBeatProfile(...),
    "rebirth_period": PulpBeatProfile(...),
    "treasure_medicine": PulpBeatProfile(...),
}
```

Add:

```python
def verify_pulp_beats(body: str, *, track: str | None = None) -> PulpBeatResult:
    profile = _profile_for(body, track=track)
```

Keep existing API callers compatible.

- [ ] **Step 4: Run Task 2 tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_pulp_beat_verifier.py tests/test_hard_floor.py::test_pulp_profile_warns_when_visible_payoff_missing -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/checker/pulp_beat.py tests/test_pulp_beat_verifier.py
git commit -m "fix: use track-aware pulp beat signals"
```

---

### Task 3: Remove Materialize Dead Code

**Files:**
- Modify: `forwin/book_genesis_core/materialize.py`
- Create or modify: `tests/test_book_genesis_materialize.py`

- [ ] **Step 1: Add failing source-shape test**

```python
import inspect

from forwin.book_genesis_core import materialize


def test_materialize_wrappers_do_not_keep_unreachable_legacy_bodies() -> None:
    book_arcs_source = inspect.getsource(materialize.materialize_book_arcs)
    chapter_plans_source = inspect.getsource(materialize.materialize_arc_chapter_plans)

    assert "pack = self.load_pack(revision)" not in book_arcs_source
    assert "pack = self.load_pack(revision)" not in chapter_plans_source
    assert "return self.handoff.arc_materializer.materialize_book_arcs" in book_arcs_source
    assert "return self.handoff.chapter_materializer.materialize_arc_chapter_plans" in chapter_plans_source
```

- [ ] **Step 2: Run failing test**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_book_genesis_materialize.py -q
```

Expected: FAIL because unreachable bodies remain.

- [ ] **Step 3: Delete unreachable bodies**

Keep only the delegating return in `materialize_book_arcs()` and `materialize_arc_chapter_plans()`. Do not delete `_ensure_arc_map_expansion()` or `promote_next_arc_if_needed()`.

- [ ] **Step 4: Run Task 3 tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_book_genesis_materialize.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/book_genesis_core/materialize.py tests/test_book_genesis_materialize.py
git commit -m "refactor: remove unreachable genesis materialize code"
```

---

### Task 4: Derive Macro Status From Evidence Sources

**Files:**
- Modify: `forwin/book_state/macro_status.py`
- Test: `tests/test_book_state_macro_status.py`

- [ ] **Step 1: Add failing macro evidence tests**

Add tests for `FactNodeRow` macro status and explicit accepted chapter evidence:

```python
def test_macro_status_prefers_book_state_fact_evidence() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(
        FactNodeRow(
            id="fact-macro-1",
            project_id="p1",
            proposition="主角完成县城阶梯跃迁",
            fact_type="macro_status",
            truth_value="true",
            created_at_chapter=9,
            state_json=json.dumps(
                {
                    "status_tier": 3,
                    "wealth_tier": 2,
                    "enemy_tier": 1,
                    "market_space": "县城",
                },
                ensure_ascii=False,
            ),
            source_refs_json=json.dumps(["book_state:fact:9"], ensure_ascii=False),
        )
    )
    session.commit()
    status = SqlBookStateQueryInterface(session).get_protagonist_macro_status(
        project_id="p1",
        as_of_chapter=10,
    )
    assert status.status_tier == 3
    assert status.wealth_tier == 2
    assert status.enemy_tier == 1
    assert status.market_space == "县城"
    assert status.evidence_refs == ["book_state:fact:9"]
    assert status.source == "book_state_macro_fact"
```

Update the existing accepted chapter macro test to expect legacy source, then add an explicit evidence case expecting `accepted_chapter_macro_evidence`.

- [ ] **Step 2: Run failing tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_book_state_macro_status.py -q
```

Expected: FAIL because fact rows are ignored and source is still `book_state_macro_projection`.

- [ ] **Step 3: Implement layered derivation**

In `macro_status.py`, import `FactNodeRow`, query true fact nodes by chapter, parse direct macro fields from `state_json` and `metadata_json`, and only fall back to accepted chapter macro JSON when no fact macro rows apply.

- [ ] **Step 4: Run Task 4 tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_book_state_macro_status.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/book_state/macro_status.py tests/test_book_state_macro_status.py
git commit -m "fix: derive macro status from evidence sources"
```

---

### Task 5: Audit Missed Arc Macro Boundaries

**Files:**
- Modify: `forwin/planning/future_plan_audit/apply.py`
- Test: `tests/test_future_plan_macro_progression_audit.py`

- [ ] **Step 1: Add failing boundary resume tests**

Add test for a missed boundary:

```python
def test_audit_and_apply_blocks_unmet_macro_target_after_boundary_was_passed() -> None:
    session = _session()
    session.add(Project(id="p1", title="t", premise="p", target_total_chapters=1000))
    session.add(
        ArcPlanVersion(
            id="a1",
            project_id="p1",
            arc_number=1,
            arc_synopsis="县城跃迁",
            chapter_start=1,
            chapter_end=10,
            macro_progression_json=dump_arc_macro_progression(
                ArcMacroProgression(status_tier_to=3, wealth_tier_to=2, market_space_to="县城")
            ),
        )
    )
    session.add(
        ChapterPlan(
            id="c12",
            project_id="p1",
            arc_plan_id="a1",
            chapter_number=12,
            status="accepted",
            experience_plan_json=json.dumps(
                {"macro_status": {"status_tier": 1, "wealth_tier": 2, "market_space": "县城"}},
                ensure_ascii=False,
            ),
        )
    )
    session.commit()
    result = FuturePlanAuditor().audit_and_apply(
        session=session,
        project_id="p1",
        current_chapter=12,
        trigger_stage="post_acceptance",
        plans=[],
        canon_quality_context={},
        obligations=[],
        target_total_chapters=1000,
        include_current=False,
    )
    assert result.status == "fail"
    assert result.issues[0].metadata["arc_id"] == "a1"
```

Add test that a previous non-failing run with metadata `{"macro_boundary_audited_arc_ids": ["a1"]}` skips re-audit.

- [ ] **Step 2: Run failing tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_future_plan_macro_progression_audit.py -q
```

Expected: missed boundary test fails because selection uses `chapter_end == current_chapter`.

- [ ] **Step 3: Implement `<=` and metadata dedupe**

In `_with_macro_progression_boundary_issues`, select boundary arcs where `chapter_end <= current`. Add helpers:

```python
def _successful_macro_boundary_audited_arc_ids(session, project_id: str) -> set[str]:
    ...

def _macro_boundary_metadata(result: FuturePlanAuditRun, arc_ids: list[str]) -> dict[str, Any]:
    ...
```

Only skip previous non-failing runs.

- [ ] **Step 4: Run Task 5 tests**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest tests/test_future_plan_macro_progression_audit.py -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add forwin/planning/future_plan_audit/apply.py tests/test_future_plan_macro_progression_audit.py
git commit -m "fix: audit missed arc macro boundaries"
```

---

### Task 6: P2/P3 Verification And Merge

**Files:**
- All P2/P3 files from Tasks 1-5.

- [ ] **Step 1: Run focused suite**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest \
  tests/test_writer_split_pipeline.py \
  tests/test_chapter_failure_stop_policy.py \
  tests/test_pulp_beat_verifier.py \
  tests/test_hard_floor.py \
  tests/test_book_genesis_materialize.py \
  tests/test_book_state_macro_status.py \
  tests/test_future_plan_macro_progression_audit.py \
  tests/test_pulp_pressure_test.py \
  -q
```

Expected: PASS.

- [ ] **Step 2: Run compile verification**

```bash
/home/kikuhiko/ForWin/.venv/bin/python -m compileall forwin scripts/pulp_pressure_test.py
```

Expected: command exits 0.

- [ ] **Step 3: Merge into master**

```bash
cd /home/kikuhiko/ForWin
git status --short
git merge --ff-only codex/p2-p3-longrun-correctness
```

Expected: fast-forward merge succeeds.

- [ ] **Step 4: Verify on master**

```bash
/home/kikuhiko/ForWin/.venv/bin/pytest \
  tests/test_writer_split_pipeline.py \
  tests/test_chapter_failure_stop_policy.py \
  tests/test_pulp_beat_verifier.py \
  tests/test_hard_floor.py \
  tests/test_book_genesis_materialize.py \
  tests/test_book_state_macro_status.py \
  tests/test_future_plan_macro_progression_audit.py \
  tests/test_pulp_pressure_test.py \
  -q
/home/kikuhiko/ForWin/.venv/bin/python -m compileall forwin scripts/pulp_pressure_test.py
```

Expected: both commands pass.

- [ ] **Step 5: Remove temporary worktree and branch**

```bash
git worktree remove .worktrees/p2-p3-longrun-correctness
git branch -d codex/p2-p3-longrun-correctness
```

Expected: temporary worktree and branch are removed after merge.

## Self-Review

- Spec coverage: each P2/P3 requirement has a task and verification.
- Placeholder scan: no unresolved placeholders remain.
- Type consistency: `structured_extraction="deferred"`, `PulpBeatProfile`, `book_state_macro_fact`, and `macro_boundary_audited_arc_ids` names are used consistently.
