# Review Engine Safety-Net Removal Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Remove the review-engine runtime safety net so engine-native rules are the only live decision path, while keeping one-release deprecation compatibility for old cutover config.

**Architecture:** Replace legacy live/shadow selection with direct engine decisions in orchestrator code. Delete or disconnect legacy dispatchers after moving their coverage to engine-native tests. Keep `FinalAcceptanceGate` only as a callable behind `review_engine.rules.final_acceptance`, and keep non-review legacy compatibility audit paths untouched.

**Tech Stack:** Python, pytest, SQLAlchemy-backed ForWin runtime, existing `forwin.review_engine` decision rules, existing `DecisionEvent` audit payloads.

---

## File Structure

- Modify `tests/test_architecture_boundaries.py`: add engine-only safety-net boundary checks.
- Create `tests/review_engine/test_review_outcome_engine_only.py`: move review-outcome behavior coverage from `ReviewOutcomeRouter` to `build_review_outcome_rules()`.
- Modify `tests/review_engine/test_repair_v2.py`: remove the legacy repair fallback expectation and assert `build_scope_driven_repair_rules()` always uses repair v2.
- Modify `tests/review_engine/test_obligation_scope.py`: add the missing direct engine obligation-scope cases before deleting the legacy router test.
- Modify `tests/review_engine/test_audit.py`: update engine-live audit warning wording for post-cutover semantics.
- Create or modify `tests/test_config_deprecations.py`: assert old cutover env fields emit one deprecation warning.
- Modify `forwin/orchestrator_loop_core/common.py`: remove `ReviewOutcomeRouter` import.
- Modify `forwin/orchestrator_loop_core/quality_gates.py`: call review-outcome engine rules directly and record engine-live audit payloads with no legacy shadow.
- Modify `forwin/orchestrator_loop_core/repair_loop.py`: call `decide_repair_v2()` directly, remove `engine_live_enabled()` and `RepairPolicy` fallback, and route final acceptance through the engine rule.
- Modify `forwin/orchestrator_loop_core/service.py`: remove `repair_policy` and `final_acceptance_gate` instance fields.
- Modify `forwin/runtime/services.py`: remove `repair_policy` and `final_acceptance_gate` service fields.
- Modify `forwin/runtime/container.py`: stop constructing `RepairPolicy` and `FinalAcceptanceGate` for runtime injection.
- Modify `forwin/review_engine/rules/repair.py`: remove legacy `RepairPolicy` dependency and keep only a compatibility builder that returns repair-v2 rules.
- Modify `forwin/review_engine/rules/__init__.py`: remove `build_repair_rules` export.
- Modify `forwin/reviser/__init__.py`: remove `RepairPolicy` / `RepairDecision` exports, keep `FinalAcceptanceGate` and `RepairVerifier`.
- Modify `forwin/config.py`: mark old live-cutover config fields as deprecated when explicitly set.
- Modify `scripts/audit_review_engine_cutover.py`: update warning text from allowlist-era language to engine-only language.
- Delete `forwin/reviewer/outcome.py`.
- Delete `forwin/reviser/policy.py`.
- Delete `forwin/planning/obligation_scope_router.py`.
- Delete `forwin/review_engine/cutover.py`.
- Delete `tests/test_review_outcome_router.py`.
- Delete `tests/test_plan_patch_scope_router.py`.
- Delete `tests/review_engine/test_cutover.py`.

---

### Task 1: Add Engine-Only Coverage And Boundary Tests

**Files:**
- Modify: `tests/test_architecture_boundaries.py`
- Create: `tests/review_engine/test_review_outcome_engine_only.py`
- Modify: `tests/review_engine/test_repair_v2.py`
- Modify: `tests/review_engine/test_obligation_scope.py`
- Test: `tests/test_architecture_boundaries.py`
- Test: `tests/review_engine/test_review_outcome_engine_only.py`
- Test: `tests/review_engine/test_repair_v2.py`
- Test: `tests/review_engine/test_obligation_scope.py`

- [ ] **Step 1: Add boundary test for safety-net removal**

Append this test to `tests/test_architecture_boundaries.py`:

```python
def test_review_engine_safety_net_runtime_paths_are_removed() -> None:
    forbidden_runtime_tokens = {
        "ReviewOutcomeRouter": [
            "forwin/orchestrator_loop_core/common.py",
            "forwin/orchestrator_loop_core/quality_gates.py",
        ],
        "RepairPolicy": [
            "forwin/runtime/container.py",
            "forwin/runtime/services.py",
            "forwin/orchestrator_loop_core/service.py",
            "forwin/orchestrator_loop_core/repair_loop.py",
            "forwin/review_engine/rules/repair.py",
        ],
        "ObligationScopeRouter": [
            "forwin/orchestrator_loop_core/quality_gates.py",
            "forwin/review_engine/rules/obligation_scope.py",
        ],
        "select_cutover_pair": [
            "forwin/orchestrator_loop_core/quality_gates.py",
        ],
        "engine_live_enabled": [
            "forwin/orchestrator_loop_core/repair_loop.py",
        ],
    }
    offenders: list[tuple[str, str]] = []
    for token, rel_paths in forbidden_runtime_tokens.items():
        for rel_path in rel_paths:
            if token in _read(rel_path):
                offenders.append((rel_path, token))

    assert offenders == []
    assert "FinalAcceptanceGate" not in _read("forwin/runtime/container.py")
    assert "FinalAcceptanceGate" not in _read("forwin/orchestrator_loop_core/repair_loop.py")
    assert "FinalAcceptanceGate" in _read("forwin/review_engine/rules/final_acceptance.py")
```

- [ ] **Step 2: Add engine-native review-outcome tests**

Create `tests/review_engine/test_review_outcome_engine_only.py`:

```python
from __future__ import annotations

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review_engine.engine import AutoDecisionEngine
from forwin.review_engine.rules.review_outcome import build_review_outcome_rules
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth


def _input(
    *,
    review: ReviewVerdict,
    signals: list[CanonQualitySignal] | None = None,
    current_chapter: int = 10,
    target_total_chapters: int = 20,
) -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=current_chapter,
        review=review,
        signals=list(signals or []),
        open_obligations=[],
        operation_mode="blackbox",
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=target_total_chapters,
        plan_layer_health=PlanLayerHealth(),
    )


def _decision(input_payload: DecisionInput) -> Decision:
    return AutoDecisionEngine(build_review_outcome_rules()).decide(input_payload)


def test_engine_routes_clean_pass_to_auto_approve_commit_clean() -> None:
    decision = _decision(_input(review=ReviewVerdict(verdict="pass")))

    assert decision.outcome == "auto_approve"
    assert decision.sub_action["review_action"] == "commit_clean"
    assert decision.sub_action["minimum_scope"] == "draft"
    assert decision.routed_from == "review_engine"


def test_engine_routes_placeholder_failure_to_local_repair() -> None:
    decision = _decision(
        _input(
            review=ReviewVerdict(
                verdict="fail",
                issues=[
                    ContinuityIssue(
                        rule_name="placeholder_leakage",
                        severity="error",
                        description="正文包含相关人员占位符。",
                        issue_type="placeholder_leakage",
                        target_scope="body",
                    )
                ],
            )
        )
    )

    assert decision.outcome == "local_repair"
    assert decision.sub_action["review_action"] == "local_rewrite"
    assert decision.sub_action["minimum_scope"] == "draft"
    assert decision.sub_action["primary_issue_class"] == "placeholder_leakage"


def test_engine_routes_motivation_gap_to_chapter_patch() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-motive",
        project_id="project-1",
        chapter_number=10,
        signal_type="motivation_gap",
        severity="warning",
        target_scope="character",
        description="动机需要后续解释。",
    )

    decision = _decision(_input(review=ReviewVerdict(verdict="warn"), signals=[signal]))

    assert decision.outcome == "chapter_patch"
    assert decision.sub_action["review_action"] == "defer_with_chapter_plan_patch"
    assert decision.sub_action["minimum_scope"] == "chapter_plan"
    assert decision.sub_action["blocking_signal_ids"] == []


def test_engine_routes_identity_failure_to_arc_patch() -> None:
    decision = _decision(
        _input(
            review=ReviewVerdict(
                verdict="fail",
                issues=[
                    ContinuityIssue(
                        rule_name="identity_conflict",
                        severity="error",
                        description="核心身份跨章冲突。",
                        issue_type="identity_ambiguity",
                        target_scope="arc",
                    )
                ],
            ),
            current_chapter=37,
            target_total_chapters=60,
        )
    )

    assert decision.outcome == "arc_patch"
    assert decision.sub_action["review_action"] == "arc_replan_then_rewrite"
    assert decision.sub_action["minimum_scope"] == "arc"


def test_engine_blocks_final_p1_book_signal() -> None:
    signal = CanonQualitySignal(
        signal_id="sig-final",
        project_id="project-1",
        chapter_number=60,
        signal_type="final_hook_closure",
        severity="warning",
        target_scope="book",
        description="终章仍有 P1 主线义务。",
    )

    decision = _decision(
        _input(
            review=ReviewVerdict(verdict="warn"),
            signals=[signal],
            current_chapter=60,
            target_total_chapters=60,
        )
    )

    assert decision.outcome == "system_block"
    assert decision.sub_action["review_action"] == "block"
    assert decision.sub_action["minimum_scope"] == "book"
    assert decision.sub_action["blocking_signal_ids"] == ["sig-final"]
```

