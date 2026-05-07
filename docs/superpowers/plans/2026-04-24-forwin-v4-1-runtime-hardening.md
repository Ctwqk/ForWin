# ForWin V4.1 Runtime Hardening Implementation Plan

> Status: historical implementation plan. This document hardened the old V4 bridge path; current architecture is `BookState DB Canon + BookMap / Scheme C`, with `world_model_v4` as compatibility projection / migration source. See `Design-docs/CURRENT_ARCHITECTURE.md` and `Design-docs/DESIGN_STATUS.md`.

> **For agentic workers:** REQUIRED SUB-SKILL: Use `superpowers:subagent-driven-development` (recommended) or `superpowers:executing-plans` to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** Turn the completed V4 vertical slice into a stronger V4.1 runtime path that can run a realistic deterministic generation loop, enforce deeper information-asymmetry review, rebuild cognition/materialized projections, and expose usable debug/export surfaces.

**Architecture:** Keep `WorldDelta / Belief / KnowledgeGap / Reveal / ReaderExperience` as the source semantics. Do not add a second canon path. Harden the current `ProjectGenesis -> contracts -> context -> writer -> extractor -> review gate -> compiler -> projection -> retrieval/debug` chain by adding deterministic integration tests first, then filling gaps in extractor, reviewer, projection, API, and UI layers.

**Tech Stack:** Python 3.12/3.13, Pydantic v2, SQLAlchemy 2, SQLite forward-only migrations, FastAPI, pytest, Playwright only for final browser smoke tests.

---

## Current Baseline

- V4 plan tasks 1-18 are implemented.
- Latest verification before this plan: `pytest -q` -> `284 passed, 8 subtests passed`.
- Current V4 strength: domain model, ledgers, planning contracts, writer protocol, deterministic extractor, v4 reviewers, compiler gate, provisional shadow layer, debug API, export, and Arc 2 synthetic E2E.
- Current V4 weakness: the full real runtime loop still relies on legacy scaffolding and shallow deterministic extraction/review; cognition snapshots and derived entity projections are not rich enough; debug UI is API/export-level rather than an operator workflow.

## Non-Goals

- Do not attempt lossless migration of old projects.
- Do not redesign the entire UI.
- Do not add new canon writers.
- Do not require real LLM calls in tests.
- Do not optimize performance beyond preventing obvious N+1 query patterns in new debug/export endpoints.

## File Responsibility Map

- Modify `forwin/extractor/world_v4.py`: move from intent-first fallback extraction toward body + writer-output + intent reconciliation.
- Create `forwin/extractor/world_v4_rules.py`: deterministic extraction rules, body span evidence, reveal phrase detection, source inference.
- Modify `forwin/reviewer_v4/*.py`: deeper policy checks for cognitive consistency, reveal timing, source quality, promise debt, and fair misdirection.
- Modify `forwin/world_model_v4/projection.py`: rebuild reader/character cognition snapshots and minimal derived `EntityState` projections.
- Modify `forwin/world_model_v4/compiler.py`: emit richer compile audit, forced-accept audit, projection refresh hooks, and retrieval index hook points.
- Modify `forwin/retrieval/broker.py`: use role-specific packs in writer/reviewer/compiler paths, not just standalone debug calls.
- Modify `forwin/orchestrator/loop.py`: add deterministic V4 runtime fixture path and richer v4 decision events.
- Modify `forwin/api_world_model_v4_routes.py`: add read-only line/gap/reveal/export endpoints around existing debug response.
- Modify `forwin/api_route_registry.py`: register new read-only V4 endpoints.
- Modify `forwin/ui_assets/home/*`: add a small V4 debug panel inside the existing home workspace.
- Modify `forwin/models/base.py`: add migration checks for any new audit/projection columns introduced by this plan.
- Tests:
  - `tests/test_world_v4_runtime_loop.py`
  - `tests/test_world_v4_extractor_deep.py`
  - `tests/test_world_v4_reviewers_deep.py`
  - `tests/test_world_v4_projection_materialization.py`
  - `tests/test_world_v4_debug_api_deep.py`
  - `tests/test_world_v4_debug_ui.py`

## Task 1: Add a Deterministic Real Runtime V4 Loop Test

