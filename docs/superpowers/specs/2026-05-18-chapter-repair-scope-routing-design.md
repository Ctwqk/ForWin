# Chapter Repair Scope Routing Design

Date: 2026-05-18

Status: approved for implementation planning

## Scope

A 30-chapter generation run stuck at chapter 18 exposed a structural mismatch between what the review-form catches and what the repair scope router can fix. The router escalates within the writer-output stack (draft → chapter_plan → band_plan) but does not touch the canon-state stack (subworld admission roster, active_rules, countdown ledger) or the validation infrastructure stack (form normalization, schema coercion). Three repair rounds therefore edited the wrong layer and produced a fail-loop with no progress.

This design adds the missing routing and the missing repair handlers, fixes the immediate code bug that polluted the loop, and stops the system from ever asking the writer to fix infrastructure problems.

## Investigation Evidence

Drawn from the chapter-18 fail-loop decision events and world-model state:

1. **plan contract over-binds mid-arc band**: chapter 18 of 30 is a band terminus, but its plan contract requires "close P0/P1 main debts". For a project with `target_total_chapters=30`, chapter 18 is mid-arc and should allow staged hand-off (hooks to next band).

2. **subworld admission empty for chapter 18**: `active_subworld_ids: []`. `守仓阙微阑` and `礼川诸州` exist in global entities and prior accepted chapters, but the chapter's subworld roster does not include them. Checker reports them as "not admitted in this chapter" — false positive.

3. **countdown ledger is a stale state source**: ledger only records early chapters. As-of chapter 17 the world model holds `main=57:00, lockdown_timer=09:47, 隐藏子程序倒计时=16:48`. Pre-write audit injected plan patches based on the stale ledger (`main <= 79`, `terminal_audit_window <= 78`). It told the writer to "register 隐藏子程序倒计时 in active_rules", but the writer can only modify body text and chapter plan, not the active_rules state source.

4. **form schema validation bug routed to writer as a "must fix"**: `_coerce_form_answer()` normalizes scalar `False` to `"false"` but does not normalize `{"value": False}` inside nested answer dicts. Schema validation fails on `countdowns.*.consistent_with_prior.value`. The Pydantic error string is inserted into the repair prompt's "must fix" list. The writer is asked to fix a typing issue in a Python schema — there is no possible prose change that satisfies this.

5. **reviewer verdict self-contradictory**: review meta reports "no fail issues" plus narrative-level suggestions, but outer verdict is fail. Deterministic checks (subworld_admission, plan_task_fulfillment, continuity) aggregate warns into hard fail without distinguishing "narrative warn the writer can fix" from "infra error nothing the writer can fix".

6. **repair scope router does not know about all repairable layers**: the escalation ladder is `draft → chapter_plan → band_plan`. When the root cause is in `subworld_admission` or `active_rules` or `form_normalization`, the ladder runs to its top without touching the real problem. Each round adds a new noise entity (裴星野 → 韩青 → PS-07 / 守仓阙微阑) instead of fixing root cause.

## Goals

- Route each form signal kind to the repair scope it can actually fix. `subworld_admission_missing` goes to a subworld handler, not to the writer. `countdown_state_drift` goes to a state-source handler, not to the writer. `form_schema_invalid` never reaches the writer.
- Auto-populate subworld admission for entities that already exist in canon, so accepted characters, factions, organizations, and locations do not register as "new and unauthorized".
- Make the pre-write countdown audit read live **BookState canonical state** through a stable query interface (not a stale ledger snapshot, not a parallel `world_model` truth source), and allow it to register new active rules through an `ActiveRuleStore` abstraction without going through the writer.
- Tag band plans by their role in the project (opening / mid_arc / final) and apply role-appropriate contract templates. Mid-arc bands do not require P0/P1 closure.
- Fix the `_coerce_form_answer` dict-bool normalization bug and prevent Pydantic schema errors from ever appearing in writer-facing repair instructions.
- Add fail-loop detection so the same scope failing twice with similar signals escalates to operator review with a structured report, instead of looping into a third round that will also fail.
- Enforce **verdict reconciliation**: if no remaining signals carry `severity in {error, critical, blocker}` and no LLM-review verdict says fail, the gate must return non-blocking. A deterministic aggregator may not synthesize a blocking verdict without at least one source signal of error+ severity.

