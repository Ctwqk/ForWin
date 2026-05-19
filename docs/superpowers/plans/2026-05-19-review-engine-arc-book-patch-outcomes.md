# Review Engine Arc and Book Patch Outcomes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make `arc_patch` and `book_patch` executable review-engine outcomes instead of falling through to manual review.

**Architecture:** Add arc/book patchers and validators that produce existing `NarrativePlanPatch` records. The engine emits executable outcomes only when the patcher flag is enabled and validation evidence is sufficient; otherwise it emits explicit policy-disabled manual review.

**Tech Stack:** Python 3, Pydantic, SQLAlchemy, pytest, existing `NarrativePlanPatch`, `NarrativeObligationRepository`, writer context assembly, arc planning, and review engine modules.

---

### Task 1: Add Arc Plan Patcher

**Files:**
- Create: `forwin/planning/arc_plan_patcher.py`
- Test: `tests/test_arc_plan_patcher.py`

- [ ] **Step 1: Write arc patcher test**

Create:

```python
from forwin.planning.arc_plan_patcher import ArcPlanPatcher


def test_arc_plan_patcher_creates_narrative_plan_patch() -> None:
    patch = ArcPlanPatcher().build_patch(
        project_id="project-1",
        origin_chapter_number=10,
        target_arc_id="arc-1",
        issue_kind="identity_ambiguity",
        summary="身份线索需要在本 arc 内澄清。",
        source_signal_ids=["sig-1"],
        source_obligation_ids=["obl-1"],
        payoff_test="本 arc 结束前必须解释身份线索。",
    )

    assert patch.project_id == "project-1"
    assert patch.target_scope == "arc"
    assert patch.target_arc_id == "arc-1"
    assert patch.source_signal_ids == ["sig-1"]
    assert "payoff_test" in patch.new_contract
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
python3 -m pytest tests/test_arc_plan_patcher.py -q
```

Expected: FAIL because arc patcher does not exist.

- [ ] **Step 3: Implement arc patcher**

Create:

```python
from __future__ import annotations

from forwin.narrative_obligations.types import NarrativePlanPatch


class ArcPlanPatcher:
    def build_patch(
        self,
        *,
        project_id: str,
        origin_chapter_number: int,
        target_arc_id: str,
        issue_kind: str,
        summary: str,
        source_signal_ids: list[str],
        source_obligation_ids: list[str],
        payoff_test: str,
    ) -> NarrativePlanPatch:
        return NarrativePlanPatch(
            project_id=project_id,
            patch_type="arc_defer_acceptance",
            target_scope="arc",
            target_arc_id=target_arc_id,
            affected_chapters=[],
            source_signal_ids=list(source_signal_ids),
            source_obligation_ids=list(source_obligation_ids),
            new_contract={
                "issue_kind": issue_kind,
                "summary": summary,
                "payoff_test": payoff_test,
                "origin_chapter_number": int(origin_chapter_number or 0),
            },
            writer_context_injections=[
                {"scope": "arc", "issue_kind": issue_kind, "instruction": summary}
            ],
            reviewer_context_injections=[
                {"scope": "arc", "payoff_test": payoff_test}
            ],
            expected_resolution_tests=[payoff_test],
        )
```

- [ ] **Step 4: Run arc patcher test**

Run:

```bash
python3 -m pytest tests/test_arc_plan_patcher.py -q
```

Expected: pass.

### Task 2: Add Book Plan Patcher

**Files:**
- Create: `forwin/planning/book_plan_patcher.py`
- Test: `tests/test_book_plan_patcher.py`

- [ ] **Step 1: Write book patcher test**

Create:

```python
from forwin.planning.book_plan_patcher import BookPlanPatcher


def test_book_plan_patcher_creates_book_scope_patch() -> None:
    patch = BookPlanPatcher().build_patch(
        project_id="project-1",
        origin_chapter_number=12,
        issue_kind="book_structure_violation",
        summary="全书结构承诺需要调整。",
        source_signal_ids=["sig-book"],
        source_obligation_ids=[],
        payoff_test="终章前必须完成结构承诺。",
    )

    assert patch.target_scope == "book"
    assert patch.patch_type == "book_defer_acceptance"
    assert patch.source_signal_ids == ["sig-book"]
```

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
python3 -m pytest tests/test_book_plan_patcher.py -q
```

Expected: FAIL because book patcher does not exist.

- [ ] **Step 3: Implement book patcher**

Create:

```python
from __future__ import annotations

