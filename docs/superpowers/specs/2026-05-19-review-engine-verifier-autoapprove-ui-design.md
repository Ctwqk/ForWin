# Review Engine Verifier Auto-Approve UI Design

Date: 2026-05-19

Status: draft for user review

## Scope

This spec closes the obligation lifecycle, adds safe auto-approve policies, and exposes review-engine decisions in the UI and dashboard. It combines the original P4 and P5 because UI explanation depends on verifier and decision-event data.

## Goals

- Add obligation resolution verification.
- Add repository state transitions for resolved, expired, blocked, and waived obligations.
- Add safe auto-approve rules behind policy flag.
- Persist decision reasons for review detail and production dashboard.
- Show why a chapter is manual, system-blocked, auto-handled, or policy-disabled.

## Non-Goals

- Do not auto-waive obligations.
- Do not let `warn` or `uncertain` block without evidence.
- Do not approve chapters when canon admission strict gate fails.
- Do not build multi-tenant policy management.

## Obligation Verifier

`ObligationResolutionVerifier` evaluates active obligations after chapter acceptance.

Verifier result statuses:

- `pass`
- `warn`
- `fail`

Only `pass` can mark an obligation resolved. `warn` remains open and non-blocking unless a separate deadline rule blocks. `fail` remains open and may block if deadline or hardness rules apply.

## Repository State Transitions

Add methods to `forwin/narrative_obligations/repository.py`:

- `mark_obligation_resolved(obligation_id, verifier_result, evidence_refs)`
- `expire_obligation(obligation_id, reason)`
- `block_expired_obligation(obligation_id)`
- `waive_obligation(obligation_id, reason, actor)`

`waive_obligation` rejects empty actor and rejects `actor="system"`.

## Trigger Points

After chapter acceptance:

- load active obligations for the project
- run verifier against accepted chapter evidence
- mark `pass` obligations resolved
- run expiry checks
- expire or block overdue unresolved obligations according to policy
- write decision events for each state transition

Manual waive path:

- available only through explicit review/project operation API
- requires actor and reason
- writes decision event

## Auto-Approve Rules

Rules live in `forwin/review_engine/rules/auto_approve.py`.

### Copilot Safe Warn

Auto-approve when:

- `operation_mode == "copilot"`
- review verdict is `warn`
- no open error signals
- no blocking obligations
- strict canon gate passes
- `review_engine.auto_approve_enabled=true`

### Review Interval Safe

Auto-approve an interval checkpoint when:

- review interval triggered this checkpoint
- strict canon gate passes
- future plan audit passes
- obligation audit passes
- no blocking reviewer issue has evidence-backed error severity
- `review_engine.auto_approve_enabled=true`

If the flag is off, engine returns `manual_review` with reason `policy_disabled`.

## UI and Dashboard

Production dashboard adds waiting-review breakdown grouped by:

- outcome
- rule id
- policy disabled reason
- missing evidence

Review detail shows:

- `Decision.rule_id`
- `Decision.reason`
- `missing_evidence`
- `routed_from`
- policy-disabled explanation when applicable

Status categories:

- manual judgment required
- system blocked
- auto-handled
- auto-handle available but policy disabled

## Tests

Focused tests:

- `tests/test_obligation_resolution_verifier.py`
- `tests/test_narrative_obligation_ledger.py`
- `tests/review_engine/test_auto_approve.py`
- `tests/test_api_pages_rendering.py`
- `tests/browser/test_governance_and_chapters.py`

Fixture coverage:

- fulfilled obligation is marked resolved after verifier pass
- verifier warn does not mark resolved and does not block by itself
- expired unresolved obligation blocks at deadline
- waive with system actor is rejected
- copilot warn-only chapter auto-approves with flag on
- same chapter becomes explicit manual review with flag off
- UI displays rule id, reason, and missing evidence

Verification commands:

```bash
python3 -m pytest tests/test_obligation_resolution_verifier.py tests/test_narrative_obligation_ledger.py -q
python3 -m pytest tests/review_engine/test_auto_approve.py tests/test_api_pages_rendering.py -q
python3 -m pytest tests/browser/test_governance_and_chapters.py -q
python3 -m compileall -q forwin
git diff --check
```

## Done Criteria

- Resolved obligations stop blocking future chapters.
- Expired unresolved obligations are visible and block according to policy.
- Waive remains human-only.
- Safe auto-approve rules work only when flag is enabled.
- UI explains manual review, system block, auto-approve, and policy-disabled states.

## Risk Controls

- Only verifier `pass` mutates to resolved.
- Auto-approve requires strict canon gate pass.
- Policy-disabled auto-handling remains auditable.
- UI consumes decision events rather than re-deriving decisions.

## Self-Review

- Placeholder scan: no unassigned UI or verifier behavior.
- Scope check: verifier, auto-approve, and UI are linked by decision events and can be planned together.
- Consistency check: waived obligations remain manual-only.