**Files:**
- Create: `tests/test_world_v4_runtime_loop.py`
- Modify: `forwin/orchestrator/loop.py`
- Modify: `forwin/orchestrator/phase24.py`

- [x] Write a failing integration test that creates a project, seeds genesis-like project data, generates Arc 2 contracts, writes chapters 21/22/23/25/28 through the orchestrator path, and asserts v4 compiler runs are recorded.

```python
def test_runtime_loop_uses_v4_contracts_review_gate_and_compiler_for_arc2():
    result = run_deterministic_v4_runtime_fixture(
        chapters=[21, 22, 23, 25, 28],
        fixture_name="arc2_homeworld_crisis",
    )

    assert result.completed_chapters == [21, 22, 23, 25, 28]
    assert result.v4_compile_runs == ["compile-21", "compile-22", "compile-23", "compile-25", "compile-28"]
    assert result.blocked_compile_runs == ["compile-23-early-reveal-blocked"]
    assert result.final_gap_status == "closed"
```

- [x] Run the test and verify it fails because `run_deterministic_v4_runtime_fixture` does not exist.

Run:

```bash
pytest tests/test_world_v4_runtime_loop.py::test_runtime_loop_uses_v4_contracts_review_gate_and_compiler_for_arc2 -v
```

Expected: FAIL with `NameError` or `ImportError`.

- [x] Implement a private deterministic fixture helper in the test file first, using existing public APIs where possible and only monkeypatching the writer/LLM boundary.

```python
class DeterministicChapterWriter:
    def write_chapter(self, context_pack):
        return fixture_writer_output_for_chapter(context_pack.chapter_number)
```

- [x] Add an orchestrator helper only if the test cannot exercise the runtime without duplicating production logic. Name it `_run_v4_fixture_chapter_sequence` and keep it private to `WritingOrchestrator`.

```python
def _run_v4_fixture_chapter_sequence(self, project_id: str, chapter_numbers: list[int]) -> RunResult:
    result = RunResult(project_id=project_id, requested_chapters=len(chapter_numbers))
    for chapter_number in chapter_numbers:
        self._run_single_chapter(project_id, chapter_number)
        result.completed_chapters.append(chapter_number)
    return result
```

- [x] Run the single test again.

Expected: PASS.

- [x] Run the existing orchestrator gate tests.

```bash
pytest tests/test_world_v4_orchestrator_gate.py tests/test_continue_project_orphan_review.py -v
```

Expected: PASS.

## Task 2: Deepen WorldDelta Extraction From Chapter Body

**Files:**
- Create: `forwin/extractor/world_v4_rules.py`
- Modify: `forwin/extractor/world_v4.py`
- Test: `tests/test_world_v4_extractor_deep.py`

- [x] Write failing tests for body-span extraction and source inference.

```python
def test_extractor_extracts_body_span_hint_and_offscreen_source():
    writer_output = WriterOutput(
        project_id="project-1",
        chapter_number=23,
        title="乱码呼号",
        body="防线修复后，通讯台传出乱码。父亲旧部的呼号一闪即逝。敌方切断第三通讯阵列。",
        char_count=50,
        end_of_chapter_summary="通讯异常升级。",
    )
    extracted = WorldDeltaExtractor().extract(writer_output, chapter_intent=chapter_23_intent())

    assert [delta.delta_kind.value for delta in extracted.world_deltas] == ["visible", "hint", "offscreen"]
    assert extracted.world_deltas[1].source.source_type.value == "information_spread"
    assert "body_span:" in extracted.world_deltas[1].source_refs[0]
```

- [x] Run the new test file.

```bash
pytest tests/test_world_v4_extractor_deep.py -v
```

Expected: FAIL because `world_v4_rules.py` and body-span extraction are not implemented.

- [x] Implement `BodyEvidenceSpan` and extraction helpers.

```python
from dataclasses import dataclass

@dataclass(frozen=True)
class BodyEvidenceSpan:
    label: str
    start: int
    end: int
    text: str

    @property
    def source_ref(self) -> str:
        return f"body_span:{self.label}:{self.start}:{self.end}"
```

- [x] Implement deterministic rule functions.