from forwin.narrative_obligations.types import NarrativePlanPatch


class BookPlanPatcher:
    def build_patch(
        self,
        *,
        project_id: str,
        origin_chapter_number: int,
        issue_kind: str,
        summary: str,
        source_signal_ids: list[str],
        source_obligation_ids: list[str],
        payoff_test: str,
    ) -> NarrativePlanPatch:
        return NarrativePlanPatch(
            project_id=project_id,
            patch_type="book_defer_acceptance",
            target_scope="book",
            affected_chapters=[],
            source_signal_ids=list(source_signal_ids),
            source_obligation_ids=list(source_obligation_ids),
            new_contract={
                "issue_kind": issue_kind,
                "summary": summary,
                "payoff_test": payoff_test,
                "origin_chapter_number": int(origin_chapter_number or 0),
            },
            writer_context_injections=[
                {"scope": "book", "issue_kind": issue_kind, "instruction": summary}
            ],
            reviewer_context_injections=[
                {"scope": "book", "payoff_test": payoff_test}
            ],
            expected_resolution_tests=[payoff_test],
        )
```

- [ ] **Step 4: Run book patcher test**

Run:

```bash
python3 -m pytest tests/test_book_plan_patcher.py -q
```

Expected: pass.

### Task 3: Add Arc and Book Patch Validators

**Files:**
- Create: `forwin/planning/arc_patch_validator.py`
- Create: `forwin/planning/book_patch_validator.py`
- Test: `tests/test_arc_patch_validator.py`
- Test: `tests/test_book_patch_validator.py`

- [ ] **Step 1: Write validator tests**

Create tests that assert a patch without source evidence fails and a patch with source evidence plus payoff test passes:

```python
def test_arc_patch_validator_requires_evidence_and_payoff_test() -> None:
    patch = NarrativePlanPatch(project_id="project-1", target_scope="arc", target_arc_id="arc-1")

    result = ArcPatchValidator().validate(patch)

    assert result.passed is False
    assert "missing_source_evidence" in result.errors
    assert "missing_payoff_test" in result.errors
```

- [ ] **Step 2: Run validator tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_arc_patch_validator.py tests/test_book_patch_validator.py -q
```

Expected: FAIL because validators do not exist.

- [ ] **Step 3: Implement validator result and rules**

In each validator module:

```python
from dataclasses import dataclass, field

from forwin.narrative_obligations.types import NarrativePlanPatch


@dataclass(frozen=True)
class PatchValidationResult:
    passed: bool
    errors: list[str] = field(default_factory=list)


def _common_errors(patch: NarrativePlanPatch) -> list[str]:
    errors: list[str] = []
    if not patch.source_signal_ids and not patch.source_obligation_ids:
        errors.append("missing_source_evidence")
    if not patch.expected_resolution_tests:
        errors.append("missing_payoff_test")
    if patch.target_scope not in {"arc", "book"}:
        errors.append(f"unsupported_target_scope:{patch.target_scope}")
    return errors
```

`ArcPatchValidator` also requires `target_arc_id`. `BookPatchValidator` requires `target_scope == "book"`.

- [ ] **Step 4: Run validator tests**

Run:

```bash
python3 -m pytest tests/test_arc_patch_validator.py tests/test_book_patch_validator.py -q
```

Expected: pass.

### Task 4: Register Arc and Book Outcomes in Review Engine

**Files:**
- Modify: `forwin/review_engine/rules/repair_v2.py`
- Create: `forwin/review_engine/rules/structural_patch.py`
- Test: `tests/review_engine/test_arc_book_outcomes.py`

- [ ] **Step 1: Write policy-disabled and enabled outcome tests**

Create:

```python
def test_arc_patch_disabled_routes_to_explicit_manual_review() -> None:
    decision = decide_structural_patch(
        input=decision_input_with_issue("identity_ambiguity"),
        arc_patcher_enabled=False,
        book_patcher_enabled=False,
    )

    assert decision.outcome == "manual_review"
    assert decision.rule_id == "arc_patcher_disabled"


def test_arc_patch_enabled_returns_arc_patch() -> None:
    decision = decide_structural_patch(
        input=decision_input_with_issue("identity_ambiguity"),
        arc_patcher_enabled=True,
        book_patcher_enabled=False,
    )

    assert decision.outcome == "arc_patch"
    assert decision.sub_action["patch_type"] == "arc_defer_acceptance"
```

- [ ] **Step 2: Run outcome tests and verify RED**

Run:

```bash
python3 -m pytest tests/review_engine/test_arc_book_outcomes.py -q
```

Expected: FAIL because structural patch rule does not exist.

- [ ] **Step 3: Implement structural patch decision helper**

Create:

```python
def decide_structural_patch(*, input: DecisionInput, arc_patcher_enabled: bool, book_patcher_enabled: bool) -> Decision:
    primary = classify_primary_issue(review=input.review, signals=input.signals)
    if primary.scope == "arc_plan" and not arc_patcher_enabled:
        return Decision("manual_review", "arc patcher disabled", "arc_patcher_disabled", [], "AutoDecisionEngine", {"issue_kind": primary.kind})
    if primary.scope == "book_plan" and not book_patcher_enabled:
        return Decision("manual_review", "book patcher disabled", "book_patcher_disabled", [], "AutoDecisionEngine", {"issue_kind": primary.kind})
    if primary.scope == "arc_plan":
        return Decision("arc_patch", f"{primary.kind} requires arc patch", "arc_patch_enabled", [], "AutoDecisionEngine", {"patch_type": "arc_defer_acceptance", "issue_kind": primary.kind})
    if primary.scope == "book_plan":
        return Decision("book_patch", f"{primary.kind} requires book patch", "book_patch_enabled", [], "AutoDecisionEngine", {"patch_type": "book_defer_acceptance", "issue_kind": primary.kind})
    return Decision("manual_review", "not a structural patch issue", "not_structural_patch", ["structural_issue"], "AutoDecisionEngine", {"issue_kind": primary.kind})
```

- [ ] **Step 4: Run outcome tests**

Run:

```bash
python3 -m pytest tests/review_engine/test_arc_book_outcomes.py -q
```

Expected: pass.

### Task 5: Wire Patch Execution Into Quality Gates

**Files:**
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Test: `tests/test_orchestrator_deferred_acceptance.py`
- Test: `tests/review_engine/test_arc_book_outcomes.py`

- [ ] **Step 1: Write orchestration test**

Add a test that feeds an `arc_patch` decision into the quality gate helper and asserts a `NarrativePlanPatch` with target scope `arc` is persisted.

- [ ] **Step 2: Run orchestration test and verify RED**

Run:

```bash
python3 -m pytest tests/test_orchestrator_deferred_acceptance.py::test_quality_gate_persists_arc_patch_outcome -q
```

Expected: FAIL because quality gates do not handle `arc_patch`.

- [ ] **Step 3: Implement patch dispatch**

In the helper that currently prepares deferred acceptance, add outcome routing:

```python
if decision.outcome == "arc_patch":
    patch = ArcPlanPatcher().build_patch(...)
    validation = ArcPatchValidator().validate(patch)
    if not validation.passed:
        return deferred_result_blocked(validation.errors)
    return persist_deferred_patch(patch)
if decision.outcome == "book_patch":
    patch = BookPlanPatcher().build_patch(...)
    validation = BookPatchValidator().validate(patch)
    if not validation.passed:
        return deferred_result_blocked(validation.errors)
    return persist_deferred_patch(patch)
```

Use existing repository and transaction patterns. Do not create a separate persistence path.

- [ ] **Step 4: Run orchestration tests**

Run:

```bash
python3 -m pytest tests/test_orchestrator_deferred_acceptance.py tests/review_engine/test_arc_book_outcomes.py -q
```