- [ ] **Step 3: Update repair-v2 builder test**

In `tests/review_engine/test_repair_v2.py`, replace `test_engine_keeps_legacy_repair_when_repair_v2_disabled` with:

```python
def test_scope_driven_repair_rules_always_use_repair_v2_after_safety_net_removal() -> None:
    engine = AutoDecisionEngine(
        build_scope_driven_repair_rules(repair_v2_enabled=False)
    )

    decision = engine.decide(_input_with_issue("identity_ambiguity"))

    assert decision.rule_id == "repair_v2_scope_driven"
    assert decision.outcome == "arc_patch"
    assert decision.routed_from == "RepairPolicy.v2"
```

- [ ] **Step 4: Add missing direct obligation-scope tests**

Append these tests to `tests/review_engine/test_obligation_scope.py`:

```python
def test_decide_obligation_scope_uses_next_band_when_current_band_has_no_future_chapters() -> None:
    scope = decide_obligation_scope(
        issue_type="reveal_escalation_needed",
        priority="P1",
        current_chapter=14,
        target_total_chapters=20,
        bands=[
            BandScopeCandidate(
                band_id="arc-1:band:2",
                arc_id="arc-1",
                chapter_start=9,
                chapter_end=14,
                planned_chapters=[],
            ),
            BandScopeCandidate(
                band_id="arc-1:band:3",
                arc_id="arc-1",
                chapter_start=15,
                chapter_end=18,
                planned_chapters=[15, 16, 17, 18],
            ),
        ],
    )

    assert scope.action == "defer_with_band_plan_patch"
    assert scope.target_scope == "band"
    assert scope.target_band_id == "arc-1:band:3"
    assert scope.affected_chapters == [15, 16, 17, 18]


def test_decide_obligation_scope_blocks_band_defer_when_no_future_band_is_available() -> None:
    scope = decide_obligation_scope(
        issue_type="reader_promise_payoff",
        priority="P1",
        current_chapter=20,
        target_total_chapters=20,
        bands=[],
    )

    assert scope.action == "manual_review_required"
    assert scope.target_scope == "band"
    assert scope.reason == "no future band plan available for band-level obligation"
```

- [ ] **Step 5: Run tests and verify RED**

Run:

```bash
python3 -m pytest \
  tests/test_architecture_boundaries.py::test_review_engine_safety_net_runtime_paths_are_removed \
  tests/review_engine/test_review_outcome_engine_only.py \
  tests/review_engine/test_repair_v2.py::test_scope_driven_repair_rules_always_use_repair_v2_after_safety_net_removal \
  tests/review_engine/test_obligation_scope.py \
  -q
```

Expected: FAIL. The boundary test should find `ReviewOutcomeRouter`, `RepairPolicy`, `select_cutover_pair`, and `engine_live_enabled`. The repair-v2 builder test should still return `legacy_repair_policy` while the old fallback exists.

- [ ] **Step 6: Commit tests**

```bash
git add \
  tests/test_architecture_boundaries.py \
  tests/review_engine/test_review_outcome_engine_only.py \
  tests/review_engine/test_repair_v2.py \
  tests/review_engine/test_obligation_scope.py
git commit -m "test: cover review engine safety net removal"
```

---

### Task 2: Remove Review-Outcome Cutover Selection

**Files:**
- Modify: `forwin/orchestrator_loop_core/common.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Delete: `forwin/reviewer/outcome.py`
- Delete: `tests/test_review_outcome_router.py`
- Test: `tests/review_engine/test_review_outcome_engine_only.py`
- Test: `tests/test_architecture_boundaries.py`

- [ ] **Step 1: Remove common import**

Delete this line from `forwin/orchestrator_loop_core/common.py`:

```python
from forwin.reviewer.outcome import ReviewOutcomeRouter
```

- [ ] **Step 2: Replace cutover imports in quality gates**

In `forwin/orchestrator_loop_core/quality_gates.py`, remove these imports:

```python
from forwin.review_engine.cutover import select_cutover_pair
from forwin.review_engine.parity import compare_shadow_decisions, severe_shadow_mismatch
```

Change the review-outcome import block from:

```python
from forwin.review_engine.rules.review_outcome import (
    build_review_outcome_rules,
    decision_from_review_outcome,
    review_action_from_decision,
)
```

to:

```python
from forwin.review_engine.rules.review_outcome import (
    build_review_outcome_rules,
    review_action_from_decision,
)
```

- [ ] **Step 3: Rename the review-action helper**

Replace `_review_action_for_cutover_decision` with:

```python
def _review_action_for_engine_decision(decision: Decision) -> str:
    fallback_action = str(decision.sub_action.get("review_action") or "").strip()
    review_action = review_action_from_decision(decision, fallback_action)
    if review_action:
        return review_action
    return _ENGINE_OUTCOME_TO_LEGACY_REVIEW_ACTION.get(
        str(decision.outcome or "").strip(),
        fallback_action,
    )