## Non-Goals

- Do not redesign the form schema or the writer prompt assembly.
- Do not change how chapters are written (this is post-write repair routing, not writer behavior).
- Do not remove the existing draft / chapter_plan / band_plan escalation paths. They remain valid for failures that those layers can actually fix.
- Do not auto-resolve infrastructure errors silently. Code bugs that block reviews should fail loud and route to operator after one repair attempt, not loop three times.
- Do not change project-wide target_total_chapters handling outside the band role classification.

## Architectural Insight

The current repair model assumes every blocking signal can be fixed by rewriting prose or plans. That assumption holds for narrative-level issues (character acting out of personality, scene structure thin, etc.) but breaks for four other failure classes that this test exposed:

| Failure class | Where the bug actually lives | Current router target | Correct router target |
|---|---|---|---|
| Subworld admission missing accepted entity | chapter plan's `active_subworld_ids` / roster | draft → plan → band | subworld admission handler |
| Countdown state source stale | world model / countdown ledger | draft → plan → band | state-source handler (pre-write audit reads live world model) |
| Form schema typing bug | `_coerce_form_answer` Python code | draft (writer gets Pydantic error) | code fix + filter from writer instructions |
| Band role mis-classified | band plan contract template | (no current target) | band role classifier + template selector |

The fix is to model the repair router as a dispatch over `(signal_kind, signal_source_layer) → handler`, not as a linear escalation ladder.

## Phases

### Phase 1: Stop the Bleeding — Form Coercion Bug and Writer-Instruction Filter

Purpose: fix the immediate code bug that polluted three repair rounds, and prevent Pydantic errors from ever reaching the writer prompt.

Files:

- `forwin/canon_quality/chapter_review_form/llm_caller.py` (or wherever `_coerce_form_answer` lives)
- `forwin/canon_quality/chapter_review_form/evidence_validator.py`
- `forwin/reviewer/repair_prompt_builder.py` (or equivalent)
- `tests/test_form_coercion_dict_bool.py`
- `tests/test_repair_prompt_filters_infrastructure_errors.py`

Steps:

1. Extend `_coerce_form_answer` (or the relevant normalizer) to walk nested `{"value": ...}` dicts and apply the same scalar coercion: `False → "false"`, `True → "true"`, `None → ""`, `int → str`. Add a unit test for each of these dict-nested cases.

2. Define a `_INFRASTRUCTURE_ERROR_PATTERNS` allowlist (Pydantic ValidationError class names, schema mismatch markers, type-coercion errors). When the repair prompt builder assembles the "must fix" section, filter out any review issue whose source matches an infrastructure pattern.

3. Filtered infrastructure errors do not vanish silently. They emit a `repair_blocked_by_infrastructure` signal that:
   - Skips the writer repair attempt
   - Marks the chapter as `needs_operator_review` (not `needs_review`)
   - Records the exact infrastructure error and the file/function suspected, for operator triage

4. Regression test: feed a fixture with a `form_schema_invalid` review issue into the repair prompt builder; assert the issue does not appear in the writer prompt, and assert the chapter status becomes `needs_operator_review`.

Acceptance:

- The exact failure mode in chapter 18 (`countdowns.*.consistent_with_prior.value Input should be a valid string`) coerces correctly without writer involvement.
- No Pydantic ValidationError text reaches any writer prompt.
- Infrastructure-class failures route to operator after one attempt, not three.

---

### Phase 2: Subworld Admission Auto-Population from Canon

Purpose: stop reporting already-accepted entities as "unauthorized in this chapter".

Files:

- `forwin/planning/subworld_admission.py` (or wherever the admission list is built)
- `forwin/context/assembler_core/canon_quality_context.py`
- `forwin/canon_quality/chapter_review_form/canon_projector.py`
- `tests/test_subworld_admission_auto_population.py`

Steps:

1. Define `EntityKind` enum covering the full taxonomy of admission-relevant entities:

   ```python
   class EntityKind(StrEnum):
       person = "person"               # named individual character (covers former named_person)
       organization = "organization"   # faction, guild, government, polity (e.g. 礼川诸州, 档案公会)
       location = "location"           # place, region, building (e.g. 旧城遗档, 白塔)
       item = "item"                   # key prop, artifact, document with continuity weight
       code = "code"                   # numeric/code reference (covers former archive_code, system_id)
       concept = "concept"             # named rule, protocol, law, system mechanic
       placeholder = "placeholder"     # temporary stand-in not meant to persist as canon
   ```

   Each subworld admission entry carries a `kind`. Default for legacy entries with no recorded kind is `person`. The original 4-kind taxonomy (`named_person | archive_code | system_id | placeholder`) was person-centric and would false-positive on every faction, organization, or location — exactly the `礼川诸州` case from the investigation.

