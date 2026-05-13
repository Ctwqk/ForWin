# Continue Generation Patch Audit - 2026-05-12

## Scope

This review covers the current local patch on `master` relative to `origin/master`.

Changed areas:

- `continue-generation` requested chapter count handling.
- Band checkpoint approval status validation.
- `world_model_v4` / `reviewer_v4` compatibility import forwarding.
- Browser mock regression coverage.
- 60-chapter real-run report.

Primary review focus:

- Patch-on-patch risk.
- Dead or misleading compatibility code.
- Potential runtime bugs not covered by the new tests.
- Follow-up improvements needed before treating this patch as complete.

## Findings

### P1 - Architecture boundary test currently fails

`forwin/world_model_v4/compiler.py` was converted into a thin forwarding module:

```python
from forwin.world_v4_compat.compiler import WorldModelCompiler
```

But `tests/test_architecture_boundaries.py::test_legacy_world_model_is_labeled_projection_in_runtime_paths` still reads that file directly and expects the phrase `compatibility projection rows`.

Observed verification:

```bash
.venv/bin/python -m pytest tests/test_architecture_boundaries.py::test_legacy_world_model_is_labeled_projection_in_runtime_paths -q
```

Result:

```text
1 failed
assert 'compatibility projection rows' in 'from __future__ import annotations...'
```

Impact:

- CI can fail even though the import alias test passes.
- The alias migration moved code but did not move or preserve the architecture boundary marker.

Recommended fix:

- Either add an explicit compatibility-projection docstring to the forwarding module, or update the architecture boundary test to inspect `forwin/world_v4_compat/compiler.py` as the new canonical implementation.
- Prefer keeping the boundary marker in both places while the legacy import path remains public.

### P1 - `requested_chapters` can still be overwritten by stale progress payloads

The API entry point now clamps initial task creation to `max_chapters`:

- `forwin/api_project_ops.py`: `requested_chapters = min(..., max_chapters)`.

The worker path still emits progress before the active-arc scoped chapter list is calculated:

- `forwin/orchestrator/loop.py` records `payload={"requested_chapters": len(chapter_plans)}` at run start.
- `forwin/orchestrator/loop.py` emits `requested_chapters=len(pending_chapter_numbers)` during `resolving_arc_envelope`.
- `forwin/api_runtime.py` blindly copies `requested_chapters` from progress payloads into the persisted task.

Impact:

- A task started with `max_chapters=2` can be created as `requested_chapters=2`, then later show a larger count in task-center state.
- This matches the already observed report note that the frontend continue response and authoritative task state diverged.
- The new regression test only patches task creation and does not exercise the worker progress callback, so it can pass while the real UI still regresses.

Recommended fix:

- Centralize requested-count calculation in the orchestrator path after active-arc scoping, or pass a task-level requested limit through the runtime progress handler and clamp progress payloads before `update_task`.
- Add a regression test that runs `run_continue_project_with_config` or an equivalent orchestrator progress callback and asserts persisted task `requested_chapters` never exceeds `max_chapters`.

### P2 - Active-arc scoping and API pre-count can diverge

`continue_project_generation()` counts all `planned` / `failed` chapter plans in the project. The orchestrator later calls `_pending_chapter_numbers_for_active_arc()` and only writes chapters from the active arc.

Impact:

- If future arcs already have planned chapters, the API task count can overstate actual work.
- If there are no remaining materialized plans but a planned future Genesis arc exists, the API falls back to `requested_chapters=1`; the orchestrator may then materialize a full arc and emit a larger count.

Recommended fix:

- Extract a single helper for "next continue-generation workset" that accounts for active arc, future arc materialization, failed chapters, and `max_chapters`.
- Use that helper for API task creation, orchestrator progress, MCP response, and tests.

### P2 - Invalid historical checkpoint statuses can still break read paths

The write request now rejects unknown approval statuses:

- `BandCheckpointApproveRequest.status: Literal["pass", "overridden"]`.
- MCP client also rejects values outside `pass` / `overridden`.

However, read models still validate `BandCheckpointDetail.status` against `CheckpointStatus`, which does not include the old invalid value `approved`.

Observed verification:

```bash
.venv/bin/python - <<'PY'
from pydantic import ValidationError
from forwin.governance import BandCheckpointDetail
try:
    BandCheckpointDetail(status="approved")
except ValidationError as exc:
    print(type(exc).__name__)
PY
```