```

- [ ] **Step 4: Replace legacy/engine selection in `_prepare_deferred_acceptance_if_needed`**

Inside `forwin/orchestrator_loop_core/quality_gates.py::_prepare_deferred_acceptance_if_needed`, replace the block from the `ReviewOutcomeRouter().route` call through the shadow mismatch warning with this engine-only block:

```python
    decision_input = DecisionInput(
        project_id=project_id,
        chapter_number=chapter_number,
        review=verdict,
        signals=list(signals),
        open_obligations=[],
        operation_mode=str(
            getattr(getattr(self, "config", None), "operation_mode", "blackbox")
            or "blackbox"
        ),
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=target_total_chapters,
        plan_layer_health=PlanLayerHealth(),
    )
    engine_decision = AutoDecisionEngine(build_review_outcome_rules()).decide(decision_input)
    selected_review_action = _review_action_for_engine_decision(engine_decision)
    selected_review_reason = str(engine_decision.reason or "")
    selected_primary_issue_class = str(
        engine_decision.sub_action.get("primary_issue_class") or ""
    ).strip()
    record_engine_decision = getattr(self, "_record_engine_decision_event", None)
    if callable(record_engine_decision):
        record_engine_decision(
            updater=StateUpdater(session),
            decision=engine_decision,
            decision_input=decision_input,
            shadow_mismatch=False,
            live_or_shadow="live",
            legacy_outcome="",
            engine_outcome=engine_decision.outcome,
            live_source="engine",
            shadow_source="",
            engine_live=True,
            legacy_shadow_evaluated=False,
            legacy_safety_net_used=False,
            severe_mismatch=False,
            related_object_type="chapter_review",
            related_object_id=review_id,
        )
```

- [ ] **Step 5: Preserve plan-layer health after action selection**

Immediately after the engine decision block, add:

```python
    decision_input = DecisionInput(
        project_id=decision_input.project_id,
        chapter_number=decision_input.chapter_number,
        review=decision_input.review,
        signals=decision_input.signals,
        open_obligations=decision_input.open_obligations,
        operation_mode=decision_input.operation_mode,
        attempts_completed=decision_input.attempts_completed,
        prior_scope_history=decision_input.prior_scope_history,
        budget=decision_input.budget,
        target_total_chapters=decision_input.target_total_chapters,
        plan_layer_health=PlanLayerHealth(
            active_chapter_patch_count=(
                1 if selected_review_action == "defer_with_chapter_plan_patch" else 0
            ),
            active_band_patch_count=(
                1 if selected_review_action == "defer_with_band_plan_patch" else 0
            ),
        ),
    )
```

- [ ] **Step 6: Remove remaining legacy outcome payload references**

In the `commit_decision` event payload inside `_prepare_deferred_acceptance_if_needed`, replace:

```python
                legacy_outcome=outcome.action,
```

with:

```python
                legacy_outcome="",
```

- [ ] **Step 7: Delete legacy review outcome files**

```bash
git rm forwin/reviewer/outcome.py tests/test_review_outcome_router.py
```

- [ ] **Step 8: Run focused tests**

Run:

```bash
python3 -m pytest \
  tests/review_engine/test_review_outcome_engine_only.py \
  tests/review_engine/test_rule_parity.py \
  tests/test_architecture_boundaries.py::test_review_engine_safety_net_runtime_paths_are_removed \
  -q
```

Expected: PASS for review outcome tests. The boundary test may still FAIL on repair and obligation safety-net tokens until later tasks.

- [ ] **Step 9: Commit review-outcome removal**

```bash
git add \
  forwin/orchestrator_loop_core/common.py \
  forwin/orchestrator_loop_core/quality_gates.py \
  forwin/reviewer/outcome.py \
  tests/test_review_outcome_router.py