2. `build_subworld_admission(*, project_id, chapter_number, window_chapters=5) -> SubworldAdmission`:
   - Queries `EntityMention` (or canon's entity index) for entities of every kind appearing in the last `window_chapters` accepted chapters.
   - Auto-includes those entities in the admission roster with `auto_carried=True` payload flag, preserving each entity's recorded kind.
   - Does not auto-include entities marked with explicit `sunset_chapter` < current chapter.
   - Per-kind admission rules:
     - `person`, `organization`, `location`, `item`, `concept`: auto-carry from canon; new ones require explicit registration.
     - `code`: admitted via project-configured patterns (e.g. `^PS-\d+$`, `^[A-Z]+-\d+$`). Codes matching any pattern need no explicit registration.
     - `placeholder`: never persists beyond the chapter that introduces it.

3. `subworld_admission_signal` from the checker uses two thresholds:
   - Entity not in canon AND (its kind has no auto-pattern, or no pattern matches) → `blocking` signal, kind `subworld_admission_unauthorized_new_entity`
   - Entity in canon but not in current chapter's admission roster → `auto_fix_signal`, kind `subworld_admission_missing_canon_entity`

4. Repair router has a new `subworld` scope handler:
   - Consumes `subworld_admission_missing_canon_entity` signals
   - Adds the entity to the current chapter's roster with `auto_carried=True`, preserving its canon `kind`
   - Re-runs review without invoking the writer
   - Does not consume retry budget on the writer (this is a metadata fix, not a content fix)

5. Tests:
   - `person` in canon, missing from chapter admission → auto-fix path adds it, review passes without writer call.
   - `organization` in canon (e.g. `礼川诸州`), missing from chapter admission → auto-fix path adds it as `organization`, no false-positive person admission.
   - `location` in canon, missing from chapter admission → auto-fix path adds it as `location`.
   - Entity not in canon and no matching `code` pattern → blocking, escalates to writer.
   - Entity matching `PS-\d+` pattern → admitted as `code`, no blocking.

Acceptance:

- The `守仓阙微阑` (person) case auto-resolves: she is in canon, her absence from chapter 18's admission is auto-fixed without a writer round.
- The `礼川诸州` (organization) case auto-resolves the same way and is not misclassified as a person.
- The `PS-07` case admits cleanly as `code` if the project's code patterns include `^PS-\d+$`; otherwise it blocks once with a clear "register code pattern or admit entity" operator action.
- No "writer hallucinates a new noise entity" rounds happen.

---

### Phase 3: Canonical State Source — BookState Query Interface + ActiveRuleStore

Purpose: pre-write audit reads from BookState (the canon source of truth in the ForWin architecture) through a stable query interface; new active rules are registered through an `ActiveRuleStore` abstraction without writer involvement.

**Architecture contract**: BookState is the canon source. `world_model` is a projection of BookState and remains useful for visualization, compatibility, and debug, but **must not be treated as a parallel truth source** by repair-time consumers. All pre-write reads of "what is the current state of invariant X as of chapter N-1" go through `BookStateQueryInterface`, not through `world_model` directly. The previous design wording ("world model is single source of truth for current countdown value") was wrong and would have introduced a competing truth source that drifts from BookState.

Files:

- `forwin/planning/countdown_drift_pre_audit.py`
- New: `forwin/book_state/query_interface.py` (defines `BookStateQueryInterface` protocol)
- New: `forwin/canon_quality/active_rule_store.py` (defines `ActiveRuleStore` protocol)
- New: `forwin/canon_quality/active_rules_handler.py`
- `tests/test_bookstate_query_interface.py`
- `tests/test_active_rule_store.py`
- `tests/test_countdown_live_state_source.py`
- `tests/test_active_rules_auto_registration.py`

Steps:

1. Define `BookStateQueryInterface` protocol:

   ```python
   class BookStateQueryInterface(Protocol):
       def get_current_invariant_state(
           self, *, project_id: str, as_of_chapter: int
       ) -> InvariantStateSnapshot: ...
       def get_current_countdown_values(
           self, *, project_id: str, as_of_chapter: int
       ) -> dict[str, CountdownState]: ...
       def get_active_rules(
           self, *, project_id: str, as_of_chapter: int
       ) -> list[ActiveRule]: ...
   ```

   The default implementation reads BookState canon. `world_model` may be queried internally for performance, but the contract is BookState-backed. Pre-write audit holds a `BookStateQueryInterface` dependency, not a `world_model` reference.

2. Pre-write audit reads countdown state through `BookStateQueryInterface.get_current_countdown_values(...)`, not from a separately-stored ledger snapshot or directly from `world_model`. The interface is the contract; the implementation locates whichever canonical store actually holds the values.

3. When pre-write audit determines a countdown should advance / pause / register due to a known trigger (high-risk export, lockdown event, etc.), it emits two artifacts:
   - A `PlanPatch` instructing the writer to narrate the trigger (writer-facing)
   - An `ActiveRulePatch` that the new active rules handler applies through `ActiveRuleStore` (not writer-facing)

4. Define `ActiveRuleStore` protocol:

   ```python
   class ActiveRuleStore(Protocol):
       def register_rule(
           self, *, project_id: str, rule: ActiveRule, trigger_quote: TriggerQuote
       ) -> RegistrationResult: ...
       def query_active_as_of(
           self, *, project_id: str, chapter_number: int
       ) -> list[ActiveRule]: ...
       def revoke_rule(
           self, *, project_id: str, rule_key: str, revoke_chapter: int, reason: str
       ) -> RevocationResult: ...
   ```

   **Implementation step (mandatory before coding)**: verify the canonical persistence location for active rules in the current ForWin schema. Options include an existing `active_rules` table, a field on an existing canon-state table, or a row in the canon_quality signal ledger with a specific type. **Do not pre-commit the design to a specific table layout.** If no canonical persistence exists, the implementation step must raise this as a discovery and propose where it should live (likely as part of BookState or canon_quality, never as a new orphan store). The `ActiveRuleStore` protocol decouples the design from this decision.

5. New `active_rules_handler.apply_pre_write_active_rules(*, project_id, chapter_number, patches, store: ActiveRuleStore) -> ApplyReport`:
   - Validates each `ActiveRulePatch`: trigger event quote must reference an accepted prior chapter; new rule key must not collide with existing.
   - Applies validated patches transactionally through `store.register_rule(...)`.
   - Reports applied / rejected counts.

6. Repair router has a new `active_rules` scope handler:
   - Consumes `countdown_state_drift` and `active_rule_missing` signals
   - Tries to auto-register via `active_rules_handler` if the audit produced a corresponding `ActiveRulePatch`
   - Falls back to operator review if auto-registration is not possible (e.g., ambiguous trigger reference, no prior chapter cites it)
   - Does not consume writer retry budget unless the writer is the right fix layer (e.g., the chapter explicitly contradicts the rule)

7. Tests:
   - Stale ledger vs BookState: ledger has no record; BookState says countdown=16:48. Pre-write audit reads BookState through the interface, emits patch with current value, not 79.
   - `BookStateQueryInterface` contract test: implementation must return the same value as BookState canon for the same chapter, even if `world_model` projection lags.
   - Active rule auto-register: chapter 17 narrates trigger, chapter 18 audit emits `ActiveRulePatch` registering 隐藏子程序倒计时. `ActiveRuleStore` records it transactionally without writer involvement.
   - Ambiguous trigger: audit cannot find a clear trigger quote → falls back to operator review, does not silently register.
   - `ActiveRuleStore` contract test: register → query returns the rule; revoke → query no longer returns it; double-register same key without revoke returns conflict.

Acceptance:

- Countdown values in pre-write audit come from BookState (via `BookStateQueryInterface`), not directly from `world_model` or a parallel ledger.
- The 隐藏子程序倒计时 case auto-registers an active rule from the accepted chapter-17 trigger through `ActiveRuleStore`; chapter 18 then has it in scope.
- Stale ledger snapshots no longer drive plan patches that contradict live BookState.
- No production code path queries `world_model.*` directly for repair-time canonical state; an architecture-boundary test enforces this.

---

### Phase 4: Band Role Classification and Contract Templates

Purpose: stop applying "close P0/P1 main debts" to mid-arc band termini.

Files:

- `forwin/planning/band_plan/band_role.py` (new)
- `forwin/planning/band_plan/contract_templates.py` (extract existing contract logic into templates)
- `forwin/planning/band_plan_service.py`
- `tests/test_band_role_classification.py`
- `tests/test_band_contract_template_selection.py`

Steps:

1. `classify_band_role(*, band_index, total_bands, target_total_chapters, last_chapter_of_band) -> BandRole`:
   - `BandRole.opening` if `band_index == 0`
   - `BandRole.final` if `last_chapter_of_band == target_total_chapters`
   - `BandRole.mid_arc` otherwise
   - Returns the role and a brief justification string (for observability).

2. Three contract templates in `contract_templates.py`:
   - `OPENING_BAND_CONTRACT`: establish protagonist goals, introduce world stakes, plant primary mystery hooks. No closure requirements.
   - `MID_ARC_BAND_CONTRACT`: deliver one staged payoff (subplot closure, evidence handoff, or alliance shift), advance main debts without closing them, end with explicit handoff hook to next band.
   - `FINAL_BAND_CONTRACT`: close all P0 main debts (main crisis, primary identity, terminal countdowns), allow one P1 to remain only if narratively framed as denouement.

3. `band_plan_service.generate_band_plan(...)` uses the classifier's verdict to select template. Existing band-plan generation logic continues to handle scene-level detail; only the contract section comes from the template.

4. Plan-time review checks that compare against contract requirements (the "close P0/P1" check that fired for chapter 18) look at the band's role and apply only the matching contract's requirements.

5. Migration: existing band plans for active projects keep their old contracts; classifier runs at next band-plan regeneration. Document this in operator notes.

6. Tests:
   - 30-chapter project, 5 bands of 6 chapters each: band 1 = opening, bands 2-4 = mid_arc, band 5 = final. Each gets the matching contract.
   - Mid-arc band's review check does not flag "P0 unaddressed" if the band delivers a staged payoff and hands off to the next band.
   - Final-band's review check does flag "P0 unaddressed" if the main crisis is left open.

Acceptance:

- Chapter 18 of 30 (mid-arc band terminus) is no longer judged against final-band contract requirements.
- The 18-25-30 fail mode (treating a mid hook as a forced closure) cannot recur on any project with `target_total_chapters > last_chapter_of_band`.
- A new test case verifying mid-arc vs final-band contract selection sits in the architecture-boundary test suite.

---

### Phase 5: Repair Scope Router — Dispatch by Signal Kind

Purpose: replace the linear `draft → chapter_plan → band_plan` escalation ladder with a kind-aware dispatch.

Files:

- `forwin/reviewer/repair_scope_router.py` (rewrite)
- `forwin/reviewer/repair_handlers/` (new directory with one handler per scope)
- `tests/test_repair_scope_router_dispatch.py`

Steps:

1. Define `RepairScope = Literal["draft", "chapter_plan", "band_plan", "subworld", "active_rules", "operator"]`.

2. Define a signal-kind → scope mapping:

   ```python
   SIGNAL_KIND_TO_SCOPE = {
       "form_open_signal_persisting": "draft",
       "form_obligation_unresolved": "chapter_plan",
       "form_countdown_inconsistency": "active_rules",
       "form_final_chapter_unresolved": "chapter_plan",
       "subworld_admission_missing_canon_entity": "subworld",
       "subworld_admission_unauthorized_new_entity": "draft",
       "personality_drift": "draft",
       "active_rule_missing": "active_rules",
       "form_schema_invalid": "operator",
       "writer_prompt_assembly_error": "operator",
   }
   ```

3. Each scope has a handler in `repair_handlers/`:
   - `draft.py`: existing writer-driven prose repair
   - `chapter_plan.py`: existing plan-text repair
   - `band_plan.py`: existing band-plan repair
   - `subworld.py`: from Phase 2 — auto-add canon entities to admission roster
   - `active_rules.py`: from Phase 3 — auto-register active rules from pre-write audit patches
   - `operator.py`: emit structured operator-review report, mark chapter `needs_operator_review`

4. Router logic:
   - For each blocking signal, look up its scope.
   - Group signals by scope.
   - Run scopes in priority order: `operator` (if any infrastructure error, escalate immediately) → `active_rules` → `subworld` → `band_plan` → `chapter_plan` → `draft`.
   - After each scope handler runs, re-validate. If clean, exit. If signals remain, continue down the priority list.
   - Each scope has its own retry budget (default 1 attempt). Exceeding the budget for any scope marks the chapter `needs_operator_review` with a structured report.

5. The `draft` and writer-facing scopes consume LLM budget; the `subworld` and `active_rules` scopes are metadata-only and free.

6. **Verdict reconciliation rule** (enforced in the router and the gate aggregator):

   ```
   IF (no remaining signal has severity in {error, critical, blocker})
   AND (no LLM-review emitted a fail verdict)
   THEN gate.verdict = pass (non-blocking)
   ```

   A deterministic aggregator may not synthesize a blocking verdict without at least one source signal at error+ severity. The investigation evidence shows the previous behavior — review meta "no fail issues" plus narrative suggestions, but outer verdict still fail — was the symptom of an aggregator inventing a fail without source. This rule eliminates that class.

   Architecture boundary test: scan `repair_scope_router`, `gate_aggregator`, and any code path that constructs a `verdict=fail` and assert each assignment is traceable to a specific source signal id. Tests that exercise the gate must assert that no `blocking=True` exits the aggregator without a recorded source.

7. **Exhaustiveness of `SIGNAL_KIND_TO_SCOPE`** (enforced by test, not by config):

   - Define `SignalKind` as a `StrEnum`; every enum value must appear as a key in `SIGNAL_KIND_TO_SCOPE`.
   - `tests/test_signal_kind_routing_exhaustive.py` iterates `SignalKind` and asserts every value has a mapping. Adding a new `SignalKind` without updating the table fails CI immediately.
   - The router's fallback for any kind not in the table is `RepairScope.operator` (never writer). This is belt-and-suspenders: exhaustiveness test prevents accidental omission at code-write time; the fallback prevents harm if the test is somehow bypassed.

8. Tests:
   - A signal mixing `form_schema_invalid` and `personality_drift` routes the schema issue to operator and skips the writer call entirely.
   - A signal mixing `subworld_admission_missing_canon_entity` and `personality_drift` first auto-fixes admission, then re-validates, then if drift remains routes to draft.
   - Three different blocking signals at three different scopes do not cause three writer rounds; they are dispatched in parallel where independent.
   - Verdict reconciliation: a chapter where all remaining signals are warnings and LLM review verdict is pass produces a non-blocking gate result. Adding any error-severity signal flips to blocking.
   - Exhaustiveness: every `SignalKind` enum value is in `SIGNAL_KIND_TO_SCOPE`. (Test fails if anyone adds a kind without updating the table.)

Acceptance:

- Chapter 18 fail mode reproduced as a test fixture: form_schema_invalid → operator (immediate, no writer), countdown_inconsistency → active_rules (auto-register if patch available), subworld_admission_missing_canon_entity → subworld (auto-add 守仓阙微阑 as person, 礼川诸州 as organization), only what remains goes to draft.
- The fail-loop pattern (3 writer rounds with no progress) becomes structurally impossible: the router will route to operator after one failed pass through the relevant scope.
- The verdict-reconciliation rule prevents the original "review meta says no fail, outer verdict says fail" contradiction from recurring at any layer.
- Adding a new `SignalKind` without routing it fails CI; no silent fallthrough.

---

### Phase 6: Fail-Loop Detection and Operator Report

Purpose: ensure no chapter ever wastes more than one repair round per scope, and operator always gets enough information to act.

Files:

- `forwin/reviewer/repair_loop_detector.py` (new)
- `forwin/canon_quality/chapter_review_form/operator_report.py` (new)
- `forwin/api/operator_review_routes.py` (or wherever the operator review surface lives)
- `tests/test_repair_loop_detection.py`

Steps:

1. `RepairLoopDetector` tracks per-chapter repair history: scope, signals, outcome. When two attempts at the same scope produce overlapping signal sets (Jaccard similarity > 0.7 on `(signal_kind, subject_key)` pairs), declare a loop and route to operator regardless of remaining retry budget.

2. `operator_report.build_report(*, project_id, chapter_number, repair_history) -> OperatorReport` produces a structured artifact:
   - All signals from the latest review (with severities)
   - Full repair history: each attempt's scope, signals before, signals after, what changed
   - Suspected root cause category (per the routing table)
   - Suggested operator actions (e.g., "register PS-07 as archive code pattern", "admit 守仓阙微阑 to chapter 18 manually", "investigate active_rules registration failure")
   - Link to relevant infrastructure (form artifact JSON, world model snapshot, plan)

3. Operator review status surfaces via the existing chapter status field. `needs_operator_review` chapters appear distinctly in the operator dashboard (or CLI listing) from `needs_review` chapters.

4. Repair loop detector logs a structured event when it triggers, so post-mortem analysis can find systemic loop patterns across projects.

5. Tests:
   - Two attempts at scope `draft` producing the same `personality_drift` signal triggers loop detector → operator route on attempt 2, not attempt 3.
   - Two attempts at different scopes (subworld then draft) with disjoint signals do not trigger loop detector.
   - Operator report contains all six required fields and is queryable via the operator review surface.

Acceptance:

- Chapter 18 fail mode: would now exit after attempt 1 with `needs_operator_review` and the structured report, not after attempt 3 with `needs_review` and ambiguous status.
- Operator can see at-a-glance which infrastructure layer is suspected and what specific action to take.
- Loop detector emits a metric so the operations team can monitor whether this class of failure recurs.

---

## Routing Table Design Choice

`SIGNAL_KIND_TO_SCOPE` is a hard-coded Python constant, not a config file, not a registry, not a plugin system. This is intentional.

### Why not YAML / external config

The routing table is a safety-critical path. Misrouting `form_schema_invalid` to `draft` is precisely the bug this whole design exists to fix; a YAML typo could re-introduce it silently. Code constants get type-checked, get reviewed in diff, and cannot misroute due to whitespace or a missing quote. Operator-facing flexibility is not a requirement: which scope can fix which signal is an architectural fact about the system, not a project preference.

### Why not a plugin / self-registering handler pattern

Handlers self-registering their kinds distributes the routing decision across N files. Reviewing "what handles X" requires reading every handler. With a single constant, the whole table is visible in one diff hunk. Conflicts (two handlers claiming the same kind) become possible silently. The cost of centralization (need to edit one file to add a kind) is exactly the right friction — adding a new kind should be a deliberate, reviewable act.

### Safeguards that make hard-coding safe

1. **Exhaustiveness test**: `SignalKind` is a `StrEnum`; the test in Phase 5 asserts every enum value has a routing entry. Forgetting to update the table fails CI loudly the moment the new kind is added.
2. **Default-to-operator fallback**: if any kind escapes the test (dynamic kinds, late-binding from external sources), the router defaults to `RepairScope.operator`. The failure mode of unknown routing is "human reviews it", never "writer gets junk".
3. **Type-safe scope values**: `RepairScope` is also a `StrEnum`. Typos in scope assignments fail at import time.

### Evolutionary path if per-project routing ever becomes a real need

A per-project override layer can be added later without changing the base table:

```python
def route(signal: Signal, project_id: str) -> RepairScope:
    overrides = load_project_routing_overrides(project_id)
    return overrides.get(signal.kind) or SIGNAL_KIND_TO_SCOPE.get(
        signal.kind, RepairScope.operator
    )
```

Overrides must be restricted to **escalating** severity (e.g. `draft → operator`), never de-escalating (e.g. `operator → draft`). This prevents per-project config from re-introducing the exact misrouting class the base table is designed to prevent. This override layer is YAGNI for now.

---

## Test Plan

Run after each phase:

- Phase 1: `pytest tests/test_form_coercion_dict_bool.py tests/test_repair_prompt_filters_infrastructure_errors.py -q`
- Phase 2: `pytest tests/test_subworld_admission_auto_population.py -q`
- Phase 3: `pytest tests/test_countdown_live_state_source.py tests/test_active_rules_auto_registration.py -q`
- Phase 4: `pytest tests/test_band_role_classification.py tests/test_band_contract_template_selection.py -q`
- Phase 5: `pytest tests/test_repair_scope_router_dispatch.py -q`
- Phase 6: `pytest tests/test_repair_loop_detection.py -q`

End-to-end regression: replay the chapter-18 failure as a fixture under `tests/fixtures/repair_routing/chapter_18_fail_loop/` containing the original signals, the world-model snapshot as-of chapter 17, and the form payload. Assert the new router resolves it in at most one pass through each scope without entering a draft repair round.

## Risk Controls

- **Auto-admission masks real entity-introduction problems**: distinguish `auto_carried=True` rows from explicit admissions in observability; operator dashboard surfaces auto-carry rate per chapter so a sudden spike indicates upstream plan-generation drift.
- **Active-rules auto-registration writes to canon without writer involvement**: every `ActiveRulePatch` carries a `trigger_event_quote` that must reference an accepted chapter. Patches without verifiable triggers fall through to operator review.
- **Loop detector triggers too aggressively**: Jaccard threshold (0.7) is configurable; ship with the conservative default and tune based on observed false-loop rate.
- **Operator review queue becomes the new bottleneck**: each operator report includes the specific suggested action so the operator can usually resolve in seconds. The router's job is to make the human decision narrow, not to make humans do the work.
- **Band role mis-classification breaks legacy projects**: classifier runs at next band-plan regeneration, not retroactively; existing in-flight bands keep their current contract until naturally regenerated.
- **Routing table omits a signal kind**: a default fallback routes unknown signal kinds to `operator` (not to writer). The Phase 5 exhaustiveness test catches this at CI time before the fallback is ever needed.
- **BookState contract drift**: if any new code path introduces a direct `world_model.*` read for repair-time canonical state, it bypasses the `BookStateQueryInterface` contract and re-creates the parallel-truth-source problem. An architecture-boundary test forbids such reads outside the `BookStateQueryInterface` implementation file itself; CI fails on violation.
- **`ActiveRuleStore` lands on the wrong persistence**: the implementation step explicitly requires verifying the canonical persistence location before coding. If no obvious home exists, the implementer escalates rather than inventing a new orphan table.
- **Verdict reconciliation lets a real fail slip through**: only signals at error+ severity can produce a blocking verdict. If a check legitimately needs to block, it must emit at error+ severity. This is a deliberate forcing function — checks that previously got "free blocking" via aggregation now have to declare their severity honestly.

## Known Limitations and Deferred Work

- **The repair handler architecture does not auto-fix personality drift**: this remains in scope `draft` and goes to the writer. A future iteration could add a `personality` scope handler that injects taboo lists into the writer prompt without a full prose rewrite.
- **The `archive_code` admission relies on operator-defined patterns per project**: there is no auto-discovery of code conventions from chapter text. A future iteration could mine accepted chapters for repeated code-like tokens and suggest patterns.
- **Active-rules handler does not back-fill rules into prior chapters**: it only registers rules from a trigger in the most recent chapter onward. Retroactive registration for historical chapters is operator-driven via the canon replay tool.
- **Band role classification is binary on `target_total_chapters`**: projects without a target (open-ended serialization) fall back to `mid_arc` for all non-opening bands. A separate mode for open-ended projects is out of scope.
- **Operator dashboard is not built here**: this design assumes operator can read structured JSON from the existing artifact surface. A polished UI is out of scope.

## Done Criteria

- `_coerce_form_answer` handles `{"value": <scalar>}` dicts; Pydantic schema errors do not appear in writer-facing repair instructions.
- Subworld admission auto-populates from the last 5 accepted chapters; entity-kind distinction (`named_person | archive_code | system_id | placeholder`) is enforced; code patterns are project-configurable.
- Pre-write countdown audit reads from world-model live state; `ActiveRulePatch` can register new rules without invoking the writer; ambiguous-trigger patches route to operator.
- Band plans are tagged by role and use role-appropriate contract templates; mid-arc band terminus does not trigger "close P0/P1" rules.
- Repair scope router dispatches by signal kind; metadata-only scopes (`subworld`, `active_rules`) run without consuming writer LLM budget; infrastructure errors route to `operator` immediately.
- Repair loop detector exits to operator after one failed pass per scope; structured operator report includes signals, history, suspected layer, suggested actions.
- Chapter-18 fail-loop fixture is part of the regression suite and resolves in at most one pass through each scope.
- A new architecture boundary test forbids any code path that routes a signal whose kind matches `_INFRASTRUCTURE_ERROR_PATTERNS` into a writer-facing repair prompt.