Result:

```text
ValidationError
```

Impact:

- Existing rows with `status="approved"` can still make project detail, checkpoint detail, or task-center serialization fail.
- The patch prevents future bad writes but does not repair or tolerate historical bad data.

Recommended fix:

- Add a migration or startup repair for invalid `band_checkpoints.status` values.
- Harden `serialize_band_checkpoint()` / `_band_checkpoint_detail()` to normalize unknown statuses to `overridden` or expose a safe error status.
- Add a regression test that seeds an invalid historical row and verifies the read API does not 500.

### P2 - Continue-after-review paths still use total chapter count

The direct continue endpoint was patched, but two related paths still pass total materialized chapter count into `create_continue_generation_task()`:

- `approve_chapter_review(... continue_generation=True)` uses `requested_chapters=int(total_chapters or 0)`.
- `retry_chapter_review(... continue_generation=True)` uses `requested_chapters=int(total_chapters or 0)`.

Impact:

- After accepting or retrying one chapter, the task may report the whole book or whole materialized plan count, not the remaining workset.
- This is the same class of bug as the direct continue-generation mismatch.

Recommended fix:

- Reuse the same continue workset helper for direct continue, accept-and-continue, retry-and-continue, production scheduler, and MCP.
- Add tests for review-accept continue and retry continue requested-count behavior.

### P3 - `max_chapters` validation is inconsistent across API and MCP

The MCP client rejects `max_chapters < 1`, but `ProjectContinueGenerationRequest` accepts any integer and the API coerces `0` or negative values to `1`.

Impact:

- Direct REST and MCP callers observe different behavior for the same invalid input.
- Silent coercion can hide UI or operator bugs.

Recommended fix:

- Put `max_chapters: int | None = Field(default=None, ge=1)` in `ProjectContinueGenerationRequest`.
- Add route-level tests for `0`, negative, and valid values.

### P3 - Checkpoint approval does not set `resolved_at`

`BandCheckpoint` has `resolved_at`, and serializers expose it, but `approve_band_checkpoint()` only updates `status` and `reason`.

Impact:

- Resolved checkpoints remain without a resolved timestamp.
- Audit views cannot reliably distinguish old unresolved checkpoints from manually resolved ones using timestamp alone.

Recommended fix:

- Set `row.resolved_at` when status moves to `pass` or `overridden`.
- Add a regression assertion on serialized checkpoint `resolved_at`.

### P3 - Browser mock regression is useful but brittle

The new browser test hardcodes `project-2` and `task-2`, relying on fixture initialization order. The mock start-writing handler also returns a generic sample task and does not model real project status transitions.

Impact:

- The test can break when fixture defaults change.
- It verifies frontend wiring and refresh persistence, but not the real backend state machine for Genesis handoff or continue-generation task counts.

Recommended fix:

- Derive project/task ids from captured responses instead of hardcoding `project-2` / `task-2`.
- Extend the mock to update project `creation_status` and chapter/task state closer to the real API contract.
- Keep this test labeled as CI mock regression, not live LLM E2E coverage.

### P4 - Tracked runtime artifacts and local scripts should be cleaned up separately

The repository has tracked `.playwright-mcp/*` run logs and `.codex-tmp/raspi-bastion-hardening.sh`.

Impact:

- These files are not part of the current patch, but they are repository hygiene debt.
- They can obscure review and make future patch boundaries noisier.

Recommended fix:

- Decide whether these artifacts are intentionally versioned.
- If not, remove them from git and add ignore rules.

## 60-Chapter Real-Run Content Findings

Run under review:

- Project: `bbe070bc8eda49c9a551c3ce1c755391`, title `端到端实测长篇小说_60章`.
- Final state observed through ForWin project/chapter APIs: `60/60 accepted`, `next_gate=completed`, `creation_status=writing`.
- Runtime log note: `data/forwin-api.log` did not show a chapter-generation crash for this run; the only observed service-level error was an unrelated port bind failure. MCP `task_get` also reports `error:null` for the relevant generation tasks. The useful failure evidence is therefore the task decision timeline, reviewer verdicts, generation metadata, accepted chapter text, and world-model conflict output.
- World conflict checks returned no conflicts (`{"conflicts":[]}` through MCP, previously observed HTTP payload `{"conflict_count":0}`) even though the accepted book contains obvious continuity contradictions. That makes this a reviewer/canon-admission failure, not a missing UI display problem.

