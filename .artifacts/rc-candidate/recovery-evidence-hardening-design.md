# ForWin v5 Live Recovery Evidence Hardening

## Status

Approved for implementation under the owner's standing instruction to continue
without approval checkpoints. This design is release-harness scope only. It
does not add a production runtime mode, compatibility path, or project-specific
content rule.

## Problem

The current live-recovery finalizer verifies artifact hashes, event-chain
shape, and report-supplied assertion values. It does not derive the assertions
from the `before`, `during`, and `after` snapshots. A report containing empty
snapshot state and copied contract values can therefore pass structural
validation.

The controller also lacks executable generation and MinIO barriers, business
state collectors, and report producers. Its service fault timestamps are
captured before the requested stop/start completes, and successful recovery
evidence does not require the disposable stack to be destroyed.

These are general release-evidence defects. They are independent of any
particular generated story, chapter, model response, or gate decision.

## Chosen Architecture

Use four narrow components:

1. `recovery_evidence.py`
   - Owns recovery report schema version 2.
   - Defines required snapshot state per fault.
   - Canonicalizes stable identities.
   - Recomputes every assertion as a pure function of immutable snapshots.
   - Rejects missing, empty, unknown, or internally inconsistent state.

2. `recovery_stack.py`
   - Continues to own only the isolated nine-service Compose lifecycle.
   - Records confirmed fault and recovery times after service state changes.
   - Records a terminal `destroyed` event whose snapshot proves every service
     is absent.
   - Exposes a typed evidence marker for API-level risk faults; the marker does
     not prove the business assertion, which remains snapshot-derived.

3. Subsystem runners
   - `generation_projection_recovery.py`
   - `minio_recovery.py`
   - `publisher_recovery.py`
   - Each runner creates a general fixture, installs only scoped disposable
     barriers, performs supported MCP/API operations, captures read-only SQL
     and service state, and writes one report.
   - Every cleanup path is in `finally`; barrier inventory must return to the
     pre-run baseline before the stack is destroyed.

4. `finalize_recovery.py`
   - Imports the shared semantic validator.
   - Reloads and hashes every snapshot.
   - Recomputes assertions instead of trusting the report.
   - Requires and independently validates the scoped barrier artifact for both
     generation and both projection faults.
   - Requires exactly one fresh lifecycle, one typed fault/recovery pair, and
     one terminal destroy event for every one of the eleven faults.
   - Rejects report assertions or prose that disagree with derived state.

## Evidence Model

Every snapshot has this envelope:

```json
{
  "schema_version": 2,
  "source_sha": "<candidate sha>",
  "fault_kind": "<fault>",
  "fault_id": "<globally unique id>",
  "stage": "before|during|after",
  "state": {
    "target": {},
    "mcp": {},
    "api": {},
    "database": {},
    "external": {},
    "barrier": {}
  }
}
```

The exact required state is fault-specific. Stable identities exclude mutable
timestamps, retry counters, leases, and status fields unless the relevant
contract explicitly compares them.

Reports use schema version 2 and retain the current top-level contract keys for
collector compatibility. `assertions` is output from the shared evaluator, not
operator input. `calculations` records the formula name, normalized inputs,
derived value, expected value, and pass/fail result.

## Deterministic Barriers

Generation pre-commit and post-commit faults use narrowly scoped PostgreSQL
advisory-lock triggers. The runner proves one holder, one blocked worker PID,
the expected relation/operation, and zero residual non-system trigger/function
objects after cleanup.

Post-Canon MinIO uses the same pattern at the `review_approved` event boundary:
Canon is committed, the request is held before phase 3, MinIO is stopped, and
the lock is released. The runner rejects a fixture that did not attempt the
expected world artifact write.

Qdrant and projection-consumer faults use the exact-project/chapter pre-Canon
barrier. The selected service is stopped only after the real automatic Canon
transaction is proven to be the sole blocked generation-worker waiter; barrier
release then creates the durable projection event without relying on a
transient manual-review state or changing project policy.

Publisher backend recovery uses a projectless cover fixture and a database
state boundary after the backend claim is durable. Publisher browser and risk
contracts use supported extension attempt fencing. They must not require a
real external publication or store authentication secrets in evidence.
Browser recovery proves the durable journal/reconciliation boundary; the three
risk fixtures prove typed pause, no bypass, authenticated idempotent resume,
and no receipt duplication.

If an exact deterministic boundary cannot be reached, the runner returns
`setup_blocked`. It must not change fixture content until a convenient model
response happens, and it must never convert an unobserved boundary into PASS.

## Lifecycle Contract

Each fault uses a new evidence directory and disposable volume set:

```text
fresh_up_started
fresh_up_completed
fault confirmed
recovery confirmed
destroyed
```

Additional snapshots and typed markers may appear between those events. The
final event must prove all nine services are absent. Event timestamps describe
confirmed state, not request initiation time.

Risk faults receive their own independent event chain and fault ID just like
service faults.

## Publisher Contract Expansion

Backend recovery additionally proves:

- owner token changed after reclaim;
- the stale token was rejected;
- the recovered shared cover path is readable;
- orphan temporary cover files were scavenged.

Browser/risk fixtures use projectless upload jobs so evidence does not depend
on a particular novel. External platform mutation is forbidden during these
fixtures; receipts must remain absent.

## Error Handling

- Missing prerequisite or unreachable deterministic boundary:
  `setup_blocked`.
- Observed invariant violation: `fail`.
- Harness or cleanup residue: `fail`.
- Only a fully derived contract with terminal cleanup can be `pass`.

Reports and final manifests are atomic and no-clobber. Failed evidence is kept
under its original directory; retries require a new fault ID and directory.

## Verification

Focused verification must prove:

- empty snapshot state is rejected for all eleven faults;
- changing a snapshot without updating a report is detected;
- changing report assertions without changing snapshots is detected;
- missing or nonterminal destroy is rejected;
- service timestamps equal confirmed event times;
- risk faults require independent lifecycle chains;
- barrier residue is rejected;
- every producer output passes the same pure evaluator imported by the
  finalizer;
- the final collector binds the new producers and evaluator to the candidate
  source revision.

The full release gates and all live faults are run only after the new harness is
committed, final images are rebuilt at that exact source SHA, and the R6 matrix
has been independently finalized.
