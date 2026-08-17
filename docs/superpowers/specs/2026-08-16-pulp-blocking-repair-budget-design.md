# Pulp Blocking Repair Budget Design

## Context

R23 exposed a policy integration gap rather than a book-specific content bug.
The pulp profile deliberately sets `max_rewrites=0`, but deterministic Canon
checks can still return a blocking issue whose repair scope is executable. A
manual chapter regeneration keeps the same chapter plan and therefore repeats
the same failure.

## Decision

Keep `max_rewrites=0` for ordinary pulp review failures. Add an explicit
`blocking_rewrites` budget to `ReviewPolicy` and set it to one for pulp. The
standard profile keeps its existing three-rewrite budget and needs no separate
blocking allowance.

The repair loop computes its phase limit from `max_rewrites`. When the current
review contains at least one issue with `blocking=true`, it raises that limit
to at least `blocking_rewrites`. All existing scope routing, plan patching,
rewrite verification, decision events, attempt persistence, and final residual
handling remain unchanged.

This is issue-agnostic: it does not name a project, chapter, thread, reviewer,
or signal kind. Operator-scoped failures remain non-executable because the
existing repair decision routes them to manual review even when a phase budget
exists.

## Alternatives Rejected

1. Set pulp `max_rewrites=1`. This would also rewrite non-blocking pulp review
   failures and silently broaden the profile.
2. Special-case `closed_thread_reopened`. This would overfit R23 and leave the
   policy mismatch intact for the next deterministic Canon blocker.
3. Keep manually regenerating chapters. This cannot change a conflicting
   chapter plan and produces repeated, unauditable operator work.

## Data Flow

1. Review produces a failed `ReviewVerdict` with structured issues.
2. The repair loop resolves the effective phase budget from the frozen task
   policy and the issue `blocking` flags.
3. A blocking pulp verdict receives at most one normal repair attempt.
4. A non-blocking pulp verdict still proceeds directly to final residual
   handling with no repair attempt.
5. A failed blocking repair returns to the existing manual-review outcome.

## Verification

- Runtime policy serialization exposes pulp `max_rewrites=0` and
  `blocking_rewrites=1`.
- A blocking verdict under a zero ordinary budget starts one repair.
- A non-blocking verdict under the same policy starts no repair.
- A prior blocking repair exhausts the one-attempt allowance.
- Existing standard repair-cycle and phase-isolation tests remain green.

Any production change invalidates R23. The next matrix starts from chapter 1
with newly built exact-revision images.