### P1 - Placeholder names leaked into accepted prose

Observed text symptoms:

- Chapter 1 repeatedly uses `相关人员`; the chapter summary also starts from that placeholder.
- Chapter 31 contains `签名人一栏写着一个名字：相关人员。`.
- Chapter 17 summary contains `温和派代表相关人员`.

Observed error log:

```text
2026-05-11 16:32:53 PDT task 1cb173fa06a9 review_verdict_recorded 第1章 review verdict: pass
payload: {"verdict":"pass","issue_types":[],"issue_groups":[],"forced_accept_applied":false}
第1章 writer generation_meta.subworld_admission_autofix: {"林澈":"相关人员","林总":"相关人员"}
第1章 related_count: 28

第31章 review verdict: warn
payload issue_types: ["lint"]
第31章 writer generation_meta.subworld_admission_autofix: {"林远舟":"相关人员","林总":"相关人员"}
第31章 related_count: 2
```

Fault flow:

1. Writer introduced unknown named entities such as `林澈` / `林远舟`.
2. Reviewer/checker treated them as `sub_world_unknown_named_entity`.
3. `WritingOrchestrator._apply_subworld_admission_autofix()` rewrote the unknown names before final admission.
4. `WritingOrchestrator._generic_subworld_reference()` fell back to `相关人员`.
5. `forwin/checker/rules.py` includes `相关人员` in `GENERIC_CHARACTER_REFERENCES`, so the placeholder then bypassed named-character checking.
6. The final reviewer verdict became `pass` or `warn`; canon admission accepted the chapter and cleared residual issues when the chapter was not force-accepted.

Code roots:

- `forwin/orchestrator/loop.py:2041` applies `subworld_admission_autofix`.
- `forwin/orchestrator/loop.py:2125` returns `相关人员` as the generic fallback.
- `forwin/checker/rules.py:26` treats `相关人员` as a generic character reference.
- `forwin/orchestrator/loop.py:3695` clears `residual_review_issues` for normally accepted chapters.

Fix study:

- Do not use `相关人员` as an in-prose repair token. Either block the chapter and request a named character repair, or replace with a role that is semantically valid in context and cannot appear as a proper signature/name.
- Add a deterministic lint rule: `相关人员` / `一名相关人员` is allowed only in metadata-like summaries, not in chapter body, dialogue, signatures, letters, legal documents, or scene-critical reveals.
- Preserve autofix metadata as a hard review input. If `subworld_admission_autofix` touched body text, the rewritten body must be rechecked for placeholder leakage before canon admission.

### P1 - Death, rescue, and alive-state contradictions were accepted

Observed text symptoms:

- Chapter 23 presents 沈砚 as shot and near death.
- Chapter 32 marks him as cleared/removed.
- Chapters 35 and 40 both rescue him again.
- Chapter 42 has another sacrifice version.
- Chapter 43 contains a public execution track.
- Chapters 47, 48, 51, 56, 57, 58, 59, and 60 continue to use him as active/alive/wounded/recovering.

Observed error log:

```text
2026-05-11 17:28:31 PDT task f989a175f66f review_verdict_recorded 第23章 review verdict: fail
payload issue_types: ["subworld_admission","payoff_miss","character_motivation","missing_anchor"]
2026-05-11 17:28:31 PDT task f989a175f66f repair_started 第1次 draft
task f989a175f66f repair_succeeded rewrite 后 verdict: pass
final payload issue_types: ["payoff_miss","behavioral_inconsistency"]
canon_commit issue_count=2

第32章 / 第35章 / 第40章 / 第42章 / 第43章 / 第51章 / 第57章 / 第58章: pass 或 warn 后 canon_commit
world-model conflicts endpoint: {"conflict_count":0}
```

Fault flow:

1. The chapter text used strong terminal-state wording, but the compiled actor state did not consistently mark `alive=false` or exact dead statuses.
2. `forwin/world_model/conflict_detector.py` only detects dead/alive conflict when state is exactly `alive is False` or status is one of `dead`, `deceased`, `死亡`, `已死`.
3. Non-exact states such as `临终`, `被清除`, `牺牲`, `处决`, `濒死`, `重伤后失踪` did not become a blocking state transition.
4. Later active participation was therefore not detected as a contradiction.
5. Warn-level continuity issues were still canon-applied in blackbox mode.

