# Review Engine Scope-Driven Repair Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Route repair by issue kind and source layer instead of using attempt count as the primary scope selector.

**Architecture:** Add an issue taxonomy and classifier under `forwin/review_engine`, then add repair-v2 rules behind `review_engine.repair_v2_enabled`. Old repair policy stays available and remains the default until shadow distribution is reviewed.

**Tech Stack:** Python 3, dataclasses, pytest, existing review engine types, reviewer issue models, canon-quality signals, and repair policy modules.

---

### Task 1: Add Issue Taxonomy

**Files:**
- Create: `forwin/review_engine/issue_taxonomy.py`
- Test: `tests/review_engine/test_issue_taxonomy.py`

- [ ] **Step 1: Write taxonomy mapping tests**

Create `tests/review_engine/test_issue_taxonomy.py`:

```python
from forwin.review_engine.issue_taxonomy import scope_for_issue_kind


def test_structural_identity_issue_routes_to_arc_plan() -> None:
    assert scope_for_issue_kind("identity_ambiguity") == "arc_plan"


def test_infrastructure_schema_issue_routes_to_operator() -> None:
    assert scope_for_issue_kind("form_schema_invalid") == "operator"


def test_unknown_issue_defaults_to_chapter_plan() -> None:
    assert scope_for_issue_kind("unknown_new_issue") == "chapter_plan"
```

- [ ] **Step 2: Run taxonomy tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_issue_taxonomy.py -q
```

Expected: FAIL because taxonomy module does not exist.

- [ ] **Step 3: Implement taxonomy**

Create:

```python
from __future__ import annotations

from typing import Literal

IssueScope = Literal["draft", "chapter_plan", "band_plan", "arc_plan", "book_plan", "subworld", "active_rules", "operator"]

ISSUE_TO_SCOPE: dict[str, IssueScope] = {
    "placeholder_leakage": "draft",
    "body_truncated": "draft",
    "single_chapter_pacing": "chapter_plan",
    "single_chapter_callback": "chapter_plan",
    "identity_within_band": "band_plan",
    "foreshadow_band": "band_plan",
    "identity_ambiguity": "arc_plan",
    "countdown_explanation": "arc_plan",
    "artifact_count_explanation": "arc_plan",
    "book_structure_violation": "book_plan",
    "subworld_admission_missing_canon_entity": "subworld",
    "countdown_state_drift": "active_rules",
    "form_schema_invalid": "operator",
}


def scope_for_issue_kind(issue_kind: str) -> IssueScope:
    return ISSUE_TO_SCOPE.get(str(issue_kind or "").strip(), "chapter_plan")
```

- [ ] **Step 4: Run taxonomy tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_issue_taxonomy.py -q
```

Expected: pass.

### Task 2: Classify Primary Issue

**Files:**
- Modify: `forwin/review_engine/issue_taxonomy.py`
- Test: `tests/review_engine/test_issue_taxonomy.py`

- [ ] **Step 1: Add classifier tests**

Add:

```python
from forwin.protocol.review import ContinuityIssue, ReviewVerdict
from forwin.review_engine.issue_taxonomy import classify_primary_issue


def test_classifier_prefers_larger_scope_when_severity_is_comparable() -> None:
    review = ReviewVerdict(
        verdict="fail",
        issues=[
            ContinuityIssue(rule_name="placeholder_leakage", severity="error", description="draft"),
            ContinuityIssue(rule_name="identity_ambiguity", severity="error", description="arc"),
        ],
    )

    primary = classify_primary_issue(review=review, signals=[])

    assert primary.kind == "identity_ambiguity"
    assert primary.scope == "arc_plan"
```

- [ ] **Step 2: Run classifier test and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_issue_taxonomy.py::test_classifier_prefers_larger_scope_when_severity_is_comparable -q
```

Expected: FAIL because classifier does not exist.

- [ ] **Step 3: Implement classifier**

Add:

```python
@dataclass(frozen=True)
class ClassifiedIssue:
    kind: str
    scope: IssueScope
    severity: str
    source_layer: str = ""
    evidence_refs: tuple[str, ...] = ()