git commit -m "refactor: remove review outcome safety net"
```

---

### Task 3: Remove RepairPolicy Runtime Fallback

**Files:**
- Modify: `forwin/review_engine/rules/repair.py`
- Modify: `forwin/review_engine/rules/__init__.py`
- Modify: `forwin/orchestrator_loop_core/repair_loop.py`
- Modify: `forwin/orchestrator_loop_core/service.py`
- Modify: `forwin/runtime/services.py`
- Modify: `forwin/runtime/container.py`
- Modify: `forwin/reviser/__init__.py`
- Delete: `forwin/reviser/policy.py`
- Test: `tests/review_engine/test_repair_v2.py`
- Test: `tests/test_repair_progress.py`
- Test: `tests/test_runtime_container.py`

- [ ] **Step 1: Make repair rule builder engine-only**

Replace `forwin/review_engine/rules/repair.py` with:

```python
from __future__ import annotations

from ..types import DecisionRule
from .repair_v2 import build_repair_v2_rules


def build_scope_driven_repair_rules(
    *,
    repair_v2_enabled: bool,
    policy: object | None = None,
) -> list[DecisionRule]:
    del repair_v2_enabled, policy
    return build_repair_v2_rules(enabled=True)
```

- [ ] **Step 2: Remove legacy repair export**

In `forwin/review_engine/rules/__init__.py`, change:

```python
from .repair import build_repair_rules, build_scope_driven_repair_rules
```

to:

```python
from .repair import build_scope_driven_repair_rules
```

Remove `"build_repair_rules"` from `__all__`.

- [ ] **Step 3: Update repair loop imports**

In `forwin/orchestrator_loop_core/repair_loop.py`, replace:

```python
from forwin.review_engine.cutover import engine_live_enabled
from forwin.review_engine.rules.repair_v2 import compare_repair_v2_shadow, decide_repair_v2
```

with:

```python
from forwin.protocol.review import FinalGateDecision
from forwin.review_engine.engine import AutoDecisionEngine
from forwin.review_engine.rules.final_acceptance import build_final_acceptance_rules
from forwin.review_engine.rules.repair_v2 import decide_repair_v2
```

- [ ] **Step 4: Add final-gate conversion helper**

Add this helper near the top of `forwin/orchestrator_loop_core/repair_loop.py`, below imports:

```python
def _final_gate_from_engine_decision(decision: Decision) -> FinalGateDecision:
    return FinalGateDecision(
        decision=str(decision.sub_action.get("legacy_decision") or "manual_review_required"),
        forceable=bool(decision.sub_action.get("forceable")),
        reason=str(decision.reason or ""),
        canon_risk=str(decision.sub_action.get("canon_risk") or "high"),
        residual_issues=list(decision.sub_action.get("residual_issues") or []),
        requires_human=bool(decision.sub_action.get("requires_human", True)),
    )
```

Extend the existing `forwin.review_engine.types` import to include `Decision`:

```python
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth
```

- [ ] **Step 5: Replace the repair-policy decision block**

In the repair loop, replace the block from the `self.repair_policy.decide` call through the nonlocal `repair_v2_live` branch with:

```python
        repair_v2_input = DecisionInput(
            project_id=project_id,
            chapter_number=chapter_plan.chapter_number,
            review=current_review,
            signals=[],
            open_obligations=[],
            operation_mode=self.config.operation_mode,
            attempts_completed=len(existing_attempts),
            prior_scope_history=[
                str(getattr(attempt, "repair_scope", "") or "")
                for attempt in existing_attempts
            ],
            budget=None,
            target_total_chapters=0,
            plan_layer_health=PlanLayerHealth(),
        )
        repair_v2_decision = decide_repair_v2(repair_v2_input)
        repair_scope = str(repair_v2_decision.sub_action.get("scope") or "")
        self._record_engine_decision_event(
            updater=updater,
            decision=repair_v2_decision,
            decision_input=repair_v2_input,
            shadow_mismatch=False,
            live_or_shadow="live",
            legacy_outcome="",
            engine_outcome=repair_scope or str(repair_v2_decision.outcome or ""),
            live_source="engine",
            shadow_source="",
            engine_live=True,
            legacy_shadow_evaluated=False,
            legacy_safety_net_used=False,
            severe_mismatch=False,
            related_object_type="chapter_review",
            related_object_id=current_review_row.id,
            parent_event_id=str(current_review_event.id or ""),
        )
        repair_can_run_locally = repair_v2_decision.outcome in {
            "local_repair",
            "chapter_patch",
            "band_patch",
        }
```

- [ ] **Step 6: Replace non-repair final acceptance branch**

Replace:

```python
        if repair_decision.kind != "repair":
            final_gate = self.final_acceptance_gate.evaluate(
                operation_mode=self.config.operation_mode,
                review=current_review,
                verification=current_review.repair_verification,
            )
```

with:

```python
        if not repair_can_run_locally:
            final_decision = AutoDecisionEngine(build_final_acceptance_rules()).decide(repair_v2_input)
            final_gate = _final_gate_from_engine_decision(final_decision)