Code roots:

- `forwin/world_model/conflict_detector.py:9` only implements `dead_alive_conflict` and `character_location_conflict`.
- `forwin/world_model/conflict_detector.py:24` uses a narrow dead-status vocabulary.
- `forwin/orchestrator/loop.py:3587` accepts `warn` verdicts in blackbox mode.

Fix study:

- Add a structured character-state transition ledger: `alive`, `dead`, `terminally_wounded`, `captured`, `missing`, `rescued`, `executed`, `sacrificed`, `recovered`.
- Treat resurrection/rescue-after-terminal-state as a hard continuity gate unless the current chapter supplies an explicit bridge event that references the terminal event.
- Expand conflict detection beyond current state to state history and canon events, so repeated rescue/death cycles are detected even if the latest snapshot overwrites prior status.

### P1 - Final reset/countdown hook was not closed before completion

Observed text symptoms:

- Chapter 59 ends on a final reset/countdown around `59:59`.
- Chapter 60 jumps to post-victory/recovery state and even says there are still `三十多天` left, without explicitly resolving the active reset/countdown threat.

Observed error log:

```text
第59章 review verdict: warn
payload issue_types: ["continuity","continuity","continuity","payoff_miss","consistency","consistency"]
canon_commit issue_count=6

2026-05-11 20:20:45 PDT 第60章 review verdict: fail
payload issue_types: ["subworld_admission","consistency","consistency","consistency"]
repair_started 第1次 draft
repair_succeeded rewrite 后 verdict: pass
rewrite payload issue_types: ["character_omniscience","character_omniscience"]
2026-05-11 20:27:48 PDT 第60章 canon_commit issue_count=2
```

Fault flow:

1. Chapter 59 installed a high-priority hook: active reset/countdown and unresolved terminal stakes.
2. Chapter 60 repair focused on the local fail issues in that chapter.
3. The reviewer did not enforce closure of the immediately previous chapter's active hook before marking the final chapter pass.
4. Accepted completion state was driven by `60/60 accepted`, not by an explicit "all terminal hooks closed" book-level gate.

Fix study:

- Add a final-chapter completion gate that requires closure evidence for all active P0/P1 hooks from the preceding chapters.
- Track countdowns and reset timers as structured obligations with `started_at_chapter`, `deadline`, `current_value`, and `resolved_at_chapter`.
- For the last requested chapter, block completion if any terminal hook remains open, even if the local chapter review is pass.

### P2 - Artifact/file count ledger drifted across the book

Observed text symptoms:

- Chapter 14 already mentions the 13th archive/file.
- Chapter 21 jumps through `第15-18份`.
- Chapter 31 obtains 12 backup files and later refers to 13.
- Chapter 43 says `还有五十九份档案，还有五十九天`.
- Chapter 57 alternates between `五十九份`, `六十份`, and `六十一份`.

Observed error log:

```text
第31章 review verdict: warn
payload issue_types: ["lint"]
canon_commit issue_count=1

第43章 review verdict: pass/warn path accepted
第57章 review verdict: warn
canon_commit accepted despite count drift
```

Fault flow:

1. The story used files/fragments as a central progress counter.
2. The counter was not represented as a structured canon resource.
3. Review saw individual count mentions as local prose/continuity warnings rather than a blocking ledger contradiction.
4. Canon commit accepted chapters with incompatible arithmetic.

Fix study:

- Add an `artifact_collection_ledger` to BookState or chapter task state: total target, collected count, consumed count, newly found item ids, duplicate ids, and source chapter.
- Add deterministic count checks for `第N份`, `还有N份`, `N/60`, and equivalent Chinese numerals.
- If a chapter changes the count by more than the declared newly acquired artifacts, fail review with a repair instruction that updates either the prose or the ledger.

### P2 - Countdown/time continuity was treated as prose, not state

Observed text symptoms:

- The book moves through `60 days`, `53 days`, `48 days`, `5 days`, `47 hours`, `24 hours`, `6 hours`, `59 minutes`.
- Chapter 60 then says `还有三十多天`.

Observed error log:

```text
第59章 review verdict: warn
payload issue_types: ["continuity","continuity","continuity","payoff_miss","consistency","consistency"]
canon_commit issue_count=6
第60章 repair_succeeded 后 pass
```

Fault flow:

1. Time pressure appears as natural language rather than a typed resource.
2. The reviewer can warn on local continuity problems, but there is no arithmetic consistency gate for countdown monotonicity.
3. A repaired final chapter can pass without reconciling the global countdown ledger.

Fix study:

- Model countdowns as typed state with monotonic constraints and explicit reset events.
- Require every time jump to declare whether it consumes time, resets the timer, branches to a different clock, or changes narrator knowledge.
- Add a deterministic review rule that fails non-monotonic countdown changes unless a reset/branch event is cited.

### P2 - Repeated reveal beats and locations passed as progress

Observed text symptoms:

- Chapters 6 and 7 are near-duplicates around `潮汐钟楼废弃观测室`, the third fragment, and `沈砚不可信`.
- Later chapters repeatedly reveal white-tower backdoors, genetic keys, father/ancestor system-designer identity, and `潮汐钟楼地下三层`.

Observed error log:

```text
第6章 review verdict: pass
payload issue_types: ["character_consistency","world_legibility"]
canon_commit issue_count=2

第7章 review verdict: pass
payload issue_types: ["character_consistency"]
canon_commit

第20章 review verdict: warn
payload includes continuity role mismatch / character consistency
canon_commit
```

Fault flow:

1. Repeated reveal beats were not tracked as first-class `reveal_id` / `payoff_id` objects.
2. The LLM reviewer prompt allows pass/warn when a chapter has micro-progress.
3. The reviewer payload contains only `body_head`, `body_tail`, first four `scene_outputs`, first five `new_events`, first four `thread_beats`, and first five `state_changes`, so full-book duplicate reveal detection is weak.

Code roots:

- `forwin/reviewer/llm_webnovel.py:468` sends only trimmed body head/tail.
- `forwin/reviewer/llm_webnovel.py:471` caps scene evidence to the first four scene outputs.
- `forwin/reviewer/llm_webnovel.py:514` explicitly limits fail verdicts when there is some progress.

Fix study:

- Add a reveal registry with stable ids, first reveal chapter, repeats, escalations, and payoff status.
- Fail or at least block final completion when the same reveal is presented as new without escalation.
- Add deterministic duplicate-scene checks using normalized key terms, locations, involved characters, and reveal tags across recent chapters.

### P2 - Full-body repetition and scene stitching were not reliably reviewed

Observed text symptoms:

- Chapter 23 repeats trap/dialogue/escape material.
- Chapter 42 contains multiple inconsistent prisoner/sacrifice versions.
- Chapter 51 repeats the moderate-faction ultimatum.
- Chapter 58 repeats broadcast debate material.

Observed error log:

```text
第23章 initial fail -> 第1次 draft repair_succeeded -> pass with residual warnings -> canon_commit
第42章 pass/warn path accepted
第51章 pass/warn path accepted
第58章 pass/warn path accepted
```

Fault flow:

1. The reviewer received a compressed draft payload rather than the full body.
2. Duplicate material in the middle of a long chapter can be absent from `body_head` and `body_tail`.
3. A local rewrite can repair the immediately cited fail issue while leaving internal duplication or incompatible alternate scene versions in the body.

Fix study:

- Add deterministic full-body checks before LLM review: repeated paragraph hash, repeated dialogue window, repeated scene-object sequence, and incompatible alternate endings in one chapter.
- Include duplicate spans as evidence refs so the repair model can remove the exact repeated blocks.
- For repaired drafts, compare pre-repair and post-repair body-level duplication metrics before canon commit.

### P3 - Character identity and role drift remained soft

Observed text symptoms:

- 顾岚 drifts among black-market intermediary, former reviewer/examiner, and 30-year-old system designer.
- 洛庭若 drifts among warning source, adversary, radical faction, and moderate-helper behavior.
- 林远 / 林启明 / 林远舟 / group executive / father / grandfather / great-grandfather roles blur together.

Observed error log:

```text
第20章 review verdict: warn
payload includes continuity role mismatch / character consistency
canon_commit

第31章 review verdict: warn
payload issue_types: ["lint"]
canon_commit issue_count=1

第47章 first task 655ba8d603b2: fail after repair attempts
第47章 later task 846219d96b55: review verdict: pass
payload issue_types: ["consistency","legibility"]
canon_commit issue_count=2
```

Fault flow:

1. Role and relationship facts are present in prose but not consistently normalized to one actor/relationship state.
2. Warn-level consistency issues are canon-applied in blackbox mode.
3. Accepted chapters clear residual non-force-accept issues, so later chapters lose a strong machine-readable signal that the role identity still needs reconciliation.

Fix study:

- Promote identity/role changes to typed state transitions with aliases, relationship roles, temporal validity, and confidence.
- Treat conflicting family/role labels for central characters as blocking unless the chapter explicitly frames the conflict as an in-world lie/reveal.
- Persist residual warn issues that affect central-character identity into context for later reviewer prompts.

### P3 - Style repetition was not measured

Observed text symptoms:

- Repeated sensory/action templates: `铁锈味`, `旧纸味`, `冷白光`, `脚步声`, `通风管道`, `密钥发热`, `警报`.
- Repeated dialogue templates: `你疯了`, `别回头`, `你知道这意味着什么吗`.

Observed error log:

```text
No deterministic style telemetry was logged for repeated motif/dialogue density.
Affected chapters mostly reached pass/warn and canon_commit without a style-specific issue type.
```

Fault flow:

1. Style quality is mostly delegated to the LLM reviewer.
2. The reviewer does not receive a cross-chapter motif-density report.
3. Repeated imagery and dialogue can be individually acceptable but collectively lower quality across 60 chapters.

Fix study:

- Add a style ledger for high-frequency sensory motifs, action templates, and dialogue templates over a rolling window.
- Feed top repeated phrases and chapter-local repeats into reviewer evidence.
- Treat repeated templates as warn by default, but fail final chapters when repetition blocks payoff clarity or makes distinct scenes read as the same scene.

### Content-Fix Order

1. Remove `相关人员` prose fallback and add placeholder leakage tests.
2. Add typed character-state transitions and expand dead/rescue/execution conflict detection.
3. Add final-chapter closure gate for active hooks, countdowns, and terminal stakes.
4. Add structured ledgers for artifact counts and countdown/time pressure.
5. Add reveal registry and duplicate-reveal checks.
6. Add full-body duplication metrics before and after repair.
7. Persist central-character identity warn issues into later context instead of clearing them on normal acceptance.
8. Add rolling style telemetry for motifs, action templates, and dialogue templates.

## Patch-Stack Assessment

The current patch has three signs of patch stacking:

1. The direct `continue-generation` response was fixed, but worker progress, accept-and-continue, and retry-and-continue still use older requested-count semantics.
2. The checkpoint write path was tightened, but historical read compatibility was not addressed.
3. The world v4 alias migration added forwarding modules, but architecture-boundary assertions and module-level design markers were not fully moved with the code.

None of these require a large rewrite, but they should be handled before declaring the patch complete.

## Suggested Fix Order

1. Restore or move the world v4 compatibility boundary marker so the architecture test passes.
2. Create one continue-generation workset/count helper and use it across API, orchestrator progress, review accept/retry continue, MCP, and production scheduler.
3. Add worker-level tests proving `requested_chapters` cannot be overwritten above `max_chapters`.
4. Add schema validation for `ProjectContinueGenerationRequest.max_chapters`.
5. Add checkpoint status historical-data hardening and `resolved_at` update.
6. Make the browser mock regression derive ids from responses.
7. Clean tracked runtime artifacts in a separate hygiene patch.

## Verification Run During Review

Commands run:

```bash
git status --short --branch
git diff --name-status origin/master...HEAD
git diff --check origin/master...HEAD
.venv/bin/python -m pytest tests/test_architecture_boundaries.py::test_legacy_world_model_is_labeled_projection_in_runtime_paths -q
.venv/bin/python -m pytest tests/test_project_operation_guards.py::ProjectOperationGuardTests::test_continue_generation_task_requested_chapters_honors_max_chapters -q
.venv/bin/python - <<'PY'
from pydantic import ValidationError
from forwin.governance import BandCheckpointDetail
try:
    BandCheckpointDetail(status="approved")
except ValidationError as exc:
    print(type(exc).__name__)
PY
```

Observed results:

- Working tree was clean before this document was added; branch was ahead of `origin/master` by one commit.
- Current patch files are the one-commit diff from `origin/master...HEAD`.
- `git diff --check origin/master...HEAD` passed.
- Architecture boundary test failed as described above.
- New continue-generation task creation test passed.
- Historical invalid checkpoint status reproduces a Pydantic validation error.
