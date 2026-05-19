# Review Engine Verifier Auto-Approve UI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Close the obligation lifecycle, add safe auto-approve rules, and expose review-engine decisions in UI and dashboard surfaces.

**Architecture:** Add verifier-driven repository transitions first, then add auto-approve rules that require strict canon gate success, then make UI consume persisted decision events rather than re-deriving review decisions.

**Tech Stack:** Python 3, Pydantic, SQLAlchemy, pytest, existing ForWin narrative obligation repository, canon quality gate, review engine audit, API page rendering, and browser tests.

---

### Task 1: Add Obligation Repository State Transitions

**Files:**
- Modify: `forwin/narrative_obligations/repository.py`
- Test: `tests/test_narrative_obligation_ledger.py`

- [ ] **Step 1: Write repository transition tests**

Add tests:

```python
def test_mark_obligation_resolved_records_evidence() -> None:
    repo = NarrativeObligationRepository(session)
    created = repo.create_obligation(_active_obligation("obl-1"))

    resolved = repo.mark_obligation_resolved(
        created.id,
        verifier_result={"status": "pass", "matched_markers": ["marker-1"]},
        evidence_refs=["chapter:12"],
    )

    assert resolved is not None
    assert resolved.status == "resolved"
    assert resolved.resolution_evidence_refs == ["chapter:12"]
```

Add tests for `expire_obligation`, `block_expired_obligation`, and `waive_obligation` rejecting `actor=""` and `actor="system"`.

- [ ] **Step 2: Run transition tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_narrative_obligation_ledger.py::test_mark_obligation_resolved_records_evidence -q
```

Expected: FAIL because transition methods do not exist.

- [ ] **Step 3: Implement transition methods**

In `NarrativeObligationRepository`:

```python
def mark_obligation_resolved(self, obligation_id: str, verifier_result: dict, evidence_refs: list[str]) -> NarrativeObligation | None:
    row = self.session.get(NarrativeObligationRow, obligation_id)
    if row is None:
        return None
    row.status = "resolved"
    row.resolved_at = datetime.now(UTC)
    row.resolution_evidence_refs_json = _json(evidence_refs)
    metadata = _loads(row.metadata_json, {})
    metadata["verifier_result"] = verifier_result
    row.metadata_json = _json(metadata)
    self.session.add(row)
    self.session.flush()
    return self._obligation_from_row(row)
```

Implement:

```python
def expire_obligation(self, obligation_id: str, reason: str) -> NarrativeObligation | None: ...
def block_expired_obligation(self, obligation_id: str) -> NarrativeObligation | None: ...
def waive_obligation(self, obligation_id: str, reason: str, actor: str) -> NarrativeObligation | None: ...
```

`waive_obligation()` raises `ValueError` when actor is blank or `system`.

- [ ] **Step 4: Run repository tests**

Run:

```bash
python3 -m pytest tests/test_narrative_obligation_ledger.py -q
```

Expected: pass.

### Task 2: Add Obligation Resolution Verifier Integration

**Files:**
- Create or modify: `forwin/canon_quality/obligation_verifier.py`
- Modify: `forwin/orchestrator_loop_core/acceptance.py`
- Test: `tests/test_obligation_resolution_verifier.py`

- [ ] **Step 1: Write verifier pass test**

Create:

```python
def test_verifier_pass_marks_obligation_resolved_after_acceptance() -> None:
    verifier = ObligationResolutionVerifier()
    result = verifier.verify(
        obligation=_obligation(payoff_test="第12章必须解释钥匙来源"),
        accepted_chapter_text="第12章解释了钥匙来源，并给出证据。",
    )

    assert result.status == "pass"
```

- [ ] **Step 2: Run verifier test and verify RED**

Run:

```bash
python3 -m pytest tests/test_obligation_resolution_verifier.py::test_verifier_pass_marks_obligation_resolved_after_acceptance -q
```

Expected: FAIL if verifier does not exist or does not return pass.

- [ ] **Step 3: Implement verifier result shape**

Use existing verifier code if present. The minimal module shape:

```python
@dataclass(frozen=True)
class ObligationVerifierResult:
    status: Literal["pass", "warn", "fail"]
    evidence_refs: list[str]
    matched_markers: list[str]
    reason: str


class ObligationResolutionVerifier:
    def verify(self, *, obligation: NarrativeObligation, accepted_chapter_text: str) -> ObligationVerifierResult:
        payoff = str(obligation.payoff_test or "").strip()
        if payoff and any(token for token in payoff.split() if token and token in accepted_chapter_text):
            return ObligationVerifierResult("pass", [f"chapter:{obligation.deadline_chapter}"], [payoff], "payoff marker found")
        return ObligationVerifierResult("warn", [], [], "payoff marker not found")
```

The real implementation may use stronger marker extraction, but only `pass` can resolve.

- [ ] **Step 4: Integrate after acceptance**

In `accept_review()`, after canon is accepted and before commit:

```python
if getattr(self.config, "review_engine_obligation_verifier_enabled", False):
    self._verify_active_obligations_after_acceptance(
        session=session,
        project_id=project_id,
        chapter_number=chapter_number,
        accepted_text=writer_output.body,
    )