```

- [ ] **Step 7: Replace repair attempt variable setup**

Replace:

```python
        attempt_no = repair_decision.attempt_no
        repair_scope = repair_decision.scope
        repair_model_preference = {
            "preferred_provider_kind": repair_decision.preferred_provider_kind,
            "preferred_model": repair_decision.preferred_model,
        }
```

with:

```python
        attempt_no = len(existing_attempts) + 1
        repair_model_preference = {
            "preferred_provider_kind": "",
            "preferred_model": "",
        }
```

- [ ] **Step 8: Remove runtime service injections**

In `forwin/runtime/services.py`, delete:

```python
    repair_policy: Any
    final_acceptance_gate: Any
```

In `forwin/runtime/container.py`, change:

```python
from forwin.reviser import FinalAcceptanceGate, RepairPolicy, RepairVerifier
```

to:

```python
from forwin.reviser import RepairVerifier
```

Remove these `RuntimeServices` constructor arguments:

```python
            repair_policy=RepairPolicy(
                max_attempts=max(1, min(3, int(config.review_fail_max_rewrites or 3))),
                model_sequence=list(getattr(config, "repair_model_sequence", []) or []),
            ),
            final_acceptance_gate=FinalAcceptanceGate(),
```

In `forwin/orchestrator_loop_core/service.py`, remove:

```python
        self.repair_policy = services.repair_policy
        self.final_acceptance_gate = services.final_acceptance_gate
```

- [ ] **Step 9: Remove legacy repair exports and file**

In `forwin/reviser/__init__.py`, replace the file with:

```python
from .final_acceptance import FinalAcceptanceGate
from .verification import RepairVerifier

__all__ = [
    "FinalAcceptanceGate",
    "RepairVerifier",
]
```

Delete the policy file:

```bash
git rm forwin/reviser/policy.py
```

- [ ] **Step 10: Delete legacy repair tests**

In `tests/test_repair_progress.py`, delete the complete definitions named:

- `test_repair_policy_keeps_requested_local_scope_for_local_hard_error`
- `test_repair_policy_selects_configured_model_sequence`

Also remove this import if it becomes unused:

```python
from forwin.reviser.policy import RepairPolicy
```

- [ ] **Step 11: Run focused repair tests**

Run:

```bash
python3 -m pytest \
  tests/review_engine/test_repair_v2.py \
  tests/test_repair_progress.py \
  tests/test_runtime_container.py \
  tests/test_architecture_boundaries.py::test_review_engine_safety_net_runtime_paths_are_removed \
  -q
```

Expected: PASS for repair-v2 and runtime-container tests. The boundary test may still FAIL on `ObligationScopeRouter` until Task 4.

- [ ] **Step 12: Commit repair removal**

```bash
git add \
  forwin/review_engine/rules/repair.py \
  forwin/review_engine/rules/__init__.py \
  forwin/orchestrator_loop_core/repair_loop.py \
  forwin/orchestrator_loop_core/service.py \
  forwin/runtime/services.py \
  forwin/runtime/container.py \
  forwin/reviser/__init__.py \
  forwin/reviser/policy.py \
  tests/review_engine/test_repair_v2.py \
  tests/test_repair_progress.py \
  tests/test_runtime_container.py
git commit -m "refactor: remove repair policy safety net"
```

---

### Task 4: Remove ObligationScopeRouter And Cutover Helper

**Files:**
- Delete: `forwin/planning/obligation_scope_router.py`
- Delete: `tests/test_plan_patch_scope_router.py`
- Delete: `forwin/review_engine/cutover.py`
- Delete: `tests/review_engine/test_cutover.py`
- Test: `tests/review_engine/test_obligation_scope.py`
- Test: `tests/test_architecture_boundaries.py`

- [ ] **Step 1: Delete obligation router and legacy tests**

```bash
git rm forwin/planning/obligation_scope_router.py tests/test_plan_patch_scope_router.py
```

- [ ] **Step 2: Delete cutover helper and tests**

```bash
git rm forwin/review_engine/cutover.py tests/review_engine/test_cutover.py
```

- [ ] **Step 3: Run reference scan**

Run:

```bash
grep -RIn "ObligationScopeRouter\\|select_cutover_pair\\|engine_live_enabled" \
  forwin tests \
  --exclude-dir=.pytest_cache \
  --exclude-dir=__pycache__ \
  --exclude='*.pyc'
```

Expected: no output.

- [ ] **Step 4: Run focused tests**

Run:

```bash
python3 -m pytest \
  tests/review_engine/test_obligation_scope.py \
  tests/test_architecture_boundaries.py::test_review_engine_safety_net_runtime_paths_are_removed \
  -q