Expected: pass.

### Task 6: Inject Active Arc and Book Patch Debt Into Writer Context

**Files:**
- Modify: `forwin/context/assembler_core/assembler.py`
- Modify: `forwin/context/providers/experience_provider.py`
- Test: `tests/test_writer_prompt_contract.py`

- [ ] **Step 1: Write context injection test**

Add a test that seeds an active arc patch and asserts the writer context contains its payoff test.

- [ ] **Step 2: Run test and verify RED**

Run:

```bash
python3 -m pytest tests/test_writer_prompt_contract.py::test_writer_context_includes_active_arc_patch_debt -q
```

Expected: FAIL because context ignores arc/book patches.

- [ ] **Step 3: Add patch-debt loader**

Add a provider helper:

```python
def active_structural_patch_debt(session, *, project_id: str, chapter_number: int) -> list[dict[str, object]]:
    patches = NarrativeObligationRepository(session).list_active_structural_patches(
        project_id=project_id,
        chapter_number=chapter_number,
    )
    return [
        {
            "patch_id": patch.id,
            "scope": patch.target_scope,
            "payoff_tests": list(patch.expected_resolution_tests),
            "writer_context_injections": list(patch.writer_context_injections),
        }
        for patch in patches
    ]
```

If the repository method does not exist, add it in the same task.

- [ ] **Step 4: Include debt in context**

Add the debt payload to `canon_quality_context` or the existing future-constraint channel used by writer prompts. Keep it structured.

- [ ] **Step 5: Run writer context tests**

Run:

```bash
python3 -m pytest tests/test_writer_prompt_contract.py -q
```

Expected: pass.

### Task 7: Add Arc and Book Completion Gates

**Files:**
- Modify: `forwin/orchestrator_loop_core/quality_gates.py`
- Test: `tests/test_arc_execution_scoping.py`

- [ ] **Step 1: Write completion gate tests**

Add tests:

```python
def test_arc_completion_blocks_unresolved_arc_patch_debt() -> None:
    result = evaluate_arc_completion_patch_debt(
        project_id="project-1",
        chapter_number=20,
        is_arc_final_chapter=True,
        active_patch_debt=[{"patch_id": "patch-arc", "target_scope": "arc"}],
    )

    assert result.commit_allowed is False
    assert "unresolved_arc_patch_debt:patch-arc" in result.blocking_reasons
```

- [ ] **Step 2: Run completion tests and verify RED**

Run:

```bash
python3 -m pytest tests/test_arc_execution_scoping.py::test_arc_completion_blocks_unresolved_arc_patch_debt -q
```

Expected: FAIL because completion debt gate does not exist.

- [ ] **Step 3: Implement gate helper**

Add a helper that returns blocking reasons for unresolved arc/book patch debt at arc or book completion. Use existing canon admission result shape where possible.

- [ ] **Step 4: Run completion tests**

Run:

```bash
python3 -m pytest tests/test_arc_execution_scoping.py -q
```

Expected: pass.

### Task 8: Final Arc/Book Outcome Verification

**Files:**
- Verify all modified arc/book outcome files.

- [ ] **Step 1: Run focused tests**

Run:

```bash
python3 -m pytest tests/test_arc_plan_patcher.py tests/test_book_plan_patcher.py tests/test_arc_patch_validator.py tests/test_book_patch_validator.py -q
python3 -m pytest tests/review_engine/test_arc_book_outcomes.py tests/test_orchestrator_deferred_acceptance.py -q
python3 -m pytest tests/test_writer_prompt_contract.py tests/test_arc_execution_scoping.py -q
```

Expected: all pass.

- [ ] **Step 2: Run syntax and diff hygiene**

Run:

```bash
python3 -m compileall -q forwin
git diff --check
```

Expected: both pass with no output.

- [ ] **Step 3: Commit arc/book patch outcomes**

Run:

```bash
git add forwin/planning forwin/review_engine forwin/orchestrator_loop_core/quality_gates.py forwin/context tests
git commit -m "feat: execute arc and book review patch outcomes"
```