_SCOPE_RANK = {
    "draft": 1,
    "chapter_plan": 2,
    "band_plan": 3,
    "arc_plan": 4,
    "book_plan": 5,
    "subworld": 4,
    "active_rules": 4,
    "operator": 6,
}
_SEVERITY_RANK = {"info": 1, "warning": 2, "error": 3, "critical": 4, "blocker": 4}


def classify_primary_issue(*, review: ReviewVerdict, signals: list[object]) -> ClassifiedIssue:
    candidates: list[ClassifiedIssue] = []
    for issue in review.issues:
        kind = str(issue.issue_type or issue.rule_name or "").strip()
        scope = scope_for_issue_kind(kind)
        candidates.append(
            ClassifiedIssue(
                kind=kind,
                scope=scope,
                severity=str(issue.severity or "warning"),
                source_layer=str(issue.source_layer or ""),
                evidence_refs=tuple(issue.evidence_refs or []),
            )
        )
    for signal in signals:
        kind = str(getattr(signal, "signal_type", "") or getattr(signal, "kind", "") or "").strip()
        if not kind:
            continue
        candidates.append(
            ClassifiedIssue(
                kind=kind,
                scope=scope_for_issue_kind(kind),
                severity=str(getattr(signal, "severity", "") or "warning"),
                source_layer=str(getattr(signal, "source_layer", "") or ""),
                evidence_refs=tuple(getattr(signal, "evidence_refs", []) or []),
            )
        )
    if not candidates:
        return ClassifiedIssue(kind="review_verdict", scope="chapter_plan", severity=review.verdict)
    return max(candidates, key=lambda item: (_SCOPE_RANK.get(item.scope, 0), _SEVERITY_RANK.get(item.severity, 0), len(item.evidence_refs)))
```

- [ ] **Step 4: Run taxonomy tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_issue_taxonomy.py -q
```

Expected: pass.

### Task 3: Add Repair V2 Decision Rule

**Files:**
- Create: `forwin/review_engine/rules/repair_v2.py`
- Test: `tests/review_engine/test_repair_v2.py`

- [ ] **Step 1: Write repair-v2 routing tests**

Create:

```python
def test_arc_level_issue_routes_to_arc_patch_scope() -> None:
    input_payload = decision_input_with_issue("identity_ambiguity", severity="error")
    decision = decide_repair_v2(input_payload)

    assert decision.outcome == "arc_patch"
    assert decision.sub_action["scope"] == "arc_plan"
```

Add similar tests for draft, chapter plan, band plan, book plan, and operator issue kinds.

- [ ] **Step 2: Run repair-v2 tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_repair_v2.py -q
```

Expected: FAIL because rule module does not exist.

- [ ] **Step 3: Implement `decide_repair_v2()`**

Create:

```python
from forwin.review_engine.issue_taxonomy import classify_primary_issue
from forwin.review_engine.types import Decision, DecisionInput

_SCOPE_TO_OUTCOME = {
    "draft": "local_repair",
    "chapter_plan": "chapter_patch",
    "band_plan": "band_patch",
    "arc_plan": "arc_patch",
    "book_plan": "book_patch",
    "subworld": "chapter_patch",
    "active_rules": "chapter_patch",
    "operator": "system_block",
}


def decide_repair_v2(input: DecisionInput) -> Decision:
    primary = classify_primary_issue(review=input.review, signals=input.signals)
    outcome = _SCOPE_TO_OUTCOME.get(primary.scope, "manual_review")
    missing_evidence = [] if primary.evidence_refs else ["evidence"]
    return Decision(
        outcome=outcome,
        reason=f"{primary.kind} routes to {primary.scope}",
        rule_id=f"repair_v2_{primary.scope}",
        missing_evidence=missing_evidence,
        routed_from="RepairPolicy.v2",
        sub_action={"scope": primary.scope, "issue_kind": primary.kind},
    )
