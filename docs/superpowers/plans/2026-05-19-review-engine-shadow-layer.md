# Review Engine Shadow Layer Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add `AutoDecisionEngine` as a typed, auditable shadow decision layer without changing live review behavior.

**Architecture:** Build a new `forwin/review_engine` package that accepts explicit facts and returns normalized decisions. In shadow mode, the old dispatcher chain remains live while engine output is compared and audited.

**Tech Stack:** Python 3, dataclasses/Pydantic, pytest, SQLAlchemy event repositories, existing ForWin reviewer/reviser/planning dispatchers.

---

### Task 1: Create Review Engine Types

**Files:**
- Create: `forwin/review_engine/__init__.py`
- Create: `forwin/review_engine/types.py`
- Test: `tests/review_engine/test_types.py`

- [ ] **Step 1: Write type construction tests**

Create `tests/review_engine/test_types.py`:

```python
from forwin.protocol.review import ReviewVerdict
from forwin.review_engine.types import Decision, DecisionInput, PlanLayerHealth


def test_decision_input_and_decision_are_serializable() -> None:
    input_payload = DecisionInput(
        project_id="project-1",
        chapter_number=3,
        review=ReviewVerdict(verdict="pass"),
        signals=[],
        open_obligations=[],
        operation_mode="copilot",
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=30,
        plan_layer_health=PlanLayerHealth(),
    )
    decision = Decision(
        outcome="manual_review",
        reason="shadow fixture",
        rule_id="fixture_manual_review",
        missing_evidence=[],
        routed_from="fixture",
        sub_action={},
    )

    assert input_payload.project_id == "project-1"
    assert decision.rule_id == "fixture_manual_review"
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_types.py -q
```

Expected: FAIL because `forwin.review_engine` does not exist.

- [ ] **Step 3: Implement type module**

Create `forwin/review_engine/types.py`:

```python
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal

from forwin.canon_quality.signals import CanonQualitySignal
from forwin.narrative_obligations.budget import ObligationBudgetResult
from forwin.narrative_obligations.types import NarrativeObligation
from forwin.protocol.review import ReviewVerdict


OperationMode = Literal["blackbox", "copilot", "checkpoint"]
DecisionOutcome = Literal[
    "auto_approve",
    "local_repair",
    "chapter_patch",
    "band_patch",
    "arc_patch",
    "book_patch",
    "commit_with_obligation",
    "manual_review",
    "system_block",
]


@dataclass(frozen=True)
class PlanLayerHealth:
    active_chapter_patch_count: int = 0
    active_band_patch_count: int = 0
    active_arc_patch_count: int = 0
    active_book_patch_count: int = 0
    overdue_obligation_count: int = 0
    missing_layers: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DecisionInput:
    project_id: str
    chapter_number: int
    review: ReviewVerdict
    signals: list[CanonQualitySignal]
    open_obligations: list[NarrativeObligation]
    operation_mode: OperationMode
    attempts_completed: int
    prior_scope_history: list[str]
    budget: ObligationBudgetResult | None
    target_total_chapters: int
    plan_layer_health: PlanLayerHealth


@dataclass(frozen=True)
class Decision:
    outcome: DecisionOutcome
    reason: str
    rule_id: str
    missing_evidence: list[str]
    routed_from: str
    sub_action: dict[str, Any]
```

Create `forwin/review_engine/__init__.py` exporting these names.

- [ ] **Step 4: Run type tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_types.py -q
```

Expected: pass.

### Task 2: Add Rule Table and Engine Scanner

**Files:**
- Modify: `forwin/review_engine/types.py`
- Create: `forwin/review_engine/engine.py`
- Test: `tests/review_engine/test_engine.py`

- [ ] **Step 1: Write engine first-match test**

Create `tests/review_engine/test_engine.py`:

```python
from forwin.protocol.review import ReviewVerdict
from forwin.review_engine.engine import AutoDecisionEngine
from forwin.review_engine.types import Decision, DecisionInput, DecisionRule, PlanLayerHealth


def _input() -> DecisionInput:
    return DecisionInput(
        project_id="project-1",
        chapter_number=1,
        review=ReviewVerdict(verdict="warn"),
        signals=[],
        open_obligations=[],
        operation_mode="copilot",
        attempts_completed=0,
        prior_scope_history=[],
        budget=None,
        target_total_chapters=10,
        plan_layer_health=PlanLayerHealth(),
    )


