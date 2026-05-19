# Review Engine Shadow Layer Design

Date: 2026-05-19

Status: draft for user review

## Scope

This spec introduces `AutoDecisionEngine` as a unified decision entry point in shadow mode. It must reproduce existing stabilized behavior and record auditable decision events. It does not change live routing behavior by default.

## Goals

- Create typed engine inputs and outputs.
- Add a deterministic rule table abstraction.
- Wrap the existing dispatchers without deleting them.
- Persist `DecisionEvent` records for replay and UI explanation.
- Add parity tests comparing engine output to the old dispatcher chain.
- Run shadow comparison by default before traffic is switched.

## Non-Goals

- Do not change repair scope strategy.
- Do not add auto-approve rules.
- Do not implement arc/book patch execution.
- Do not remove `ReviewOutcomeRouter`, `RepairPolicy`, `ObligationScopeRouter`, or `FinalAcceptanceGate`.
- Do not introduce an LLM supervisor.

## Architecture

New package:

```text
forwin/review_engine/
  __init__.py
  types.py
  engine.py
  audit.py
  parity.py
  rules/
    __init__.py
    review_outcome.py
    repair.py
    obligation_scope.py
    final_acceptance.py
```

### DecisionInput

The engine input is a fact bundle. It should not fetch hidden state.

Required fields:

- `project_id`
- `chapter_number`
- `review`
- `signals`
- `open_obligations`
- `operation_mode`
- `attempts_completed`
- `prior_scope_history`
- `budget`
- `target_total_chapters`
- `plan_layer_health`

`plan_layer_health` contains counts and health facts needed for routing, such as active patch counts, overdue obligations, and missing plan layers.

### Decision

The engine returns:

- `outcome`
- `reason`
- `rule_id`
- `missing_evidence`
- `routed_from`
- `sub_action`

P1 valid outcomes are limited to existing behavior equivalents. Future outcomes may exist in type definitions, but no P1 rule emits them unless they map to an existing dispatcher result.

## Rule Table

Rules are ordered and deterministic. Each rule has:

- `rule_id`
- `source_dispatcher`
- `priority`
- `matches(input)`
- `decide(input)`

P1 rules may call existing dispatcher methods internally. The point of P1 is unified input/output and audit, not rewriting decision logic.

## Orchestrator Integration

Add engine construction to the runtime path without deleting old calls:

- In shadow mode, orchestrator runs the old dispatcher chain as the live path.
- Engine also decides on the same facts.
- Differences are logged as shadow mismatches.
- The chapter follows old behavior.

When `review_engine.shadow_mode=false` and `review_engine.enabled=true`, orchestrator can use engine output as the live decision source. This switch must not be enabled until parity tests and replay pass.

## Audit

Persist one audit record per decision:

- `project_id`
- `chapter_number`
- `rule_id`
- `outcome`
- `reason`
- `missing_evidence`
- `input_digest`
- `routed_from`
- `shadow_mismatch`
- `timestamp`

Implementation should reuse the existing governance or canon-quality event style instead of creating an orphan persistence pattern.

## Parity

Parity fixtures cover:

- clean pass
- warn handled as current mode allows
- fail routed to local rewrite
- fail routed to manual review
- obligation deferred
- obligation blocked
- final acceptance force-accept
- final acceptance manual-required

Each fixture asserts:

- old dispatcher outcome
- engine outcome
- byte-stable normalized outcome payload
- audit event contains rule id and routed-from source

## Tests

Focused tests:

- `tests/review_engine/test_types.py`
- `tests/review_engine/test_rule_parity.py`
- `tests/review_engine/test_audit.py`
- existing dispatcher tests remain unchanged

Verification commands:

```bash
python3 -m pytest tests/test_review_outcome_router.py tests/test_repair_progress.py tests/test_final_gate_obligation_clearance.py -q
python3 -m pytest tests/review_engine -q
python3 -m compileall -q forwin
git diff --check
```

## Done Criteria

- Engine package exists with typed input/output.
- Shadow mode records decisions and mismatches.
- Parity fixtures pass.
- Existing behavior remains unchanged with default config.
- Old dispatchers still exist and remain directly testable.

## Risk Controls

- Build in shadow mode first.
- Keep engine inputs explicit.
- Treat parity differences as implementation bugs, not policy changes.
- Keep rollback simple: `review_engine.enabled=false`.

## Self-Review

- Placeholder scan: no unspecified modules or behavior.
- Scope check: this is an architecture shell and parity phase only.
- Consistency check: no behavior-changing repair or auto-approve work is included.