```

Expected: PASS.

- [ ] **Step 5: Commit obligation and cutover helper removal**

```bash
git add \
  forwin/planning/obligation_scope_router.py \
  tests/test_plan_patch_scope_router.py \
  forwin/review_engine/cutover.py \
  tests/review_engine/test_cutover.py \
  tests/review_engine/test_obligation_scope.py
git commit -m "refactor: remove obligation and cutover safety net helpers"
```

---

### Task 5: Deprecate Cutover Config And Update Audit Language

**Files:**
- Modify: `forwin/config.py`
- Create: `tests/test_config_deprecations.py`
- Modify: `scripts/audit_review_engine_cutover.py`
- Modify: `tests/review_engine/test_audit.py`
- Modify: `docs/designs/review-engine-cutover-spec.md`
- Test: `tests/test_config_deprecations.py`
- Test: `tests/review_engine/test_audit.py`

- [ ] **Step 1: Add config deprecation test**

Create `tests/test_config_deprecations.py`:

```python
from __future__ import annotations

import pytest

from forwin.config import Config


def test_review_engine_live_cutover_env_is_deprecated(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_ENABLED", "true")
    monkeypatch.setenv("FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_PROJECT_ALLOWLIST", "project-1")

    with pytest.warns(
        DeprecationWarning,
        match="review engine is globally live",
    ):
        config = Config.from_env()

    assert config.review_engine_live_cutover_enabled is True
    assert config.review_engine_live_cutover_project_allowlist == ["project-1"]
```

- [ ] **Step 2: Run config deprecation test and verify RED**

Run:

```bash
python3 -m pytest tests/test_config_deprecations.py -q
```

Expected: FAIL because `Config.from_env()` does not emit the deprecation warning yet.

- [ ] **Step 3: Add deprecation warning helper**

In `forwin/config.py`, add this import near the existing imports:

```python
import warnings
```

Add this constant near the other module constants:

```python
_DEPRECATED_REVIEW_CUTOVER_FIELDS = {
    "review_engine_live_cutover_enabled",
    "review_engine_live_cutover_project_allowlist",
}
```

Add this helper above `_env_values()`:

```python
def _warn_deprecated_review_cutover_config(explicit_keys: set[str]) -> None:
    used = sorted(_DEPRECATED_REVIEW_CUTOVER_FIELDS & set(explicit_keys))
    if not used:
        return
    warnings.warn(
        "review engine is globally live; "
        "review_engine_live_cutover_enabled and "
        "review_engine_live_cutover_project_allowlist are deprecated and ignored by runtime routing",
        DeprecationWarning,
        stacklevel=3,
    )
```

- [ ] **Step 4: Track allowlist as explicit env**

In `_env_values()`, replace:

```python
        "review_engine_live_cutover_project_allowlist": _env_csv(
            env,
            "FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_PROJECT_ALLOWLIST",
        ),
```

with:

```python
        "review_engine_live_cutover_project_allowlist": tracked_csv(
            "review_engine_live_cutover_project_allowlist",
            "FORWIN_REVIEW_ENGINE_LIVE_CUTOVER_PROJECT_ALLOWLIST",
        ),
```

- [ ] **Step 5: Emit warning from `Config.from_env()`**

Replace:

```python
        config = cls(**values)
        return apply_quality_profile(config, explicit_keys=explicit_keys)
```

with:

```python
        config = cls(**values)
        config = apply_quality_profile(config, explicit_keys=explicit_keys)
        _warn_deprecated_review_cutover_config(explicit_keys)
        return config
```

- [ ] **Step 6: Update audit warning wording**

In `scripts/audit_review_engine_cutover.py`, replace the warning string in `cutover_audit_warnings()` with:

```python
"WARNING: ENGINE NEVER DROVE LIVE - review engine safety-net removal requires engine-live decision events for every generated chapter"
```

In `tests/review_engine/test_audit.py`, update the expected warning list to the same string.

- [ ] **Step 7: Update cutover spec status wording**

In `docs/designs/review-engine-cutover-spec.md`, update the implementation status lines that still say legacy removal has not started. Use this wording:

```markdown
- Legacy removal: review safety-net removal is now in the implementation phase after a 60-chapter live pilot; non-review legacy compatibility remains separate and blocked by runtime usage.
```

In the "Trigger conditions" section, add:

```markdown
Current removal wave is limited to review safety-net dispatchers. The older global 30-day condition still applies to deleting deployment config fields and non-review compatibility paths, not to this engine-only runtime cleanup.
```

- [ ] **Step 8: Run config and audit tests**

Run:

```bash
python3 -m pytest \
  tests/test_config_deprecations.py \
  tests/review_engine/test_audit.py \
  -q
```

Expected: PASS.

- [ ] **Step 9: Commit config and audit updates**

```bash
git add \
  forwin/config.py \
  tests/test_config_deprecations.py \
  scripts/audit_review_engine_cutover.py \
  tests/review_engine/test_audit.py \
  docs/designs/review-engine-cutover-spec.md
git commit -m "chore: deprecate review cutover config"
```

---

### Task 6: Final Verification And Container Audit

**Files:**
- Verify: all changed files
- Test: focused unit suite
- Test: 60-chapter container audit

- [ ] **Step 1: Run final static scan**

Run:

```bash
grep -RIn "ReviewOutcomeRouter\\|ObligationScopeRouter\\|select_cutover_pair\\|engine_live_enabled" \
  forwin/orchestrator_loop_core forwin/runtime forwin/reviewer forwin/planning forwin/review_engine/rules \
  --exclude-dir=.pytest_cache \
  --exclude-dir=__pycache__ \
  --exclude='*.pyc' || true
grep -RIn "RepairPolicy" \
  forwin/orchestrator_loop_core forwin/runtime forwin/reviser forwin/review_engine/rules \
  --exclude-dir=.pytest_cache \
  --exclude-dir=__pycache__ \
  --exclude='*.pyc' | grep -v "RepairPolicy.v2" || true
```

Expected output: no lines.

- [ ] **Step 2: Run focused unit suite**

Run:

```bash
python3 -m pytest \
  tests/test_architecture_boundaries.py \
  tests/review_engine/test_review_outcome_engine_only.py \
  tests/review_engine/test_rule_parity.py \
  tests/review_engine/test_repair_v2.py \
  tests/review_engine/test_obligation_scope.py \
  tests/review_engine/test_audit.py \
  tests/review_engine/test_legacy_compatibility_audit.py \
  tests/test_config_deprecations.py \
  tests/test_runtime_container.py \
  tests/test_repair_progress.py \
  -q
```

Expected: PASS.

- [ ] **Step 3: Run import smoke test**

Run:

```bash
python3 - <<'PY'
from forwin.runtime.container import RuntimeContainer
from forwin.review_engine.rules.review_outcome import build_review_outcome_rules
from forwin.review_engine.rules.repair_v2 import decide_repair_v2
from forwin.review_engine.rules.obligation_scope import decide_obligation_scope
print(RuntimeContainer.__name__)
print(bool(build_review_outcome_rules()))
print(callable(decide_repair_v2))
print(callable(decide_obligation_scope))
PY
```

Expected output:

```text
RuntimeContainer
True
True
True
```

- [ ] **Step 4: Build and deploy container**

Run:

```bash
docker compose up -d --build forwin forwin-mcp
docker compose ps forwin forwin-mcp postgres qdrant
```

Expected: `forwin`, `forwin-mcp`, `postgres`, and `qdrant` are running. Health-check-enabled services show `healthy` or are still moving from `starting` to `healthy`; the command does not remove the existing Postgres volume.

- [ ] **Step 5: Run a 60-chapter engine-only audit**

After a clean 60-chapter project completes or the existing clean 60-chapter pilot has fresh engine-only events, run:

```bash
python3 scripts/audit_review_engine_cutover.py \
  --project-id 09e38c798dc44286869705478c1c735e \
  --expected-chapters 60 \
  --include-legacy-compat
```

Expected review cutover fields:

```text
"passed": true
"engine_live_chapters": 60
"legacy_safety_net_chapters": []
"severe_mismatch_chapters": []
"non_live_chapters": []
```

The `legacy_compat` section may still show blockers for non-review compatibility. Do not use those blockers to fail this review safety-net removal unless the `review` cutover fields above fail.

- [ ] **Step 6: Commit final verification note**

If `docs/designs/review-engine-cutover-spec.md` was updated with verification evidence in Task 6, commit that file:

```bash
git add docs/designs/review-engine-cutover-spec.md
git commit -m "docs: record review engine safety net removal verification"
```

If `docs/designs/review-engine-cutover-spec.md` has no diff, run this command and do not create a commit:

```bash
git diff --quiet -- docs/designs/review-engine-cutover-spec.md
```

---

## Self-Review Checklist

- Spec coverage: runtime boundary, audit semantics, deprecated config, deletion targets, exclusions, testing, and rollback are each mapped to tasks.
- Scope check: non-review legacy compatibility paths are excluded and remain under `legacy_compatibility_used` audit.
- Type consistency: plan uses existing `DecisionInput`, `Decision`, `PlanLayerHealth`, `FinalGateDecision`, `BandScopeCandidate`, and current engine rule functions.
- Test handoff: every deletion has engine-native coverage before the delete step.