def test_engine_returns_first_matching_rule() -> None:
    rules = [
        DecisionRule(
            rule_id="first",
            source_dispatcher="fixture",
            priority=10,
            matches=lambda _: True,
            decide=lambda _: Decision("manual_review", "first", "first", [], "fixture", {}),
        ),
        DecisionRule(
            rule_id="second",
            source_dispatcher="fixture",
            priority=20,
            matches=lambda _: True,
            decide=lambda _: Decision("system_block", "second", "second", [], "fixture", {}),
        ),
    ]

    decision = AutoDecisionEngine(rules).decide(_input())

    assert decision.rule_id == "first"
```

- [ ] **Step 2: Run engine test and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_engine.py -q
```

Expected: FAIL because `DecisionRule` and engine do not exist.

- [ ] **Step 3: Implement `DecisionRule` and engine**

Add to `types.py`:

```python
from collections.abc import Callable


@dataclass(frozen=True)
class DecisionRule:
    rule_id: str
    source_dispatcher: str
    priority: int
    matches: Callable[[DecisionInput], bool]
    decide: Callable[[DecisionInput], Decision]
```

Create `forwin/review_engine/engine.py`:

```python
from __future__ import annotations

from .types import Decision, DecisionInput, DecisionRule


class AutoDecisionEngine:
    def __init__(self, rules: list[DecisionRule]) -> None:
        self.rules = sorted(rules, key=lambda item: item.priority)

    def decide(self, input: DecisionInput) -> Decision:
        for rule in self.rules:
            if rule.matches(input):
                decision = rule.decide(input)
                if decision.rule_id != rule.rule_id:
                    return Decision(
                        outcome=decision.outcome,
                        reason=decision.reason,
                        rule_id=rule.rule_id,
                        missing_evidence=list(decision.missing_evidence),
                        routed_from=decision.routed_from or rule.source_dispatcher,
                        sub_action=dict(decision.sub_action),
                    )
                return decision
        return Decision(
            outcome="manual_review",
            reason="no review-engine rule matched",
            rule_id="no_rule_matched",
            missing_evidence=["matching_rule"],
            routed_from="review_engine",
            sub_action={},
        )
```

- [ ] **Step 4: Run engine tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_engine.py tests/review_engine/test_types.py -q
```

Expected: pass.

### Task 3: Wrap Existing Dispatchers as Parity Rules

**Files:**
- Create: `forwin/review_engine/rules/__init__.py`
- Create: `forwin/review_engine/rules/review_outcome.py`
- Create: `forwin/review_engine/rules/repair.py`
- Create: `forwin/review_engine/rules/obligation_scope.py`
- Create: `forwin/review_engine/rules/final_acceptance.py`
- Test: `tests/review_engine/test_rule_parity.py`

- [ ] **Step 1: Write parity fixture tests**

Create fixtures that normalize current dispatcher outputs:

```python
def test_pass_review_routes_like_existing_review_outcome_router() -> None:
    old = route_existing_dispatcher_fixture(verdict="pass", mode="blackbox")
    new = route_engine_fixture(verdict="pass", mode="blackbox")
    assert new.outcome == old.outcome
    assert new.routed_from
```

Include fixtures for pass, warn, fail, obligation defer, obligation block, repair exhausted, and final force accept.

- [ ] **Step 2: Run parity tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_rule_parity.py -q
```

Expected: FAIL because rule modules do not exist.

- [ ] **Step 3: Implement wrapper rules**

Each rule module should expose a `build_*_rules()` function. Example in `review_outcome.py`:

```python
def build_review_outcome_rules(router: ReviewOutcomeRouter | None = None) -> list[DecisionRule]:
    resolved_router = router or ReviewOutcomeRouter()
    return [
        DecisionRule(
            rule_id="legacy_review_outcome_router",
            source_dispatcher="ReviewOutcomeRouter",
            priority=100,
            matches=lambda input: True,
            decide=lambda input: _decision_from_review_outcome(resolved_router, input),
        )
    ]
```

`_decision_from_review_outcome()` maps the legacy outcome into P1 `Decision` outcomes without policy changes.