```

Add helper on orchestrator module that loads active obligations, runs verifier, and calls repository transition only for `status == "pass"`.

- [ ] **Step 5: Run verifier tests**

Run:

```bash
python3 -m pytest tests/test_obligation_resolution_verifier.py tests/test_narrative_obligation_ledger.py -q
```

Expected: pass.

### Task 3: Add Expiry and Block Checks

**Files:**
- Modify: `forwin/orchestrator_loop_core/acceptance.py`
- Test: `tests/test_final_gate_obligation_clearance.py`

- [ ] **Step 1: Write expiry test**

Add a test where an active P1 obligation deadline is before the accepted chapter and verifier does not pass. Assert it becomes expired or blocking according to existing policy.

- [ ] **Step 2: Run expiry test and verify RED**

Run:

```bash
python3 -m pytest tests/test_final_gate_obligation_clearance.py::test_expired_unresolved_obligation_blocks_after_acceptance -q
```

Expected: FAIL because expiry check is not integrated.

- [ ] **Step 3: Implement expiry helper**

Add:

```python
def _expire_unresolved_obligations_after_acceptance(*, repo: NarrativeObligationRepository, project_id: str, chapter_number: int) -> list[NarrativeObligation]:
    expired: list[NarrativeObligation] = []
    for obligation in repo.list_active_for_context(project_id, chapter_number=chapter_number + 1):
        if obligation.status != "active":
            continue
        if int(obligation.deadline_chapter or 0) <= int(chapter_number or 0):
            updated = repo.expire_obligation(obligation.id, reason="deadline passed after accepted chapter")
            if updated is not None:
                expired.append(updated)
    return expired
```

If policy requires immediate blocking, call `block_expired_obligation()` after expiry.

- [ ] **Step 4: Run obligation clearance tests**

Run:

```bash
python3 -m pytest tests/test_final_gate_obligation_clearance.py tests/test_obligation_resolution_verifier.py -q
```

Expected: pass.

### Task 4: Add Auto-Approve Rules

**Files:**
- Create: `forwin/review_engine/rules/auto_approve.py`
- Modify: `forwin/config.py`
- Test: `tests/review_engine/test_auto_approve.py`

- [ ] **Step 1: Write auto-approve tests**

Create:

```python
def test_copilot_warn_only_auto_approves_when_flag_enabled() -> None:
    decision = decide_auto_approve(
        input=decision_input(verdict="warn", mode="copilot", error_signals=[], blocking_obligations=[]),
        canon_gate_passed=True,
        auto_approve_enabled=True,
        future_plan_audit_passed=True,
        obligation_audit_passed=True,
    )

    assert decision.outcome == "auto_approve"
    assert decision.rule_id == "copilot_safe_warn"
```

Add flag-off test expecting `manual_review` with `policy_disabled`.

- [ ] **Step 2: Run auto-approve tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_auto_approve.py -q
```

Expected: FAIL because auto-approve rule does not exist.

- [ ] **Step 3: Implement auto-approve helper**

Create:

```python
def decide_auto_approve(
    *,
    input: DecisionInput,
    canon_gate_passed: bool,
    auto_approve_enabled: bool,
    future_plan_audit_passed: bool,
    obligation_audit_passed: bool,
) -> Decision:
    if not auto_approve_enabled:
        return Decision("manual_review", "policy disabled: review_engine.auto_approve_enabled=false", "auto_approve_policy_disabled", [], "AutoDecisionEngine", {})
    if input.operation_mode == "copilot" and input.review.verdict == "warn" and canon_gate_passed and future_plan_audit_passed and obligation_audit_passed:
        if not _has_error_signals(input.signals) and not _has_blocking_obligations(input.open_obligations):
            return Decision("auto_approve", "warn-only with passing gates", "copilot_safe_warn", [], "AutoDecisionEngine", {})
    return Decision("manual_review", "auto-approve conditions not met", "auto_approve_conditions_not_met", ["safe_warn_conditions"], "AutoDecisionEngine", {})
```

Add private helpers for error signals and blocking obligations.

- [ ] **Step 4: Run auto-approve tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_auto_approve.py -q
```

Expected: pass.

### Task 5: Persist Auto-Approve Decision Events

**Files:**
- Modify: `forwin/review_engine/audit.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Test: `tests/review_engine/test_audit.py`

- [ ] **Step 1: Add audit test for policy disabled explanation**

Add:

```python
def test_policy_disabled_decision_event_explains_auto_approve_flag() -> None:
    payload = build_decision_event_payload(
        decision=Decision("manual_review", "policy disabled: review_engine.auto_approve_enabled=false", "auto_approve_policy_disabled", [], "AutoDecisionEngine", {}),
        input_digest="digest",
        shadow_mismatch=False,
    )

    assert payload["reason"] == "policy disabled: review_engine.auto_approve_enabled=false"
    assert payload["rule_id"] == "auto_approve_policy_disabled"
```

- [ ] **Step 2: Run audit test**

Run:

```bash
python3 -m pytest tests/review_engine/test_audit.py -q
```

Expected: pass or fail with missing reason field.

- [ ] **Step 3: Ensure event payload stores reason and rule id**

Update audit payload builder to include:

```python
"reason": decision.reason,
"rule_id": decision.rule_id,
"outcome": decision.outcome,
"missing_evidence": list(decision.missing_evidence),
```

- [ ] **Step 4: Run audit and auto-approve tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_audit.py tests/review_engine/test_auto_approve.py -q
```

Expected: pass.

### Task 6: Add UI Decision Breakdown Data

**Files:**
- Modify: `forwin/api_project_payloads/project_detail.py`
- Modify: `forwin/api_pages_home.py`
- Modify: `forwin/ui_assets/home/body.html`
- Modify: `forwin/ui_assets/home/app_task_governance.js`
- Test: `tests/test_api_pages_rendering.py`

- [ ] **Step 1: Write API rendering test**

Add a fixture project detail with decision event payload:

```python
def test_home_page_renders_review_engine_decision_breakdown() -> None:
    html = render_home_page(
        projects=[],
        runtime_settings={},
        review_engine_breakdown=[
            {"rule_id": "auto_approve_policy_disabled", "outcome": "manual_review", "count": 2}
        ],
    )

    assert "auto_approve_policy_disabled" in html
```

- [ ] **Step 2: Run rendering test and verify RED**

Run:

```bash
python3 -m pytest tests/test_api_pages_rendering.py::test_home_page_renders_review_engine_decision_breakdown -q
```

Expected: FAIL because page renderer does not accept or show breakdown.

- [ ] **Step 3: Add breakdown payload**

Build breakdown by grouping persisted decision events by:

- outcome
- rule id
- policy disabled reason
- missing evidence

Expose a JSON-safe list to the home page state:

```python
"review_engine_breakdown": [
    {
        "outcome": outcome,
        "rule_id": rule_id,
        "reason": reason,
        "missing_evidence": missing_evidence,
        "count": count,
    }
]
```

- [ ] **Step 4: Render breakdown in UI**

Add a compact dashboard section with categories:

- manual judgment required
- system blocked
- auto-handled
- auto-handle available but policy disabled

Keep text concise and data-driven. Do not re-run decision logic in JavaScript.

- [ ] **Step 5: Run rendering tests**

Run:

```bash
python3 -m pytest tests/test_api_pages_rendering.py -q
```

Expected: pass.

### Task 7: Add Review Detail Explanation

**Files:**
- Modify: `forwin/project_payloads/project_detail.py`
- Modify: `forwin/ui_assets/home/app_task_governance.js`
- Test: `tests/browser/test_governance_and_chapters.py`

- [ ] **Step 1: Add browser-facing fixture test**

Extend browser fixture so a review detail includes:

```json
{
  "rule_id": "arc_patcher_disabled",
  "reason": "arc patcher disabled",
  "missing_evidence": ["arc_patch"],
  "routed_from": "AutoDecisionEngine"
}
```

Assert the UI displays rule id and missing evidence.

- [ ] **Step 2: Run browser test and verify RED**

Run:

```bash
python3 -m pytest tests/browser/test_governance_and_chapters.py::test_review_detail_shows_review_engine_reason -q
```

Expected: FAIL because review detail does not render these fields.

- [ ] **Step 3: Add detail payload and rendering**

Add latest decision event fields to chapter review payload:

```python
review_engine_decision={
    "rule_id": rule_id,
    "reason": reason,
    "missing_evidence": missing_evidence,
    "routed_from": routed_from,
}
```

Render them in the existing review detail panel.

- [ ] **Step 4: Run browser test**

Run:

```bash
python3 -m pytest tests/browser/test_governance_and_chapters.py -q
```

Expected: pass.

### Task 8: Final Verifier Auto-Approve UI Verification

**Files:**
- Verify all modified Spec E files.

- [ ] **Step 1: Run focused backend tests**

Run:

```bash
python3 -m pytest tests/test_obligation_resolution_verifier.py tests/test_narrative_obligation_ledger.py tests/test_final_gate_obligation_clearance.py -q
python3 -m pytest tests/review_engine/test_auto_approve.py tests/review_engine/test_audit.py -q
python3 -m pytest tests/test_api_pages_rendering.py -q
```

Expected: all pass.

- [ ] **Step 2: Run browser-focused test**

Run:

```bash
python3 -m pytest tests/browser/test_governance_and_chapters.py -q
```

Expected: pass. If browser dependencies are unavailable, record the missing dependency and run the closest API rendering tests instead.

- [ ] **Step 3: Run syntax and diff hygiene**

Run:

```bash
python3 -m compileall -q forwin
git diff --check
```

Expected: both pass with no output.

- [ ] **Step 4: Commit verifier and UI work**

Run:

```bash
git add forwin/narrative_obligations forwin/canon_quality forwin/orchestrator_loop_core forwin/review_engine forwin/api_project_payloads.py forwin/api_pages_home.py forwin/ui_assets tests
git commit -m "feat: add review verifier auto approve and audit UI"
```