```python
def find_hint_spans(body: str) -> list[BodyEvidenceSpan]:
    keywords = ("乱码", "旧部呼号", "通讯延迟", "残缺求援")
    return _find_keyword_spans(body, "hint", keywords)

def infer_source_type(text: str) -> DeltaSourceType:
    if any(token in text for token in ("通讯", "呼号", "求援")):
        return DeltaSourceType.INFORMATION_SPREAD
    if any(token in text for token in ("敌方", "舰队", "切断")):
        return DeltaSourceType.FACTION_ACTION
    return DeltaSourceType.CHARACTER_ACTION
```

- [x] Update `WorldDeltaExtractor.extract` to reconcile writer self-report, body spans, and chapter intent in this order:
  - preserve writer self-report when present and well-formed
  - add missing body-derived deltas
  - use chapter intent only as fallback
  - always attach `source_refs`

- [x] Run:

```bash
pytest tests/test_world_v4_extractor.py tests/test_world_v4_extractor_deep.py -v
```

Expected: PASS.

## Task 3: Harden V4 Reviewers With Policy-Level Checks

**Files:**
- Modify: `forwin/reviewer_v4/cognitive.py`
- Modify: `forwin/reviewer_v4/world_delta.py`
- Modify: `forwin/reviewer_v4/reveal.py`
- Modify: `forwin/reviewer_v4/reader_cognition.py`
- Modify: `forwin/reviewer_v4/types.py`
- Test: `tests/test_world_v4_reviewers_deep.py`

- [x] Write failing tests for four deeper checks.

```python
def test_reviewers_warn_false_belief_without_evidence():
    verdict = V4ReviewGate().review(
        extracted_false_belief_without_evidence(),
        chapter_intent=chapter_23_intent(),
        chapter_body="主角仍以为通讯问题只是距离导致。",
    )
    assert any(issue.failure_type == "unsupported_false_belief" for issue in verdict.issues)

def test_reveal_reviewer_blocks_reveal_before_ladder_step():
    verdict = V4ReviewGate().review(
        extracted_direct_reveal(),
        chapter_intent=chapter_23_intent_with_reveal_chapter_25(),
        chapter_body="父亲明确说自己已经被围。",
    )
    assert verdict.passed is False
    assert any(issue.failure_type == "reveal_before_planned_chapter" for issue in verdict.issues)

def test_world_delta_reviewer_warns_hidden_line_without_hint_plan():
    verdict = V4ReviewGate().review(
        extracted_long_hidden_offscreen_delta(),
        chapter_intent=chapter_intent_without_hint_plan(),
        chapter_body="幕后敌方舰队推进。",
    )
    assert any(issue.failure_type == "offscreen_without_reveal_plan" for issue in verdict.issues)

def test_reader_cognition_reviewer_fails_band_without_increment():
    verdict = V4ReviewGate().review(
        ExtractedWorldChangeSet(project_id="p", chapter_number=24),
        chapter_intent=empty_chapter_intent(),
        chapter_body="没有事件，也没有认知变化。",
        promise_debt_count=5,
    )
    assert any(issue.failure_type == "missing_chapter_increment" for issue in verdict.issues)
```

- [x] Run:

```bash
pytest tests/test_world_v4_reviewers_deep.py -v
```

Expected: FAIL because deeper failure types are not implemented.

- [x] Add `severity` expectations:
  - `unsupported_false_belief`: `warn`
  - `reveal_before_planned_chapter`: `fail`
  - `offscreen_without_reveal_plan`: `warn`
  - `missing_chapter_increment`: `fail`

- [x] Implement each reviewer change with explicit evidence refs and repair patches.

```python
V4ReviewIssue(
    reviewer=self.name,
    severity="fail",
    failure_type="reveal_before_planned_chapter",
    message="本章 reveal 早于 RevealLadder 计划章节。",
    evidence_refs=["chapter_intent:planned_reveal_ladder", "chapter_body:direct_reveal"],
    repair_patch={"must_not_reveal": list(chapter_intent.must_not_reveal)},
)
```

- [x] Run:

```bash
pytest tests/test_world_v4_review_gate.py tests/test_world_v4_reviewers_deep.py -v
```

Expected: PASS.

## Task 4: Rebuild Cognition Snapshots and Derived EntityState Projections

**Files:**
- Modify: `forwin/world_model_v4/projection.py`
- Modify: `forwin/world_model_v4/repository.py`
- Modify: `forwin/world_model_v4/compiler.py`
- Test: `tests/test_world_v4_projection_materialization.py`

