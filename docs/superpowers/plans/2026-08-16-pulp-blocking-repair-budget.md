# Pulp Blocking Repair Budget Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Allow one audited repair for blocking pulp review failures while retaining zero rewrites for ordinary pulp failures.

**Architecture:** `ReviewPolicy` owns both the ordinary and blocking rewrite limits and exposes one effective-limit method. The repair loop supplies only whether the current structured verdict contains a blocking issue, then continues through the existing scope router, repair executor, verifier, persistence, and final residual policy.

**Tech Stack:** Python 3.13, Pydantic, pytest, existing ForWin repair decision engine.

## Global Constraints

- Do not name or branch on a project, chapter, title, reviewer, or signal kind.
- Keep pulp `max_rewrites=0`; only `blocking=true` issues receive the separate allowance.
- Keep standard profile behavior at three rewrites.
- Operator-scoped failures continue to route to manual review.
- R23 remains invalid; R24 must start from chapter 1 with new exact-revision images.

---

### Task 1: Add And Enforce The Blocking Rewrite Budget

**Files:**
- Modify: `forwin/runtime/policy.py`
- Modify: `forwin/review/repair/service.py`
- Test: `tests/test_runtime_policy.py`
- Test: `tests/test_rc_repair_control.py`

**Interfaces:**
- Produces: `ReviewPolicy.blocking_rewrites: int`
- Produces: `ReviewPolicy.effective_rewrite_limit(*, has_blocking_issue: bool) -> int`
- Consumes: `ContinuityIssue.blocking` from the current `ReviewVerdict.issues`

- [ ] **Step 1: Write failing policy tests**

Extend the pulp policy test with:

```python
assert policy.review.max_rewrites == 0
assert policy.review.blocking_rewrites == 1
assert policy.review.effective_rewrite_limit(has_blocking_issue=False) == 0
assert policy.review.effective_rewrite_limit(has_blocking_issue=True) == 1
```

Also assert the standard profile returns three for both blocking and
non-blocking inputs.

- [ ] **Step 2: Write failing repair-loop tests**

Extend `_runtime_policy` and `_RepairHarness` so tests can set
`blocking_rewrites`. Let `_hard_failure_review` accept `blocking: bool = False`
and pass it to `ContinuityIssue`.

Add a test using `max_rewrites=0`, `blocking_rewrites=1`, and
`_hard_failure_review(blocking=True)`. Stub `decide_repair_v2` to return an
executable `chapter_patch`, then make `_apply_repair_patch` raise a sentinel.
Assert the sentinel is raised, proving the repair decision ran.

Keep `test_zero_phase_budget_starts_no_rewrite` non-blocking and unchanged in
behavior. Add a completed `review_repair` attempt to the blocking case and
assert it goes directly to final residual, proving the allowance is one.

- [ ] **Step 3: Run RED tests**

Run:

```bash
uv run pytest -q \
  tests/test_runtime_policy.py \
  tests/test_rc_repair_control.py
```

Expected: failures because `blocking_rewrites` and
`effective_rewrite_limit` do not exist and the zero ordinary budget prevents
the blocking repair.

- [ ] **Step 4: Implement the policy contract**

Add to `ReviewPolicy`:

```python
blocking_rewrites: int = Field(default=0, ge=0, le=12)

def effective_rewrite_limit(self, *, has_blocking_issue: bool) -> int:
    if not has_blocking_issue:
        return self.max_rewrites
    return max(self.max_rewrites, self.blocking_rewrites)
```

Set `blocking_rewrites=1` in the pulp profile. Standard relies on the default
because its ordinary limit is already three.

- [ ] **Step 5: Enforce the effective phase limit**

In `_run_repair_loop_for_phase`, compute:

```python
phase_limit = self.policy.review.effective_rewrite_limit(
    has_blocking_issue=any(issue.blocking for issue in current_review.issues)
)
```

Replace the comparison against `self.policy.review.max_rewrites` with
`phase_limit`. Do not change scope routing or final residual behavior.

- [ ] **Step 6: Run GREEN and focused regression tests**

Run:

```bash
uv run pytest -q \
  tests/test_runtime_policy.py \
  tests/test_rc_repair_control.py \
  tests/test_canon_repair_stage.py \
  tests/test_generation_auto_continue.py \
  tests/test_candidate_draft_records.py
uv run python -m compileall -q forwin tests
git diff --check
```

Expected: all tests pass, compileall exits zero, and diff check is clean.

- [ ] **Step 7: Review and commit**

Verify the diff contains only the policy, repair-loop, and focused tests. Then
commit:

```bash
git add \
  forwin/runtime/policy.py \
  forwin/review/repair/service.py \
  tests/test_runtime_policy.py \
  tests/test_rc_repair_control.py
git commit -m "fix: repair blocking pulp review failures"
```
