# Review Engine Upgrade Program Design

Date: 2026-05-19

Status: draft for user review

## Purpose

This program upgrades the ForWin review system without turning manual review into a hidden fallback. The target architecture is a deterministic, auditable decision layer where every review outcome is explained by rule id, input facts, missing evidence, policy flags, and execution state.

The source plan in `docs/designs/review-engine-plan.md` is broad enough that it should not be implemented as one change. This spec splits it into five independently reviewable and testable work packages.

## Program Split

### Spec A: P0 Stabilization

File: `docs/superpowers/specs/2026-05-19-review-engine-p0-stabilization-design.md`

Fix the three verified bugs before adding new architecture:

- preserve arc-level repair scope instead of downgrading it
- make production review quota execute real review work
- pass deferred-obligation budget state into canon admission

### Spec B: AutoDecisionEngine Shadow Layer

File: `docs/superpowers/specs/2026-05-19-review-engine-shadow-layer-design.md`

Introduce the unified decision type, rule table, audit event, and shadow-mode parity checks. This phase must preserve behavior.

### Spec C: Scope-Driven Repair Policy

File: `docs/superpowers/specs/2026-05-19-review-engine-scope-driven-repair-design.md`

Move repair scope selection from attempt-count driven escalation to issue-scope driven routing, behind feature flag and shadow comparison.

### Spec D: Arc and Book Patch Outcomes

File: `docs/superpowers/specs/2026-05-19-review-engine-arc-book-patch-outcomes-design.md`

Make `arc_patch` and `book_patch` executable outcomes, with validators and completion gates.

### Spec E: Verifier, Auto-Approve, and UI Audit Surface

File: `docs/superpowers/specs/2026-05-19-review-engine-verifier-autoapprove-ui-design.md`

Close the narrative obligation lifecycle, add safe auto-approve rules, and expose decision reasons in UI and dashboard surfaces.

## Ordering

The order is strict for A through C:

1. Spec A must land first because it fixes known incorrect behavior.
2. Spec B depends on Spec A because parity tests should compare against stabilized behavior.
3. Spec C depends on Spec B because scope-v2 should be shadowed through the new engine.

Specs D and E can be developed after B, but the safer release order is D before E. Auto-approve must not hide unresolved arc/book patch debt.

## Shared Invariants

- BookState remains the canon source. Obsidian, LLM KB, World Studio, Qdrant, and older world-model surfaces are projections or compatibility layers.
- `warn`, `uncertain`, and no-evidence findings do not block unless a specific rule with evidence says they block.
- Review/gate normalization preserves provenance fields such as `source_layer`, `source_analyzer`, `source_mode`, `original_verdict`, `original_confidence`, and `blocking_origin`.
- No LLM supervisor is introduced. The review engine is deterministic and replayable.
- Original dispatcher classes remain in place until the engine has run in production-like shadow mode and the UI/audit surfaces can explain the decision path.
- `waived` obligations remain human-only and require actor plus reason.

## Feature Flags

The program uses reversible flags:

- `review_engine.enabled`
- `review_engine.shadow_mode`
- `review_engine.repair_v2_enabled`
- `review_engine.arc_patcher_enabled`
- `review_engine.book_patcher_enabled`
- `review_engine.obligation_verifier_enabled`
- `review_engine.auto_approve_enabled`

Default rollout policy:

- P0 fixes are direct bug fixes and do not require a new engine flag.
- New decision architecture starts in shadow mode.
- Behavior-changing repair, patch, verifier, and auto-approve rules start disabled unless a spec states otherwise.

## Program Acceptance

The program is complete when:

- P0 regressions are fixed and covered.
- Engine shadow parity has zero expected differences on a representative replay set.
- Repair v2 routes structural issues to the appropriate plan layer instead of using attempts as the primary scope driver.
- Arc/book patch outcomes are executable and auditable.
- Obligation verifier state transitions unblock fulfilled obligations and block expired unresolved obligations.
- Auto-approve only applies to safe, evidence-backed cases.
- UI surfaces can answer why a chapter needs manual review, why it was auto-handled, or why it was system-blocked.

## Self-Review

- Placeholder scan: no placeholder implementation slots are left in this index.
- Scope check: each spec can become a separate implementation plan.
- Consistency check: feature flags and dependencies match the phase split.