- [x] Write failing tests that compile belief updates and knowledge updates, then assert `CognitionSnapshotRow` contains reader and protagonist snapshots.

```python
def test_projection_rebuilds_reader_and_character_cognition_snapshots():
    result = compile_ch25_partial_reveal()
    snapshots = load_cognition_snapshots(result.project_id, chapter=25)

    assert snapshots["reader"].visibility_by_delta["delta_ch25_distress_call"] == "partially_revealed"
    assert snapshots["protagonist"].suspected_gap_ids == ["gap_homeworld_siege"]
```

- [x] Add a second failing test for derived `EntityState`.

```python
def test_projection_materializes_entity_state_without_overwriting_hidden_truth():
    compile_offscreen_homeworld_siege()
    entity_state = load_entity_state("homeworld")

    assert entity_state["visible_to_reader"] != "father_sieged_confirmed"
    assert entity_state["objective_layer"]["siege_status"] == "under_siege"
```

- [x] Run:

```bash
pytest tests/test_world_v4_projection_materialization.py -v
```

Expected: FAIL because projection currently stores shallow snapshot data.

- [x] Extend `WorldModelProjection.rebuild_snapshot` to derive:
  - `reader_cognition_state_json`
  - `character_cognition_states_json`
  - `CognitionSnapshotRow` rows
  - minimal derived `EntityState` rows only under v4 projection metadata

- [x] Keep ledger rows append-only. If projection is re-run, replace only snapshot/materialized projection rows for the same `project_id + as_of_chapter`.

- [x] Run:

```bash
pytest tests/test_world_v4_repository.py tests/test_world_v4_compiler.py tests/test_world_v4_projection_materialization.py -v
```

Expected: PASS.

## Task 5: Use Role-Specific Packs Inside Runtime, Reviewer, and Compiler Paths

**Files:**
- Modify: `forwin/retrieval/broker.py`
- Modify: `forwin/orchestrator/loop.py`
- Modify: `forwin/reviewer/context_builder.py`
- Test: `tests/test_world_v4_runtime_loop.py`

- [x] Extend the runtime loop test to assert:

```python
assert result.writer_pack_hidden_truth_count == 0
assert result.review_pack_hidden_truth_count >= 1
assert result.compiler_pack_accepted_delta_ids == ["delta_ch23_callsign_hint"]
```

- [x] Run:

```bash
pytest tests/test_world_v4_runtime_loop.py -v
```

Expected: FAIL because role-specific packs are not yet passed through the runtime audit path.

- [x] Update `WritingOrchestrator._apply_canon_candidate` to build:
  - `WritingPack` before writer call
  - `ReviewPack` before v4 review
  - `CompilerPack` before compiler call

- [x] Attach pack metadata to review verdict payloads and compiler run input JSON.

```python
verdict.metadata["v4_review_pack"] = review_pack.model_dump(mode="json")
compile_request.metadata["v4_compiler_pack"] = compiler_pack.model_dump(mode="json")
```

- [x] Run:

```bash
pytest tests/test_world_v4_context_pack.py tests/test_world_v4_retrieval_packs.py tests/test_world_v4_runtime_loop.py -v
```

Expected: PASS.

## Task 6: Expand V4 Debug API and Add a Minimal Debug Panel

**Files:**
- Modify: `forwin/api_world_model_v4_routes.py`
- Modify: `forwin/api_route_registry.py`
- Modify: `forwin/api_schemas.py`
- Modify: `forwin/ui_assets/home/body.html`
- Modify: `forwin/ui_assets/home/app_bootstrap.js`
- Modify: `forwin/ui_assets/home/app_state.js`
- Create: `forwin/ui_assets/home/app_world_model_v4.js`
- Test: `tests/test_world_v4_debug_api_deep.py`
- Test: `tests/test_world_v4_debug_ui.py`

- [x] Write API tests for read-only endpoints:

```python
def test_v4_debug_api_exposes_lines_gaps_reveals_and_export():
    client = build_test_client_with_v4_fixture()

    assert client.get(f"/api/projects/{project_id}/world-model/v4/lines").status_code == 200
    assert client.get(f"/api/projects/{project_id}/world-model/v4/gaps").status_code == 200
    assert client.get(f"/api/projects/{project_id}/world-model/v4/reveals").status_code == 200
    assert client.get(f"/api/projects/{project_id}/world-model/v4/export?as_of_chapter=28").status_code == 200
```