- [ ] **Step 4: Run parity tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_rule_parity.py tests/test_review_outcome_router.py -q
```

Expected: pass.

### Task 4: Persist Decision Audit Events

**Files:**
- Create: `forwin/review_engine/audit.py`
- Test: `tests/review_engine/test_audit.py`

- [ ] **Step 1: Write audit event payload test**

Create `tests/review_engine/test_audit.py`:

```python
from forwin.review_engine.audit import build_decision_event_payload
from forwin.review_engine.types import Decision


def test_decision_event_payload_contains_rule_and_digest() -> None:
    payload = build_decision_event_payload(
        decision=Decision("manual_review", "needs human", "rule-1", ["deadline"], "router", {}),
        input_digest="abc123",
        shadow_mismatch=False,
    )

    assert payload["rule_id"] == "rule-1"
    assert payload["input_digest"] == "abc123"
    assert payload["missing_evidence"] == ["deadline"]
```

- [ ] **Step 2: Run audit test and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_audit.py -q
```

Expected: FAIL because audit module does not exist.

- [ ] **Step 3: Implement audit helpers**

Create:

```python
def build_decision_event_payload(*, decision: Decision, input_digest: str, shadow_mismatch: bool) -> dict[str, object]:
    return {
        "rule_id": decision.rule_id,
        "outcome": decision.outcome,
        "reason": decision.reason,
        "missing_evidence": list(decision.missing_evidence),
        "routed_from": decision.routed_from,
        "sub_action": dict(decision.sub_action),
        "input_digest": input_digest,
        "shadow_mismatch": bool(shadow_mismatch),
    }
```

Add `digest_decision_input(input: DecisionInput) -> str` using stable JSON serialization of primitive fields.

- [ ] **Step 4: Run audit tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_audit.py -q
```

Expected: pass.

### Task 5: Add Shadow Comparison Boundary

**Files:**
- Create: `forwin/review_engine/parity.py`
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Test: `tests/review_engine/test_shadow_mode.py`

- [ ] **Step 1: Write shadow mismatch test**

Test a helper that returns old decision, engine decision, and mismatch flag:

```python
def test_shadow_comparison_marks_mismatch() -> None:
    result = compare_shadow_decisions(
        live=Decision("manual_review", "old", "old_rule", [], "old", {}),
        shadow=Decision("system_block", "new", "new_rule", [], "engine", {}),
    )

    assert result.shadow_mismatch is True
```

- [ ] **Step 2: Run shadow test and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_shadow_mode.py -q
```

Expected: FAIL because shadow comparison does not exist.

- [ ] **Step 3: Implement comparison helper**

Create:

```python
@dataclass(frozen=True)
class ShadowDecisionComparison:
    live: Decision
    shadow: Decision
    shadow_mismatch: bool


def compare_shadow_decisions(*, live: Decision, shadow: Decision) -> ShadowDecisionComparison:
    return ShadowDecisionComparison(
        live=live,
        shadow=shadow,
        shadow_mismatch=(live.outcome, live.sub_action) != (shadow.outcome, shadow.sub_action),
    )
```

In `quality_gates.py`, add the call at the narrowest point where the current four-dispatcher result is known. In P1, do not change live behavior when shadow mode is on.

- [ ] **Step 4: Run review-engine tests**

Run:

```bash
python3 -m pytest tests/review_engine -q
python3 -m pytest tests/test_review_outcome_router.py tests/test_final_gate_obligation_clearance.py -q
```

Expected: all pass.

### Task 6: Final Shadow Layer Verification

**Files:**
- Verify all review-engine P1 files.

- [ ] **Step 1: Run focused suite**

Run:

```bash
python3 -m pytest tests/review_engine -q
python3 -m pytest tests/test_review_outcome_router.py tests/test_repair_progress.py tests/test_final_gate_obligation_clearance.py -q
```

Expected: all pass.

- [ ] **Step 2: Run syntax and diff hygiene**

Run:

```bash
python3 -m compileall -q forwin
git diff --check
```

Expected: both pass with no output.

- [ ] **Step 3: Commit shadow layer**

Run:

```bash
git add forwin/review_engine tests/review_engine forwin/orchestrator_loop_core/quality_gates.py
git commit -m "feat: add review engine shadow layer"
```