```

- [ ] **Step 4: Run repair-v2 tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_repair_v2.py -q
```

Expected: pass.

### Task 4: Add Feature Flag and Shadow Comparison

**Files:**
- Modify: `forwin/config.py`
- Modify: `forwin/review_engine/rules/repair.py`
- Modify: `forwin/review_engine/rules/repair_v2.py`
- Test: `tests/review_engine/test_repair_v2_shadow.py`

- [ ] **Step 1: Write flag behavior tests**

Create tests:

```python
def test_repair_v2_shadow_records_old_and_new_scope_when_flag_off() -> None:
    result = compare_repair_v2_shadow(old_scope="draft", new_scope="arc_plan", enabled=False)
    assert result.live_scope == "draft"
    assert result.shadow_scope == "arc_plan"
    assert result.enabled is False
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_repair_v2_shadow.py -q
```

Expected: FAIL because flag comparison helper does not exist.

- [ ] **Step 3: Add config field and shadow helper**

Add config field with default false:

```python
review_engine_repair_v2_enabled: bool = False
```

Add:

```python
@dataclass(frozen=True)
class RepairV2ShadowResult:
    live_scope: str
    shadow_scope: str
    enabled: bool


def compare_repair_v2_shadow(*, old_scope: str, new_scope: str, enabled: bool) -> RepairV2ShadowResult:
    return RepairV2ShadowResult(
        live_scope=new_scope if enabled else old_scope,
        shadow_scope=new_scope,
        enabled=enabled,
    )
```

- [ ] **Step 4: Run shadow tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_repair_v2_shadow.py tests/test_config_defaults.py -q
```

Expected: pass.

### Task 5: Integrate Repair V2 Into Engine Rules

**Files:**
- Modify: `forwin/review_engine/rules/repair.py`
- Modify: `forwin/review_engine/rules/__init__.py`
- Test: `tests/review_engine/test_rule_parity.py`
- Test: `tests/review_engine/test_repair_v2.py`

- [ ] **Step 1: Add engine integration test**

Add a test that builds an engine with repair-v2 enabled and an arc issue, then asserts `arc_patch`.

- [ ] **Step 2: Run integration test and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_repair_v2.py::test_engine_uses_repair_v2_when_enabled -q
```

Expected: FAIL because engine rules do not include repair v2.

- [ ] **Step 3: Register repair-v2 rule after legacy parity rules**

Add a builder:

```python
def build_repair_v2_rules(*, enabled: bool) -> list[DecisionRule]:
    return [
        DecisionRule(
            rule_id="repair_v2_scope_driven",
            source_dispatcher="RepairPolicy.v2",
            priority=80,
            matches=lambda input: enabled and input.review.verdict == "fail",
            decide=decide_repair_v2,
        )
    ]
```

Ensure disabled mode still uses legacy repair parity rule.

- [ ] **Step 4: Run repair engine tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_repair_v2.py tests/review_engine/test_rule_parity.py -q
```

Expected: pass.

### Task 6: Final Scope-Driven Repair Verification

**Files:**
- Verify all modified Scope V2 files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_issue_taxonomy.py tests/review_engine/test_repair_v2.py tests/review_engine/test_repair_v2_shadow.py -q
python3 -m pytest tests/test_repair_scope_router.py tests/test_repair_scope_router_dispatch.py tests/test_chapter18_repair_routing_regression.py -q
```

Expected: all pass.

- [ ] **Step 2: Run syntax and diff hygiene**

Run:

```bash
python3 -m compileall -q forwin
git diff --check
```

Expected: both pass with no output.

- [ ] **Step 3: Commit repair v2**

Run:

```bash
git add forwin/review_engine forwin/config.py tests/review_engine
git commit -m "feat: add scope-driven repair routing"
```