- [x] Run:

```bash
pytest tests/test_world_v4_debug_api_deep.py -v
```

Expected: FAIL because endpoints do not exist.

- [x] Implement read-only route handlers:
  - `GET /api/projects/{project_id}/world-model/v4/lines`
  - `GET /api/projects/{project_id}/world-model/v4/gaps`
  - `GET /api/projects/{project_id}/world-model/v4/reveals`
  - `GET /api/projects/{project_id}/world-model/v4/export`

- [x] Write UI rendering tests that ensure the home page contains a V4 debug panel mount point and JS module.

```python
def test_home_page_includes_v4_world_model_debug_panel_assets():
    html = render_home_page(...)
    assert "world-model-v4-panel" in html
    assert "app_world_model_v4.js" in html
```

- [x] Implement a minimal panel with:
  - active world lines
  - hidden lines
  - open gaps
  - planned/overdue reveals
  - accepted/rejected deltas for selected chapter
  - reader/protagonist cognition summary
  - promise debts

- [x] Run:

```bash
pytest tests/test_world_v4_debug_api_deep.py tests/test_world_v4_debug_ui.py tests/test_api_pages_rendering.py -v
```

Expected: PASS.

## Task 7: Add Forward-Only Migration Coverage for V4.1 Projection/Audit Fields

**Files:**
- Modify: `forwin/models/base.py`
- Modify: `forwin/models/world_v4.py`
- Modify: `forwin/models/phase.py`
- Test: `tests/test_world_v4_schema.py`

- [x] Add a migration test that initializes an older schema without V4.1 fields, runs `init_db`, and verifies new columns/tables are present without dropping data.

```python
def test_v41_migration_adds_projection_and_audit_fields_without_data_loss(tmp_path):
    engine = create_legacy_v4_database(tmp_path / "legacy.db")
    seed_legacy_world_delta(engine, delta_id="delta_existing")

    init_db(engine)

    assert has_table(engine, "world_projection_deltas")
    assert has_column(engine, "world_compile_runs_v4", "retrieval_pack_json")
    assert load_world_delta_ids(engine) == ["delta_existing"]
```

- [x] Run:

```bash
pytest tests/test_world_v4_schema.py::test_v41_migration_adds_projection_and_audit_fields_without_data_loss -v
```

Expected: FAIL until migration exists.

- [x] Implement idempotent migration helpers using `CREATE TABLE IF NOT EXISTS` and guarded `ALTER TABLE`.

- [x] Run:

```bash
pytest tests/test_world_v4_schema.py -v
```

Expected: PASS.

## Task 8: Add V4.1 Acceptance Scenario Covering Realistic Repair Loop

**Files:**
- Modify: `tests/test_world_v4_runtime_loop.py`
- Modify: `forwin/orchestrator/loop.py`
- Modify: `forwin/protocol/review.py`

- [x] Add a deterministic test where chapter 23 first draft violates `must_not_reveal`, v4 gate blocks compile, repair rewrites the body to hint-only, and compiler commits the repaired version.

```python
def test_runtime_repair_loop_blocks_early_reveal_then_commits_hint_only_revision():
    result = run_deterministic_v4_runtime_fixture(
        chapters=[23],
        fixture_name="arc2_ch23_repair_loop",
    )

    assert result.first_attempt_gate_status == "blocked"
    assert result.repair_instruction.must_not_reveal == ["father_sieged"]
    assert result.final_attempt_gate_status == "approved"
    assert result.committed_delta_ids == ["delta_ch23_callsign_hint"]
```

- [x] Run:

```bash
pytest tests/test_world_v4_runtime_loop.py::test_runtime_repair_loop_blocks_early_reveal_then_commits_hint_only_revision -v
```

Expected: FAIL until repair metadata is propagated through the deterministic runtime path.

- [x] Ensure `RepairInstruction` carries:
  - `repair_scope="chapter"`
  - `failure_type="early_reveal"`
  - `must_fix`
  - `must_preserve`
  - `must_not_reveal`
  - `required_hint_patch`
  - `evidence_refs`

- [x] Update runtime loop so failed v4 gate does not call `WorldModelCompiler.compile`; it may call `compile_gate_verdict` only to record a blocked audit row.

- [x] Run:

```bash
pytest tests/test_world_v4_runtime_loop.py tests/test_world_v4_orchestrator_gate.py tests/test_world_v4_review_gate.py -v
```

Expected: PASS.

## Task 9: Full V4.1 Verification and Deployment Smoke

**Files:**
- Modify only if previous tasks reveal regressions.

- [x] Run all V4 tests.

```bash
pytest tests/test_world_v4_protocol.py tests/test_world_v4_schema.py tests/test_world_v4_repository.py tests/test_world_v4_bootstrap.py tests/test_planning_world_contracts.py tests/test_world_v4_context_pack.py tests/test_world_v4_writer_protocol.py tests/test_world_v4_extractor.py tests/test_world_v4_extractor_deep.py tests/test_world_v4_review_gate.py tests/test_world_v4_reviewers_deep.py tests/test_world_v4_compiler.py tests/test_world_v4_orchestrator_gate.py tests/test_world_v4_retrieval_packs.py tests/test_world_v4_provisional.py tests/test_world_v4_api.py tests/test_world_v4_debug_api_deep.py tests/test_world_v4_export.py tests/test_world_v4_e2e.py tests/test_world_v4_runtime_loop.py tests/test_world_v4_projection_materialization.py -v
```

Expected: PASS.

- [x] Run full suite.

```bash
pytest -q
```

Expected: PASS with zero failures.

- [x] Rebuild and restart the 8899 container.

```bash
docker compose build forwin
docker compose up -d forwin
```

Expected: `forwin` starts and becomes healthy.

- [x] Smoke test runtime and debug API.

```bash
curl -fsS http://localhost:8899/health
curl -fsS http://localhost:8899/openapi.json >/tmp/forwin-openapi.json
curl -fsS http://localhost:8899/api/projects
```

Expected: all commands exit 0.

- [x] Browser smoke test the home page.

```bash
docker compose exec -T forwin python - <<'PY'
from playwright.sync_api import sync_playwright
with sync_playwright() as p:
    browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
    page = browser.new_page()
    page.goto("http://127.0.0.1:8899/", wait_until="networkidle", timeout=15000)
    text = page.locator("body").inner_text(timeout=5000)
    assert "FORWIN WORKSPACE" in text
    assert "V4" in text or "World Model" in text or "世界模型" in text
    browser.close()
PY
```

Expected: script exits 0.

## Acceptance Gates

- A deterministic project can run through the real V4 runtime path without a real LLM.
- Chapter 23 early reveal is blocked before canon commit and repaired into a hint-only version.
- Writer packs do not expose hidden objective truth; review/compiler packs do.
- Extractor emits body-span evidence and source types for key deltas.
- Reviewers catch unsupported false beliefs, early reveal, source gaps, and missing chapter increment.
- Compiler rebuilds reader/character cognition snapshots and derived materialized views.
- V4 debug API and minimal home-page debug panel expose active lines, hidden lines, gaps, reveal ladder, accepted/rejected deltas, cognition, and promise debt.
- All new V4.1 tests and the full suite pass.

## Commit Boundaries

- Commit 1: deterministic V4 runtime loop fixture.
- Commit 2: deeper extractor rules and tests.
- Commit 3: reviewer hardening and repair metadata.
- Commit 4: cognition snapshot and materialized projection rebuild.
- Commit 5: runtime role-specific retrieval pack integration.
- Commit 6: V4 debug API and minimal UI panel.
- Commit 7: V4.1 migrations.
- Commit 8: repair-loop acceptance scenario and final verification.

## Implementation Status

Completed on 2026-04-24.

Verification evidence:

- `pytest -q`: 295 passed, 8 subtests passed.
- V4 focused runtime/debug/UI tests: 43 passed.
- `docker compose build forwin && docker compose up -d forwin`: deployed to port 8899.
- `curl -fsS http://127.0.0.1:8899/health`: `{"status":"ok"}`.
- OpenAPI includes V4 debug routes for debug, lines, gaps, reveals, and export.
- Browser smoke loaded the home page and opened the `V4 世界` debug panel.
